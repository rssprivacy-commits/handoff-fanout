"""S3 — EventLog (C2): the single-writer append-only event store (design §4.2).

``events.jsonl`` is the one source of truth for plan state (``State = reduce(plan,
events)`` — the Reducer, ``reducer.py``). S0 froze the :class:`~handoff_fanout.
supervisor.events.Event` envelope + the closed event set; this module is the
*persistence layer* that enforces the design §4.2 write discipline:

* **single writer (only the supervisor).** Every appended event already carries
  ``writer == "supervisor"`` (S0 rejects anything else). Workers/auditors/fixers
  never call this — they drop an :class:`~handoff_fanout.supervisor.actions.Ack`
  into the AckInbox and the supervisor translates it (``ack_inbox.py``).
* **``flock(events.lock)`` + ``expected_prev_seq`` CAS.** Appends serialize behind
  a cross-process ``fcntl.flock`` (reusing the engine's crash-safe
  :func:`~handoff_fanout.atomic.acquire_dir_lock`). The compare-and-swap rejects a
  concurrent double-write: an event may only land if the on-disk tail seq equals
  its ``expected_prev_seq`` (design §9 "并发双写 → flock+CAS").
* **dedupe_key idempotency (INV-4 at-least-once).** A re-delivered event (same
  ``dedupe_key``) is a no-op: the existing event is returned, the CAS is *not*
  re-checked (the re-delivery carries a now-stale ``expected_prev_seq``, which is
  benign — dedupe dominates).
* **bad line → quarantine + fail-closed (design §4.2 "坏行→quarantine+fail-closed,
  不静默跳").** A malformed / non-contiguous log is never silently skipped: the bad
  line is copied to a quarantine sidecar AND :class:`QuarantinedLogError` is raised
  so nothing reduces a corrupted log.
* **schema_version gate.** S0's :class:`Event` already refuses a future
  ``schema_version`` (fail-closed); a bad version surfaces here as a quarantined
  bad line.

Compaction/snapshot is a **minimal interface** here (design §4.2 "可留接口/最小实现",
configured by S4 DiskGuard): a verifiable checkpoint sidecar (:meth:`write_snapshot`
/ :meth:`read_snapshot`) bound to the reducer's deterministic state fingerprint.
Physical truncation of old events is deliberately left to S4 (it would change the
seq-from-0 contiguity contract this module enforces), so S3 keeps the snapshot as
an additional durable checkpoint, not a truncation trigger — honest minimal.

This module touches the filesystem (that is its job — it is the durable log), but
it is **not wired into the running handoff engine**: it is a self-contained S3
component under ``supervisor/`` that only reuses the leaf
:mod:`~handoff_fanout.atomic` helper (S3 红线: 只增不改运行路径).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from ..atomic import acquire_dir_lock
from ._base import SCHEMA_VERSION, Contract, SchemaError
from .events import Event, EventType, Provenance

# In-process serialization layer over the cross-process ``flock``. The engine's
# ``acquire_dir_lock`` is cross-*process* safe (the kernel fences crashed holders) but
# **not** cross-*thread* safe: its re-entrant registry is keyed by realpath, so a second
# thread acquiring the SAME path would be mis-counted as re-entrant and skip the flock
# wait → lost update. The supervisor is single-writer/single-threaded, but defending the
# §9 "并发双写" invariant fully means serializing threads too (defense in depth). We take
# a per-path in-process RLock *before* the flock so only one thread is ever inside the
# read-tail→CAS→append critical section, and the flock still serializes other processes.
_INPROC_META = threading.Lock()
_INPROC_LOCKS: dict[str, threading.RLock] = {}


def _inproc_lock_for(path: Path) -> threading.RLock:
    key = os.path.realpath(str(path))
    with _INPROC_META:
        lk = _INPROC_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _INPROC_LOCKS[key] = lk
        return lk


class CASConflict(RuntimeError):
    """Raised when an event's ``expected_prev_seq`` does not match the on-disk tail
    (a concurrent double-write, design §9). The caller re-reads the tail and rebuilds
    the event with the fresh ``expected_prev_seq`` before retrying."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"CAS conflict: event.expected_prev_seq={expected} but on-disk tail "
            f"seq={actual} (concurrent double-write — re-read tail and rebuild)"
        )


class QuarantinedLogError(SchemaError):
    """Raised after a bad line is quarantined (design §4.2 "坏行→quarantine+
    fail-closed"). The log is NOT silently skipped past the corruption — nothing may
    reduce a corrupted log until an operator resolves it."""


