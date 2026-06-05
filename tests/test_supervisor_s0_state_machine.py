"""S0 state-machine closure tests (design §5).

Verifies the §5 state machine is closed/consistent (the C1-C7 properties), that
the specific §5 edges are present, that the event taxonomy partitions the §4.2
event set exactly, and — crucially — that the closure checker is NOT vacuous: it
actually raises on injected violations.

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s0_state_machine.py
"""

from __future__ import annotations

import dataclasses

import pytest

from handoff_fanout.supervisor import (
    EventType,
    NodeState,
    PlanState,
    SchemaError,
)
from handoff_fanout.supervisor import states as st


def test_closure_passes() -> None:
    st.validate_state_machine_closure()  # must not raise


def test_state_counts() -> None:
    assert len(list(NodeState)) == 10
    assert len(list(PlanState)) == 2
    # S0-fix added global_resumed (GAP-1) + owner_override (GAP-3) to the original 22.
    assert len(list(EventType)) == 24


def test_c1_every_transition_endpoint_is_nodestate() -> None:
    states = set(NodeState)
    for t in st.NODE_TRANSITIONS:
        assert t.frm in states and t.to in states


def test_c2_every_transition_event_is_eventtype() -> None:
    for t in st.NODE_TRANSITIONS:
        assert isinstance(t.event, EventType)


def test_c3_no_dead_ends() -> None:
    for s in NodeState:
        if s not in st.TERMINAL_NODE_STATES:
            assert st.outgoing(s), f"{s.value} is a dead end"


def test_c4_terminal_has_no_outgoing() -> None:
    for s in st.TERMINAL_NODE_STATES:
        assert st.outgoing(s) == ()


def test_terminal_set_is_exactly_cancelled() -> None:
    assert frozenset({NodeState.CANCELLED}) == st.TERMINAL_NODE_STATES


def test_c5_all_states_reachable_from_pending() -> None:
    assert st.reachable_node_states() == set(NodeState)
    assert st.INITIAL_NODE_STATE is NodeState.PENDING


def test_c6_event_taxonomy_is_a_partition() -> None:
    groups = [st.NODE_STATE_EVENTS, st.PLAN_LEVEL_EVENTS, st.INFORMATIONAL_EVENTS]
    # disjoint
    seen: set[EventType] = set()
    for g in groups:
        assert not (seen & g), "event taxonomy groups overlap"
        seen |= set(g)
    # total
    assert seen == set(EventType)


def test_c6_node_state_events_match_used() -> None:
    used = {t.event for t in st.NODE_TRANSITIONS}
    assert used == st.NODE_STATE_EVENTS


def test_c7_plan_transitions_endpoints_and_backed() -> None:
    plan_states = set(PlanState)
    for pt in st.PLAN_TRANSITIONS:
        assert pt.frm in plan_states and pt.to in plan_states
        # S0-fix GAP-1: every plan transition now carries a backing event (no gaps).
        assert pt.event is not None
    # the resume edge is now backed by global_resumed (was the GAP-1 None edge)
    resume = next(
        pt
        for pt in st.PLAN_TRANSITIONS
        if pt.frm is PlanState.GLOBAL_PAUSED and pt.to is PlanState.RUNNING
    )
    assert resume.event is EventType.GLOBAL_RESUMED
    pause = next(
        pt
        for pt in st.PLAN_TRANSITIONS
        if pt.frm is PlanState.RUNNING and pt.to is PlanState.GLOBAL_PAUSED
    )
    assert pause.event is EventType.GLOBAL_PAUSED


def test_known_gaps_are_all_closed() -> None:
    # S0-fix: GAP-1/2/3 are resolved, so the surfaced-gaps list is now empty.
    assert st.KNOWN_EVENT_GAPS == ()


def test_gap1_resume_is_replayable() -> None:
    # GAP-1 closed: a paused plan can be replayed back to RUNNING via global_resumed.
    assert EventType.GLOBAL_RESUMED in EventType
    edge = next(
        pt
        for pt in st.PLAN_TRANSITIONS
        if pt.frm is PlanState.GLOBAL_PAUSED and pt.to is PlanState.RUNNING
    )
    assert edge.event is EventType.GLOBAL_RESUMED


def test_gap2_rolled_back_returns_node_to_pending() -> None:
    # GAP-2 closed: rolled_back drives settled states back to PENDING (re-do).
    rb = {t.frm for t in st.NODE_TRANSITIONS if t.event is EventType.ROLLED_BACK}
    assert rb == {NodeState.DONE, NodeState.BLOCKED, NodeState.BLOCKED_BY_FIX}
    assert all(
        t.to is NodeState.PENDING for t in st.NODE_TRANSITIONS if t.event is EventType.ROLLED_BACK
    )
    # and rolled_back is no longer an informational event (it moves state now)
    assert EventType.ROLLED_BACK not in st.INFORMATIONAL_EVENTS
    assert EventType.ROLLED_BACK in st.NODE_STATE_EVENTS


