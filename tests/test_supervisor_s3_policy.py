"""S3 Policy (C4) + shadow tests — deterministic control plane (design §5 / §8 / §12 S3).

Covers the INV-1 命脉: the decision is a pure deterministic ``if/else`` (zero LLM), the
full §5 decision table (PENDING→DISPATCH / irreversible→REQUEST_APPROVAL /
AUDITING→START_AUDIT / EVALUATING verdict×oracle → ADVANCE|FIXER|BLOCK / BLOCKED_BY_FIX
retry/block / TIMED_OUT retry/escalate / DONE-stale→REVALIDATE), the Sweeper (injected
``now``, INV-3), GLOBAL_PAUSED stopping new dispatch while still reaping timeouts, the
parallel-DAG fan-out, and shadow replay determinism + report-only + history compare.

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s3_policy.py
"""

from __future__ import annotations

import pytest
from _s3_helpers import (
    TS,
    FixerState,
    FixerTrigger,
    Provenance,
    Seq,
    green_verdict,
    make_plan,
    node,
    red_verdict,
    unknown_verdict,
)

from handoff_fanout.supervisor.actions import SideEffect, SideEffectKind
from handoff_fanout.supervisor.events import EventType
from handoff_fanout.supervisor.oracle import OracleScope
from handoff_fanout.supervisor.payloads import NodeAttempt
from handoff_fanout.supervisor.plan import Node
from handoff_fanout.supervisor.policy import (
    Decision,
    DecisionKind,
    PolicyConfig,
    ShadowReplay,
    compare_to_history,
    decide,
)
from handoff_fanout.supervisor.reducer import reduce, state_fingerprint


def _timeout(seq: Seq, n: str, attempt: int = 1) -> Seq:
    return seq._add(
        EventType.WORKER_TIMEOUT,
        NodeAttempt(node=n, attempt=attempt),
        f"worker_timeout:{n}:{attempt}",
    )


LATER = "2026-06-06T12:00:00"  # 2h after TS — past the default 1800s timeout


def _decide(plan, seq: Seq, *, now: str = TS, config: PolicyConfig | None = None) -> list[Decision]:
    return decide(plan, reduce(plan, seq.events), now=now, config=config)


def _find(decisions: list[Decision], node_id: str) -> Decision | None:
    for d in decisions:
        if d.node == node_id:
            return d
    return None


def _kinds(decisions: list[Decision]) -> set[DecisionKind]:
    return {d.kind for d in decisions}


# --- PENDING / approval ------------------------------------------------------


def test_pending_with_deps_done_dispatches() -> None:
    plan = make_plan()
    d = _find(_decide(plan, Seq(plan).plan_created()), "n1")
    assert d is not None and d.kind is DecisionKind.DISPATCH and d.attempt == 1


def test_pending_blocked_on_unmet_deps_is_not_actionable() -> None:
    plan = make_plan(nodes=[node("a"), node("b", deps=["a"])])
    decisions = _decide(plan, Seq(plan).plan_created())
    assert _find(decisions, "a").kind is DecisionKind.DISPATCH  # a is ready
    assert _find(decisions, "b") is None  # b waits on a (not actionable)


def test_irreversible_node_requests_approval_first() -> None:
    irreversible = Node(
        node_id="n1",
        brief="drop prod db",
        base_ref="main",
        reversible=False,
        side_effects=[SideEffect(kind=SideEffectKind.DB_MIGRATION, needs_preauth=True)],
    )
    plan = make_plan(nodes=[irreversible])
    d = _find(_decide(plan, Seq(plan).plan_created()), "n1")
    assert d is not None and d.kind is DecisionKind.REQUEST_APPROVAL


# --- AUDITING ----------------------------------------------------------------


def test_auditing_without_audit_started_starts_audit() -> None:
    plan = make_plan()
    seq = Seq(plan).plan_created().dispatch("n1").worker_done("n1")
    d = _find(_decide(plan, seq), "n1")
    assert d is not None and d.kind is DecisionKind.START_AUDIT


def test_auditing_with_audit_started_is_not_actionable() -> None:
    plan = make_plan()
    seq = Seq(plan).plan_created().dispatch("n1").worker_done("n1").audit_started("n1")
    assert _find(_decide(plan, seq), "n1") is None  # audit in flight


