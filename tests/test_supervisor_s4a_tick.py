"""S4a — minimal auto-reaction kernel (SupervisorTick) test-suite.

Exercises the advisory-mode auto-reaction round end to end: deterministic delivery
detection (worker_reported sentinel + AckInbox) → single-writer ingest → reduce → decide
→ **auto-apply internal state** / **surface** spawn/merge/oracle/approval. The headline
property is the **advisory safety boundary**: a tick NEVER appends a spawn/merge/oracle/
approval event — it only advances internal state and surfaces the rest.

Times are explicit ISO-8601 strings (injected, never the wall clock — INV-3).
"""

from __future__ import annotations

import pytest
from _s3_helpers import (
    Seq,
    green_verdict,
    make_plan,
    node,
    red_verdict,
    unknown_verdict,
)

from handoff_fanout.supervisor.ack_inbox import AckInbox, InboxSignalKind
from handoff_fanout.supervisor.actions import Ack
from handoff_fanout.supervisor.event_log import EventLog
from handoff_fanout.supervisor.events import Event, EventType
from handoff_fanout.supervisor.plan import Node, Plan, RiskTier
from handoff_fanout.supervisor.policy import DecisionKind, PolicyConfig, would_emit
from handoff_fanout.supervisor.reducer import reduce
from handoff_fanout.supervisor.states import NodeState, PlanState
from handoff_fanout.supervisor.supervisor_tick import (
    _AUTO_APPLY_EVENTS,
    _AUTO_APPLY_KINDS,
    _SURFACE_KINDS,
    AdvisoryClass,
    AlertKind,
    DeliveryDetector,
    PlanStatus,
    SentinelWatch,
    SupervisorTick,
    TickError,
    TickTrigger,
    advisory_class,
    assert_advisory_partition_total,
)
from handoff_fanout.supervisor.verdict import Verdict

T0 = "2026-06-06T10:00:00"


# --- test fixtures / helpers -------------------------------------------------


def _log(tmp_path, plan: Plan) -> EventLog:
    return EventLog(tmp_path / "events.jsonl", plan.plan_id)


def _seed(log: EventLog, events: list[Event]) -> None:
    """Write a pre-built (Seq) event list to an EMPTY log in order (CAS-checked)."""
    for e in events:
        log.append(e)


def _inbox(tmp_path) -> AckInbox:
    return AckInbox(tmp_path / "inbox")


def _sentinels(tmp_path) -> SentinelWatch:
    return SentinelWatch(tmp_path / "sentinels")


def _green_for(_ack: Ack) -> Verdict:
    return green_verdict()


def _event_types(log: EventLog) -> list[EventType]:
    return [e.type for e in log.read_all()]


# --- 1. advisory partition (structural safety boundary) ----------------------


class TestAdvisoryPartition:
    def test_partition_total_and_disjoint(self):
        assert_advisory_partition_total()  # raises on violation
        assert set(DecisionKind) == _AUTO_APPLY_KINDS | _SURFACE_KINDS
        assert not (_AUTO_APPLY_KINDS & _SURFACE_KINDS)

    def test_only_internal_state_kinds_are_auto(self):
        assert {
            DecisionKind.ADVANCE,
            DecisionKind.BLOCK,
            DecisionKind.TIMEOUT,
        } == _AUTO_APPLY_KINDS

    def test_every_auto_kind_backs_an_internal_state_event(self):
        for kind in _AUTO_APPLY_KINDS:
            assert would_emit(kind) in _AUTO_APPLY_EVENTS

    @pytest.mark.parametrize(
        "kind",
        [
            DecisionKind.DISPATCH,
            DecisionKind.START_AUDIT,
            DecisionKind.SPAWN_FIXER,
            DecisionKind.REQUEST_APPROVAL,
            DecisionKind.RUN_ORACLE,
            DecisionKind.FINAL_ORACLE,
            DecisionKind.REVALIDATE_STALE,
        ],
    )
    def test_spawn_merge_oracle_approval_are_surfaced(self, kind):
        assert advisory_class(kind) is AdvisoryClass.SURFACE
        assert kind in _SURFACE_KINDS

    def test_construction_runs_the_self_check(self, tmp_path):
        # The kernel asserts the partition at construction (fail-closed if it drifts).
        plan = make_plan()
        SupervisorTick(plan, _log(tmp_path, plan), _inbox(tmp_path))


