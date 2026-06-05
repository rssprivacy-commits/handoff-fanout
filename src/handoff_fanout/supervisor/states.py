"""S0 — the closed state machine (design §5).

§5 is the *single* closed definition of supervisor state, shared by three
representations: the event set (§4.2), the policy pseudocode (§5), and the
diagram (§5). S0 freezes it so the three never drift. This module is the one
place the states + transitions live; every later slice reads them from here.

Reconciliations S0 makes (each a deliberate interpretive choice — see the R2
dual-brain notes / S0 lesson; flagged because §5 is shorthand in places):

1. **Node vs plan scope.** §5 lists 11 state names. Ten are *node-lifecycle*
   states; ``GLOBAL_PAUSED`` is *plan-scoped* (the pseudocode uses a
   ``s.global_paused`` boolean, not a per-node status). S0 models the node
   states as :class:`NodeState` (10) and the plan scope as :class:`PlanState`
   (``RUNNING`` / ``GLOBAL_PAUSED``). ``RUNNING`` is the only name not literally
   in §5 — it is the explicit complement of ``GLOBAL_PAUSED`` so the pause/resume
   pair is a closed transition rather than a bare boolean. This is why the task
   brief says "10 态" while the doc lists 11 names.

2. **STALE is derived, not a state** (refined per R2 dual-brain). §5's "DONE 若
   final-oracle RED → BLOCKED_BY_FIX 并级联标下游 STALE 重验" does not add an 11th
   node state. STALE is a *derived* predicate the reducer (S3) computes — but the
   trigger is **upstream provenance**, not merely "upstream not DONE": a DONE node
   is stale iff a transitive upstream's current commit/tree differs from the one
   this node was built/validated against (so an upstream that regressed *and then
   re-passed at a new commit* still makes the downstream stale). Because the oracle
   is slow + isolated (it cannot run inside one reducer tick), a stale DONE node
   does **not** "re-validate in place"; the policy spawns a Fixer to async
   rebase+revalidate, i.e. the existing DONE → BLOCKED_BY_FIX edge. So still no new
   state and no new event — but the guard is "spawn Fixer", not "re-validation RED".

3. **AWAIT_APPROVAL → DISPATCHED.** §5's diagram collapses
   "AWAIT_APPROVAL ─granted→ (执行) → DONE". S0 models the execute as a normal
   dispatch (AWAIT_APPROVAL → DISPATCHED) so the approved irreversible work still
   passes audit + oracle before reaching DONE (§6.4 "才执行"). The diagram's
   "→DONE" is the collapsed happy path. The ``irreversible_executed`` event (§4.2)
   is **informational**: the worker records it *after* performing the irreversible
   action during its DISPATCHED run, feeding the SideEffectRegistry — it does not
   itself move a node between states (R2 codex C-P1-5).

4. **UNKNOWN routes to BLOCKED, not BLOCKED_BY_FIX** (R2 P0 — both brains). §5's
   shorthand bundles "verdict RED|UNKNOWN → BLOCKED_BY_FIX → spawn Fixer", but a
   Verdict is UNKNOWN *only* when the dual-brain read could not be trusted
   (degraded / a provider not OK — see ``verdict.py``), i.e. an infrastructure
   failure, never "a real defect was found" (that is RED). Spawning an
   LLM-dependent Fixer on an infra outage just hits the same outage → retry storm
   (R2 gemini G-P0-1). §9 routes "双脑挂/降级 → UNKNOWN → BLOCK → 告警". So S0
   splits the edge: RED / oracle-RED → BLOCKED_BY_FIX (repair), UNKNOWN → BLOCKED
   (escalate human + alert). Consequently a Fixer is never spawned for UNKNOWN, so
   ``FixerTrigger`` (fixer.py) correctly has only VERDICT_RED / ORACLE_RED.

5. **GAP-1/2/3 are now CLOSED (S0-fix, both brains: "surfacing a gap ≠ freezing
   the contract").** The original S0 only *recorded* these in ``KNOWN_EVENT_GAPS``;
   the S0-fix audit ruled they sit on S0's acceptance boundary (replay closure)
   and froze them here. :data:`KNOWN_EVENT_GAPS` is now empty. The §4.2 event set
   and the design doc §5 were amended to match.

   * **GAP-1 (resume replay).** Added the ``global_resumed`` event (events.py) so
     ``GLOBAL_PAUSED → RUNNING`` has a backing event. Without it a paused plan,
     replayed from disk, deadlocks in GLOBAL_PAUSED forever (violating INV-3).
   * **GAP-2 (rollback state effect).** ``rolled_back`` now drives a transition:
     the rolled-back node returns to ``PENDING`` (re-do from base_ref); downstream
     nodes become *derived-stale* (reconciliation #2) and re-validate via the
     existing ``DONE → BLOCKED_BY_FIX`` edge — no separate cascade edges needed.
   * **GAP-3 (human rescue).** Added the ``owner_override`` event + a
     ``BLOCKED → {PENDING|DISPATCHED|AWAIT_APPROVAL}`` recovery edge (the legal
     targets are the closed :class:`~handoff_fanout.supervisor.payloads.RecoveryTarget`
     enum), so a DLQ'd node is no longer a dead node (INV-10 human-rescuable).

This module defines the state machine + a closure self-check
(:func:`validate_state_machine_closure`). It does NOT drive transitions — that is
the Policy/Reducer (slices S3+).
"""