# --- EVALUATING: verdict × oracle decision table -----------------------------


def _to_evaluating(plan, verdict) -> Seq:
    return (
        Seq(plan)
        .plan_created()
        .dispatch("n1")
        .worker_done("n1")
        .audit_started("n1")
        .audit_done("n1", verdict)
    )


def test_evaluating_green_no_oracle_runs_oracle() -> None:
    plan = make_plan()
    d = _find(_decide(plan, _to_evaluating(plan, green_verdict())), "n1")
    assert d.kind is DecisionKind.RUN_ORACLE and d.scope is OracleScope.MILESTONE


def test_evaluating_green_oracle_green_advances() -> None:
    plan = make_plan()
    seq = _to_evaluating(plan, green_verdict()).oracle_checked("n1", True)
    d = _find(_decide(plan, seq), "n1")
    assert d.kind is DecisionKind.ADVANCE


def test_evaluating_green_oracle_red_spawns_fixer() -> None:
    plan = make_plan()
    seq = _to_evaluating(plan, green_verdict()).oracle_checked("n1", False, failed=["o1"])
    d = _find(_decide(plan, seq), "n1")
    assert d.kind is DecisionKind.SPAWN_FIXER and d.trigger is FixerTrigger.ORACLE_RED


def test_evaluating_red_spawns_fixer() -> None:
    plan = make_plan()
    d = _find(_decide(plan, _to_evaluating(plan, red_verdict())), "n1")
    assert d.kind is DecisionKind.SPAWN_FIXER and d.trigger is FixerTrigger.VERDICT_RED


def test_evaluating_unknown_blocks_not_fixer() -> None:
    # reconciliation #4: UNKNOWN is an infra failure → escalate (BLOCK), never a Fixer.
    plan = make_plan()
    d = _find(_decide(plan, _to_evaluating(plan, unknown_verdict())), "n1")
    assert d.kind is DecisionKind.BLOCK


def test_evaluating_red_with_zero_fix_budget_blocks_not_fixer() -> None:
    # A node forbidden from auto-fix (max_fix_attempts=0) must BLOCK on a RED, never
    # spawn a Fixer it has no budget for (the EVALUATING fix-budget gate).
    plan = make_plan(nodes=[node("n1", max_fix_attempts=0)])
    d = _find(_decide(plan, _to_evaluating(plan, red_verdict())), "n1")
    assert d.kind is DecisionKind.BLOCK


# --- BLOCKED_BY_FIX ----------------------------------------------------------


def _to_blocked_by_fix(plan, *, max_fix: int = 2) -> Seq:
    nodes = [node("n1", max_fix_attempts=max_fix)]
    return (
        Seq(make_plan(nodes=nodes))
        .plan_created()
        .dispatch("n1")
        .worker_done("n1")
        .audit_started("n1")
        .audit_done("n1", red_verdict())
        .fixer_spawned("n1", "f-n1-1")
    )


def test_blocked_by_fix_fixer_done_runs_oracle_then_advances() -> None:
    plan = make_plan(nodes=[node("n1")])
    seq = _to_blocked_by_fix(plan).fixer_done("n1", "f-n1-1", FixerState.DONE)
    d = _find(_decide(plan, seq), "n1")
    assert d.kind is DecisionKind.RUN_ORACLE  # re-run affected→milestone oracle first
    seq.oracle_checked("n1", True)
    d2 = _find(_decide(plan, seq), "n1")
    assert d2.kind is DecisionKind.ADVANCE


def test_blocked_by_fix_failed_under_cap_retries() -> None:
    plan = make_plan(nodes=[node("n1", max_fix_attempts=2)])
    seq = _to_blocked_by_fix(plan, max_fix=2).fixer_done("n1", "f-n1-1", FixerState.FAILED)
    d = _find(_decide(plan, seq), "n1")
    assert d.kind is DecisionKind.SPAWN_FIXER  # 1 attempt < cap 2 → retry


def test_blocked_by_fix_failed_at_cap_blocks() -> None:
    plan = make_plan(nodes=[node("n1", max_fix_attempts=1)])
    seq = _to_blocked_by_fix(plan, max_fix=1).fixer_done("n1", "f-n1-1", FixerState.FAILED)
    d = _find(_decide(plan, seq), "n1")
    assert d.kind is DecisionKind.BLOCK  # 1 attempt == cap 1 → DLQ + escalate


