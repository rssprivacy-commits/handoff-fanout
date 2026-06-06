"""S3 Reducer (C3) tests — pure ``reduce(plan, events) -> State`` (design §3 C3 / §5 / §12).

Covers the INV-3 命脉: determinism + idempotent replay, no external-mutable-state read
(a "poisoned clock/fs" still reduces), legal-edge-only transitions (fail-closed on a
corrupt log), attempt fencing, ContextPatch merge, deterministic Fixer rebuild,
rollback (GAP-2) / owner-override (GAP-3) / pause-resume, derived staleness, and the
snapshot/fingerprint contract.

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s3_reducer.py
"""

from __future__ import annotations

import builtins

import pytest
from _s3_helpers import (
    ContextPatchOp,
    ContextPatchOpKind,
    FixerState,
    FixerTrigger,
    RecoveryTarget,
    Seq,
    green_verdict,
    make_plan,
    node,
    red_verdict,
)

from handoff_fanout.supervisor.reducer import (
    ReductionError,
    is_stale,
    reduce,
    state_fingerprint,
)
from handoff_fanout.supervisor.states import NodeState, PlanState


def _full_lifecycle(plan, n: str = "n1") -> Seq:
    return (
        Seq(plan)
        .plan_created()
        .dispatch(n)
        .worker_done(n)
        .audit_started(n)
        .audit_done(n, green_verdict())
        .oracle_checked(n, True)
        .node_advanced(n)
    )


# --- happy path + determinism ------------------------------------------------


def test_full_lifecycle_reaches_done() -> None:
    plan = make_plan()
    state = reduce(plan, _full_lifecycle(plan).events)
    n = state.nodes["n1"]
    assert n.status is NodeState.DONE
    assert n.verdict is not None and n.attempt == 1
    assert n.inflight_since_ts is None  # nothing in flight once settled
    assert state.plan_state is PlanState.RUNNING and state.last_seq == 6


def test_reduce_is_deterministic_and_idempotent() -> None:
    plan = make_plan()
    events = _full_lifecycle(plan).events
    a = reduce(plan, events)
    b = reduce(plan, events)
    assert a == b
    assert state_fingerprint(a) == state_fingerprint(b)


def test_incremental_prefix_replay_matches_whole() -> None:
    # Replaying prefixes (a fresh process resuming from the log) yields the same state
    # as reducing the whole log at once — INV-3 replay determinism (design §9).
    plan = make_plan()
    events = _full_lifecycle(plan).events
    whole = reduce(plan, events)
    incremental = reduce(plan, events[: len(events)])
    assert state_fingerprint(whole) == state_fingerprint(incremental)
    # Every prefix reduces without error and the final equals the whole.
    for k in range(len(events) + 1):
        reduce(plan, events[:k])


def test_plan_created_seeds_all_nodes_pending() -> None:
    plan = make_plan(nodes=[node("a"), node("b", deps=["a"])])
    state = reduce(plan, Seq(plan).plan_created().events)
    assert state.nodes["a"].status is NodeState.PENDING
    assert state.nodes["b"].status is NodeState.PENDING


# --- INV-3: pure, no external-mutable-state read -----------------------------