# --- 2. SentinelWatch (explicit worker_reported delivery sentinel) -----------


class TestSentinelWatch:
    def test_deposit_then_reported(self, tmp_path):
        sw = _sentinels(tmp_path)
        assert sw.reported_nodes() == set()
        sw.deposit("n1")
        sw.deposit("n2")
        assert sw.reported_nodes() == {"n1", "n2"}

    def test_consume_moves_to_consumed_idempotent(self, tmp_path):
        sw = _sentinels(tmp_path)
        sw.deposit("n1")
        sw.consume("n1")
        assert sw.reported_nodes() == set()
        assert (sw.consumed_dir / "n1.worker_reported").exists()
        sw.consume("n1")  # idempotent — missing sentinel is a no-op
        sw.consume("never")  # no error

    def test_deposit_idempotent(self, tmp_path):
        sw = _sentinels(tmp_path)
        sw.deposit("n1")
        sw.deposit("n1")
        assert sw.reported_nodes() == {"n1"}


# --- 3. DeliveryDetector (deterministic trigger, not silence threshold) ------


class TestDeliveryDetector:
    def test_no_signal_no_sentinel(self, tmp_path):
        det = DeliveryDetector(_inbox(tmp_path), _sentinels(tmp_path))
        sig = det.poll()
        assert sig.pending is False
        assert sig.reported_nodes == []
        assert sig.inbox_signal_count == 0

    def test_sentinel_makes_it_pending(self, tmp_path):
        sw = _sentinels(tmp_path)
        sw.deposit("n1")
        sig = DeliveryDetector(_inbox(tmp_path), sw).poll()
        assert sig.pending is True
        assert sig.reported_nodes == ["n1"]

    def test_inbox_signal_makes_it_pending_and_is_read_only(self, tmp_path):
        inbox = _inbox(tmp_path)
        ack = Ack(node="n1", run_id="r1", attempt=1, tree_oid="tree1")
        inbox.deposit(InboxSignalKind.WORKER, ack)
        det = DeliveryDetector(inbox, _sentinels(tmp_path))
        sig = det.poll()
        assert sig.pending is True
        assert sig.inbox_signal_count == 1
        # poll() must not drain/move the signal (detection is read-only).
        assert det.poll().inbox_signal_count == 1


# --- 4. ingest (single-writer translation of worker signals) -----------------


class TestIngest:
    def test_worker_signal_is_ingested_into_an_event(self, tmp_path):
        plan = make_plan()
        log = _log(tmp_path, plan)
        _seed(log, Seq(plan).plan_created().dispatch("n1", 1, ts=T0).events)
        inbox = _inbox(tmp_path)
        inbox.deposit(
            InboxSignalKind.WORKER, Ack(node="n1", run_id="r1", attempt=1, tree_oid="tree1")
        )
        tick = SupervisorTick(plan, log, inbox)

        res = tick.run(now=T0, triggered_by=TickTrigger.DELIVERY)

        assert EventType.WORKER_DONE in _event_types(log)
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.AUDITING
        assert "n1" in res.delivered_nodes
        assert res.triggered_by is TickTrigger.DELIVERY

    def test_audit_signal_uses_injected_verdict_and_surfaces_run_oracle(self, tmp_path):
        plan = make_plan()
        log = _log(tmp_path, plan)
        _seed(
            log,
            Seq(plan)
            .plan_created()
            .dispatch("n1", 1, ts=T0)
            .worker_done("n1", 1, ts=T0)
            .audit_started("n1", 1, ts=T0)
            .events,
        )
        inbox = _inbox(tmp_path)
        inbox.deposit(
            InboxSignalKind.AUDIT, Ack(node="n1", run_id="r1", attempt=1, tree_oid="tree1")
        )
        tick = SupervisorTick(plan, log, inbox, verdict_for=_green_for)

        res = tick.run(now=T0)

        # The supervisor computed the machine verdict (INV-2) and baked audit_done.
        assert EventType.AUDIT_DONE in _event_types(log)
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.EVALUATING
        # GREEN verdict, no oracle yet → RUN_ORACLE is SURFACED (execute is not auto).
        kinds = {a.decision.kind for a in res.advisories}
        assert DecisionKind.RUN_ORACLE in kinds
        assert EventType.ORACLE_CHECKED not in _event_types(log)


