"""S0 — structured event payloads (design §4.1 / §3 C15 / §6.3).

Several events in the §4.2 set carry *structured* data the design specifies but
the bare envelope leaves as an open ``dict`` — exactly where S1+ would each invent
an incompatible format (the drift S0 exists to kill, R2 codex C-P1-1 / gemini
G-P1-2). This module freezes the payload contracts whose shape the design names
explicitly:

* :class:`PlanAmendment` — ``plan_amended`` carries diff / reason / approver / hash
  (design §4.1: "改 plan = plan_amended 事件（diff/理由/批准人/hash）").
* :class:`ContextPatch` — ``context_patched`` is a strong-typed KV patch merged via
  the event log (design §3 C15 "强类型KV / ContextPatch 事件经 reducer 合并"),
  NOT an in-place file edit (that would break INV-3 replay).
* :class:`RollbackRecord` — ``rolled_back`` records the joint Git+DB rollback target
  (design §6.3). Its *payload* is frozen here; its node-state *effect* is GAP-2
  (deferred to S7) — see :data:`~handoff_fanout.supervisor.states.KNOWN_EVENT_GAPS`.

Events that reuse an existing contract as their payload (``fixer_spawned``→Fixer,
``approval_granted``→Approval, ``dlq_entered``→DLQEntry, ``worker_done``→Ack,
``plan_created``→Plan) are wired in ``event_payloads.py``; this module only holds
the *new* payload shapes.
"""

from __future__ import annotations

import dataclasses
import enum

from ._base import Contract, SchemaError


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

    NOTE: the node-state *effect* of a rollback is GAP-2 (deferred to S7); this
    contract only freezes the *record* of what was rolled back."""

    to_node: str
    to_commit: str
    db_snapshot_restored: bool = False
    side_effects_compensated: list[str] = dataclasses.field(default_factory=list)

    def validate(self) -> None:
        if not self.to_node:
            raise SchemaError("RollbackRecord.to_node required")
        if not self.to_commit:
            raise SchemaError("RollbackRecord.to_commit required")
