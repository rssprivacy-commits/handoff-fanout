"""S3 — Reducer (C3): the pure ``reduce(plan, events) -> State`` (design §3 C3 / §5).

INV-3 is the命脉 of this module: ``reduce`` is a **pure function** of ``(plan,
events)``. It reads **no external mutable state** — no wall clock, no filesystem, no
git HEAD, no context file. Everything it needs (time, verdicts, oracle outcomes,
fixer outcomes, cross-bar context) arrives *inside the events* (the EventLog is the
single source of truth). Consequently a replay is deterministic and idempotent:
``reduce(plan, events) == reduce(plan, events)`` always, and feeding the same log to
a fresh process rebuilds byte-identical state (design §9 "中枢回合死 → reduce 重建").

The reducer drives state strictly through the S0 frozen state machine
(``states.NODE_TRANSITIONS`` / ``PLAN_TRANSITIONS``): every node-state change is
asserted to be a legal edge before it is applied, so a log that would force an
illegal transition is rejected fail-closed (corruption surfaces, it is never
silently mis-reduced). It rebuilds Fixer sub-workflows deterministically from
``fixer_spawned`` / ``fixer_done`` (design §4.6), merges ``context_patched`` KV ops
into the ContextStore (design §3 C15, replayable — never an in-place file edit), and
records snapshot/compaction markers.

It does NOT decide anything (advance / dispatch / spawn-fixer / block) — that is the
Policy (``policy.py``). The reducer answers "what *is* the state"; the policy answers
"what should happen next". Keeping decision out of the reducer is what lets the
reducer stay pure (INV-1 + INV-3).
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Any

from ._base import Contract, SchemaError
from .actions import Approval
from .event_log import canonical_json
from .events import Event, EventType, Provenance
from .fixer import Fixer, FixerState
from .payloads import (
    AuditDone,
    ContextPatch,
    FixerDone,
    GlobalPaused,
    GlobalResumed,
    NodeAttempt,
    NodeReason,
    OracleChecked,
    OwnerOverride,
    RollbackRecord,
    SnapshotTaken,
)
from .plan import Plan
from .states import (
    ABORTABLE_NODE_STATES,
    INITIAL_NODE_STATE,
    NODE_TRANSITIONS,
    NodeState,
    PlanState,
)
from .verdict import Verdict

#: Precomputed legal node-state edges: ``(from, event) -> {to, ...}``. A reduction
#: asserts the (from, event, to) triple is in here before applying — the reducer can
#: only walk edges S0 froze (§5), so a malformed log can't push a node off the graph.
_LEGAL_EDGES: dict[tuple[NodeState, EventType], frozenset[NodeState]] = {}
for _t in NODE_TRANSITIONS:
    _LEGAL_EDGES.setdefault((_t.frm, _t.event), frozenset())
    _LEGAL_EDGES[(_t.frm, _t.event)] = _LEGAL_EDGES[(_t.frm, _t.event)] | {_t.to}


class ReductionError(SchemaError):
    """An event would drive an illegal/ inconsistent transition (corruption). Raised
    fail-closed so a bad log is never silently mis-reduced (INV-3)."""


@dataclasses.dataclass
class FixerRuntime:
    """The deterministically-rebuilt state of a node's active Fixer sub-workflow
    (design §4.6). Reconstructed from ``fixer_spawned`` / ``fixer_done`` events — never
    constructed ad-hoc in memory (INV-3)."""

    fixer_id: str
    parent_node: str
    attempt: int
    state: FixerState

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixer_id": self.fixer_id,
            "parent_node": self.parent_node,
            "attempt": self.attempt,
            "state": self.state.value,
        }


@dataclasses.dataclass
class NodeRuntime:
    """The runtime state of one plan node (distinct from the static
    :class:`~handoff_fanout.supervisor.plan.Node`)."""

    node_id: str
    status: NodeState = INITIAL_NODE_STATE
    attempt: int = 0  # 0 = never dispatched; set to the dispatched attempt on dispatch
    #: ts of the in-flight operation's start (worker run / audit run) — the Sweeper's
    #: clock anchor. ``None`` ⟺ no operation in flight. Sourced from event.ts (which
    #: is part of the event, so the reducer stays time-free — INV-3).
    inflight_since_ts: str | None = None
    audit_started: bool = False  # audit_started seen for this attempt (policy: START_AUDIT once)
    verdict: Verdict | None = None  # from the current attempt's audit_done
    oracle: OracleChecked | None = None  # from the current attempt's oracle_checked
    approval: Approval | None = None  # from approval_granted (gates AWAIT_APPROVAL dispatch)
    fix_attempts: int = 0  # number of fixers spawned for this node
    active_fixer: FixerRuntime | None = None
    built_provenance: Provenance | None = None  # provenance at last DONE (staleness, §5 #2)
    #: For each upstream dep, the dep's built_provenance fingerprint *at the time this
    #: node advanced to DONE* (states.py reconciliation #2). A DONE node is stale iff a
    #: dep is no longer DONE OR its current provenance differs from this snapshot — so an
    #: upstream that regressed *and re-passed at a new commit* still makes this stale.
    upstream_snapshot: dict[str, str] = dataclasses.field(default_factory=dict)
    last_reason: str | None = None  # owner-facing block/cancel/escalate reason (INV-10)

    def _reset_attempt_fields(self) -> None:
        """Clear per-attempt derived fields when a node (re)enters an active attempt."""
        self.verdict = None
        self.oracle = None
        self.active_fixer = None
        self.audit_started = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status.value,
            "attempt": self.attempt,
            "inflight_since_ts": self.inflight_since_ts,
            "audit_started": self.audit_started,
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "oracle": self.oracle.to_dict() if self.oracle else None,
            "approval": self.approval.to_dict() if self.approval else None,
            "fix_attempts": self.fix_attempts,
            "active_fixer": self.active_fixer.to_dict() if self.active_fixer else None,
            "built_provenance": self.built_provenance.to_dict() if self.built_provenance else None,
            "upstream_snapshot": {
                k: self.upstream_snapshot[k] for k in sorted(self.upstream_snapshot)
            },
            "last_reason": self.last_reason,
        }


@dataclasses.dataclass
class SupervisorState:
    """The full reduced state of a plan (design §5). Deterministically serialisable so
    a snapshot/fingerprint is reproducible (INV-1)."""

    plan_id: str
    plan_state: PlanState = PlanState.RUNNING
    nodes: dict[str, NodeRuntime] = dataclasses.field(default_factory=dict)
    context: dict[str, str] = dataclasses.field(default_factory=dict)  # ContextStore (C15)
    last_seq: int = -1
    snapshot_through_seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_state": self.plan_state.value,
            "nodes": {nid: self.nodes[nid].to_dict() for nid in sorted(self.nodes)},
            "context": {k: self.context[k] for k in sorted(self.context)},
            "last_seq": self.last_seq,
            "snapshot_through_seq": self.snapshot_through_seq,
        }


def state_fingerprint(state: SupervisorState) -> str:
    """A deterministic content hash of the reduced state (design §4.2 snapshot
    ``state_hash``). Two replays of the same log fingerprint identically (INV-1/3)."""
    return hashlib.sha256(canonical_json(state.to_dict()).encode("utf-8")).hexdigest()


def _provenance_fp(provenance: Provenance | None) -> str:
    """A stable fingerprint of a node's build provenance (``""`` if none). Used for
    staleness: a downstream's upstream-provenance snapshot vs the upstream's current
    provenance (states.py reconciliation #2)."""
    if provenance is None:
        return ""
    return hashlib.sha256(canonical_json(provenance.to_dict()).encode("utf-8")).hexdigest()


def is_stale(state: SupervisorState, plan: Plan, node_id: str) -> bool:
    """Whether a ``DONE`` node is stale and needs async rebase+revalidate (states.py
    reconciliation #2 / design §5 "DONE 若 final-oracle RED … 级联标下游 STALE").

    Stale iff a direct upstream dep is no longer ``DONE`` (regressed) OR its current
    provenance differs from the snapshot taken when this node advanced (so an upstream
    that regressed *and re-passed at a new commit* is still caught). Staleness cascades
    level-by-level across ticks, so checking direct deps suffices. A non-DONE node is
    never stale (it has not been built)."""
    node = state.nodes.get(node_id)
    if node is None or node.status is not NodeState.DONE:
        return False
    deps = next((n.deps for n in plan.nodes if n.node_id == node_id), [])
    for dep_id in deps:
        dep = state.nodes.get(dep_id)
        if dep is None or dep.status is not NodeState.DONE:
            return True  # upstream regressed out of DONE
        if _provenance_fp(dep.built_provenance) != node.upstream_snapshot.get(dep_id, ""):
            return True  # upstream re-passed at a different provenance
    return False


def reduce(plan: Plan, events: list[Event]) -> SupervisorState:
    """Fold ``events`` (seq-ordered) onto the static ``plan`` to produce the current
    :class:`SupervisorState`. Pure: no clock / fs / git reads (INV-3).

    Raises :class:`ReductionError` fail-closed if an event drives an illegal
    transition or is inconsistent with the plan/log (corruption is surfaced, never
    silently mis-reduced)."""
    state = SupervisorState(plan_id=plan.plan_id)
    node_ids = {n.node_id for n in plan.nodes}
    for event in events:
        if event.plan_id != plan.plan_id:
            raise ReductionError(
                f"event {event.event_id} plan_id={event.plan_id!r} != plan {plan.plan_id!r}"
            )
        if event.seq != state.last_seq + 1:
            raise ReductionError(
                f"event {event.event_id} seq={event.seq} is non-contiguous "
                f"(last applied {state.last_seq})"
            )
        _apply(state, plan, node_ids, event)
        state.last_seq = event.seq
    return state


# --- per-event reduction -----------------------------------------------------


def _payload(event: Event, contract: type[Contract]) -> Any:
    """Re-instantiate a typed payload from the event's open wire dict (fail-closed).
    S0 already validated it on the Event; this returns the typed view for reduction."""
    return contract.from_dict(event.payload)


def _node(state: SupervisorState, node_ids: set[str], node_id: str) -> NodeRuntime:
    if node_id not in node_ids:
        raise ReductionError(f"event references unknown node {node_id!r}")
    return state.nodes.setdefault(node_id, NodeRuntime(node_id=node_id))


def _transition(node: NodeRuntime, event_type: EventType, to: NodeState) -> None:
    """Apply a node-state transition iff ``(node.status, event_type, to)`` is a legal
    S0 edge (§5); else fail-closed."""
    legal = _LEGAL_EDGES.get((node.status, event_type))
    if legal is None or to not in legal:
        raise ReductionError(
            f"illegal transition for node {node.node_id!r}: "
            f"{node.status.value} --{event_type.value}--> {to.value} is not a frozen "
            "S0 edge (§5)"
        )
    node.status = to


def _require_attempt(node: NodeRuntime, attempt: int, event_type: EventType) -> None:
    """A completion event must reference the node's current attempt (INV-4 fencing).
    A stale attempt should have been dropped by AckInbox fencing; if it reached the
    log it is corruption (fail-closed)."""
    if attempt != node.attempt:
        raise ReductionError(
            f"{event_type.value} for node {node.node_id!r} carries attempt {attempt} "
            f"but the node's current attempt is {node.attempt} (stale/fencing violation)"
        )


def _apply(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    handler = _HANDLERS.get(event.type)
    if handler is None:  # pragma: no cover - map is total over EventType (asserted in tests)
        raise ReductionError(f"no reducer handler for event type {event.type.value}")
    handler(state, plan, node_ids, event)


def _h_plan_created(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    if event.seq != 0:
        raise ReductionError("plan_created must be the genesis event (seq 0)")
    payload = _payload(event, Plan)
    if payload.plan_id != plan.plan_id:
        raise ReductionError(
            f"plan_created plan_id={payload.plan_id!r} != reduce() plan {plan.plan_id!r}"
        )
    # Initialise every node PENDING (INITIAL_NODE_STATE). Nodes are materialised lazily
    # elsewhere, but plan_created seeds them all so an untouched node is visibly PENDING.
    for n in plan.nodes:
        state.nodes.setdefault(n.node_id, NodeRuntime(node_id=n.node_id))


def _h_node_dispatched(
    state: SupervisorState, plan: Plan, node_ids: set[str], event: Event
) -> None:
    payload: NodeAttempt = _payload(event, NodeAttempt)
    node = _node(state, node_ids, payload.node)
    _transition(node, EventType.NODE_DISPATCHED, NodeState.DISPATCHED)
    node.attempt = payload.attempt
    node._reset_attempt_fields()
    node.inflight_since_ts = event.ts  # worker phase start (Sweeper anchor)


def _h_worker_done(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    from .actions import Ack

    payload: Ack = _payload(event, Ack)
    node = _node(state, node_ids, payload.node)
    _require_attempt(node, payload.attempt, EventType.WORKER_DONE)
    _transition(node, EventType.WORKER_DONE, NodeState.AUDITING)
    node.inflight_since_ts = event.ts  # audit phase start (Sweeper anchor)


def _h_audit_started(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: NodeAttempt = _payload(event, NodeAttempt)
    node = _node(state, node_ids, payload.node)
    _require_attempt(node, payload.attempt, EventType.AUDIT_STARTED)
    if node.status is not NodeState.AUDITING:
        raise ReductionError(
            f"audit_started for node {node.node_id!r} but it is {node.status.value}, not AUDITING"
        )
    node.audit_started = True  # policy: only START_AUDIT once per attempt
    node.inflight_since_ts = event.ts  # informational; refresh the audit Sweeper anchor


def _h_audit_done(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: AuditDone = _payload(event, AuditDone)
    node = _node(state, node_ids, payload.node)
    _require_attempt(node, payload.attempt, EventType.AUDIT_DONE)
    _transition(node, EventType.AUDIT_DONE, NodeState.EVALUATING)
    node.verdict = payload.verdict
    node.inflight_since_ts = None  # settled into EVALUATING — nothing in flight


def _h_oracle_checked(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: OracleChecked = _payload(event, OracleChecked)
    node = _node(state, node_ids, payload.node)
    # Informational (no state change): records the oracle outcome the policy reads in
    # EVALUATING / BLOCKED_BY_FIX. An UNKNOWN oracle never becomes oracle_checked (S1
    # refuses to project it — it escalates), so this is always a GREEN/RED result.
    if node.status not in (NodeState.EVALUATING, NodeState.BLOCKED_BY_FIX):
        raise ReductionError(
            f"oracle_checked for node {node.node_id!r} but it is {node.status.value} "
            "(oracle is only checked while EVALUATING or BLOCKED_BY_FIX)"
        )
    node.oracle = payload


def _h_node_advanced(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: NodeAttempt = _payload(event, NodeAttempt)
    node = _node(state, node_ids, payload.node)
    _require_attempt(node, payload.attempt, EventType.NODE_ADVANCED)
    _transition(node, EventType.NODE_ADVANCED, NodeState.DONE)
    node.built_provenance = event.provenance  # provenance the node passed at (staleness §5 #2)
    node.active_fixer = None
    node.inflight_since_ts = None
    # Snapshot each upstream dep's provenance at advance time so this DONE node can
    # later be detected stale if an upstream regresses or re-passes at a new commit
    # (states.py reconciliation #2). Deps are DONE here (the node couldn't have run
    # otherwise), so each has a settled built_provenance.
    deps = next((n.deps for n in plan.nodes if n.node_id == node.node_id), [])
    node.upstream_snapshot = {d: _provenance_fp(state.nodes[d].built_provenance) for d in deps}


def _h_fixer_spawned(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: Fixer = _payload(event, Fixer)
    node = _node(state, node_ids, payload.parent_node)
    _transition(node, EventType.FIXER_SPAWNED, NodeState.BLOCKED_BY_FIX)
    node.fix_attempts += 1
    node.oracle = None  # the pre-fix oracle result is stale; re-check after the fixer
    node.active_fixer = FixerRuntime(
        fixer_id=payload.fixer_id,
        parent_node=payload.parent_node,
        attempt=payload.attempt,
        state=payload.state,
    )
    node.inflight_since_ts = event.ts  # fixer in flight


def _h_fixer_done(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: FixerDone = _payload(event, FixerDone)
    node = _node(state, node_ids, payload.parent_node)
    # Informational: records the fixer's terminal state; the parent stays
    # BLOCKED_BY_FIX until the policy re-runs the oracle and advances/blocks it.
    if node.active_fixer is None or node.active_fixer.fixer_id != payload.fixer_id:
        raise ReductionError(
            f"fixer_done for {payload.fixer_id!r} but node {node.node_id!r} has no such "
            "active fixer (deterministic rebuild violated)"
        )
    node.active_fixer.state = payload.state
    node.inflight_since_ts = None  # fixer settled; parent awaits a re-eval decision


def _h_node_blocked(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: NodeReason = _payload(event, NodeReason)
    node = _node(state, node_ids, payload.node)
    _transition(node, EventType.NODE_BLOCKED, NodeState.BLOCKED)
    node.last_reason = payload.reason
    node.inflight_since_ts = None


def _h_node_cancelled(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: NodeReason = _payload(event, NodeReason)
    node = _node(state, node_ids, payload.node)
    if node.status not in ABORTABLE_NODE_STATES:
        raise ReductionError(
            f"node_cancelled for node {node.node_id!r} in non-abortable state {node.status.value}"
        )
    _transition(node, EventType.NODE_CANCELLED, NodeState.CANCELLED)
    node.last_reason = payload.reason
    node.inflight_since_ts = None


def _h_worker_timeout(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: NodeAttempt = _payload(event, NodeAttempt)
    node = _node(state, node_ids, payload.node)
    _require_attempt(node, payload.attempt, EventType.WORKER_TIMEOUT)
    _transition(node, EventType.WORKER_TIMEOUT, NodeState.TIMED_OUT)
    node.inflight_since_ts = None


def _h_context_patched(
    state: SupervisorState, plan: Plan, node_ids: set[str], event: Event
) -> None:
    from .payloads import ContextPatchOpKind

    payload: ContextPatch = _payload(event, ContextPatch)
    # Replayable KV merge into the ContextStore (design §3 C15) — never an in-place
    # file edit (that would break INV-3 replay).
    for op in payload.patches:
        if op.op is ContextPatchOpKind.UPSERT:
            if op.value is None:  # S0 ContextPatchOp.validate guarantees this; defend anyway
                raise ReductionError(f"context_patched upsert for {op.key!r} has no value")
            state.context[op.key] = op.value
        else:  # DELETE
            state.context.pop(op.key, None)


def _h_escalated(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: NodeReason = _payload(event, NodeReason)
    node = _node(state, node_ids, payload.node)
    node.last_reason = payload.reason  # informational; the following node_blocked moves state


def _h_approval_requested(
    state: SupervisorState, plan: Plan, node_ids: set[str], event: Event
) -> None:
    payload: NodeReason = _payload(event, NodeReason)
    node = _node(state, node_ids, payload.node)
    _transition(node, EventType.APPROVAL_REQUESTED, NodeState.AWAIT_APPROVAL)
    node.last_reason = payload.reason


def _h_approval_granted(
    state: SupervisorState, plan: Plan, node_ids: set[str], event: Event
) -> None:
    payload: Approval = _payload(event, Approval)
    node = _node(state, node_ids, payload.node)
    # Informational: records the approval; the subsequent node_dispatched moves
    # AWAIT_APPROVAL -> DISPATCHED (the gate the policy checks).
    node.approval = payload


def _h_irreversible_executed(
    state: SupervisorState, plan: Plan, node_ids: set[str], event: Event
) -> None:
    from .payloads import IrreversibleExecuted

    payload: IrreversibleExecuted = _payload(event, IrreversibleExecuted)
    _node(state, node_ids, payload.node)  # validate node exists; SideEffectRegistry is S7


def _h_rolled_back(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: RollbackRecord = _payload(event, RollbackRecord)
    node = _node(state, node_ids, payload.to_node)
    _transition(node, EventType.ROLLED_BACK, NodeState.PENDING)  # GAP-2: redo from base
    node._reset_attempt_fields()
    node.approval = None
    node.inflight_since_ts = None
    # attempt is left as-is; the next node_dispatched assigns the redo attempt.


def _h_dlq_entered(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    from .actions import DLQEntry

    payload: DLQEntry = _payload(event, DLQEntry)
    node = _node(state, node_ids, payload.node)
    node.last_reason = payload.reason  # informational; node is already BLOCKED


def _h_global_paused(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    _payload(event, GlobalPaused)  # validate shape
    state.plan_state = PlanState.GLOBAL_PAUSED


def _h_global_resumed(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    _payload(event, GlobalResumed)
    state.plan_state = PlanState.RUNNING


def _h_owner_override(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: OwnerOverride = _payload(event, OwnerOverride)
    node = _node(state, node_ids, payload.node)
    target = NodeState(payload.target_state.value)
    _transition(node, EventType.OWNER_OVERRIDE, target)  # BLOCKED -> RecoveryTarget (GAP-3)
    node.last_reason = payload.reason
    if target is NodeState.PENDING:
        node._reset_attempt_fields()
    # R2 codex #5 (safety): a re-gating override must drop any stale approval, else the
    # policy would dispatch a previously-approved irreversible node without fresh owner
    # consent (PENDING re-does from scratch; AWAIT_APPROVAL re-gates) — mirrors rollback,
    # which already clears approval. A force-run override → DISPATCHED keeps approval
    # (the override IS the owner act, and DISPATCHED is not re-gated).
    if target in (NodeState.PENDING, NodeState.AWAIT_APPROVAL):
        node.approval = None


def _h_snapshot_taken(state: SupervisorState, plan: Plan, node_ids: set[str], event: Event) -> None:
    payload: SnapshotTaken = _payload(event, SnapshotTaken)
    if payload.through_seq > event.seq:
        raise ReductionError(
            f"snapshot_taken through_seq={payload.through_seq} is ahead of its own seq={event.seq}"
        )
    state.snapshot_through_seq = payload.through_seq


_HANDLERS = {
    EventType.PLAN_CREATED: _h_plan_created,
    EventType.PLAN_AMENDED: lambda s, p, n, e: None,  # plan mutation is S7+; record-only here
    EventType.NODE_DISPATCHED: _h_node_dispatched,
    EventType.WORKER_DONE: _h_worker_done,
    EventType.WORKER_TIMEOUT: _h_worker_timeout,
    EventType.AUDIT_STARTED: _h_audit_started,
    EventType.AUDIT_DONE: _h_audit_done,
    EventType.ORACLE_CHECKED: _h_oracle_checked,
    EventType.NODE_ADVANCED: _h_node_advanced,
    EventType.FIXER_SPAWNED: _h_fixer_spawned,
    EventType.FIXER_DONE: _h_fixer_done,
    EventType.CONTEXT_PATCHED: _h_context_patched,
    EventType.NODE_BLOCKED: _h_node_blocked,
    EventType.NODE_CANCELLED: _h_node_cancelled,
    EventType.ESCALATED: _h_escalated,
    EventType.APPROVAL_REQUESTED: _h_approval_requested,
    EventType.APPROVAL_GRANTED: _h_approval_granted,
    EventType.IRREVERSIBLE_EXECUTED: _h_irreversible_executed,
    EventType.ROLLED_BACK: _h_rolled_back,
    EventType.DLQ_ENTERED: _h_dlq_entered,
    EventType.GLOBAL_PAUSED: _h_global_paused,
    EventType.GLOBAL_RESUMED: _h_global_resumed,
    EventType.OWNER_OVERRIDE: _h_owner_override,
    EventType.SNAPSHOT_TAKEN: _h_snapshot_taken,
}