# --- 5. auto-advance (terminal/verdict triggers internal advance) ------------


class TestAutoAdvance:
    def _to_evaluating_green(self, tmp_path):
        plan = make_plan()
        log = _log(tmp_path, plan)
        _seed(
            log,
            Seq(plan)
            .plan_created()
            .dispatch("n1", 1, ts=T0)
            .worker_done("n1", 1, ts=T0)
            .audit_started("n1", 1, ts=T0)
            .audit_done("n1", green_verdict(), 1, ts=T0)
            .oracle_checked("n1", True)
            .events,
        )
        return plan, log

    def test_green_verdict_and_oracle_auto_advances_to_done(self, tmp_path):
        plan, log = self._to_evaluating_green(tmp_path)
        tick = SupervisorTick(plan, log, _inbox(tmp_path))

        res = tick.run(now=T0)

        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.DONE
        assert EventType.NODE_ADVANCED in _event_types(log)
        assert [a.decision.kind for a in res.applied] == [DecisionKind.ADVANCE]
        assert res.plan_status is PlanStatus.ALL_DONE
        assert any(a.kind is AlertKind.PLAN_COMPLETE for a in res.alerts)

    def test_advance_binds_provenance_from_the_verdict(self, tmp_path):
        plan, log = self._to_evaluating_green(tmp_path)
        SupervisorTick(plan, log, _inbox(tmp_path)).run(now=T0)
        advanced = next(e for e in log.read_all() if e.type is EventType.NODE_ADVANCED)
        # green_verdict() binds tree_oid="tree1" — the DONE node carries that provenance.
        assert advanced.provenance is not None
        assert advanced.provenance.tree_oid == "tree1"

    def test_all_done_surfaces_final_oracle_not_executes_it(self, tmp_path):
        plan, log = self._to_evaluating_green(tmp_path)
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(now=T0)
        kinds = {a.decision.kind for a in res.advisories}
        assert DecisionKind.FINAL_ORACLE in kinds  # surfaced
        # FINAL_ORACLE has no single backing event and is never executed by the kernel.


# --- 6. escalation (UNKNOWN → block; RED → surface fixer or auto-block) -------


class TestEscalation:
    def _to_evaluating(self, tmp_path, verdict: Verdict, *, n=None):
        n = n or node("n1")
        plan = make_plan(nodes=[n])
        log = _log(tmp_path, plan)
        _seed(
            log,
            Seq(plan)
            .plan_created()
            .dispatch("n1", 1, ts=T0)
            .worker_done("n1", 1, ts=T0)
            .audit_started("n1", 1, ts=T0)
            .audit_done("n1", verdict, 1, ts=T0)
            .events,
        )
        return plan, log

    def test_unknown_verdict_auto_blocks_and_escalates(self, tmp_path):
        plan, log = self._to_evaluating(tmp_path, unknown_verdict())
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(now=T0)

        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.BLOCKED
        assert [a.decision.kind for a in res.applied] == [DecisionKind.BLOCK]
        assert res.plan_status is PlanStatus.BLOCKED
        assert any(a.kind is AlertKind.ESCALATION for a in res.alerts)

    def test_red_verdict_surfaces_fixer_and_does_not_spawn(self, tmp_path):
        plan, log = self._to_evaluating(tmp_path, red_verdict())  # max_fix_attempts default 2
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(now=T0)

        # SPAWN_FIXER is surfaced, NOT executed — node stays EVALUATING, no fixer event.
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.EVALUATING
        assert EventType.FIXER_SPAWNED not in _event_types(log)
        kinds = {a.decision.kind for a in res.advisories}
        assert DecisionKind.SPAWN_FIXER in kinds
        assert any(a.kind is AlertKind.ADVISORY for a in res.alerts)
        assert res.applied == []

    def test_red_verdict_with_no_fix_budget_auto_blocks(self, tmp_path):
        # max_fix_attempts=0 → a RED with no repair budget escalates (auto-block), not spawn.
        plan, log = self._to_evaluating(tmp_path, red_verdict(), n=node("n1", max_fix_attempts=0))
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(now=T0)

        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.BLOCKED
        assert EventType.FIXER_SPAWNED not in _event_types(log)
        assert [a.decision.kind for a in res.applied] == [DecisionKind.BLOCK]


