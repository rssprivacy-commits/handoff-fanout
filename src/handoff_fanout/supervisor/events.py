"""S0 — EventLog envelope + the full event type set (design §4.2).

The event log (``events.jsonl``) is the single source of truth for plan state:
``State = reduce(plan, events)``. It is **append-only with a single writer (the
supervisor only)** — workers/auditors/fixers never write events; they drop a
signal into the AckInbox and the supervisor translates that into an event
(design §4.2, INV-3). Every event carries an envelope with a monotonic ``seq`` and
an ``expected_prev_seq`` for compare-and-swap (CAS) append, plus a ``dedupe_key``
for at-least-once idempotency (INV-4).

This module only *defines* the envelope + the closed set of event types. It does
NOT reduce events into state (that is the Reducer, slice S3).
"""

from __future__ import annotations

import dataclasses
import enum

from ._base import SCHEMA_VERSION, Contract, SchemaError

#: The only legal value of :attr:`Event.writer` — the event log has exactly one
#: writer (INV-3). Worker/audit/fixer completion signals go to the AckInbox
#: (:class:`~handoff_fanout.supervisor.actions.Ack`), never directly to events.
SUPERVISOR_WRITER = "supervisor"


class EventType(enum.StrEnum):
    """The closed set of event types (design §4.2 "事件全集" — exactly 22).

    No slice may invent an event type outside this set; a new orchestration fact
    is a new member here (a schema change, bumping ``SCHEMA_VERSION``), never an
    ad-hoc string.
    """

    PLAN_CREATED = "plan_created"
    PLAN_AMENDED = "plan_amended"
    NODE_DISPATCHED = "node_dispatched"
    WORKER_DONE = "worker_done"
    WORKER_TIMEOUT = "worker_timeout"
    AUDIT_STARTED = "audit_started"
    AUDIT_DONE = "audit_done"
    ORACLE_CHECKED = "oracle_checked"
    NODE_ADVANCED = "node_advanced"
    FIXER_SPAWNED = "fixer_spawned"
    FIXER_DONE = "fixer_done"
    CONTEXT_PATCHED = "context_patched"
    NODE_BLOCKED = "node_blocked"
    NODE_CANCELLED = "node_cancelled"
    ESCALATED = "escalated"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    IRREVERSIBLE_EXECUTED = "irreversible_executed"
    ROLLED_BACK = "rolled_back"
    DLQ_ENTERED = "dlq_entered"
    GLOBAL_PAUSED = "global_paused"
    SNAPSHOT_TAKEN = "snapshot_taken"


@dataclasses.dataclass
class Provenance(Contract):
    """Artefact provenance — binds an event to the exact code/env/model that
    produced it (design §4.2 envelope ``provenance``; reused from v5.4 + the
    dual-brain-everywhere EPIC). All optional: bookkeeping events (snapshot) need
    none; work/audit events carry the binding hashes used for anti-replay.
    """

    commit: str | None = None
    staged_diff_hash: str | None = None
    tree_oid: str | None = None
    env_hash: str | None = None
    models: list[str] = dataclasses.field(default_factory=list)
    tool_ver: str | None = None


@dataclasses.dataclass
class Event(Contract):
    """A single append-only event log entry (design §4.2 envelope).

    Contiguity contract (single-writer CAS): ``seq == expected_prev_seq + 1``.
    The genesis event has ``seq == 0`` and ``expected_prev_seq == -1`` (expect an
    empty log). The supervisor refuses to append unless the on-disk tail seq
    equals this event's ``expected_prev_seq`` — that's the CAS that makes
    concurrent double-append impossible (design §9 "并发双写 → flock+CAS").
    """

    GENESIS_PREV_SEQ = -1  # ClassVar sentinel — not a dataclass field

    schema_version: int
    event_id: str
    seq: int
    ts: str
    plan_id: str
    type: EventType
    expected_prev_seq: int
    dedupe_key: str
    writer: str = SUPERVISOR_WRITER
    run_id: str | None = None
    attempt_id: str | None = None
    payload: dict = dataclasses.field(default_factory=dict)
    provenance: Provenance | None = None

    def validate(self) -> None:
        if self.writer != SUPERVISOR_WRITER:
            raise SchemaError(
                f"Event.writer must be {SUPERVISOR_WRITER!r} (INV-3 single writer), "
                f"got {self.writer!r}"
            )
        if not 1 <= self.schema_version <= SCHEMA_VERSION:
            raise SchemaError(
                f"Event.schema_version must be in 1..{SCHEMA_VERSION} "
                f"(fail-closed: refuse an unknown future shape), got "
                f"{self.schema_version}"
            )
        if not self.event_id:
            raise SchemaError("Event.event_id required")
        if not self.plan_id:
            raise SchemaError("Event.plan_id required")
        if self.seq < 0:
            raise SchemaError("Event.seq must be >= 0")
        if self.expected_prev_seq < self.GENESIS_PREV_SEQ:
            raise SchemaError(
                f"Event.expected_prev_seq must be >= {self.GENESIS_PREV_SEQ} "
                "(-1 = genesis / expect-empty-log)"
            )
        if self.seq != self.expected_prev_seq + 1:
            raise SchemaError(
                "Event CAS contiguity violated: seq must equal expected_prev_seq+1 "
                f"(seq={self.seq}, expected_prev_seq={self.expected_prev_seq})"
            )
        if not self.dedupe_key:
            raise SchemaError("Event.dedupe_key required (INV-4 idempotency)")