# --- TIMED_OUT ---------------------------------------------------------------


def test_timed_out_under_cap_retries() -> None:
    plan = make_plan(nodes=[node("n1")])
    seq = _timeout(Seq(plan).plan_created().dispatch("n1", 1), "n1")  # TIMED_OUT at attempt 1
    d = _find(_decide(plan, seq, config=PolicyConfig(max_dispatch_attempts=2)), "n1")
    assert d.kind is DecisionKind.DISPATCH and d.attempt == 2


def test_timed_out_at_cap_blocks() -> None:
    plan = make_plan(nodes=[node("n1")])
    seq = _timeout(Seq(plan).plan_created().dispatch("n1", 1), "n1")
    d = _find(_decide(plan, seq, config=PolicyConfig(max_dispatch_attempts=1)), "n1")
    assert d.kind is DecisionKind.BLOCK


# --- DONE staleness ----------------------------------------------------------


def _done(seq: Seq, n: str, *, prov: Provenance | None = None) -> Seq:
    return (
        seq.dispatch(n)
        .worker_done(n)
        .audit_started(n)
        .audit_done(n, green_verdict())
        .oracle_checked(n, True)
        .node_advanced(n, provenance=prov)
    )


def test_done_current_is_not_actionable() -> None:
    plan = make_plan(nodes=[node("n1")])
    seq = _done(Seq(plan).plan_created(), "n1")
    assert _find(_decide(plan, seq), "n1") is None


def test_done_stale_revalidates() -> None:
    plan = make_plan(nodes=[node("a"), node("b", deps=["a"])])
    seq = Seq(plan).plan_created()
    _done(seq, "a")
    _done(seq, "b")
    seq.rolled_back("a")  # a regresses → b stale
    decisions = _decide(plan, seq)
    assert _find(decisions, "b").kind is DecisionKind.REVALIDATE_STALE
    assert _find(decisions, "a").kind is DecisionKind.DISPATCH  # a redoes from base


# --- Sweeper (injected now / INV-3) + GLOBAL_PAUSED --------------------------


def test_sweeper_times_out_in_flight_node() -> None:
    plan = make_plan(nodes=[node("n1")])
    seq = Seq(plan).plan_created().dispatch("n1", 1, ts=TS)  # in flight since TS
    # now == dispatch ts → within budget → no timeout (the only decision is the dispatch
    # for the *next* attempt is not made; the node is in flight DISPATCHED so it waits).
    assert DecisionKind.TIMEOUT not in _kinds(_decide(plan, seq, now=TS))
    # now 2h later (> the default 1800s budget) → TIMEOUT.
    assert _find(_decide(plan, seq, now=LATER), "n1").kind is DecisionKind.TIMEOUT


def test_global_paused_stops_new_dispatch_but_still_reaps_timeouts() -> None:
    plan = make_plan(nodes=[node("n1"), node("n2")])
    seq = Seq(plan).plan_created().dispatch("n1", 1, ts=TS).global_paused()
    decisions = _decide(plan, seq, now=LATER)
    # n1 (in flight) still times out; n2 (PENDING) is NOT newly dispatched while paused.
    assert _find(decisions, "n1").kind is DecisionKind.TIMEOUT
    assert _find(decisions, "n2") is None