from __future__ import annotations

import dataclasses
import enum

from ._base import SchemaError
from .events import EventType
from .payloads import RecoveryTarget


class NodeState(enum.StrEnum):
    """The 10 node-lifecycle states (design §5)."""

    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    AUDITING = "AUDITING"
    EVALUATING = "EVALUATING"
    DONE = "DONE"
    BLOCKED_BY_FIX = "BLOCKED_BY_FIX"
    BLOCKED = "BLOCKED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    AWAIT_APPROVAL = "AWAIT_APPROVAL"


class PlanState(enum.StrEnum):
    """Plan-scoped state. ``RUNNING`` is S0's explicit complement of the
    doc-named ``GLOBAL_PAUSED`` (reconciliation #1)."""

    RUNNING = "RUNNING"
    GLOBAL_PAUSED = "GLOBAL_PAUSED"


#: The initial state of every node.
INITIAL_NODE_STATE = NodeState.PENDING

#: Only truly terminal node state (no outgoing edges). DONE is NOT terminal —
#: it can regress to BLOCKED_BY_FIX on a final-oracle / stale-revalidation RED.
TERMINAL_NODE_STATES = frozenset({NodeState.CANCELLED})

#: States from which an owner ``abort`` cascades to CANCELLED. Excludes DONE
#: (its committed work is not un-done by abort — that is rollback, a separate op)
#: and CANCELLED (already terminal).
ABORTABLE_NODE_STATES = frozenset(
    {
        NodeState.PENDING,
        NodeState.DISPATCHED,
        NodeState.AUDITING,
        NodeState.EVALUATING,
        NodeState.BLOCKED_BY_FIX,
        NodeState.BLOCKED,
        NodeState.TIMED_OUT,
        NodeState.AWAIT_APPROVAL,
    }
)


@dataclasses.dataclass(frozen=True)
class Transition:
    """One node-state transition, backed by exactly one event (§4.2)."""

    frm: NodeState
    to: NodeState
    event: EventType
    guard: str


@dataclasses.dataclass(frozen=True)
class PlanTransition:
    """One plan-scoped transition. ``event`` may be ``None`` when the §4.2 event
    set lacks a backing event — that is a surfaced gap (see KNOWN_EVENT_GAPS),
    not an invented event."""

    frm: PlanState
    to: PlanState
    event: EventType | None
    guard: str


