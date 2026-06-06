"""S3-fix repro suite — the 4 P1 + 2 P2 fault-tolerance/escape contracts the
supervisor's independent dual-brain audit收敛 RED on the S3 骨架 (06970e4).

Each test reproduces one defect: it FAILS on the un-fixed S3 skeleton and PASSES
after the fix. The S3 architecture骨架 (EventLog single-writer / AckInbox seam /
pure Reducer / shadow Policy) is correct and unchanged — these close容错/逃生 holes.

  P1-1  AckInbox must not punch through EventLog's dedupe-collision fail-closed:
        a contradictory second Ack for the same logical event is surfaced
        (quarantined), never silently swallowed as a benign re-delivery.
  P1-2  the Policy must reclaim a hung in-flight Fixer (BLOCKED_BY_FIX past budget)
        instead of dead-locking the pipeline forever.
  P1-3  owner_override→DONE is unreachable by construction (RecoveryTarget excludes
        DONE), so the "forced-DONE staleness rollback" defect is moot — proven here.
  P1-4  an irreversible node is never dispatched on an EXPIRED approval (anti-replay).
  P2-a  a fix budget is reset when a node re-enters a fresh attempt (rollback /
        owner_override→PENDING), else the redo暴毙 on the first RED with no repair.
  P2-b  the AckInbox deposit filename distinguishes different-content signals so a
        contradiction is not overwritten on disk before the supervisor ever sees it.

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s3_fix.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _s3_helpers import (
    TS,
    Ack,
    FixerState,
    RecoveryTarget,
    Seq,
    green_verdict,
    make_plan,
    node,
    red_verdict,
)

from handoff_fanout.supervisor.ack_inbox import (
    AckInbox,
    InboxSignalKind,
    TranslationDisposition,
)
from handoff_fanout.supervisor.actions import (
    Approval,
    SideEffect,
    SideEffectKind,
)
from handoff_fanout.supervisor.event_log import EventLog
from handoff_fanout.supervisor.events import EventType
from handoff_fanout.supervisor.payloads import NodeReason
from handoff_fanout.supervisor.policy import Decision, DecisionKind, PolicyConfig, decide
from handoff_fanout.supervisor.reducer import reduce
from handoff_fanout.supervisor.states import (
    NODE_TRANSITIONS,
    NodeState,
    validate_state_machine_closure,
)

TS2 = "2026-06-06T11:00:00"
LATER = "2026-06-06T12:00:00"  # 2h after TS — past the default 1800s budget


# --- shared helpers (mirror the policy-test local helpers; not importable) ----


def _decide(plan, seq: Seq, *, now: str = TS, config: PolicyConfig | None = None) -> list[Decision]:
    return decide(plan, reduce(plan, seq.events), now=now, config=config)


def _find(decisions: list[Decision], node_id: str) -> Decision | None:
    return next((d for d in decisions if d.node == node_id), None)


def _seed(tmp_path: Path, seq: Seq, plan_id: str = "p1") -> EventLog:
    log = EventLog(tmp_path / "events.jsonl", plan_id)
    for e in seq.events:
        log.append(e)
    return log


def _ack(node_id: str, attempt: int = 1, *, run: str | None = None, tree: str = "tree1") -> Ack:
    return Ack(
        node=node_id,
        run_id=run or f"run-{node_id}-{attempt}",
        attempt=attempt,
        tree_oid=tree,
    )


# =============================================================================
# P1-1 — AckInbox must NOT silently swallow a contradictory dual Ack
# =============================================================================


def _dispatched_log(tmp_path: Path):
    plan = make_plan()
    return plan, _seed(tmp_path, Seq(plan).plan_created().dispatch("n1"))


def _auditing_log(tmp_path: Path):
    plan = make_plan()
    seq = Seq(plan).plan_created().dispatch("n1").worker_done("n1").audit_started("n1")
    return plan, _seed(tmp_path, seq)


def _blocked_by_fix_log(tmp_path: Path):
    plan = make_plan()
    seq = (
        Seq(plan)
        .plan_created()
        .dispatch("n1")
        .worker_done("n1")
        .audit_started("n1")
        .audit_done("n1", red_verdict())
        .fixer_spawned("n1", "f-n1-1")
    )
    return plan, _seed(tmp_path, seq)


def test_contradictory_worker_ack_is_quarantined_not_swallowed(tmp_path: Path) -> None:
    plan, log = _dispatched_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1", tree="tree1"))
    inbox.drain(log, plan, ts=TS2)  # worker_done landed → node AUDITING
    n_before = len(log.read_all())
    # A SECOND worker Ack for the SAME node+attempt (== same dedupe_key) but a DIFFERENT
    # body (different tree_oid → different provenance): a worker bug / replay. It must NOT
    # be silently deduped as a benign re-delivery — that masks a divergent single-writer
    # write (破 INV-3). The EventLog collision guard fails it closed; the AckInbox surfaces
    # it as QUARANTINED (untrusted input → quarantine, not crash the whole drain).
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1", tree="tree-DIFFERENT"))
    outcomes = inbox.drain(log, plan, ts=TS2)
    assert outcomes[0].disposition is TranslationDisposition.QUARANTINED
    assert "collision" in (outcomes[0].reason or "").lower()
    assert len(log.read_all()) == n_before  # the original event is untouched (no overwrite)
    assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.AUDITING


def test_contradictory_audit_ack_is_quarantined_not_swallowed(tmp_path: Path) -> None:
    plan, log = _auditing_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.AUDIT, _ack("n1", tree="tree1"))
    inbox.drain(log, plan, ts=TS2, verdict_for=lambda a: green_verdict())  # audit_done landed
    n_before = len(log.read_all())
    inbox.deposit(InboxSignalKind.AUDIT, _ack("n1", tree="tree-DIFFERENT"))
    outcomes = inbox.drain(log, plan, ts=TS2, verdict_for=lambda a: green_verdict())
    assert outcomes[0].disposition is TranslationDisposition.QUARANTINED
    assert "collision" in (outcomes[0].reason or "").lower()
    assert len(log.read_all()) == n_before


def test_contradictory_fixer_ack_is_quarantined_not_swallowed(tmp_path: Path) -> None:
    plan, log = _blocked_by_fix_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.FIXER, _ack("n1", tree="tree1"))
    inbox.drain(log, plan, ts=TS2, fixer_state_for=lambda a, fx: FixerState.DONE)
    n_before = len(log.read_all())
    inbox.deposit(InboxSignalKind.FIXER, _ack("n1", tree="tree-DIFFERENT"))
    outcomes = inbox.drain(log, plan, ts=TS2, fixer_state_for=lambda a, fx: FixerState.DONE)
    assert outcomes[0].disposition is TranslationDisposition.QUARANTINED
    assert "collision" in (outcomes[0].reason or "").lower()
    assert len(log.read_all()) == n_before


def test_benign_redelivery_still_dedupes_after_collision_fix(tmp_path: Path) -> None:
    # Guard: the collision fix must NOT mistake an IDENTICAL re-delivery (whose node has
    # already advanced past the fence's expected state) for a collision — it still dedupes.
    plan, log = _dispatched_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1"))
    inbox.drain(log, plan, ts=TS2)  # node now AUDITING
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1"))  # identical re-delivery
    outcomes = inbox.drain(log, plan, ts=TS2)
    assert outcomes[0].disposition is TranslationDisposition.DEDUPED


# --- P1-1 redelivery robustness (R2 dual-brain consensus: ts-independence + callback
#     purity) — a legitimate at-least-once re-delivery must DEDUPE even at a DIFFERENT
#     drain ``ts``; the collision guard must only fire on a genuinely divergent body. The
#     EventLog logical signature deliberately EXCLUDES the envelope ``ts`` (event_log.py
#     LogicalSignature), so a re-delivery drained later is the same logical event. ----------


def test_worker_redelivery_at_a_later_ts_still_dedupes(tmp_path: Path) -> None:
    plan, log = _dispatched_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1"))
    inbox.drain(log, plan, ts=TS2)  # worker_done landed at ts=TS2
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1"))  # identical signal, drained LATER
    outcomes = inbox.drain(log, plan, ts=LATER)  # different drain ts must NOT collide
    assert outcomes[0].disposition is TranslationDisposition.DEDUPED


def test_audit_redelivery_at_a_later_ts_with_pure_callback_dedupes(tmp_path: Path) -> None:
    plan, log = _auditing_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    verdict_for = lambda a: green_verdict()  # noqa: E731 — pure in `ack` (INV-2 contract)
    inbox.deposit(InboxSignalKind.AUDIT, _ack("n1"))
    inbox.drain(log, plan, ts=TS2, verdict_for=verdict_for)
    inbox.deposit(InboxSignalKind.AUDIT, _ack("n1"))
    outcomes = inbox.drain(log, plan, ts=LATER, verdict_for=verdict_for)
    assert outcomes[0].disposition is TranslationDisposition.DEDUPED


def test_fixer_redelivery_at_a_later_ts_with_pure_callback_dedupes(tmp_path: Path) -> None:
    plan, log = _blocked_by_fix_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    fixer_state_for = lambda a, fx: FixerState.DONE  # noqa: E731 — pure in (ack, fixer)
    inbox.deposit(InboxSignalKind.FIXER, _ack("n1"))
    inbox.drain(log, plan, ts=TS2, fixer_state_for=fixer_state_for)
    inbox.deposit(InboxSignalKind.FIXER, _ack("n1"))
    outcomes = inbox.drain(log, plan, ts=LATER, fixer_state_for=fixer_state_for)
    assert outcomes[0].disposition is TranslationDisposition.DEDUPED


def test_nonpure_verdict_callback_on_redelivery_is_quarantined(tmp_path: Path) -> None:
    # Contract proof (R2 consensus): verdict_for MUST be pure in `ack` (INV-2). If a buggy
    # supervisor recomputes a DIFFERENT verdict for the SAME ack on re-delivery, the divergent
    # body is caught fail-closed (collision → quarantined), never silently accepted — the
    # collision guard protects against an INV-2 violation, it does not "false-positive" a
    # legitimate (pure) re-delivery.
    plan, log = _auditing_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    verdicts = iter([green_verdict(), red_verdict()])  # non-pure: green then red
    nonpure = lambda a: next(verdicts)  # noqa: E731
    inbox.deposit(InboxSignalKind.AUDIT, _ack("n1"))
    inbox.drain(log, plan, ts=TS2, verdict_for=nonpure)  # green appended
    inbox.deposit(InboxSignalKind.AUDIT, _ack("n1"))
    outcomes = inbox.drain(log, plan, ts=TS2, verdict_for=nonpure)  # red → divergent body
    assert outcomes[0].disposition is TranslationDisposition.QUARANTINED
    assert "collision" in (outcomes[0].reason or "").lower()


# =============================================================================
# P1-2 — Policy must reclaim a hung in-flight Fixer (no permanent deadlock)
# =============================================================================


def _blocked_by_fix_seq(*, max_fix: int) -> tuple:
    plan = make_plan(nodes=[node("n1", max_fix_attempts=max_fix)])
    seq = (
        Seq(plan)
        .plan_created()
        .dispatch("n1")
        .worker_done("n1")
        .audit_started("n1")
        .audit_done("n1", red_verdict())
        .fixer_spawned("n1", "f-n1-1")  # → BLOCKED_BY_FIX, fixer DISPATCHED, inflight since TS
    )
    return plan, seq


def test_in_flight_fixer_within_budget_is_not_actionable() -> None:
    # Control: a fixer genuinely in flight (now == spawn ts, within budget) is still
    # waited on — the fix must not reclaim a fixer that has not yet timed out.
    plan, seq = _blocked_by_fix_seq(max_fix=2)
    assert _find(_decide(plan, seq, now=TS), "n1") is None


def test_hung_fixer_under_cap_is_reclaimed_by_retry() -> None:
    # The defect: a hung Fixer (in flight, past budget) was never reclaimed — the node
    # stayed BLOCKED_BY_FIX forever (永久死锁). The fix reclaims it: under the fix cap it
    # retries (spawn next Fixer), so the pipeline makes progress.
    plan, seq = _blocked_by_fix_seq(max_fix=2)  # fix_attempts==1 < cap 2
    d = _find(_decide(plan, seq, now=LATER), "n1")
    assert d is not None and d.kind is DecisionKind.SPAWN_FIXER


def test_hung_fixer_at_cap_escalates_to_block() -> None:
    # A hung Fixer at the fix cap is reclaimed by escalation (BLOCK→DLQ), within the
    # existing BLOCKED_BY_FIX→BLOCKED edge — no new S0 state needed, still no deadlock.
    plan, seq = _blocked_by_fix_seq(max_fix=1)  # fix_attempts==1 == cap 1
    d = _find(_decide(plan, seq, now=LATER), "n1")
    assert d is not None and d.kind is DecisionKind.BLOCK


# =============================================================================
# P1-3 — owner_override→DONE is unreachable by construction (defect is moot)
# =============================================================================


def test_recovery_target_cannot_be_done() -> None:
    # The owner_override legal destinations are the closed RecoveryTarget set — redo /
    # force-run / re-gate. DONE is NOT one, so the payload itself cannot carry it.
    assert {t.value for t in RecoveryTarget} == {"PENDING", "DISPATCHED", "AWAIT_APPROVAL"}
    with pytest.raises(ValueError):
        RecoveryTarget("DONE")


def test_no_blocked_owner_override_edge_to_done() -> None:
    # The S0 transition table has no BLOCKED --owner_override--> DONE edge, so even a
    # forged event could not drive a node to a stale DONE via override (P1-3 moot).
    override_targets = {
        t.to
        for t in NODE_TRANSITIONS
        if t.frm is NodeState.BLOCKED and t.event is EventType.OWNER_OVERRIDE
    }
    assert NodeState.DONE not in override_targets
    assert override_targets == {NodeState.PENDING, NodeState.DISPATCHED, NodeState.AWAIT_APPROVAL}
    validate_state_machine_closure()  # C8: override edges == RecoveryTarget set (no drift)


# =============================================================================
# P1-4 — an irreversible node is never dispatched on an EXPIRED approval
# =============================================================================


def _irreversible_plan():
    irreversible = node(
        "n1",
        reversible=False,
        side_effects=[SideEffect(kind=SideEffectKind.DB_MIGRATION, needs_preauth=True)],
    )
    return make_plan(nodes=[irreversible])


def _await_approval_seq(plan, *, expires_at: str) -> Seq:
    seq = Seq(plan).plan_created()
    seq._add(EventType.APPROVAL_REQUESTED, NodeReason(node="n1", reason="needs approval"), "ar:n1")
    seq._add(
        EventType.APPROVAL_GRANTED,
        Approval(
            node="n1",
            grantor="owner",
            granted_at=TS,
            expires_at=expires_at,
            bound_hash="h1",
        ),
        "ag:n1",
    )
    return seq


def test_expired_approval_does_not_dispatch_irreversible() -> None:
    plan = _irreversible_plan()
    # approval expires at 10:30; now is 12:00 (LATER) → expired → must NOT dispatch.
    seq = _await_approval_seq(plan, expires_at="2026-06-06T10:30:00")
    d = _find(_decide(plan, seq, now=LATER), "n1")
    assert d is None or d.kind is not DecisionKind.DISPATCH


def test_valid_approval_still_dispatches_irreversible() -> None:
    # Control: a non-expired approval DOES dispatch — the gate rejects only EXPIRED ones.
    plan = _irreversible_plan()
    seq = _await_approval_seq(plan, expires_at="2026-12-31T00:00:00")
    d = _find(_decide(plan, seq, now=TS), "n1")
    assert d is not None and d.kind is DecisionKind.DISPATCH


# =============================================================================
# P2-a — fix budget resets when a node re-enters a fresh attempt
# =============================================================================


def _exhausted_then_blocked(plan) -> Seq:
    return (
        Seq(plan)
        .plan_created()
        .dispatch("n1")
        .worker_done("n1")
        .audit_started("n1")
        .audit_done("n1", red_verdict())
        .fixer_spawned("n1", "f-n1-1")  # fix_attempts → 1
        .fixer_done("n1", "f-n1-1", FixerState.FAILED)
        .node_blocked("n1", "fix budget exhausted")  # → BLOCKED
    )


def test_fix_budget_resets_on_rollback() -> None:
    plan = make_plan(nodes=[node("n1", max_fix_attempts=1)])
    seq = _exhausted_then_blocked(plan)
    assert reduce(plan, seq.events).nodes["n1"].fix_attempts == 1  # consumed
    seq.rolled_back("n1")
    n = reduce(plan, seq.events).nodes["n1"]
    assert n.status is NodeState.PENDING
    assert n.fix_attempts == 0  # FRESH budget for the redo (P2-a)


def test_fix_budget_resets_on_owner_override_to_pending() -> None:
    plan = make_plan(nodes=[node("n1", max_fix_attempts=1)])
    seq = _exhausted_then_blocked(plan)
    seq.owner_override("n1", RecoveryTarget.PENDING)
    n = reduce(plan, seq.events).nodes["n1"]
    assert n.status is NodeState.PENDING
    assert n.fix_attempts == 0  # FRESH budget on owner rescue→redo (P2-a)


# =============================================================================
# P2-b — deposit filename distinguishes different-content signals
# =============================================================================


def test_contradictory_deposits_do_not_overwrite_on_disk(tmp_path: Path) -> None:
    # Two DIFFERENT-content signals for the SAME (kind,node,run,attempt) must both survive
    # on disk — else the supervisor only ever drains the last one and the P1-1 collision
    # check never fires (the contradiction is lost before it is ever seen).
    inbox = AckInbox(tmp_path / "inbox")
    a = _ack("n1", run="run-A", tree="treeA")
    b = _ack("n1", run="run-A", tree="treeB")  # same run_id+attempt, different body
    pa = inbox.deposit(InboxSignalKind.WORKER, a)
    pb = inbox.deposit(InboxSignalKind.WORKER, b)
    assert pa != pb
    assert len(list((tmp_path / "inbox").glob("*.json"))) == 2


def test_identical_redelivery_deposit_is_idempotent_one_file(tmp_path: Path) -> None:
    # Guard: a true at-least-once re-delivery of the IDENTICAL signal collapses to one
    # file (content-addressed name) — no duplicate-file noise, INV-1 reproducible.
    inbox = AckInbox(tmp_path / "inbox")
    a = _ack("n1", run="run-A", tree="treeA")
    p1 = inbox.deposit(InboxSignalKind.WORKER, a)
    p2 = inbox.deposit(InboxSignalKind.WORKER, a)
    assert p1 == p2
    assert len(list((tmp_path / "inbox").glob("*.json"))) == 1