# --- 7. Sweeper (timeout detection on in-flight nodes) -----------------------


class TestSweeper:
    def _dispatched(self, tmp_path, *, config=None):
        plan = make_plan()
        log = _log(tmp_path, plan)
        _seed(log, Seq(plan).plan_created().dispatch("n1", 1, ts=T0).events)
        return plan, log

    def test_in_flight_past_budget_times_out(self, tmp_path):
        plan, log = self._dispatched(tmp_path)
        # default timeout_s=1800 → T0 + 2000s exceeds it.
        late = "2026-06-06T10:33:20"  # T0 + 2000s
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(
            now=late, triggered_by=TickTrigger.SWEEP
        )

        n1 = reduce(plan, log.read_all()).nodes["n1"]
        assert EventType.WORKER_TIMEOUT in _event_types(log)
        assert any(a.decision.kind is DecisionKind.TIMEOUT for a in res.applied)
        # attempt 1 < default max_dispatch_attempts 2 → retry DISPATCH is SURFACED.
        assert n1.status is NodeState.TIMED_OUT
        kinds = {a.decision.kind for a in res.advisories}
        assert DecisionKind.DISPATCH in kinds  # retry surfaced, not auto-spawned
        assert any(a.kind is AlertKind.TIMEOUT for a in res.alerts)

    def test_within_budget_no_timeout(self, tmp_path):
        plan, log = self._dispatched(tmp_path)
        soon = "2026-06-06T10:00:30"  # T0 + 30s, well within 1800s
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(now=soon)
        assert EventType.WORKER_TIMEOUT not in _event_types(log)
        assert res.applied == []
        assert res.advisories == []  # DISPATCHED node is just waiting

    def test_timeout_with_retries_exhausted_auto_blocks(self, tmp_path):
        plan, log = self._dispatched(tmp_path)
        cfg = PolicyConfig(max_dispatch_attempts=1)  # attempt 1 is the last
        late = "2026-06-06T10:33:20"
        res = SupervisorTick(plan, log, _inbox(tmp_path), config=cfg).run(now=late)

        # cascade settles in ONE round: TIMEOUT (auto) → TIMED_OUT → BLOCK (auto).
        applied_kinds = [a.decision.kind for a in res.applied]
        assert DecisionKind.TIMEOUT in applied_kinds
        assert DecisionKind.BLOCK in applied_kinds
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.BLOCKED
        assert res.iterations >= 2  # the settle loop ran more than once


# --- 8. THE advisory safety boundary (核心 / 绝不自动 spawn/merge/不可逆) -----

#: Every surface scenario: a seeded state that decides a SURFACE kind, and the event
#: that kind WOULD emit in enforce — which a tick must NEVER append.
_FORBIDDEN_SURFACE_EVENTS = frozenset(
    {
        EventType.NODE_DISPATCHED,  # DISPATCH (spawn worker)
        EventType.AUDIT_STARTED,  # START_AUDIT (spawn audit)
        EventType.FIXER_SPAWNED,  # SPAWN_FIXER (spawn fixer)
        EventType.APPROVAL_REQUESTED,  # REQUEST_APPROVAL (gate human)
        EventType.ORACLE_CHECKED,  # RUN_ORACLE / FINAL_ORACLE (execute oracle)
    }
)