class DedupeCollisionError(SchemaError):
    """Raised when an append reuses an existing ``dedupe_key`` for a *different* logical
    event (R2 codex #1). A genuine at-least-once re-delivery carries the same logical
    body (type/payload/run_id/attempt_id/provenance) and is a benign no-op; the same key
    on a *different* body is a caller bug that must fail closed, never silently return the
    old event (which would mask a divergent write — a single-writer integrity hole)."""


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, unicode preserved. Used for
    event lines and state fingerprints so byte-output is reproducible (INV-1)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def derive_event_id(plan_id: str, dedupe_key: str) -> str:
    """A stable event id derived from the logical event identity (``dedupe_key``).

    Two at-least-once deliveries of the *same* logical event share a ``dedupe_key``
    and therefore an ``event_id`` — so the id never depends on wall-clock/random
    state (INV-1 / INV-3 reproducibility) and a deduped re-delivery is recognisably
    the same event."""
    digest = hashlib.sha256(f"{plan_id}\x00{dedupe_key}".encode()).hexdigest()
    return f"evt-{digest[:24]}"


#: A logical-event signature for dedupe-collision detection (R2 codex #1): the
#: *semantic* body of an event, deliberately excluding the envelope position/time
#: (``seq`` / ``expected_prev_seq`` / ``event_id`` / ``ts``). Two at-least-once
#: deliveries of the same logical event share this signature (a benign no-op);
#: differing signatures on the same ``dedupe_key`` are a caller bug → fail closed.
LogicalSignature = tuple


def _logical_signature_from_fields(
    type: EventType,
    payload_dict: dict,
    run_id: str | None,
    attempt_id: str | None,
    provenance: Provenance | None,
    schema_version: int,
) -> LogicalSignature:
    prov = provenance.to_dict() if provenance is not None else None
    return (
        type.value,
        canonical_json(payload_dict),
        run_id,
        attempt_id,
        canonical_json(prov),
        schema_version,
    )


def _logical_signature(event: Event) -> LogicalSignature:
    return _logical_signature_from_fields(
        event.type,
        event.payload,
        event.run_id,
        event.attempt_id,
        event.provenance,
        event.schema_version,
    )


def build_event(
    *,
    plan_id: str,
    prev_seq: int,
    type: EventType,
    payload: Contract | Mapping[str, Any],
    dedupe_key: str,
    ts: str,
    run_id: str | None = None,
    attempt_id: str | None = None,
    provenance: Provenance | None = None,
    schema_version: int = SCHEMA_VERSION,
) -> Event:
    """Construct an :class:`Event` whose envelope is consistent with appending right
    after ``prev_seq`` (``seq = prev_seq + 1``, ``expected_prev_seq = prev_seq``).

    ``ts`` is **injected by the caller** (the supervisor reads the clock and passes
    it in) — this module never reads the wall clock, so the deterministic core stays
    time-free (INV-3). ``payload`` may be a frozen S0 contract (serialized via
    ``to_dict``) or an already-jsonable mapping. S0's :meth:`Event.validate` runs on
    construction, so a malformed payload raises here, not at append."""
    payload_dict = payload.to_dict() if isinstance(payload, Contract) else dict(payload)
    return Event(
        schema_version=schema_version,
        event_id=derive_event_id(plan_id, dedupe_key),
        seq=prev_seq + 1,
        ts=ts,
        plan_id=plan_id,
        type=type,
        expected_prev_seq=prev_seq,
        dedupe_key=dedupe_key,
        run_id=run_id,
        attempt_id=attempt_id,
        payload=payload_dict,
        provenance=provenance,
    )


@dataclasses.dataclass(frozen=True)
class AppendResult:
    """Outcome of an append. ``deduped`` ⟺ the event's ``dedupe_key`` was already in
    the log, so this was an idempotent no-op (``event`` is the *existing* one)."""

    event: Event
    appended: bool
    deduped: bool


