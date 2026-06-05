"""S0 — the frozen ``EventType → payload contract`` map (design §4.2).

The envelope's ``payload`` is an open wire ``dict`` so the *transport* is uniform,
but each event's payload *shape* is frozen here so S1+ cannot each invent a format
(S0-fix P0-1, both brains). **Every** EventType maps to a concrete
:class:`Contract` — there is no ``None`` / open payload anymore (the original S0
left 14 events as ``None`` "留给以后那片", which was the RED: format drift not
actually killed). A new event can never be added without a deliberate payload
decision (enforced by :func:`assert_payload_map_total`).

S0 freezes the *contract* + the *map* + a validator, and (S0-fix P0-3) the
validator is now **enforced on every** ``Event`` construction via
``Event.validate()`` — fail-closed at the single-writer entry, not optional.
"""

from __future__ import annotations

from collections.abc import Mapping

from ._base import Contract, SchemaError
from .actions import Ack, Approval, DLQEntry
from .events import Event, EventType
from .fixer import Fixer
from .payloads import (
    AuditDone,
    ContextPatch,
    FixerDone,
    GlobalPaused,
    GlobalResumed,
    IrreversibleExecuted,
    NodeAttempt,
    NodeReason,
    OracleChecked,
    OwnerOverride,
    PlanAmendment,
    RollbackRecord,
    SnapshotTaken,
)
from .plan import Plan

#: EventType → the concrete contract its payload MUST satisfy. EVERY EventType has
#: an entry (enforced by :func:`assert_payload_map_total`) and every entry is a
#: real contract (no open/None payloads — S0-fix P0-1), so a new event can never be
#: added, and an existing event's payload can never drift, without a deliberate
#: contract change.
EVENT_PAYLOAD_CONTRACT: dict[EventType, type[Contract]] = {
    EventType.PLAN_CREATED: Plan,
    EventType.PLAN_AMENDED: PlanAmendment,
    EventType.NODE_DISPATCHED: NodeAttempt,
    EventType.WORKER_DONE: Ack,
    EventType.WORKER_TIMEOUT: NodeAttempt,
    EventType.AUDIT_STARTED: NodeAttempt,
    EventType.AUDIT_DONE: AuditDone,
    EventType.ORACLE_CHECKED: OracleChecked,
    EventType.NODE_ADVANCED: NodeAttempt,
    EventType.FIXER_SPAWNED: Fixer,
    EventType.FIXER_DONE: FixerDone,
    EventType.CONTEXT_PATCHED: ContextPatch,
    EventType.NODE_BLOCKED: NodeReason,
    EventType.NODE_CANCELLED: NodeReason,
    EventType.ESCALATED: NodeReason,
    EventType.APPROVAL_REQUESTED: NodeReason,
    EventType.APPROVAL_GRANTED: Approval,
    EventType.IRREVERSIBLE_EXECUTED: IrreversibleExecuted,
    EventType.ROLLED_BACK: RollbackRecord,
    EventType.DLQ_ENTERED: DLQEntry,
    EventType.GLOBAL_PAUSED: GlobalPaused,
    EventType.GLOBAL_RESUMED: GlobalResumed,
    EventType.OWNER_OVERRIDE: OwnerOverride,
    EventType.SNAPSHOT_TAKEN: SnapshotTaken,
}


def assert_payload_map_total() -> None:
    """Every EventType must map to a concrete payload contract. Raises
    :class:`SchemaError` if the map drifts from the event set."""
    missing = set(EventType) - set(EVENT_PAYLOAD_CONTRACT)
    extra = set(EVENT_PAYLOAD_CONTRACT) - set(EventType)
    if missing or extra:
        raise SchemaError(
            "EVENT_PAYLOAD_CONTRACT is not total: "
            f"missing={sorted(e.value for e in missing)} "
            f"extra={sorted(getattr(e, 'value', e) for e in extra)}"
        )


def coerce_payload(event_type: EventType, payload: Mapping) -> Contract:
    """Coerce a raw payload dict into its frozen contract (fail-closed). Raises
    :class:`SchemaError` if malformed or not a mapping."""
    contract = EVENT_PAYLOAD_CONTRACT[event_type]
    if not isinstance(payload, Mapping):
        raise SchemaError(f"{event_type.value} payload must be a mapping for {contract.__name__}")
    return contract.from_dict(payload)


def validate_event_payload(event: Event) -> None:
    """Validate ``event.payload`` against its frozen contract (fail-closed). Called
    automatically from :meth:`Event.validate` on every construction (S0-fix P0-3),
    so a malformed payload can never become a legal :class:`Event`."""
    coerce_payload(event.type, event.payload)