class TestAdvisoryBoundary:
    def _irreversible_node(self):
        from handoff_fanout.supervisor.actions import SideEffect, SideEffectKind

        eff = SideEffect(kind=SideEffectKind.DB_MIGRATION, sandboxed=False, needs_preauth=True)
        return Node(
            node_id="n1",
            brief="irreversible",
            base_ref="main",
            reversible=False,
            side_effects=[eff],
            risk_tier=RiskTier.H,
        )

    def test_fresh_pending_dispatch_is_surfaced_not_executed(self, tmp_path):
        plan = make_plan()
        log = _log(tmp_path, plan)
        _seed(log, Seq(plan).plan_created().events)  # n1 PENDING, deps satisfied
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(now=T0)

        kinds = {a.decision.kind for a in res.advisories}
        assert DecisionKind.DISPATCH in kinds
        # The node was NOT dispatched — it stays PENDING, no node_dispatched event.
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.PENDING
        assert EventType.NODE_DISPATCHED not in _event_types(log)

    def test_irreversible_node_only_alerts_for_approval(self, tmp_path):
        plan = make_plan(nodes=[self._irreversible_node()])
        log = _log(tmp_path, plan)
        _seed(log, Seq(plan).plan_created().events)
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(now=T0)

        assert any(a.kind is AlertKind.APPROVAL_NEEDED for a in res.alerts)
        # The kernel does NOT move an irreversible node — it stays PENDING (no approval_requested).
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.PENDING
        assert EventType.APPROVAL_REQUESTED not in _event_types(log)

    def test_a_tick_never_appends_a_spawn_merge_oracle_event(self, tmp_path):
        """Property: across a battery of states that decide every SURFACE kind, a tick's
        appended events are ONLY ingest (worker/audit/fixer) + internal-state (advance/
        block/timeout) — never a spawn/merge/oracle/approval event."""
        scenarios = []

        # DISPATCH: fresh PENDING.
        p1 = make_plan(plan_id="bnd-dispatch")
        scenarios.append((p1, Seq(p1).plan_created().events))

        # START_AUDIT: worker done, audit not started.
        p2 = make_plan(plan_id="bnd-audit")
        scenarios.append(
            (p2, Seq(p2).plan_created().dispatch("n1", 1, ts=T0).worker_done("n1", 1, ts=T0).events)
        )

        # SPAWN_FIXER: EVALUATING with RED verdict + fix budget.
        p3 = make_plan(plan_id="bnd-fixer")
        scenarios.append(
            (
                p3,
                Seq(p3)
                .plan_created()
                .dispatch("n1", 1, ts=T0)
                .worker_done("n1", 1, ts=T0)
                .audit_started("n1", 1, ts=T0)
                .audit_done("n1", red_verdict(), 1, ts=T0)
                .events,
            )
        )

        # RUN_ORACLE: EVALUATING GREEN, no oracle yet.
        p4 = make_plan(plan_id="bnd-oracle")
        scenarios.append(
            (
                p4,
                Seq(p4)
                .plan_created()
                .dispatch("n1", 1, ts=T0)
                .worker_done("n1", 1, ts=T0)
                .audit_started("n1", 1, ts=T0)
                .audit_done("n1", green_verdict(), 1, ts=T0)
                .events,
            )
        )

        for plan, seed_events in scenarios:
            log = _log(tmp_path / plan.plan_id, plan)
            (tmp_path / plan.plan_id).mkdir(exist_ok=True)
            _seed(log, seed_events)
            before = set(_event_types(log))
            SupervisorTick(plan, log, AckInbox(tmp_path / plan.plan_id / "inbox")).run(now=T0)
            new_events = set(_event_types(log)) - before
            assert not (new_events & _FORBIDDEN_SURFACE_EVENTS), (
                f"tick appended a forbidden spawn/merge/oracle event for {plan.plan_id}: "
                f"{new_events & _FORBIDDEN_SURFACE_EVENTS}"
            )
            # And the only new events are internal-state ones (no scenario ingests here).
            assert new_events <= _AUTO_APPLY_EVENTS


# --- 9. cascade settle + downstream surfacing --------------------------------


class TestCascadeSettle:
    def test_advance_upstream_then_surface_downstream_dispatch(self, tmp_path):
        n1 = node("n1")
        n2 = node("n2", deps=["n1"])
        plan = make_plan(nodes=[n1, n2])
        log = _log(tmp_path, plan)
        _seed(
            log,
            Seq(plan)
            .plan_created()
            .dispatch("n1", 1, ts=T0)
            .worker_done("n1", 1, ts=T0)
            .audit_started("n1", 1, ts=T0)
            .audit_done("n1", green_verdict(), 1, ts=T0)
            .oracle_checked("n1", True)
            .events,
        )
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(now=T0)

        state = reduce(plan, log.read_all())
        assert state.nodes["n1"].status is NodeState.DONE  # auto-advanced
        assert state.nodes["n2"].status is NodeState.PENDING  # NOT auto-dispatched
        # n1 advance was auto; n2 dispatch is surfaced (deps now satisfied).
        assert [a.decision.kind for a in res.applied] == [DecisionKind.ADVANCE]
        n2_dispatch = [a for a in res.advisories if a.decision.node == "n2"]
        assert n2_dispatch and n2_dispatch[0].decision.kind is DecisionKind.DISPATCH
        assert res.iterations >= 2