def test_reduce_does_not_read_the_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A poisoned clock must not affect reduce — proving it reads time only from events
    (event.ts), never the wall clock (INV-3)."""
    import time as _time

    monkeypatch.setattr(_time, "time", lambda: (_ for _ in ()).throw(AssertionError("clock read")))

    import datetime as _dt

    class _Poison(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            raise AssertionError("datetime.now read")

    monkeypatch.setattr(_dt, "datetime", _Poison)

    plan = make_plan()
    state = reduce(plan, _full_lifecycle(plan).events)
    assert state.nodes["n1"].status is NodeState.DONE  # reduced fine without any clock


def test_reduce_does_not_read_the_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """reduce takes only (plan, events); it must never open a file (INV-3)."""
    real_open = builtins.open

    def _poison(*a, **k):
        raise AssertionError("filesystem read during reduce")

    plan = make_plan()
    events = _full_lifecycle(plan).events
    monkeypatch.setattr(builtins, "open", _poison)
    try:
        state = reduce(plan, events)
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
    assert state.nodes["n1"].status is NodeState.DONE


# --- legal-edge-only transitions (fail-closed) -------------------------------


def test_illegal_transition_fails_closed() -> None:
    # node_advanced straight from DISPATCHED (attempt matches, but DISPATCHED→DONE is not
    # a legal S0 edge — only EVALUATING/BLOCKED_BY_FIX → DONE) → ReductionError
    # (corruption surfaced, not silently mis-reduced).
    plan = make_plan()
    seq = Seq(plan).plan_created().dispatch("n1").node_advanced("n1", attempt=1)
    with pytest.raises(ReductionError, match="illegal transition"):
        reduce(plan, seq.events)


def test_attempt_fencing_rejects_stale_completion() -> None:
    plan = make_plan()
    # dispatch attempt 1, but worker_done claims attempt 2 (a stale/fencing violation).
    seq = Seq(plan).plan_created().dispatch("n1", 1)
    seq.worker_done("n1", attempt=2)
    with pytest.raises(ReductionError, match="stale/fencing"):
        reduce(plan, seq.events)


def test_unknown_node_reference_fails_closed() -> None:
    plan = make_plan(nodes=[node("n1")])
    seq = Seq(plan).plan_created().dispatch("ghost")
    with pytest.raises(ReductionError, match="unknown node"):
        reduce(plan, seq.events)


def test_non_genesis_plan_created_rejected() -> None:
    plan = make_plan()
    seq = Seq(plan).plan_created()
    seq.plan_created()  # a second plan_created at seq 1
    with pytest.raises(ReductionError, match="genesis"):
        reduce(plan, seq.events)


# --- ContextPatch merge (design §3 C15, replayable) --------------------------


def test_context_patch_upsert_and_delete() -> None:
    plan = make_plan()
    seq = (
        Seq(plan)
        .plan_created()
        .context_patched([ContextPatchOp(op=ContextPatchOpKind.UPSERT, key="k1", value="v1")])
        .context_patched(
            [
                ContextPatchOp(op=ContextPatchOpKind.UPSERT, key="k2", value="v2"),
                ContextPatchOp(op=ContextPatchOpKind.UPSERT, key="k1", value="v1b"),
            ]
        )
        .context_patched([ContextPatchOp(op=ContextPatchOpKind.DELETE, key="k2")])
    )
    state = reduce(plan, seq.events)
    assert state.context == {"k1": "v1b"}  # k1 upserted twice, k2 deleted


# --- Fixer deterministic rebuild (design §4.6) -------------------------------


def test_fixer_spawn_and_done_rebuild() -> None:
    plan = make_plan()
    seq = (
        Seq(plan)
        .plan_created()
        .dispatch("n1")
        .worker_done("n1")
        .audit_started("n1")
        .audit_done("n1", red_verdict())  # RED → a fixer will be spawned
        .fixer_spawned("n1", "f-n1-1", trigger=FixerTrigger.VERDICT_RED)
    )
    state = reduce(plan, seq.events)
    n = state.nodes["n1"]
    assert n.status is NodeState.BLOCKED_BY_FIX
    assert n.fix_attempts == 1
    assert n.active_fixer is not None and n.active_fixer.fixer_id == "f-n1-1"
    assert n.active_fixer.state is FixerState.DISPATCHED
    assert n.oracle is None  # pre-fix oracle (if any) reset for the post-fix re-check

    seq.fixer_done("n1", "f-n1-1", FixerState.DONE)
    state2 = reduce(plan, seq.events)
    assert state2.nodes["n1"].active_fixer.state is FixerState.DONE
    assert state2.nodes["n1"].status is NodeState.BLOCKED_BY_FIX  # awaits policy re-eval


def test_fixer_done_for_unknown_fixer_fails_closed() -> None:
    plan = make_plan()
    seq = (
        Seq(plan)
        .plan_created()
        .dispatch("n1")
        .worker_done("n1")
        .audit_started("n1")
        .audit_done("n1", red_verdict())
        .fixer_spawned("n1", "f-real", trigger=FixerTrigger.VERDICT_RED)
        .fixer_done("n1", "f-ghost", FixerState.DONE)  # wrong fixer id
    )
    with pytest.raises(ReductionError, match="no such"):
        reduce(plan, seq.events)


# --- rollback (GAP-2) / owner override (GAP-3) / pause-resume -----------------


def test_rollback_returns_node_to_pending() -> None:
    plan = make_plan()
    seq = _full_lifecycle(plan)  # n1 DONE
    seq.rolled_back("n1")
    state = reduce(plan, seq.events)
    n = state.nodes["n1"]
    assert n.status is NodeState.PENDING  # redo from base (GAP-2)
    assert n.verdict is None and n.oracle is None and n.active_fixer is None


def test_owner_override_rescues_blocked_node() -> None:
    plan = make_plan()
    seq = (
        Seq(plan)
        .plan_created()
        .dispatch("n1")
        .worker_done("n1")
        .audit_started("n1")
        .audit_done("n1", green_verdict())
        .node_blocked("n1", "stuck")  # EVALUATING → BLOCKED
        .owner_override("n1", RecoveryTarget.PENDING)
    )
    state = reduce(plan, seq.events)
    assert state.nodes["n1"].status is NodeState.PENDING  # rescued (GAP-3)


def test_owner_override_to_pending_clears_stale_approval() -> None:
    # R2 codex #5 (safety): an irreversible node that was approved then blocked, when
    # rescued back to PENDING via owner_override, must NOT keep its stale approval — else
    # the policy would re-dispatch it without fresh owner consent.
    from handoff_fanout.supervisor.actions import Approval, SideEffect, SideEffectKind
    from handoff_fanout.supervisor.events import EventType
    from handoff_fanout.supervisor.payloads import NodeAttempt, NodeReason

    irreversible = node(
        "n1",
        reversible=False,
        side_effects=[SideEffect(kind=SideEffectKind.DB_MIGRATION, needs_preauth=True)],
    )
    plan = make_plan(nodes=[irreversible])
    seq = Seq(plan).plan_created()
    # approval_requested → AWAIT_APPROVAL; approval_granted (sets approval); dispatch;
    # timeout; block → BLOCKED (approval still set throughout).
    seq._add(EventType.APPROVAL_REQUESTED, NodeReason(node="n1", reason="needs approval"), "ar:n1")
    seq._add(
        EventType.APPROVAL_GRANTED,
        Approval(
            node="n1",
            grantor="owner",
            granted_at="2026-06-06T10:00:00",
            expires_at="2026-12-31T00:00:00",
            bound_hash="h1",
        ),
        "ag:n1",
    )
    seq.dispatch("n1", 1)  # AWAIT_APPROVAL → DISPATCHED
    seq._add(EventType.WORKER_TIMEOUT, NodeAttempt(node="n1", attempt=1), "wt:n1:1")  # → TIMED_OUT
    seq.node_blocked("n1", "stuck")  # TIMED_OUT → BLOCKED
    assert reduce(plan, seq.events).nodes["n1"].approval is not None  # approval set pre-override

    seq.owner_override("n1", RecoveryTarget.PENDING)
    state = reduce(plan, seq.events)
    assert state.nodes["n1"].status is NodeState.PENDING
    assert state.nodes["n1"].approval is None  # stale approval cleared (fresh consent req'd)


def test_global_pause_then_resume() -> None:
    plan = make_plan()
    seq = Seq(plan).plan_created().global_paused().global_resumed()
    states = [reduce(plan, seq.events[:k]) for k in range(len(seq.events) + 1)]
    assert states[2].plan_state is PlanState.GLOBAL_PAUSED  # after global_paused
    assert states[3].plan_state is PlanState.RUNNING  # after global_resumed (GAP-1 replayable)


# --- derived staleness (states.py reconciliation #2) -------------------------


def test_downstream_is_stale_when_upstream_regresses() -> None:
    plan = make_plan(nodes=[node("a"), node("b", deps=["a"])])
    seq = Seq(plan).plan_created()
    # a → DONE
    seq.dispatch("a").worker_done("a").audit_started("a").audit_done(
        "a", green_verdict()
    ).oracle_checked("a", True).node_advanced("a")
    # b → DONE (built on a)
    seq.dispatch("b").worker_done("b").audit_started("b").audit_done(
        "b", green_verdict()
    ).oracle_checked("b", True).node_advanced("b")
    state = reduce(plan, seq.events)
    assert not is_stale(state, plan, "b")  # both DONE, b built on current a

    # a regresses (rolled back) → b becomes stale (upstream no longer DONE).
    seq.rolled_back("a")
    state2 = reduce(plan, seq.events)
    assert state2.nodes["a"].status is NodeState.PENDING
    assert is_stale(state2, plan, "b")  # upstream regressed out of DONE


def test_non_done_node_is_never_stale() -> None:
    plan = make_plan(nodes=[node("a"), node("b", deps=["a"])])
    state = reduce(plan, Seq(plan).plan_created().events)
    assert not is_stale(state, plan, "b")  # PENDING is not stale (never built)


# --- snapshot marker ---------------------------------------------------------


def test_snapshot_taken_records_through_seq() -> None:
    plan = make_plan()
    seq = _full_lifecycle(plan)
    state_before = reduce(plan, seq.events)
    seq.snapshot_taken(
        through_seq=state_before.last_seq, state_hash=state_fingerprint(state_before)
    )
    state = reduce(plan, seq.events)
    assert state.snapshot_through_seq == 6


def test_non_contiguous_event_list_rejected() -> None:
    plan = make_plan()
    seq = _full_lifecycle(plan)
    # Drop an interior event → the reducer sees a seq gap and fails closed.
    broken = seq.events[:3] + seq.events[4:]
    with pytest.raises(ReductionError, match="non-contiguous"):
        reduce(plan, broken)