# --- node transitions (design §5 diagram + pseudocode) -----------------------
_CORE_NODE_TRANSITIONS: tuple[Transition, ...] = (
    Transition(
        NodeState.PENDING,
        NodeState.DISPATCHED,
        EventType.NODE_DISPATCHED,
        "deps satisfied → dispatch",
    ),
    Transition(
        NodeState.PENDING,
        NodeState.AWAIT_APPROVAL,
        EventType.APPROVAL_REQUESTED,
        "reversible=false & approval not yet satisfied",
    ),
    Transition(
        NodeState.AWAIT_APPROVAL,
        NodeState.DISPATCHED,
        EventType.NODE_DISPATCHED,
        "approval_granted & joint-rollback-ready → execute (audited)",
    ),
    Transition(
        NodeState.DISPATCHED,
        NodeState.AUDITING,
        EventType.WORKER_DONE,
        "AckInbox worker_done ingested",
    ),
    Transition(
        NodeState.AUDITING, NodeState.EVALUATING, EventType.AUDIT_DONE, "audit findings landed"
    ),
    Transition(
        NodeState.EVALUATING,
        NodeState.DONE,
        EventType.NODE_ADVANCED,
        "verdict GREEN & oracle GREEN",
    ),
    Transition(
        NodeState.EVALUATING,
        NodeState.BLOCKED_BY_FIX,
        EventType.FIXER_SPAWNED,
        "verdict RED OR oracle RED → spawn Fixer (a real defect to repair)",
    ),
    # reconciliation #5 (R2 P0): UNKNOWN is produced ONLY by infra failure
    # (degraded / provider not OK — see verdict.py), so spawning an LLM Fixer
    # would just hit the same outage → retry storm. §9 routes UNKNOWN to
    # BLOCK+escalate, not to a Fixer. We split §5's shorthand "RED|UNKNOWN→fixer".
    Transition(
        NodeState.EVALUATING,
        NodeState.BLOCKED,
        EventType.NODE_BLOCKED,
        "verdict UNKNOWN (infra failure, not auto-fixable) → escalate human + alert",
    ),
    Transition(
        NodeState.BLOCKED_BY_FIX,
        NodeState.DONE,
        EventType.NODE_ADVANCED,
        "fixer_done & affected→milestone oracle GREEN",
    ),
    Transition(
        NodeState.BLOCKED_BY_FIX,
        NodeState.BLOCKED_BY_FIX,
        EventType.FIXER_SPAWNED,
        "fixer failed but under fix cap → spawn next Fixer (§5 spawn_fixer if under_cap)",
    ),
    Transition(
        NodeState.BLOCKED_BY_FIX,
        NodeState.BLOCKED,
        EventType.NODE_BLOCKED,
        "fix attempts>max OR breaker → DLQ+escalate+alert",
    ),
    Transition(
        NodeState.DISPATCHED, NodeState.TIMED_OUT, EventType.WORKER_TIMEOUT, "Sweeper timeout"
    ),
    Transition(
        NodeState.AUDITING, NodeState.TIMED_OUT, EventType.WORKER_TIMEOUT, "Sweeper timeout"
    ),
    Transition(
        NodeState.TIMED_OUT,
        NodeState.DISPATCHED,
        EventType.NODE_DISPATCHED,
        "retry under cap (new attempt)",
    ),
    Transition(
        NodeState.TIMED_OUT,
        NodeState.BLOCKED,
        EventType.NODE_BLOCKED,
        "escalate (retries exhausted)",
    ),
    Transition(
        NodeState.DONE,
        NodeState.BLOCKED_BY_FIX,
        EventType.FIXER_SPAWNED,
        "final-oracle RED, OR downstream is stale (a transitive upstream "
        "regressed / re-done at a new provenance) → spawn a Fixer to async "
        "rebase+revalidate (NOT instant re-eval — oracle is slow/isolated)",
    ),
)

# --- S0-fix GAP-2: rolled_back state effect (joint Git+DB rollback, §6.3) ------
#: ``rolled_back`` undoes a *settled* node's committed work (git reset to base_ref
#: + DB drop-recreate) and returns it to PENDING for re-do. Only settled states are
#: rollback sources (in-flight DISPATCHED/AUDITING should be aborted/timed-out
#: first). Downstream staleness is handled by the derived-stale DONE→BLOCKED_BY_FIX
#: edge (reconciliation #2), so no extra cascade edges are needed.
_ROLLBACK_TRANSITIONS: tuple[Transition, ...] = tuple(
    Transition(
        s,
        NodeState.PENDING,
        EventType.ROLLED_BACK,
        "owner rollback-to: joint Git+DB rollback → redo from base_ref (§6.3)",
    )
    for s in (NodeState.DONE, NodeState.BLOCKED, NodeState.BLOCKED_BY_FIX)
)

# --- S0-fix GAP-3: owner override — human rescue of a BLOCKED (DLQ'd) node -----
#: A human owner revives a BLOCKED node into one of the closed RecoveryTarget
#: states (redo / force-run / re-gate). The `to` states are generated from the
#: RecoveryTarget enum so the enum and the transition table can never drift
#: (asserted by validate_state_machine_closure C8).
_OWNER_OVERRIDE_TRANSITIONS: tuple[Transition, ...] = tuple(
    Transition(
        NodeState.BLOCKED,
        NodeState(target.value),
        EventType.OWNER_OVERRIDE,
        f"owner override (handoff-cli): rescue BLOCKED node → {target.value} (INV-10)",
    )
    for target in sorted(RecoveryTarget, key=lambda x: x.value)
)

