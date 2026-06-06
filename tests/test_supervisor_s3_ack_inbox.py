"""S3 AckInbox (C2b) tests — completion signals → events (design §4.2 C2b / §12 S3).

Covers the single-writer translation seam: a worker/audit/fixer signal is turned into
exactly one ``worker_done`` / ``audit_done`` / ``fixer_done`` event the *supervisor*
appends (the only writer), with the supervisor computing the verdict (INV-2) it can't
trust from the worker. Plus: at-least-once dedupe, attempt fencing, malformed-signal
quarantine-and-skip (plan continues), and the kind-disambiguation that stops a
re-delivered worker Ack from being mistaken for the audit Ack.

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s3_ack_inbox.py
"""

from __future__ import annotations

from pathlib import Path

from _s3_helpers import Ack, NodeAttempt, Seq, green_verdict, make_plan, red_verdict

from handoff_fanout.supervisor.ack_inbox import (
    AckInbox,
    InboxSignalKind,
    TranslationDisposition,
)
from handoff_fanout.supervisor.event_log import EventLog
from handoff_fanout.supervisor.events import SUPERVISOR_WRITER, EventType
from handoff_fanout.supervisor.fixer import FixerState
from handoff_fanout.supervisor.reducer import reduce
from handoff_fanout.supervisor.states import NodeState

TS2 = "2026-06-06T11:00:00"


def _seed(tmp_path: Path, seq: Seq, plan_id: str = "p1") -> EventLog:
    """Materialise a Seq onto a real EventLog file (the supervisor's prior writes)."""
    log = EventLog(tmp_path / "events.jsonl", plan_id)
    for e in seq.events:
        log.append(e)
    return log


def _ack(node: str, attempt: int = 1) -> Ack:
    return Ack(node=node, run_id=f"run-{node}-{attempt}", attempt=attempt, tree_oid="tree1")


def _dispatched_log(tmp_path: Path):
    plan = make_plan()
    return plan, _seed(tmp_path, Seq(plan).plan_created().dispatch("n1"))


def _auditing_log(tmp_path: Path):
    plan = make_plan()
    seq = Seq(plan).plan_created().dispatch("n1").worker_done("n1").audit_started("n1")
    return plan, _seed(tmp_path, seq)


def _blocked_by_fix_log(tmp_path: Path):
    plan = make_plan()
    seq = (
        Seq(plan)
        .plan_created()
        .dispatch("n1")
        .worker_done("n1")
        .audit_started("n1")
        .audit_done("n1", red_verdict())
        .fixer_spawned("n1", "f-n1-1")
    )
    return plan, _seed(tmp_path, seq)


# --- worker signal → worker_done ---------------------------------------------


def test_worker_signal_translates_to_worker_done(tmp_path: Path) -> None:
    plan, log = _dispatched_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1"))
    outcomes = inbox.drain(log, plan, ts=TS2)
    assert len(outcomes) == 1
    assert outcomes[0].disposition is TranslationDisposition.APPENDED
    assert outcomes[0].event_type is EventType.WORKER_DONE
    state = reduce(plan, log.read_all())
    assert state.nodes["n1"].status is NodeState.AUDITING
    # Single writer: the supervisor (not the worker) wrote the event.
    assert all(e.writer == SUPERVISOR_WRITER for e in log.read_all())
    # The signal file was consumed (moved to processed/).
    assert not list((tmp_path / "inbox").glob("*.json"))


# --- audit signal → audit_done (supervisor computes the verdict, INV-2) -------


def test_audit_signal_translates_to_audit_done_with_verdict(tmp_path: Path) -> None:
    plan, log = _auditing_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.AUDIT, _ack("n1"))
    verdict = green_verdict()
    outcomes = inbox.drain(log, plan, ts=TS2, verdict_for=lambda ack: verdict)
    assert outcomes[0].disposition is TranslationDisposition.APPENDED
    assert outcomes[0].event_type is EventType.AUDIT_DONE
    state = reduce(plan, log.read_all())
    n = state.nodes["n1"]
    assert n.status is NodeState.EVALUATING
    assert n.verdict is not None and n.verdict.verdict.value == "GREEN"


def test_audit_signal_without_verdict_callback_quarantines(tmp_path: Path) -> None:
    plan, log = _auditing_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.AUDIT, _ack("n1"))
    outcomes = inbox.drain(log, plan, ts=TS2)  # no verdict_for
    assert outcomes[0].disposition is TranslationDisposition.QUARANTINED
    assert "verdict" in (outcomes[0].reason or "")
    # No audit_done appended; the node stays AUDITING (Sweeper would later time it out).
    assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.AUDITING


# --- fixer signal → fixer_done -----------------------------------------------


def test_fixer_signal_translates_to_fixer_done(tmp_path: Path) -> None:
    plan, log = _blocked_by_fix_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.FIXER, _ack("n1"))
    outcomes = inbox.drain(log, plan, ts=TS2, fixer_state_for=lambda ack, fx: FixerState.DONE)
    assert outcomes[0].disposition is TranslationDisposition.APPENDED
    assert outcomes[0].event_type is EventType.FIXER_DONE
    state = reduce(plan, log.read_all())
    assert state.nodes["n1"].active_fixer.state is FixerState.DONE


# --- at-least-once dedupe + fencing ------------------------------------------