def test_gap3_blocked_owner_recovery() -> None:
    # GAP-3 closed: a BLOCKED (DLQ'd) node can be rescued by an owner_override into
    # exactly the RecoveryTarget set — it is no longer a dead node (INV-10).
    from handoff_fanout.supervisor import RecoveryTarget

    targets = {
        t.to
        for t in st.NODE_TRANSITIONS
        if t.frm is NodeState.BLOCKED and t.event is EventType.OWNER_OVERRIDE
    }
    assert targets == {NodeState(rt.value) for rt in RecoveryTarget}
    assert targets == {NodeState.PENDING, NodeState.DISPATCHED, NodeState.AWAIT_APPROVAL}


def test_c8_recovery_target_matches_node_states() -> None:
    from handoff_fanout.supervisor import RecoveryTarget

    assert {rt.value for rt in RecoveryTarget} <= {s.value for s in NodeState}


def test_unknown_verdict_routes_to_blocked_not_fixer() -> None:
    # R2 P0: UNKNOWN (infra failure) escalates to a human, never spawns a Fixer.
    eval_outs = {(t.to, t.event) for t in st.outgoing(NodeState.EVALUATING)}
    assert (NodeState.BLOCKED, EventType.NODE_BLOCKED) in eval_outs
    # the verdict-UNKNOWN edge is BLOCKED, and its guard says so
    unknown_edge = next(
        t
        for t in st.NODE_TRANSITIONS
        if t.frm is NodeState.EVALUATING and t.to is NodeState.BLOCKED
    )
    assert "UNKNOWN" in unknown_edge.guard
    # the BLOCKED_BY_FIX (repair) edge from EVALUATING must NOT mention UNKNOWN
    fix_edge = next(
        t
        for t in st.NODE_TRANSITIONS
        if t.frm is NodeState.EVALUATING and t.to is NodeState.BLOCKED_BY_FIX
    )
    assert "UNKNOWN" not in fix_edge.guard


def test_fixer_trigger_has_no_unknown() -> None:
    # consequence of routing UNKNOWN away from fixers: no VERDICT_UNKNOWN trigger
    from handoff_fanout.supervisor import FixerTrigger

    assert {t.value for t in FixerTrigger} == {"verdict_RED", "oracle_RED"}


def test_multi_round_fixer_self_loop_present() -> None:
    # §5 `spawn_fixer if under_cap`: a failed fixer under cap spawns the next one
    assert any(
        t.frm is NodeState.BLOCKED_BY_FIX
        and t.to is NodeState.BLOCKED_BY_FIX
        and t.event is EventType.FIXER_SPAWNED
        for t in st.NODE_TRANSITIONS
    )


# --- specific §5 edges --------------------------------------------------------

CORE_EDGES = [
    (NodeState.PENDING, NodeState.DISPATCHED),
    (NodeState.PENDING, NodeState.AWAIT_APPROVAL),
    (NodeState.AWAIT_APPROVAL, NodeState.DISPATCHED),
    (NodeState.DISPATCHED, NodeState.AUDITING),
    (NodeState.AUDITING, NodeState.EVALUATING),
    (NodeState.EVALUATING, NodeState.DONE),
    (NodeState.EVALUATING, NodeState.BLOCKED_BY_FIX),
    (NodeState.EVALUATING, NodeState.BLOCKED),  # verdict UNKNOWN → escalate
    (NodeState.BLOCKED_BY_FIX, NodeState.DONE),
    (NodeState.BLOCKED_BY_FIX, NodeState.BLOCKED_BY_FIX),  # multi-round fixer
    (NodeState.BLOCKED_BY_FIX, NodeState.BLOCKED),
    (NodeState.DISPATCHED, NodeState.TIMED_OUT),
    (NodeState.AUDITING, NodeState.TIMED_OUT),
    (NodeState.TIMED_OUT, NodeState.DISPATCHED),
    (NodeState.TIMED_OUT, NodeState.BLOCKED),
    (NodeState.DONE, NodeState.BLOCKED_BY_FIX),
    # S0-fix GAP-2: rolled_back → PENDING from settled states
    (NodeState.DONE, NodeState.PENDING),
    (NodeState.BLOCKED, NodeState.PENDING),
    (NodeState.BLOCKED_BY_FIX, NodeState.PENDING),
    # S0-fix GAP-3: owner_override rescue of a BLOCKED node
    (NodeState.BLOCKED, NodeState.DISPATCHED),
    (NodeState.BLOCKED, NodeState.AWAIT_APPROVAL),
]