#: owner abort: every abortable state → CANCELLED (generated, not hand-listed).
_ABORT_TRANSITIONS: tuple[Transition, ...] = tuple(
    Transition(s, NodeState.CANCELLED, EventType.NODE_CANCELLED, "owner abort cascade")
    for s in sorted(ABORTABLE_NODE_STATES, key=lambda x: x.value)
)

#: The full node-state transition table (§5). Frozen tuple = single source.
NODE_TRANSITIONS: tuple[Transition, ...] = (
    _CORE_NODE_TRANSITIONS
    + _ROLLBACK_TRANSITIONS
    + _OWNER_OVERRIDE_TRANSITIONS
    + _ABORT_TRANSITIONS
)

#: Plan-scoped transitions. Both edges are now backed by an event (S0-fix GAP-1:
#: GLOBAL_PAUSED → RUNNING is backed by ``global_resumed``, so a paused plan can be
#: replayed back to RUNNING — INV-3).
PLAN_TRANSITIONS: tuple[PlanTransition, ...] = (
    PlanTransition(
        PlanState.RUNNING,
        PlanState.GLOBAL_PAUSED,
        EventType.GLOBAL_PAUSED,
        "owner pause OR DiskGuard over-limit",
    ),
    PlanTransition(
        PlanState.GLOBAL_PAUSED,
        PlanState.RUNNING,
        EventType.GLOBAL_RESUMED,
        "owner/auto resume (S0-fix GAP-1: replayable resume event)",
    ),
)


# --- event taxonomy (S0-derived grouping for closure, not a doc taxonomy) -----
#: Events that directly drive a node-state transition.
NODE_STATE_EVENTS: frozenset[EventType] = frozenset(t.event for t in NODE_TRANSITIONS)

#: Plan-lifecycle / global events (do not change a node's state).
PLAN_LEVEL_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.PLAN_CREATED,
        EventType.PLAN_AMENDED,
        EventType.GLOBAL_PAUSED,
        EventType.GLOBAL_RESUMED,
        EventType.SNAPSHOT_TAKEN,
    }
)

#: Informational / sub-events recorded inside a node's lifecycle (audit/oracle
#: sub-steps, fixer-internal, approval grant record, dlq/escalation records,
#: context patches). They do not themselves move a node between states. NOTE:
#: ``rolled_back`` was moved OUT of this group in the S0-fix — it now drives a
#: node-state transition (GAP-2 closed), so it lives in NODE_STATE_EVENTS.
INFORMATIONAL_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.AUDIT_STARTED,
        EventType.ORACLE_CHECKED,
        EventType.FIXER_DONE,
        EventType.CONTEXT_PATCHED,
        EventType.ESCALATED,
        EventType.APPROVAL_GRANTED,
        EventType.IRREVERSIBLE_EXECUTED,
        EventType.DLQ_ENTERED,
    }
)


@dataclasses.dataclass(frozen=True)
class EventGap:
    """A §5 transition that lacks a backing event in the §4.2 event set.

    Surfaced, not silently fixed (design §12: S0 freezes the contract AND flags
    where §4 / §5 disagree, so the owner resolves it via amendment). The S0-fix
    kept this mechanism but resolved every gap, so the tuple below is now empty —
    a future amendment that adds an unbacked transition would record it here."""

    where: str
    missing_event: str
    recommendation: str


#: The surfaced §4↔§5 gaps. **Empty** after the S0-fix: GAP-1 (resume),
#: GAP-2 (rolled_back state effect), and GAP-3 (BLOCKED human recovery) were all
#: closed by adding the ``global_resumed`` / ``owner_override`` events and the
#: ``rolled_back`` / owner-override transitions (see the module docstring
#: reconciliation #5). The closure check therefore now requires EVERY plan
#: transition to carry a backing event (no documented-gap escape hatch in use).
KNOWN_EVENT_GAPS: tuple[EventGap, ...] = ()


def is_terminal(state: NodeState) -> bool:
    return state in TERMINAL_NODE_STATES


def outgoing(state: NodeState) -> tuple[Transition, ...]:
    """All transitions leaving ``state``."""
    return tuple(t for t in NODE_TRANSITIONS if t.frm is state)


def reachable_node_states() -> set[NodeState]:
    """Every node state reachable from :data:`INITIAL_NODE_STATE`."""
    seen = {INITIAL_NODE_STATE}
    frontier = [INITIAL_NODE_STATE]
    while frontier:
        cur = frontier.pop()
        for t in outgoing(cur):
            if t.to not in seen:
                seen.add(t.to)
                frontier.append(t.to)
    return seen


