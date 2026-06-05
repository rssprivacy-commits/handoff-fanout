"""S0 — structured event payloads (design §4.1 / §3 C15 / §6.3 / §4.2).

Every event in the §4.2 set carries a **frozen, typed** payload. The envelope's
``payload`` stays an open wire ``dict`` so the *transport* is uniform, but each
event's payload *shape* is frozen here (or reuses an existing contract) so S1+
cannot each invent an incompatible format — the drift S0 exists to kill (S0-fix
audit P0-1: 禁止 ``None`` 当「以后再说」, both brains). There is **no** open/None
payload anymore; an event with no extra data still gets an explicit typed shape.

Design-named structured payloads:

* :class:`PlanAmendment` — ``plan_amended`` carries diff / reason / approver / hash
  (design §4.1: "改 plan = plan_amended 事件（diff/理由/批准人/hash）").
* :class:`ContextPatch` — ``context_patched`` is a strong-typed KV patch merged via
  the event log (design §3 C15 "强类型KV / ContextPatch 事件经 reducer 合并"),
  NOT an in-place file edit (that would break INV-3 replay).
* :class:`RollbackRecord` — ``rolled_back`` records the joint Git+DB rollback target
  (design §6.3). The S0-fix freezes BOTH its payload AND its node-state effect
  (the rolled-back node → ``PENDING``; see ``states.py``) — GAP-2 is now closed.

The remaining node/plan event payloads (formerly mapped to ``None``) are frozen
below: :class:`NodeAttempt`, :class:`NodeReason`, :class:`AuditDone`,
:class:`OracleChecked`, :class:`FixerDone`, :class:`IrreversibleExecuted`,
:class:`GlobalPaused`, :class:`GlobalResumed`, :class:`OwnerOverride`,
:class:`SnapshotTaken`. Events that reuse an existing contract as their payload
(``fixer_spawned``→Fixer, ``approval_granted``→Approval, ``dlq_entered``→DLQEntry,
``worker_done``→Ack, ``plan_created``→Plan) are wired in ``event_payloads.py``.
"""

from __future__ import annotations

import dataclasses
import enum

from ._base import Contract, SchemaError
from .actions import SideEffect
from .fixer import FixerState
from .oracle import OracleScope
from .verdict import Verdict


@dataclasses.dataclass
class PlanAmendment(Contract):
    """Payload of a ``plan_amended`` event (design §4.1). A plan change is never
    silent: it carries the diff, the reason, the approver, and the hash it was
    approved against."""

    plan_id: str
    diff: str
    reason: str
    approver: str
    bound_hash: str

    def validate(self) -> None:
        for name in ("plan_id", "diff", "reason", "approver", "bound_hash"):
            if not getattr(self, name):
                raise SchemaError(f"PlanAmendment.{name} required")


class ContextPatchOpKind(enum.StrEnum):
    UPSERT = "upsert"
    DELETE = "delete"


@dataclasses.dataclass
class ContextPatchOp(Contract):
    """One strong-typed KV operation in a context patch (design §3 C15)."""

    op: ContextPatchOpKind
    key: str
    value: str | None = None

    def validate(self) -> None:
        if not self.key:
            raise SchemaError("ContextPatchOp.key required")
        if self.op is ContextPatchOpKind.UPSERT and self.value is None:
            raise SchemaError("ContextPatchOp upsert requires a value")
        if self.op is ContextPatchOpKind.DELETE and self.value is not None:
            raise SchemaError("ContextPatchOp delete must not carry a value")


@dataclasses.dataclass
class ContextPatch(Contract):
    """Payload of a ``context_patched`` event — a set of KV ops merged into the
    ContextStore by the reducer (design §3 C15 / §4.2). Cross-bar shared knowledge
    is patched through events (replayable), never edited in place (INV-3)."""

    patches: list[ContextPatchOp] = dataclasses.field(default_factory=list)

    def validate(self) -> None:
        if not self.patches:
            raise SchemaError("ContextPatch.patches must be non-empty")
        keys = [p.key for p in self.patches]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise SchemaError(f"ContextPatch touches the same key twice (ambiguous): {dupes}")