class EventLog:
    """Append-only single-writer event store backing one plan's ``events.jsonl``.

    Concurrency: every mutating path takes the cross-process ``flock`` so the
    read-tail → CAS → append sequence is atomic; the CAS additionally rejects a
    stale/concurrent writer that slipped a different event in (design §4.2 / §9).
    """

    def __init__(self, path: str | Path, plan_id: str) -> None:
        if not plan_id:
            raise SchemaError("EventLog requires a plan_id")
        self.path = Path(path)
        self.plan_id = plan_id
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.quarantine_path = self.path.with_name(self.path.name + ".quarantine")
        self.snapshot_path = self.path.with_name(self.path.name + ".snapshot")
        self._inproc = _inproc_lock_for(self.lock_path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the in-process lock then the cross-process ``flock`` for the whole
        read-tail→CAS→append critical section (serializes both threads and processes)."""
        with self._inproc, acquire_dir_lock(self.lock_path):
            yield

    # --- reading -------------------------------------------------------------

    def read_all(self) -> list[Event]:
        """Parse the whole log into :class:`Event` objects (fail-closed).

        A line that is not valid JSON, fails S0 :class:`Event` validation, or breaks
        seq contiguity (0,1,2,…) is quarantined and :class:`QuarantinedLogError` is
        raised (design §4.2). Truly empty lines (file formatting) are ignored."""
        return self._parse(self._raw_lines())

    def tail_seq(self) -> int:
        """The highest seq on disk, or :attr:`Event.GENESIS_PREV_SEQ` (``-1``) for an
        empty log — i.e. the ``expected_prev_seq`` the next append must carry."""
        events = self.read_all()
        return events[-1].seq if events else Event.GENESIS_PREV_SEQ

    def _raw_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8")
        return text.split("\n")

    def _parse(self, raw_lines: list[str]) -> list[Event]:
        events: list[Event] = []
        for lineno, raw in enumerate(raw_lines):
            if raw.strip() == "":
                continue  # blank line (trailing newline / formatting) — not a record
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise SchemaError("event line is not a JSON object")
                event = Event.from_dict(data)
            except (json.JSONDecodeError, SchemaError, ValueError) as exc:
                self._quarantine(raw, f"line {lineno}: {exc}")
                raise QuarantinedLogError(
                    f"{self.path.name} line {lineno} is malformed and was quarantined "
                    f"(fail-closed, not skipped): {exc}"
                ) from exc
            if event.plan_id != self.plan_id:
                self._quarantine(raw, f"line {lineno}: plan_id mismatch")
                raise QuarantinedLogError(
                    f"{self.path.name} line {lineno} belongs to plan {event.plan_id!r}, "
                    f"not {self.plan_id!r} (cross-plan contamination — quarantined)"
                )
            events.append(event)
        self._assert_contiguous(events)
        return events

    def _assert_contiguous(self, events: list[Event]) -> None:
        """Seqs must be 0,1,2,… with no gap or duplicate, and dedupe_keys unique — a
        single writer can never produce otherwise, so a violation is corruption. The
        offending event is quarantined before raising (R2 codex #2: honor the "坏行→
        quarantine+fail-closed" forensic contract for contiguity/dup violations too,
        not only for unparseable lines)."""
        seen_keys: set[str] = set()
        for expected_seq, event in enumerate(events):
            if event.seq != expected_seq:
                reason = f"seq non-contiguous at position {expected_seq}: event.seq={event.seq}"
                self._quarantine(canonical_json(event.to_dict()), reason)
                raise QuarantinedLogError(
                    f"{self.path.name} {reason} (expected {expected_seq}) — corrupted log, "
                    "fail-closed (offending event quarantined)"
                )
            if event.dedupe_key in seen_keys:
                reason = f"duplicate dedupe_key {event.dedupe_key!r} on disk"
                self._quarantine(canonical_json(event.to_dict()), reason)
                raise QuarantinedLogError(
                    f"{self.path.name} has a {reason} — the dedupe guard was bypassed, "
                    "corrupted log (fail-closed, offending event quarantined)"
                )
            seen_keys.add(event.dedupe_key)

    def _quarantine(self, raw: str, reason: str) -> None:
        """Copy a bad line + its reason to the quarantine sidecar (forensics). Best
        effort: a quarantine-write failure must not mask the original corruption."""
        try:
            record = canonical_json({"reason": reason, "raw": raw}) + "\n"
            with open(self.quarantine_path, "a", encoding="utf-8") as fh:
                fh.write(record)
        except OSError:
            pass

    # --- appending (single writer, flock + CAS + dedupe) ---------------------

    def append(self, event: Event) -> AppendResult:
        """CAS-append a pre-built :class:`Event` (design §4.2 single-writer path).

        Under ``flock``: dedupe first (idempotent no-op on a re-delivery), then CAS
        ``event.expected_prev_seq`` against the on-disk tail (raise
        :class:`CASConflict` on a concurrent double-write). The event's
        ``plan_id`` / ``seq`` self-consistency was already checked by S0."""
        if event.plan_id != self.plan_id:
            raise SchemaError(
                f"event.plan_id={event.plan_id!r} does not match this log's "
                f"plan_id={self.plan_id!r}"
            )
        with self._locked():
            events = self.read_all()
            existing = self._find_dedupe(events, event.dedupe_key)
            if existing is not None:
                self._assert_same_logical_event(existing, _logical_signature(event))
                return AppendResult(event=existing, appended=False, deduped=True)
            tail = events[-1].seq if events else Event.GENESIS_PREV_SEQ
            if event.expected_prev_seq != tail:
                raise CASConflict(event.expected_prev_seq, tail)
            self._write_line(event)
            return AppendResult(event=event, appended=True, deduped=False)

    def append_event(
        self,
        *,
        type: EventType,
        payload: Contract | Mapping[str, Any],
        dedupe_key: str,
        ts: str,
        run_id: str | None = None,
        attempt_id: str | None = None,
        provenance: Provenance | None = None,
    ) -> AppendResult:
        """Build + append in one locked critical section (no CAS race possible from a
        single process). The primary supervisor API; ``ts`` is injected by the caller
        (INV-3 — this module never reads the clock)."""
        with self._locked():
            events = self.read_all()
            existing = self._find_dedupe(events, dedupe_key)
            if existing is not None:
                payload_dict = payload.to_dict() if isinstance(payload, Contract) else dict(payload)
                self._assert_same_logical_event(
                    existing,
                    _logical_signature_from_fields(
                        type, payload_dict, run_id, attempt_id, provenance, SCHEMA_VERSION
                    ),
                )
                return AppendResult(event=existing, appended=False, deduped=True)
            tail = events[-1].seq if events else Event.GENESIS_PREV_SEQ
            event = build_event(
                plan_id=self.plan_id,
                prev_seq=tail,
                type=type,
                payload=payload,
                dedupe_key=dedupe_key,
                ts=ts,
                run_id=run_id,
                attempt_id=attempt_id,
                provenance=provenance,
            )
            self._write_line(event)
            return AppendResult(event=event, appended=True, deduped=False)

    @staticmethod
    def _find_dedupe(events: list[Event], dedupe_key: str) -> Event | None:
        for e in events:
            if e.dedupe_key == dedupe_key:
                return e
        return None

    @staticmethod
    def _assert_same_logical_event(existing: Event, incoming_sig: LogicalSignature) -> None:
        """R2 codex #1: a dedupe hit is a benign re-delivery only if the incoming logical
        body matches the existing one; the same ``dedupe_key`` on a *different* body is a
        caller bug → fail closed (never silently return the old event)."""
        if _logical_signature(existing) != incoming_sig:
            raise DedupeCollisionError(
                f"dedupe_key {existing.dedupe_key!r} (event {existing.event_id}, seq "
                f"{existing.seq}) is being reused for a DIFFERENT logical event — refusing "
                "to mask a divergent write (a benign re-delivery must carry the same "
                "type/payload/run_id/attempt_id/provenance)"
            )

    def _write_line(self, event: Event) -> None:
        """Append one event line + newline with a single ``os.write`` under the held
        lock, then fsync. One short write keeps the line atomic for a concurrent
        reader (no torn line) on a local filesystem."""
        line = canonical_json(event.to_dict()) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    # --- snapshot / compaction (minimal interface, design §4.2) --------------

    def write_snapshot(
        self, *, through_seq: int, state_hash: str, state: Mapping[str, Any]
    ) -> None:
        """Write a verifiable checkpoint sidecar (design §4.2 "可留接口/最小实现").

        Binds the reducer's deterministic ``state_hash`` to ``through_seq`` so a
        reader can confirm a replay up to that seq reproduces the same state.
        Physical truncation of events ≤ ``through_seq`` is left to S4 DiskGuard (it
        would change the seq-from-0 contiguity this log enforces), so the snapshot is
        an *additional* durable checkpoint, not a truncation trigger."""
        if through_seq < 0:
            raise SchemaError("snapshot through_seq must be >= 0")
        if not state_hash:
            raise SchemaError("snapshot state_hash required")
        body = canonical_json(
            {"through_seq": through_seq, "state_hash": state_hash, "state": dict(state)}
        )
        with self._locked(), open(self.snapshot_path, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())

    def read_snapshot(self) -> dict[str, Any] | None:
        """Read the checkpoint sidecar, or ``None`` if there is no snapshot."""
        if not self.snapshot_path.exists():
            return None
        data = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise QuarantinedLogError(f"{self.snapshot_path.name} is not a JSON object")
        return data