# --- 10. determinism / idempotency -------------------------------------------


class TestDeterminism:
    def test_rerunning_a_settled_tick_is_idempotent(self, tmp_path):
        plan = make_plan()
        log = _log(tmp_path, plan)
        _seed(
            log,
            Seq(plan)
            .plan_created()
            .dispatch("n1", 1, ts=T0)
            .worker_done("n1", 1, ts=T0)
            .audit_started("n1", 1, ts=T0)
            .audit_done("n1", green_verdict(), 1, ts=T0)
            .oracle_checked("n1", True)
            .events,
        )
        tick = SupervisorTick(plan, log, _inbox(tmp_path))

        r1 = tick.run(now=T0)
        len_after_1 = len(log.read_all())
        r2 = tick.run(now=T0)
        len_after_2 = len(log.read_all())

        assert len_after_2 == len_after_1  # no new events on the second run
        assert r1.state_fingerprint == r2.state_fingerprint
        assert r2.applied == []  # nothing left to auto-apply


# --- 11. global pause (Sweeper still reaps, no new forward) ------------------


class TestPaused:
    def test_paused_plan_reaps_timeout_but_no_forward(self, tmp_path):
        plan = make_plan()
        log = _log(tmp_path, plan)
        _seed(
            log,
            Seq(plan).plan_created().dispatch("n1", 1, ts=T0).global_paused().events,
        )
        late = "2026-06-06T10:33:20"  # past the budget
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(now=late)

        assert reduce(plan, log.read_all()).plan_state is PlanState.GLOBAL_PAUSED
        # Sweeper still reaps the in-flight node even while paused.
        assert any(a.decision.kind is DecisionKind.TIMEOUT for a in res.applied)
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.TIMED_OUT
        # No forward decision while paused (the timed-out node is NOT auto-retried).
        assert res.advisories == []
        assert res.plan_status is PlanStatus.PAUSED
        assert any(a.kind is AlertKind.PLAN_PAUSED for a in res.alerts)


# --- 12. end-to-end advisory lifecycle (sentinel-driven) ---------------------