@dataclasses.dataclass
class RollbackRecord(Contract):
    """Payload of a ``rolled_back`` event (design §6.3 joint Git+DB rollback).

    S0-fix (GAP-2 closed): the node-state *effect* of a rollback is now frozen in
    ``states.py`` — a rolled-back node returns to ``PENDING`` (re-do from base);
    downstream nodes become derived-stale (reconciliation #2) and re-validate via
    the existing ``DONE → BLOCKED_BY_FIX`` edge. This contract freezes the *record*
    of what was rolled back (target node + commit + DB/side-effect compensation)."""

    to_node: str
    to_commit: str
    db_snapshot_restored: bool = False
    side_effects_compensated: list[str] = dataclasses.field(default_factory=list)

    def validate(self) -> None:
        if not self.to_node:
            raise SchemaError("RollbackRecord.to_node required")
        if not self.to_commit:
            raise SchemaError("RollbackRecord.to_commit required")
        dupes = sorted(
            {s for s in self.side_effects_compensated if self.side_effects_compensated.count(s) > 1}
        )
        if dupes:
            raise SchemaError(f"RollbackRecord.side_effects_compensated has duplicates: {dupes}")


# --- formerly-open ("None") event payloads — now frozen (S0-fix P0-1) ---------


@dataclasses.dataclass
class NodeAttempt(Contract):
    """Payload for node-lifecycle events that reference a node + its attempt:
    ``node_dispatched`` / ``worker_timeout`` / ``audit_started`` / ``node_advanced``
    (S0-fix P0-1). ``node`` is carried explicitly because the §4.2 envelope has no
    ``node`` field, so the reducer needs it to rebuild state deterministically
    (INV-3)."""

    node: str
    attempt: int = 1

    def validate(self) -> None:
        if not self.node:
            raise SchemaError("NodeAttempt.node required")
        if self.attempt < 1:
            raise SchemaError("NodeAttempt.attempt must be >= 1")


@dataclasses.dataclass
class NodeReason(Contract):
    """Payload for node events that carry a human-readable reason: ``node_blocked``
    / ``node_cancelled`` / ``escalated`` / ``approval_requested`` (S0-fix P0-1).
    The ``reason`` is owner-facing (INV-10) — a non-technical owner reads it on the
    status board."""

    node: str
    reason: str

    def validate(self) -> None:
        if not self.node:
            raise SchemaError("NodeReason.node required")
        if not self.reason:
            raise SchemaError("NodeReason.reason required (owner-facing, INV-10)")


@dataclasses.dataclass
class AuditDone(Contract):
    """Payload of an ``audit_done`` event (S0-fix P0-1 / gemini #4). Raw findings
    landed AND the supervisor computed the deterministic machine :class:`Verdict`
    (INV-2). Baking the verdict into the event persists it in the log so the reducer
    never re-reads ``findings.json`` (INV-3) and the verdict is always auditable."""

    node: str
    attempt: int
    verdict: Verdict

    def validate(self) -> None:
        if not self.node:
            raise SchemaError("AuditDone.node required")
        if self.attempt < 1:
            raise SchemaError("AuditDone.attempt must be >= 1")


@dataclasses.dataclass
class OracleChecked(Contract):
    """Payload of an ``oracle_checked`` event (S0-fix P0-1). Records the outcome of
    running the oracle for one scope so the EVALUATING→DONE / →BLOCKED_BY_FIX
    decision is replayable from the log, not recomputed (INV-3)."""

    node: str
    scope: OracleScope
    passed: bool
    failed_criteria: list[str] = dataclasses.field(default_factory=list)

    def validate(self) -> None:
        if not self.node:
            raise SchemaError("OracleChecked.node required")
        dupes = sorted({c for c in self.failed_criteria if self.failed_criteria.count(c) > 1})
        if dupes:
            raise SchemaError(f"OracleChecked.failed_criteria has duplicates: {dupes}")
        if not self.passed and not self.failed_criteria:
            raise SchemaError(
                "OracleChecked.passed=false requires at least one failed criterion "
                "(an oracle RED must name what failed)"
            )
        if self.passed and self.failed_criteria:
            raise SchemaError("OracleChecked.passed=true cannot list failed criteria")


