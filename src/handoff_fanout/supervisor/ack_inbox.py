"""S3 — AckInbox (C2b): worker/audit/fixer completion signals → events (design §4.2).

This is the mechanism that keeps the EventLog single-writer (INV-3) *and* lets work
flow in: a worker/auditor/fixer never appends an event — it drops a **completion
signal** into the inbox, and the **supervisor** reads the inbox and translates the
signal into the one matching event (``worker_done`` / ``audit_done`` / ``fixer_done``)
which it appends itself. Only the supervisor ever writes ``events.jsonl``.

The signal wraps the **frozen S0** :class:`~handoff_fanout.supervisor.actions.Ack`
with a ``kind`` (worker/audit/fixer). The kind is what disambiguates two Acks that
look identical by node alone — e.g. a *re-delivered worker Ack* (its
``worker_done`` already landed, node now AUDITING) must never be mistaken for the
*audit Ack*. Disambiguation is by (a) the signal kind and (b) the dedupe_key of the
resulting event: a re-delivery deduplicates to a no-op (INV-4 at-least-once), and a
stale attempt is fenced off (dropped) rather than applied.

The supervisor *computes* the event extras the Ack cannot carry — for an audit
signal the machine :class:`~handoff_fanout.supervisor.verdict.Verdict` (INV-2: the
supervisor reads raw findings and computes it, it is never trusted from the worker),
for a fixer signal the terminal :class:`~handoff_fanout.supervisor.fixer.FixerState`.
Those arrive via injected callbacks (``verdict_for`` / ``fixer_state_for``); in the
live engine (S4) ``verdict_for`` runs the S2 VerdictComputer over the audit's raw
findings, here in S3 they are injected so the translation is exercised without
re-testing S2.

**Callback-purity contract (P1-1 dedupe seam).** ``verdict_for`` / ``fixer_state_for``
MUST be pure deterministic functions of their inputs (``ack`` / ``ack``+``fixer``) —
which they are by INV-2/INV-3 (the verdict is a deterministic fold over raw findings).
This is what makes an at-least-once *re-delivery* safe: a re-delivered audit/fixer
signal is re-translated to a byte-identical event, so :meth:`EventLog.append_event`
recognises it as a benign dedupe no-op (the deterministic event id / logical signature
deliberately excludes the envelope ``ts``, so the SAME signal drained at a later turn
is still the same logical event). A non-pure callback that recomputed a *different*
body for the same ack would (correctly) be caught fail-closed as a dedupe collision —
that is the INV-2 violation surfacing, not a re-delivery false-positive.

A malformed signal is quarantined and **skipped** (not fail-closed-crash): one
garbage worker signal must not deadlock the whole plan — the node simply looks
unreported and the Sweeper times it out. (Contrast the EventLog, where a bad line in
the *supervisor's own* append-only log IS fail-closed — that would be supervisor
corruption, not untrusted worker input.)
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from ..atomic import atomic_replace
from ._base import SchemaError
from .actions import Ack
from .event_log import AppendResult, DedupeCollisionError, EventLog
from .events import EventType, Provenance
from .fixer import FixerState
from .payloads import AuditDone, FixerDone
from .plan import Plan
from .reducer import FixerRuntime, NodeRuntime, SupervisorState, reduce
from .states import NodeState
from .verdict import Verdict

VerdictFor = Callable[[Ack], Verdict]
FixerStateFor = Callable[[Ack, FixerRuntime], FixerState]


class InboxSignalKind(enum.StrEnum):
    """Which kind of completion a signal reports — the disambiguator the frozen Ack
    cannot carry. The supervisor translates each kind into exactly one event."""

    WORKER = "worker"
    AUDIT = "audit"
    FIXER = "fixer"


class TranslationDisposition(enum.StrEnum):
    """What the supervisor did with one inbox signal."""

    APPENDED = "appended"  # translated → a new event was appended
    DEDUPED = "deduped"  # the event already existed (at-least-once re-delivery)
    DROPPED_STALE = "dropped_stale"  # fenced: signal's attempt != the node's current attempt
    QUARANTINED = "quarantined"  # malformed / out-of-order — moved aside, plan continues


@dataclasses.dataclass(frozen=True)
class TranslationOutcome:
    """The result of processing one inbox signal file."""

    path: Path
    disposition: TranslationDisposition
    kind: InboxSignalKind | None = None
    node: str | None = None
    event_type: EventType | None = None
    append: AppendResult | None = None
    reason: str | None = None


class AckInbox:
    """A directory of completion-signal files the supervisor drains into events."""

    def __init__(self, inbox_dir: str | Path) -> None:
        self.dir = Path(inbox_dir)
        self.processed_dir = self.dir / "processed"
        self.quarantine_dir = self.dir / "quarantine"

    # --- producers (workers/audit/fixer drop signals) -----------------------

    def deposit(self, kind: InboxSignalKind, ack: Ack, *, name: str | None = None) -> Path:
        """Write one completion signal (used by workers/tests). The supervisor is the
        only reader; this never touches ``events.jsonl``."""
        self.dir.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {"kind": kind.value, "ack": ack.to_dict()},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        # P2-b: the name names the logical event (kind+node+run_id+attempt) AND carries a
        # short CONTENT hash that discriminates DIFFERENT bodies sharing that tuple. Without
        # it, two *contradictory* signals (same dedupe tuple, divergent tree_oid/staged_diff)
        # atomically overwrote each other on disk, so the supervisor only ever drained the
        # last one and the EventLog dedupe-collision guard (P1-1) never saw the contradiction.
        # A content hash (not a clock/uuid) keeps the name reproducible (INV-1): an IDENTICAL
        # at-least-once re-delivery collapses to the SAME name (idempotent — no duplicate
        # noise), while distinct bodies land in distinct files and both reach the drain.
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        fname = name or f"{kind.value}-{ack.node}-{ack.run_id}-{ack.attempt}-{digest}.json"
        path = self.dir / fname
        # R2 codex #3: write atomically (temp + fsync + rename) so a supervisor draining
        # mid-deposit never sees a truncated file → quarantines it → permanently loses a
        # valid completion signal. A reader sees either no file or the whole file.
        atomic_replace(path, body)
        return path

    # --- supervisor drains the inbox into events ----------------------------

    def _signal_files(self) -> list[Path]:
        if not self.dir.exists():
            return []
        # Sort by name for deterministic processing order (INV-1).
        return sorted(p for p in self.dir.iterdir() if p.is_file() and p.suffix == ".json")

    def drain(
        self,
        log: EventLog,
        plan: Plan,
        *,
        ts: str,
        verdict_for: VerdictFor | None = None,
        fixer_state_for: FixerStateFor | None = None,
    ) -> list[TranslationOutcome]:
        """Translate every pending signal into its event and append it (single-writer).

        Deterministic given (log, inbox contents, ts, callbacks). ``ts`` is injected
        (the supervisor's turn clock) — this never reads the wall clock. Each appended
        event re-reads the log under the EventLog lock, so dedupe + CAS apply per
        signal."""
        outcomes: list[TranslationOutcome] = []
        for path in self._signal_files():
            outcome = self._process_one(path, log, plan, ts, verdict_for, fixer_state_for)
            outcomes.append(outcome)
            # File disposition mirrors the outcome (forensics; idempotent re-drain). R2
            # codex #4: a move failure must NOT abort the rest of the drain — the event is
            # already durable (appended under the EventLog lock), so a later re-drain
            # dedupes the leftover signal; we suppress the move error and keep processing
            # the remaining signals rather than stranding them behind one bad rename.
            dest = (
                self.quarantine_dir
                if outcome.disposition is TranslationDisposition.QUARANTINED
                else self.processed_dir
            )
            with contextlib.suppress(OSError):
                self._move(path, dest)
        return outcomes

    def _process_one(
        self,
        path: Path,
        log: EventLog,
        plan: Plan,
        ts: str,
        verdict_for: VerdictFor | None,
        fixer_state_for: FixerStateFor | None,
    ) -> TranslationOutcome:
        try:
            kind, ack = self._parse_signal(path)
        except (json.JSONDecodeError, SchemaError, ValueError) as exc:
            return TranslationOutcome(
                path=path,
                disposition=TranslationDisposition.QUARANTINED,
                reason=f"malformed signal: {exc}",
            )
        # Reduce the *current* log so translation dispatches on live node state (so a
        # prior signal in this same drain is reflected). Reading the log is the live
        # supervisor turn — not the pure reducer (INV-3 governs reduce(), not this).
        state = reduce(plan, log.read_all())
        node = state.nodes.get(ack.node)
        if node is None:
            return TranslationOutcome(
                path=path,
                disposition=TranslationDisposition.QUARANTINED,
                kind=kind,
                node=ack.node,
                reason=f"signal references unknown node {ack.node!r}",
            )
        try:
            if kind is InboxSignalKind.WORKER:
                return self._translate_worker(path, log, ts, kind, ack, node)
            if kind is InboxSignalKind.AUDIT:
                return self._translate_audit(path, log, ts, kind, ack, node, verdict_for)
            return self._translate_fixer(path, log, ts, kind, ack, node, fixer_state_for)
        except DedupeCollisionError as exc:
            # P1-1: a contradictory second Ack for an already-recorded logical event (same
            # dedupe_key, divergent body — a worker bug / replay). EventLog.append_event is
            # the single dedupe+collision authority and fails it closed. At THIS untrusted-
            # input seam we surface it as QUARANTINED (recorded with a reason, the opposite
            # of silently swallowing it as a benign dedupe) rather than crash the whole drain
            # — one buggy worker must not deadlock the plan (the AckInbox quarantine-and-skip
            # philosophy; contrast the EventLog's OWN-log corruption, which fails-closed-crash).
            return TranslationOutcome(
                path=path,
                disposition=TranslationDisposition.QUARANTINED,
                kind=kind,
                node=ack.node,
                reason=f"dedupe collision — contradictory Ack for an existing logical event "
                f"(fail-closed, not silently deduped): {exc}",
            )

    # --- per-kind translation -----------------------------------------------

    def _translate_worker(
        self,
        path: Path,
        log: EventLog,
        ts: str,
        kind: InboxSignalKind,
        ack: Ack,
        node: NodeRuntime,
    ) -> TranslationOutcome:
        dedupe = f"worker_done:{ack.node}:{ack.attempt}"
        # P1-1: never short-circuit a dedupe hit by returning the existing event blindly —
        # that bypasses the EventLog collision guard and silently swallows a contradictory
        # re-delivery. The fence gates only a NEW signal (a genuine re-delivery whose node
        # has already advanced past DISPATCHED must still dedupe, not be quarantined by the
        # state check); the append ALWAYS flows through ``append_event`` — the sole dedupe+
        # collision authority (same body → idempotent no-op; different body → raises
        # DedupeCollisionError, caught in ``_process_one`` → quarantined, never swallowed).
        if not self._already(log, dedupe):
            guard = self._fence(path, kind, ack, node, NodeState.DISPATCHED, "worker")
            if guard is not None:
                return guard
        result = log.append_event(
            type=EventType.WORKER_DONE,
            payload=ack,
            dedupe_key=dedupe,
            ts=ts,
            run_id=ack.run_id,
            attempt_id=str(ack.attempt),
            provenance=_provenance(ack),
        )
        return _appended(path, kind, ack.node, EventType.WORKER_DONE, result)

    def _translate_audit(
        self,
        path: Path,
        log: EventLog,
        ts: str,
        kind: InboxSignalKind,
        ack: Ack,
        node: NodeRuntime,
        verdict_for: VerdictFor | None,
    ) -> TranslationOutcome:
        dedupe = f"audit_done:{ack.node}:{ack.attempt}"
        # P1-1 (see ``_translate_worker``): fence only a NEW signal, then route the append
        # through ``append_event`` so a contradictory re-delivery collides fail-closed
        # instead of being silently deduped.
        if not self._already(log, dedupe):
            guard = self._fence(path, kind, ack, node, NodeState.AUDITING, "audit")
            if guard is not None:
                return guard
        if verdict_for is None:
            return TranslationOutcome(
                path=path,
                disposition=TranslationDisposition.QUARANTINED,
                kind=kind,
                node=ack.node,
                reason="audit signal but no verdict_for callback (supervisor must "
                "compute the machine verdict from raw findings — INV-2)",
            )
        verdict = verdict_for(ack)
        payload = AuditDone(node=ack.node, attempt=ack.attempt, verdict=verdict)
        result = log.append_event(
            type=EventType.AUDIT_DONE,
            payload=payload,
            dedupe_key=dedupe,
            ts=ts,
            run_id=ack.run_id,
            attempt_id=str(ack.attempt),
            provenance=_provenance(ack),
        )
        return _appended(path, kind, ack.node, EventType.AUDIT_DONE, result)

    def _translate_fixer(
        self,
        path: Path,
        log: EventLog,
        ts: str,
        kind: InboxSignalKind,
        ack: Ack,
        node: NodeRuntime,
        fixer_state_for: FixerStateFor | None,
    ) -> TranslationOutcome:
        fixer = node.active_fixer
        if fixer is None:
            return TranslationOutcome(
                path=path,
                disposition=TranslationDisposition.QUARANTINED,
                kind=kind,
                node=ack.node,
                reason=f"fixer signal for node {ack.node!r} with no active fixer",
            )
        dedupe = f"fixer_done:{fixer.fixer_id}"
        # P1-1 (see ``_translate_worker``): fence only a NEW signal (a genuine re-delivery
        # whose fixer already settled must dedupe, not be quarantined); the append always
        # flows through ``append_event`` so a contradictory fixer Ack collides fail-closed.
        if not self._already(log, dedupe):
            # Fence on the FIXER's attempt (its node lives in BLOCKED_BY_FIX while the fixer
            # runs), not the node's dispatch attempt.
            if ack.attempt != fixer.attempt:
                return TranslationOutcome(
                    path=path,
                    disposition=TranslationDisposition.DROPPED_STALE,
                    kind=kind,
                    node=ack.node,
                    reason=f"fixer signal attempt {ack.attempt} != active fixer attempt "
                    f"{fixer.attempt} (fenced)",
                )
            if node.status is not NodeState.BLOCKED_BY_FIX:
                return TranslationOutcome(
                    path=path,
                    disposition=TranslationDisposition.QUARANTINED,
                    kind=kind,
                    node=ack.node,
                    reason=f"fixer signal but node is {node.status.value}, not BLOCKED_BY_FIX",
                )
        if fixer_state_for is None:
            return TranslationOutcome(
                path=path,
                disposition=TranslationDisposition.QUARANTINED,
                kind=kind,
                node=ack.node,
                reason="fixer signal but no fixer_state_for callback (supervisor "
                "determines the fixer's terminal state from its own audit+oracle)",
            )
        terminal = fixer_state_for(ack, fixer)
        payload = FixerDone(
            fixer_id=fixer.fixer_id,
            parent_node=fixer.parent_node,
            attempt=fixer.attempt,
            state=terminal,
        )
        result = log.append_event(
            type=EventType.FIXER_DONE,
            payload=payload,
            dedupe_key=dedupe,
            ts=ts,
            run_id=ack.run_id,
            attempt_id=str(ack.attempt),
            provenance=_provenance(ack),
        )
        return _appended(path, kind, ack.node, EventType.FIXER_DONE, result)

    # --- shared helpers ------------------------------------------------------

    @staticmethod
    def _already(log: EventLog, dedupe_key: str) -> bool:
        """Whether this dedupe_key is already in the log — used ONLY to decide whether to
        run the new-signal fence (a re-delivery skips it). The dedupe DECISION itself
        (idempotent no-op vs collision) is made authoritatively by ``EventLog.append_event``,
        never here (P1-1: this must not become a second, collision-blind dedupe path)."""
        return any(e.dedupe_key == dedupe_key for e in log.read_all())

    @staticmethod
    def _fence(
        path: Path,
        kind: InboxSignalKind,
        ack: Ack,
        node: NodeRuntime,
        expected_state: NodeState,
        label: str,
    ) -> TranslationOutcome | None:
        """Attempt-fence then state-check. Returns a terminal outcome if the signal
        must be dropped/quarantined, or ``None`` to proceed with translation."""
        if ack.attempt != node.attempt:
            return TranslationOutcome(
                path=path,
                disposition=TranslationDisposition.DROPPED_STALE,
                kind=kind,
                node=ack.node,
                reason=f"{label} signal attempt {ack.attempt} != node attempt "
                f"{node.attempt} (fenced)",
            )
        if node.status is not expected_state:
            return TranslationOutcome(
                path=path,
                disposition=TranslationDisposition.QUARANTINED,
                kind=kind,
                node=ack.node,
                reason=f"{label} signal but node is {node.status.value}, not "
                f"{expected_state.value} (out of order)",
            )
        return None

    def _parse_signal(self, path: Path) -> tuple[InboxSignalKind, Ack]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SchemaError("signal is not a JSON object")
        unknown = set(data) - {"kind", "ack"}
        if unknown:
            raise SchemaError(f"signal has unknown keys {sorted(unknown)}")
        if "kind" not in data or "ack" not in data:
            raise SchemaError("signal requires both 'kind' and 'ack'")
        try:
            kind = InboxSignalKind(data["kind"])
        except ValueError as exc:
            raise SchemaError(f"unknown signal kind {data['kind']!r}") from exc
        ack = Ack.from_dict(data["ack"])
        return kind, ack

    @staticmethod
    def _move(path: Path, dest_dir: Path) -> None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        path.replace(dest_dir / path.name)


def _provenance(ack: Ack) -> Provenance:
    """Bind the translated event to the exact code state the worker/audit produced
    (anti-replay, INV-4 / design §6.1)."""
    return Provenance(
        commit=ack.commit,
        staged_diff_hash=ack.staged_diff_hash,
        tree_oid=ack.tree_oid,
    )


def _appended(
    path: Path,
    kind: InboxSignalKind,
    node: str,
    event_type: EventType,
    result: AppendResult,
) -> TranslationOutcome:
    disp = TranslationDisposition.DEDUPED if result.deduped else TranslationDisposition.APPENDED
    return TranslationOutcome(
        path=path,
        disposition=disp,
        kind=kind,
        node=node,
        event_type=event_type,
        append=result,
    )


# Re-exported for callers that want the typed state without importing reducer directly.
__all__ = [
    "AckInbox",
    "InboxSignalKind",
    "TranslationDisposition",
    "TranslationOutcome",
    "VerdictFor",
    "FixerStateFor",
    "SupervisorState",
]