def test_decide_does_not_read_the_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Sweeper reasons about timeouts from the INJECTED ``now`` + event ts only — a
    poisoned ``datetime.now`` must not break a decision (INV-3)."""
    import datetime as _dt

    class _Poison(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            raise AssertionError("datetime.now read in the control plane")

    monkeypatch.setattr(_dt, "datetime", _Poison)
    plan = make_plan(nodes=[node("n1")])
    seq = Seq(plan).plan_created().dispatch("n1", 1, ts=TS)
    assert _find(_decide(plan, seq, now=LATER), "n1").kind is DecisionKind.TIMEOUT


# --- INV-1: deterministic (zero-LLM) -----------------------------------------


def test_decide_is_deterministic() -> None:
    plan = make_plan()
    state = reduce(plan, _to_evaluating(plan, green_verdict()).events)
    a = decide(plan, state, now=TS)
    b = decide(plan, state, now=TS)
    assert [d.to_dict() for d in a] == [d.to_dict() for d in b]


def test_parallel_dag_dispatches_all_ready_nodes() -> None:
    # Three independent roots → all dispatched in one tick (parallel DAG, not one-per-tick).
    plan = make_plan(nodes=[node("a"), node("b"), node("c")])
    decisions = _decide(plan, Seq(plan).plan_created())
    dispatched = {d.node for d in decisions if d.kind is DecisionKind.DISPATCH}
    assert dispatched == {"a", "b", "c"}


def test_all_done_emits_final_oracle() -> None:
    plan = make_plan(nodes=[node("n1")])
    seq = _done(Seq(plan).plan_created(), "n1")
    assert DecisionKind.FINAL_ORACLE in _kinds(_decide(plan, seq))


# --- shadow replay: determinism + report-only + history compare --------------


def _recorded_lifecycle(plan) -> Seq:
    seq = Seq(plan).plan_created()
    # a full a→DONE control flow as the supervisor itself would have driven it.
    seq.dispatch("n1").worker_done("n1").audit_started("n1").audit_done(
        "n1", green_verdict()
    ).oracle_checked("n1", True).node_advanced("n1")
    return seq


def test_shadow_replay_is_deterministic() -> None:
    plan = make_plan(nodes=[node("n1")])
    events = _recorded_lifecycle(plan).events
    t1 = ShadowReplay(plan).run(events)
    t2 = ShadowReplay(plan).run(events)
    assert [s.to_dict() for s in t1] == [s.to_dict() for s in t2]  # byte-identical trace


def test_shadow_replay_produces_state_and_decision_sequence() -> None:
    plan = make_plan(nodes=[node("n1")])
    events = _recorded_lifecycle(plan).events
    steps = ShadowReplay(plan).run(events)
    assert len(steps) == len(events) + 1  # one step per prefix (0..N)
    # The final prefix fingerprints the fully-reduced state.
    assert steps[-1].state_fingerprint == state_fingerprint(reduce(plan, events))
    # At prefix 1 (just plan_created) the policy would dispatch n1.
    assert any(d.kind is DecisionKind.DISPATCH and d.node == "n1" for d in steps[1].decisions)


def test_shadow_replay_is_report_only_no_mutation() -> None:
    # ShadowReplay takes the events list and never appends/spawns — the input list is
    # untouched and re-running yields the same trace (no side effects).
    plan = make_plan(nodes=[node("n1")])
    events = _recorded_lifecycle(plan).events
    n_before = len(events)
    ShadowReplay(plan).run(events)
    assert len(events) == n_before  # the recorded log was not grown by the shadow


def test_compare_to_history_matches_supervisor_driven_flow() -> None:
    # A lifecycle whose CONTROL events (dispatch/audit_started/advanced) are exactly what
    # the policy would decide → zero mismatches (the supervisor reproduces history).
    plan = make_plan(nodes=[node("n1")])
    events = _recorded_lifecycle(plan).events
    steps = ShadowReplay(plan).run(events)
    mismatches = compare_to_history(steps, events)
    assert mismatches == []


def test_compare_to_history_flags_a_decision_divergence() -> None:
    # A recorded log that advances n1 WITHOUT the policy's verdict+oracle gate (a human
    # forced it) → the policy would NOT have advanced there → a surfaced mismatch.
    plan = make_plan(nodes=[node("n1")])
    seq = Seq(plan).plan_created().dispatch("n1").worker_done("n1").audit_started("n1")
    # jump straight to node_advanced with no audit_done/oracle — illegal for the reducer,
    # so instead model the divergence at the EVALUATING gate: audit_done GREEN, then a
    # forced advance the policy would have gated behind RUN_ORACLE.
    seq.audit_done("n1", green_verdict())
    seq._add(EventType.NODE_ADVANCED, NodeAttempt(node="n1", attempt=1), "advanced:n1:1")
    steps = ShadowReplay(plan).run(seq.events)
    mismatches = compare_to_history(steps, seq.events)
    # At the prefix before node_advanced, the policy decided RUN_ORACLE (not ADVANCE),
    # so the recorded node_advanced is an unpredicted control event.
    assert any(m.recorded_event_type is EventType.NODE_ADVANCED for m in mismatches)
