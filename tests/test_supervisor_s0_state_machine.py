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
    assert len(list(EventType)) == 22


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


def test_c7_plan_transitions_endpoints_and_gap() -> None:
    plan_states = set(PlanState)
    for pt in st.PLAN_TRANSITIONS:
        assert pt.frm in plan_states and pt.to in plan_states
    # the resume edge is the documented gap (event is None)
    resume = next(
        pt
        for pt in st.PLAN_TRANSITIONS
        if pt.frm is PlanState.GLOBAL_PAUSED and pt.to is PlanState.RUNNING
    )
    assert resume.event is None
    pause = next(
        pt
        for pt in st.PLAN_TRANSITIONS
        if pt.frm is PlanState.RUNNING and pt.to is PlanState.GLOBAL_PAUSED
    )
    assert pause.event is EventType.GLOBAL_PAUSED


def test_known_gaps_documented() -> None:
    assert len(st.KNOWN_EVENT_GAPS) == 3, "GAP-1 resume, GAP-2 rolled_back, GAP-3 BLOCKED recovery"
    resume = st.KNOWN_EVENT_GAPS[0]
    assert resume.missing_event == "global_resumed"
    assert "GLOBAL_PAUSED" in resume.where and "RUNNING" in resume.where
    wheres = " ".join(g.where for g in st.KNOWN_EVENT_GAPS)
    assert "rolled_back" in wheres  # GAP-2
    assert "BLOCKED" in wheres and "recovery" in wheres  # GAP-3


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
    shrunk = frozenset(st.INFORMATIONAL_EVENTS - {EventType.ROLLED_BACK})
    monkeypatch.setattr(st, "INFORMATIONAL_EVENTS", shrunk)
    with pytest.raises(SchemaError, match="taxonomy not total"):
        st.validate_state_machine_closure()


def test_closure_catches_node_event_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # declare an extra event in NODE_STATE_EVENTS not used by any transition (C6).
    bogus = frozenset(st.NODE_STATE_EVENTS | {EventType.SNAPSHOT_TAKEN})
    monkeypatch.setattr(st, "NODE_STATE_EVENTS", bogus)
    with pytest.raises(SchemaError):
        st.validate_state_machine_closure()


def test_transitions_are_immutable() -> None:
    # frozen dataclass — a transition can't be mutated in place.
    t = st.NODE_TRANSITIONS[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.guard = "tampered"  # type: ignore[misc]