@dataclasses.dataclass
class FixerDone(Contract):
    """Payload of a ``fixer_done`` event (S0-fix P0-1 / gemini #4). Records the
    terminal state of a Fixer sub-workflow so the reducer can deterministically
    rebuild it (design §4.6) and the parent node knows whether to re-run its oracle
    (DONE) or block (FAILED)."""

    fixer_id: str
    parent_node: str
    attempt: int
    state: FixerState

    def validate(self) -> None:
        if not self.fixer_id:
            raise SchemaError("FixerDone.fixer_id required")
        if not self.parent_node:
            raise SchemaError("FixerDone.parent_node required")
        if self.attempt < 1:
            raise SchemaError("FixerDone.attempt must be >= 1")
        if self.state not in (FixerState.DONE, FixerState.FAILED):
            raise SchemaError(
                "FixerDone.state must be a terminal Fixer state (DONE or FAILED), "
                f"got {self.state.value}"
            )


@dataclasses.dataclass
class IrreversibleExecuted(Contract):
    """Payload of an ``irreversible_executed`` event (S0-fix P0-1). The worker
    records the irreversible :class:`SideEffect` it performed during its DISPATCHED
    run, feeding the SideEffectRegistry (design §5 reconciliation #3 / INV-6).
    Informational: it does not move the node between states."""

    node: str
    side_effect: SideEffect

    def validate(self) -> None:
        if not self.node:
            raise SchemaError("IrreversibleExecuted.node required")


@dataclasses.dataclass
class GlobalPaused(Contract):
    """Payload of a ``global_paused`` event (S0-fix P0-1 / P0-2). Carries WHY the
    plan paused (owner request or DiskGuard) and WHO/what triggered it, so the pause
    is replayable (INV-3) and owner-auditable (INV-10)."""

    reason: str
    actor: str

    def validate(self) -> None:
        if not self.reason:
            raise SchemaError("GlobalPaused.reason required (e.g. 'owner' / 'disk')")
        if not self.actor:
            raise SchemaError("GlobalPaused.actor required")


@dataclasses.dataclass
class GlobalResumed(Contract):
    """Payload of a ``global_resumed`` event (S0-fix P0-2 / GAP-1 closed). Without a
    resume event a paused plan, replayed from disk, would deadlock in GLOBAL_PAUSED
    forever (violating INV-3). This is the backing event for the
    ``GLOBAL_PAUSED → RUNNING`` transition."""

    actor: str

    def validate(self) -> None:
        if not self.actor:
            raise SchemaError("GlobalResumed.actor required")


class RecoveryTarget(enum.StrEnum):
    """The legal destinations of an owner recovery of a ``BLOCKED`` (DLQ'd) node
    (S0-fix P0-4 / GAP-3 closed). A closed set — an ``owner_override`` can only
    revive a blocked node into one of these. Values MUST match the corresponding
    ``NodeState`` names (asserted by ``states.validate_state_machine_closure``)."""

    PENDING = "PENDING"  # redo from scratch
    DISPATCHED = "DISPATCHED"  # force-run (owner judged it safe)
    AWAIT_APPROVAL = "AWAIT_APPROVAL"  # re-gate behind approval


@dataclasses.dataclass
class OwnerOverride(Contract):
    """Payload of an ``owner_override`` event (S0-fix P0-4 / GAP-3 closed). A human
    owner rescues a ``BLOCKED`` (DLQ'd) node back to an active state via the dumb
    CLI (C18, design §10 ``resume``/``rollback-to`` / INV-10). ``bound_hash`` binds
    the override to the exact blocked evidence so one approval can never be silently
    replayed onto a different state (anti-replay, like :class:`Approval`)."""

    node: str
    target_state: RecoveryTarget
    actor: str
    reason: str
    bound_hash: str

    def validate(self) -> None:
        if not self.node:
            raise SchemaError("OwnerOverride.node required")
        if not self.actor:
            raise SchemaError("OwnerOverride.actor required")
        if not self.reason:
            raise SchemaError("OwnerOverride.reason required (owner-facing, INV-10)")
        if not self.bound_hash:
            raise SchemaError("OwnerOverride.bound_hash required (INV-4 anti-replay)")


@dataclasses.dataclass
class SnapshotTaken(Contract):
    """Payload of a ``snapshot_taken`` event (S0-fix P0-1). Marks an event-log
    compaction point: the reducer state through ``through_seq`` is snapshotted and
    the older events may be truncated (design §4.2 compaction / §5c DiskGuard).
    ``state_hash`` lets a reader verify the snapshot matches the replayed state."""

    through_seq: int
    state_hash: str

    def validate(self) -> None:
        if self.through_seq < 0:
            raise SchemaError("SnapshotTaken.through_seq must be >= 0")
        if not self.state_hash:
            raise SchemaError("SnapshotTaken.state_hash required")