class TestEndToEnd:
    def test_sentinel_delivery_drives_a_round_and_is_consumed(self, tmp_path):
        plan = make_plan()
        log = _log(tmp_path, plan)
        _seed(log, Seq(plan).plan_created().dispatch("n1", 1, ts=T0).events)
        inbox = _inbox(tmp_path)
        sw = _sentinels(tmp_path)
        # Worker delivers: drops the AckInbox signal AND touches the explicit sentinel.
        inbox.deposit(
            InboxSignalKind.WORKER, Ack(node="n1", run_id="r1", attempt=1, tree_oid="tree1")
        )
        sw.deposit("n1")

        det = DeliveryDetector(inbox, sw)
        assert det.poll().pending is True  # deterministic delivery detected

        tick = SupervisorTick(plan, log, inbox, sentinel_watch=sw)
        res = tick.run(now=T0, triggered_by=TickTrigger.DELIVERY)

        assert "n1" in res.delivered_nodes
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.AUDITING
        # sentinel consumed — not re-counted next round.
        assert sw.reported_nodes() == set()
        assert det.poll().reported_nodes == []
        # worker delivered → START_AUDIT surfaced (next human/S4b step).
        assert DecisionKind.START_AUDIT in {a.decision.kind for a in res.advisories}

    def test_full_lifecycle_advisory_then_approved_steps(self, tmp_path):
        """Drive a node DISPATCHED→DONE through ticks (kernel auto-reacts) interleaved with
        the 'approved enforce steps' a human/S4b performs on each surfaced advisory."""
        plan = make_plan()
        log = _log(tmp_path, plan)
        _seed(log, Seq(plan).plan_created().events)
        inbox = _inbox(tmp_path)
        tick = SupervisorTick(plan, log, inbox, verdict_for=_green_for)

        # Round 1: PENDING → DISPATCH surfaced (kernel does NOT dispatch).
        r = tick.run(now=T0)
        assert DecisionKind.DISPATCH in {a.decision.kind for a in r.advisories}
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.PENDING

        # Human/S4b approves the dispatch (appends node_dispatched + spawns the worker).
        from handoff_fanout.supervisor.payloads import NodeAttempt

        log.append_event(
            type=EventType.NODE_DISPATCHED,
            payload=NodeAttempt(node="n1", attempt=1),
            dedupe_key="dispatch:n1:1",
            ts=T0,
        )
        # Worker delivers its signal → tick ingests worker_done, surfaces START_AUDIT.
        inbox.deposit(
            InboxSignalKind.WORKER, Ack(node="n1", run_id="r1", attempt=1, tree_oid="tree1")
        )
        r = tick.run(now=T0, triggered_by=TickTrigger.DELIVERY)
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.AUDITING
        assert DecisionKind.START_AUDIT in {a.decision.kind for a in r.advisories}

        # Human/S4b approves the audit (appends audit_started + spawns auditor).
        log.append_event(
            type=EventType.AUDIT_STARTED,
            payload=NodeAttempt(node="n1", attempt=1),
            dedupe_key="audit_started:n1:1",
            ts=T0,
        )
        # Auditor delivers → tick computes the machine verdict (GREEN) → EVALUATING; RUN_ORACLE surfaced.
        inbox.deposit(
            InboxSignalKind.AUDIT, Ack(node="n1", run_id="r2", attempt=1, tree_oid="tree1")
        )
        r = tick.run(now=T0, triggered_by=TickTrigger.DELIVERY)
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.EVALUATING
        assert DecisionKind.RUN_ORACLE in {a.decision.kind for a in r.advisories}

        # Human/S4b runs the oracle (appends oracle_checked GREEN).
        from handoff_fanout.supervisor.oracle import OracleScope
        from handoff_fanout.supervisor.payloads import OracleChecked

        log.append_event(
            type=EventType.ORACLE_CHECKED,
            payload=OracleChecked(node="n1", scope=OracleScope.MILESTONE, passed=True),
            dedupe_key="oracle:n1",
            ts=T0,
        )
        # Final round: verdict GREEN + oracle GREEN → kernel AUTO-advances to DONE.
        r = tick.run(now=T0)
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.DONE
        assert [a.decision.kind for a in r.applied] == [DecisionKind.ADVANCE]
        assert r.plan_status is PlanStatus.ALL_DONE


# --- 13. fail-closed convergence ---------------------------------------------


class TestConvergence:
    def test_to_dict_is_owner_facing_and_compact(self, tmp_path):
        plan, log = TestAutoAdvance()._to_evaluating_green(tmp_path)
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(now=T0)
        d = res.to_dict()
        # O(1) owner-facing keys — fingerprint + decisions, not the whole log.
        assert set(d) >= {
            "plan_id",
            "triggered_by",
            "applied",
            "advisories",
            "alerts",
            "plan_status",
            "state_fingerprint",
        }
        assert "events" not in d  # the full log is never dumped into a round result

    def test_tickerror_is_raisable(self):
        # Smoke: TickError is the fail-closed signal type (raised on non-convergence /
        # log inconsistency in _settle).
        with pytest.raises(TickError):
            raise TickError("x")


# --- 14. R2 dual-brain hardening -------------------------------------------------