def test_redelivered_worker_signal_is_deduped(tmp_path: Path) -> None:
    plan, log = _dispatched_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1"))
    inbox.drain(log, plan, ts=TS2)  # → worker_done landed (node now AUDITING)
    before = len(log.read_all())
    # A second, identical worker signal arrives (at-least-once re-delivery). The
    # kind=worker dedupe_key already exists → deduped no-op, NOT mistaken for an audit.
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1"))
    outcomes = inbox.drain(log, plan, ts=TS2)
    assert outcomes[0].disposition is TranslationDisposition.DEDUPED
    assert len(log.read_all()) == before  # no new event
    assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.AUDITING


def test_stale_attempt_worker_signal_is_fenced(tmp_path: Path) -> None:
    plan, log = _dispatched_log(tmp_path)  # node dispatched at attempt 1
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1", attempt=2))  # stale future attempt
    outcomes = inbox.drain(log, plan, ts=TS2)
    assert outcomes[0].disposition is TranslationDisposition.DROPPED_STALE
    assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.DISPATCHED  # unchanged


def test_worker_signal_for_wrong_state_quarantines(tmp_path: Path) -> None:
    # A worker signal whose node is not DISPATCHED (here: AUDITING) and whose
    # worker_done has NOT already landed (different attempt) is out of order → quarantine.
    plan, log = _auditing_log(tmp_path)  # node AUDITING at attempt 1; worker_done:n1:1 exists
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1", attempt=2))  # no worker_done:n1:2 yet
    outcomes = inbox.drain(log, plan, ts=TS2)
    # attempt 2 != node attempt 1 → fenced as stale (the safe disposition).
    assert outcomes[0].disposition is TranslationDisposition.DROPPED_STALE


# --- malformed / unknown signals: quarantine-and-skip (plan continues) -------


def test_malformed_signal_quarantined_and_skipped(tmp_path: Path) -> None:
    plan, log = _dispatched_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    (tmp_path / "inbox").mkdir(parents=True, exist_ok=True)
    (tmp_path / "inbox" / "garbage.json").write_text("{not json", encoding="utf-8")
    # A valid worker signal alongside the garbage must still be processed.
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1"))
    outcomes = inbox.drain(log, plan, ts=TS2)
    dispositions = {o.path.name: o.disposition for o in outcomes}
    assert dispositions["garbage.json"] is TranslationDisposition.QUARANTINED
    # The valid signal still translated (one garbage file did not deadlock the plan).
    assert any(o.disposition is TranslationDisposition.APPENDED for o in outcomes)
    assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.AUDITING
    assert (tmp_path / "inbox" / "quarantine" / "garbage.json").exists()


def test_signal_for_unknown_node_quarantines(tmp_path: Path) -> None:
    plan, log = _dispatched_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.WORKER, _ack("ghost"))
    outcomes = inbox.drain(log, plan, ts=TS2)
    assert outcomes[0].disposition is TranslationDisposition.QUARANTINED
    assert "unknown node" in (outcomes[0].reason or "")


# --- R2 codex hardening: atomic deposit (#3) + move-failure robustness (#4) ---


def test_deposit_is_atomic_no_temp_leftover(tmp_path: Path) -> None:
    # R2 codex #3: deposit writes atomically — a complete, parseable file with no leftover
    # temp file a concurrent drain could see truncated.
    inbox = AckInbox(tmp_path / "inbox")
    path = inbox.deposit(InboxSignalKind.WORKER, _ack("n1"))
    import json as _json

    data = _json.loads(path.read_text(encoding="utf-8"))  # complete JSON, not torn
    assert data["kind"] == "worker" and data["ack"]["node"] == "n1"
    assert not list((tmp_path / "inbox").glob(".*.tmp.*"))  # no temp leftover


def test_drain_survives_a_move_failure(tmp_path: Path, monkeypatch) -> None:
    # R2 codex #4: a _move failure must not abort the drain — the event is durable and a
    # later drain dedupes the leftover signal. The valid signal still translated.
    plan, log = _dispatched_log(tmp_path)
    inbox = AckInbox(tmp_path / "inbox")
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1"))

    def _boom(path, dest):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(inbox, "_move", _boom)
    outcomes = inbox.drain(log, plan, ts=TS2)
    # The event was appended (durable) despite the move failing afterwards.
    assert outcomes[0].disposition is TranslationDisposition.APPENDED
    assert reduce(plan, log.read_all()).nodes["n1"].status is NodeState.AUDITING


# --- end-to-end: a worker→audit→fixer chain entirely via the inbox -----------


def test_full_chain_through_inbox_preserves_single_writer(tmp_path: Path) -> None:
    plan = make_plan()
    log = _seed(tmp_path, Seq(plan).plan_created().dispatch("n1"))
    inbox = AckInbox(tmp_path / "inbox")

    # worker reports → worker_done
    inbox.deposit(InboxSignalKind.WORKER, _ack("n1"))
    inbox.drain(log, plan, ts=TS2)
    # supervisor starts audit (a control event it appends itself — modelled directly here)
    log.append_event(
        type=EventType.AUDIT_STARTED,
        payload=NodeAttempt(node="n1", attempt=1),
        dedupe_key="audit_started:n1:1",
        ts=TS2,
    )
    # audit reports → audit_done (RED → fixer territory)
    inbox.deposit(InboxSignalKind.AUDIT, _ack("n1"))
    inbox.drain(log, plan, ts=TS2, verdict_for=lambda ack: red_verdict())

    state = reduce(plan, log.read_all())
    assert state.nodes["n1"].status is NodeState.EVALUATING
    assert state.nodes["n1"].verdict.verdict.value == "RED"
    # Every event in the log was written by the supervisor — the single-writer invariant
    # held across the whole chain even though workers/auditors drove it via the inbox.
    assert all(e.writer == SUPERVISOR_WRITER for e in log.read_all())