def validate_state_machine_closure() -> None:
    """Assert the state machine is closed/consistent (design §5). Raises
    :class:`SchemaError` on the first violation. Called by the test-suite and
    usable as a runtime self-check.

    Closure properties:
      C1 every transition endpoint is a real NodeState;
      C2 every transition is backed by a real NodeState-driving EventType;
      C3 every non-terminal state has >=1 outgoing edge (no dead end);
      C4 the terminal state (CANCELLED) has no outgoing edge;
      C5 every node state is reachable from PENDING;
      C6 the event taxonomy partitions EventType exactly (disjoint + total) and
         NODE_STATE_EVENTS equals the events actually used in NODE_TRANSITIONS;
      C7 every plan transition endpoint is a real PlanState, and a plan
         transition has a backing event unless it is a documented KNOWN gap;
      C8 (S0-fix GAP-3) every RecoveryTarget value is a real NodeState, and the
         BLOCKED owner-override edges target EXACTLY the RecoveryTarget set (the
         closed enum and the transition table cannot drift).
    """
    node_states = set(NodeState)

    # C1 + C2
    for t in NODE_TRANSITIONS:
        if t.frm not in node_states or t.to not in node_states:
            raise SchemaError(f"C1 transition endpoint not a NodeState: {t}")
        if not isinstance(t.event, EventType):
            raise SchemaError(f"C2 transition event not an EventType: {t}")

    # C3 + C4
    for s in node_states:
        outs = outgoing(s)
        if is_terminal(s):
            if outs:
                raise SchemaError(f"C4 terminal state {s.value} has outgoing edges")
        elif not outs:
            raise SchemaError(f"C3 non-terminal state {s.value} is a dead end")

    # C5
    unreachable = node_states - reachable_node_states()
    if unreachable:
        raise SchemaError(f"C5 unreachable node states: {sorted(s.value for s in unreachable)}")

    # C6 — taxonomy partitions EventType exactly
    all_events = set(EventType)
    groups = [NODE_STATE_EVENTS, PLAN_LEVEL_EVENTS, INFORMATIONAL_EVENTS]
    union: set[EventType] = set()
    for g in groups:
        overlap = union & g
        if overlap:
            raise SchemaError(
                f"C6 event taxonomy groups overlap: {sorted(e.value for e in overlap)}"
            )
        union |= set(g)
    if union != all_events:
        missing = all_events - union
        extra = union - all_events
        raise SchemaError(
            f"C6 event taxonomy not total: missing={sorted(e.value for e in missing)} "
            f"extra={sorted(e.value for e in extra)}"
        )
    used = {t.event for t in NODE_TRANSITIONS}
    if used != NODE_STATE_EVENTS:
        raise SchemaError(
            "C6 NODE_STATE_EVENTS disagrees with events used in NODE_TRANSITIONS: "
            f"declared={sorted(e.value for e in NODE_STATE_EVENTS)} "
            f"used={sorted(e.value for e in used)}"
        )

    # C7 — plan transitions
    plan_states = set(PlanState)
    gap_edges = {g.where for g in KNOWN_EVENT_GAPS}
    for pt in PLAN_TRANSITIONS:
        if pt.frm not in plan_states or pt.to not in plan_states:
            raise SchemaError(f"C7 plan transition endpoint not a PlanState: {pt}")
        if pt.event is None:
            edge = f"PlanState.{pt.frm.name} -> PlanState.{pt.to.name}"
            if not any(edge in where for where in gap_edges):
                raise SchemaError(
                    f"C7 plan transition has no event and is not a documented gap: {pt}"
                )
        elif not isinstance(pt.event, EventType):
            raise SchemaError(f"C7 plan transition event not an EventType: {pt}")

    # C8 (S0-fix GAP-3) — RecoveryTarget enum ↔ BLOCKED owner-override edges
    state_values = {s.value for s in NodeState}
    stray_targets = sorted({t.value for t in RecoveryTarget} - state_values)
    if stray_targets:
        raise SchemaError(f"C8 RecoveryTarget values are not all NodeStates: {stray_targets}")
    override_targets = {
        t.to
        for t in NODE_TRANSITIONS
        if t.frm is NodeState.BLOCKED and t.event is EventType.OWNER_OVERRIDE
    }
    expected_targets = {NodeState(t.value) for t in RecoveryTarget}
    if override_targets != expected_targets:
        raise SchemaError(
            "C8 BLOCKED owner-override edges must target exactly the RecoveryTarget "
            f"set: edges={sorted(s.value for s in override_targets)} "
            f"expected={sorted(s.value for s in expected_targets)}"
        )
