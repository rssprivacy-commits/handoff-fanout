"""S0 — the frozen ``EventType → payload contract`` map (design §4.2).

The envelope's ``payload`` is an open ``dict`` so the *transport* is uniform, but
each event's payload *shape* is frozen here so S1+ cannot each invent a format
(R2 codex C-P1-1 / gemini G-P1-2). An event whose type maps to a contract MUST
carry a payload that validates as that contract; an event that maps to ``None``
has an intentionally open payload (its shape is not specified by §4 and is left
to the slice that first emits it — surfaced, not invented).

S0 freezes the *contract* + the *map* + a validator. Where the validator is
*enforced* (at ``Event`` construction vs. at the single-writer append in S1) is an
enforcement-location detail; the anti-drift guarantee is the frozen map itself.
"""

from __future__ import annotations

from collections.abc import Mapping

from ._base import Contract, SchemaError
from .actions import Ack, Approval, DLQEntry
from .events import Event, EventType
from .fixer import Fixer
from .payloads import ContextPatch, PlanAmendment, RollbackRecord
from .plan import Plan

#: EventType → the contract its payload must satisfy, or ``None`` for an
#: intentionally-open payload. EVERY EventType has an entry (enforced by
#: :func:`assert_payload_map_total`), so a new event can never be added without a
#: deliberate payload decision.
EVENT_PAYLOAD_CONTRACT: dict[EventType, type[Contract] | None] = {
    EventType.PLAN_CREATED: Plan,
    EventType.PLAN_AMENDED: PlanAmendment,
    EventType.NODE_DISPATCHED: None,
    EventType.WORKER_DONE: Ack,
    EventType.WORKER_TIMEOUT: None,
    EventType.AUDIT_STARTED: None,
    EventType.AUDIT_DONE: None,
    EventType.ORACLE_CHECKED: None,
    EventType.NODE_ADVANCED: None,
    EventType.FIXER_SPAWNED: Fixer,
    EventType.FIXER_DONE: None,
    EventType.CONTEXT_PATCHED: ContextPatch,
    EventType.NODE_BLOCKED: None,
    EventType.NODE_CANCELLED: None,
    EventType.ESCALATED: None,
    EventType.APPROVAL_REQUESTED: None,
    EventType.APPROVAL_GRANTED: Approval,
    EventType.IRREVERSIBLE_EXECUTED: None,
    EventType.ROLLED_BACK: RollbackRecord,
    EventType.DLQ_ENTERED: DLQEntry,
    EventType.GLOBAL_PAUSED: None,
    EventType.SNAPSHOT_TAKEN: None,
}


def assert_payload_map_total() -> None:
    """Every EventType must have a payload decision (contract or explicit None).
    Raises :class:`SchemaError` if the map drifts from the event set."""
    missing = set(EventType) - set(EVENT_PAYLOAD_CONTRACT)
    extra = set(EVENT_PAYLOAD_CONTRACT) - set(EventType)
    if missing or extra:
        raise SchemaError(
            "EVENT_PAYLOAD_CONTRACT is not total: "
            f"missing={sorted(e.value for e in missing)} "
            f"extra={sorted(getattr(e, 'value', e) for e in extra)}"
        )


def coerce_payload(event_type: EventType, payload: Mapping) -> Contract | dict:
    """Coerce a raw payload dict into its frozen contract (or return the dict
    unchanged for an open payload). Raises :class:`SchemaError` if malformed."""
    contract = EVENT_PAYLOAD_CONTRACT[event_type]
    if contract is None:
        return dict(payload)
    if not isinstance(payload, Mapping):
        raise SchemaError(f"{event_type.value} payload must be a mapping for {contract.__name__}")
    return contract.from_dict(payload)


def validate_event_payload(event: Event) -> None:
    """Validate ``event.payload`` against its frozen contract (fail-closed). A
    no-op for events with an open (None) payload. Intended for the single-writer
    append path (S1); exposed now so the contract is testable in S0."""
    coerce_payload(event.type, event.payload)