@pytest.mark.parametrize("frm,to", CORE_EDGES, ids=lambda x: getattr(x, "value", x))
def test_core_edge_present(frm: NodeState, to: NodeState) -> None:
    assert any(t.frm is frm and t.to is to for t in st.NODE_TRANSITIONS)


def test_abort_from_every_abortable_state() -> None:
    for s in st.ABORTABLE_NODE_STATES:
        assert any(
            t.frm is s and t.to is NodeState.CANCELLED and t.event is EventType.NODE_CANCELLED
            for t in st.NODE_TRANSITIONS
        ), f"{s.value} has no abort edge"


def test_done_and_cancelled_are_not_abortable() -> None:
    # DONE work is committed (abort != rollback); CANCELLED is terminal.
    assert NodeState.DONE not in st.ABORTABLE_NODE_STATES
    assert NodeState.CANCELLED not in st.ABORTABLE_NODE_STATES
    assert not any(
        t.frm is NodeState.DONE and t.to is NodeState.CANCELLED for t in st.NODE_TRANSITIONS
    )


def test_await_approval_executes_via_dispatch_not_direct_done() -> None:
    # reconciliation #3: approved irreversible node still gets audited.
    outs = {t.to for t in st.outgoing(NodeState.AWAIT_APPROVAL)}
    assert NodeState.DISPATCHED in outs
    assert NodeState.DONE not in outs


# --- the checker must not be vacuous: injected violations must be caught ------


def test_closure_catches_dead_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # drop every outgoing edge of DISPATCHED → it becomes a dead end (C3).
    broken = tuple(t for t in st.NODE_TRANSITIONS if t.frm is not NodeState.DISPATCHED)
    monkeypatch.setattr(st, "NODE_TRANSITIONS", broken)
    with pytest.raises(SchemaError, match="dead end"):
        st.validate_state_machine_closure()


def test_closure_catches_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    # remove the only edge into AWAIT_APPROVAL → unreachable (C5).
    broken = tuple(t for t in st.NODE_TRANSITIONS if t.to is not NodeState.AWAIT_APPROVAL)
    monkeypatch.setattr(st, "NODE_TRANSITIONS", broken)
    with pytest.raises(SchemaError):
        st.validate_state_machine_closure()


def test_closure_catches_taxonomy_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    # drop one event from the informational group → taxonomy no longer total (C6).
    shrunk = frozenset(st.INFORMATIONAL_EVENTS - {EventType.DLQ_ENTERED})
    monkeypatch.setattr(st, "INFORMATIONAL_EVENTS", shrunk)
    with pytest.raises(SchemaError, match="taxonomy not total"):
        st.validate_state_machine_closure()


def test_closure_catches_node_event_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # declare an extra event in NODE_STATE_EVENTS not used by any transition (C6).
    bogus = frozenset(st.NODE_STATE_EVENTS | {EventType.SNAPSHOT_TAKEN})
    monkeypatch.setattr(st, "NODE_STATE_EVENTS", bogus)
    with pytest.raises(SchemaError):
        st.validate_state_machine_closure()


def test_closure_catches_unbacked_plan_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    # S0-fix GAP-1: with no documented gaps, an event=None plan transition is illegal.
    from handoff_fanout.supervisor import PlanTransition

    broken = st.PLAN_TRANSITIONS + (
        PlanTransition(PlanState.GLOBAL_PAUSED, PlanState.RUNNING, None, "smuggled gap"),
    )
    monkeypatch.setattr(st, "PLAN_TRANSITIONS", broken)
    with pytest.raises(SchemaError):
        st.validate_state_machine_closure()


def test_closure_catches_owner_override_target_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    # C8: drop one BLOCKED owner-override edge → edges no longer match RecoveryTarget.
    broken = tuple(
        t
        for t in st.NODE_TRANSITIONS
        if not (
            t.frm is NodeState.BLOCKED
            and t.event is EventType.OWNER_OVERRIDE
            and t.to is NodeState.DISPATCHED
        )
    )
    monkeypatch.setattr(st, "NODE_TRANSITIONS", broken)
    with pytest.raises(SchemaError, match="C8"):
        st.validate_state_machine_closure()


def test_transitions_are_immutable() -> None:
    # frozen dataclass — a transition can't be mutated in place.
    t = st.NODE_TRANSITIONS[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.guard = "tampered"  # type: ignore[misc]