class TestR2Hardening:
    def test_re_advance_same_attempt_does_not_collide(self, tmp_path):
        """R2 gemini #5 root cause: a node can legally re-advance at the SAME attempt (DONE →
        BLOCKED_BY_FIX via a stale revalidation → DONE again). The OLD ``(node, attempt)``
        dedupe key collided the second advance with the first → dedupe no-op → crash/stall.
        Both advances must go through the kernel (its key scheme); the occurrence-unique key
        (incl. last_seq) lets the second advance land fresh."""
        from handoff_fanout.supervisor.fixer import Fixer, FixerState, FixerTrigger
        from handoff_fanout.supervisor.oracle import OracleScope
        from handoff_fanout.supervisor.payloads import FixerDone, OracleChecked

        plan = make_plan()
        log = _log(tmp_path, plan)
        _seed(
            log,
            Seq(plan)
            .plan_created()
            .dispatch("n1", 1, ts=T0)
            .worker_done("n1", 1, ts=T0)
            .audit_started("n1", 1, ts=T0)
            .audit_done("n1", green_verdict(), 1, ts=T0)
            .oracle_checked("n1", True)
            .events,
        )
        tick = SupervisorTick(plan, log, _inbox(tmp_path))

        # Advance #1 (via the kernel — its key scheme).
        tick.run(now=T0)
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.DONE
        assert _event_types(log).count(EventType.NODE_ADVANCED) == 1

        # Human/S4b acts on a stale revalidation: DONE → BLOCKED_BY_FIX, fixer succeeds, oracle GREEN.
        log.append_event(
            type=EventType.FIXER_SPAWNED,
            payload=Fixer(
                fixer_id="fix1",
                parent_node="n1",
                attempt=1,
                trigger=FixerTrigger.ORACLE_RED,
                base_ref="main",
            ),
            dedupe_key="fixer_spawned:fix1",
            ts=T0,
        )
        log.append_event(
            type=EventType.FIXER_DONE,
            payload=FixerDone(fixer_id="fix1", parent_node="n1", attempt=1, state=FixerState.DONE),
            dedupe_key="fixer_done:fix1",
            ts=T0,
        )
        log.append_event(
            type=EventType.ORACLE_CHECKED,
            payload=OracleChecked(node="n1", scope=OracleScope.MILESTONE, passed=True),
            dedupe_key="oracle_checked:n1:revalidate",
            ts=T0,
        )

        # Advance #2 at the SAME attempt — must NOT crash/collide; a fresh node_advanced lands.
        res = tick.run(now=T0)
        assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.DONE
        assert _event_types(log).count(EventType.NODE_ADVANCED) == 2  # fresh, not deduped
        assert [a.decision.kind for a in res.applied] == [DecisionKind.ADVANCE]

    def test_blocked_nodes_visible_even_when_paused(self, tmp_path):
        """R2 codex #2: a BLOCKED node stays visible to the owner even when the round's
        plan_status is PAUSED (so 'still needs me' is never hidden)."""
        plan = make_plan()
        log = _log(tmp_path, plan)
        _seed(
            log,
            Seq(plan)
            .plan_created()
            .dispatch("n1", 1, ts=T0)
            .worker_done("n1", 1, ts=T0)
            .audit_started("n1", 1, ts=T0)
            .audit_done("n1", unknown_verdict(), 1, ts=T0)
            .node_blocked("n1", "infra outage")
            .global_paused()
            .events,
        )
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(now=T0)
        assert res.plan_status is PlanStatus.PAUSED
        assert res.blocked_nodes == ["n1"]  # still surfaced despite PAUSED
        assert "n1" in res.to_dict()["blocked_nodes"]

    def test_timeout_alert_is_honest_about_not_killing(self, tmp_path):
        """R2 gemini #4: the timeout alert must not imply the worker process was killed."""
        plan = make_plan()
        log = _log(tmp_path, plan)
        _seed(log, Seq(plan).plan_created().dispatch("n1", 1, ts=T0).events)
        res = SupervisorTick(plan, log, _inbox(tmp_path)).run(now="2026-06-06T10:33:20")
        timeout_alerts = [a for a in res.alerts if a.kind is AlertKind.TIMEOUT]
        assert timeout_alerts
        assert "does NOT kill" in timeout_alerts[0].message

    def test_attempt_helper_fails_closed_on_broken_invariant(self):
        """R2 codex #4: _attempt raises (not silently coerces to 1) on an attempt < 1."""
        from handoff_fanout.supervisor.policy import Decision
        from handoff_fanout.supervisor.reducer import NodeRuntime
        from handoff_fanout.supervisor.supervisor_tick import _attempt

        bad = Decision(kind=DecisionKind.ADVANCE, node="n1", attempt=0)
        node = NodeRuntime(node_id="n1", attempt=0)
        with pytest.raises(TickError):
            _attempt(bad, node)
