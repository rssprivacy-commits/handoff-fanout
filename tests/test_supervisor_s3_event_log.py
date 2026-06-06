"""S3 EventLog (C2) tests — single-writer append-only store (design §4.2 / §9 / §12 S3).

Covers the §4.2 write discipline: CAS-on-``expected_prev_seq`` (concurrent double-write
rejected), ``dedupe_key`` idempotency (at-least-once re-delivery is a no-op), bad-line
quarantine + fail-closed (not silently skipped), seq contiguity, cross-plan rejection,
the snapshot/compaction checkpoint interface, and real ``flock`` serialization under a
threaded append storm.

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s3_event_log.py
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from _s3_helpers import TS

from handoff_fanout.supervisor.event_log import (
    CASConflict,
    DedupeCollisionError,
    EventLog,
    QuarantinedLogError,
    build_event,
    canonical_json,
    derive_event_id,
)
from handoff_fanout.supervisor.events import SUPERVISOR_WRITER, Event, EventType
from handoff_fanout.supervisor.payloads import GlobalPaused, NodeReason


def _log(tmp_path: Path, plan_id: str = "p1") -> EventLog:
    return EventLog(tmp_path / "events.jsonl", plan_id)


def _paused(log: EventLog, dedupe: str = "d", *, ts: str = TS):
    return log.append_event(
        type=EventType.GLOBAL_PAUSED,
        payload=GlobalPaused(reason="owner", actor="owner"),
        dedupe_key=dedupe,
        ts=ts,
    )


# --- basic append / read round-trip ------------------------------------------


def test_append_then_read_round_trips(tmp_path: Path) -> None:
    log = _log(tmp_path)
    r = _paused(log, "first")
    assert r.appended and not r.deduped
    assert r.event.seq == 0 and r.event.expected_prev_seq == Event.GENESIS_PREV_SEQ
    assert r.event.writer == SUPERVISOR_WRITER
    events = log.read_all()
    assert len(events) == 1 and events[0].type is EventType.GLOBAL_PAUSED
    assert log.tail_seq() == 0


def test_empty_log_tail_is_genesis(tmp_path: Path) -> None:
    assert _log(tmp_path).tail_seq() == Event.GENESIS_PREV_SEQ  # -1 → next append expects empty


def test_seq_increments_contiguously(tmp_path: Path) -> None:
    log = _log(tmp_path)
    for i in range(5):
        _paused(log, f"d{i}")
    assert [e.seq for e in log.read_all()] == [0, 1, 2, 3, 4]


def test_derive_event_id_is_stable_and_dedupe_keyed() -> None:
    # Same (plan, dedupe_key) → same id (a deduped re-delivery is the same event).
    assert derive_event_id("p1", "k") == derive_event_id("p1", "k")
    assert derive_event_id("p1", "k") != derive_event_id("p1", "k2")
    assert derive_event_id("p1", "k") != derive_event_id("p2", "k")


# --- dedupe (INV-4 at-least-once idempotency) --------------------------------


def test_dedupe_key_makes_redelivery_a_noop(tmp_path: Path) -> None:
    log = _log(tmp_path)
    first = _paused(log, "same")
    again = _paused(log, "same")
    assert again.deduped and not again.appended
    assert again.event.event_id == first.event.event_id
    assert len(log.read_all()) == 1  # only one event on disk


def test_dedupe_collision_on_different_body_fails_closed(tmp_path: Path) -> None:
    # R2 codex #1: same dedupe_key, DIFFERENT logical body → fail closed (never silently
    # mask a divergent write). Here the re-delivery flips the GlobalPaused reason.
    log = _log(tmp_path)
    log.append_event(
        type=EventType.GLOBAL_PAUSED,
        payload=GlobalPaused(reason="owner", actor="owner"),
        dedupe_key="k",
        ts=TS,
    )
    with pytest.raises(DedupeCollisionError):
        log.append_event(
            type=EventType.GLOBAL_PAUSED,
            payload=GlobalPaused(reason="disk", actor="diskguard"),  # different body
            dedupe_key="k",  # same key
            ts=TS,
        )


def test_dedupe_same_body_different_ts_is_benign(tmp_path: Path) -> None:
    # A genuine re-delivery (same logical body) at a different append time is a no-op,
    # NOT a collision (ts is excluded from the logical signature).
    log = _log(tmp_path)
    log.append_event(
        type=EventType.GLOBAL_PAUSED,
        payload=GlobalPaused(reason="owner", actor="owner"),
        dedupe_key="k",
        ts=TS,
    )
    r = log.append_event(
        type=EventType.GLOBAL_PAUSED,
        payload=GlobalPaused(reason="owner", actor="owner"),
        dedupe_key="k",
        ts="2026-06-06T23:59:59",  # later re-delivery
    )
    assert r.deduped and not r.appended
    assert len(log.read_all()) == 1


def test_dedupe_dominates_stale_cas(tmp_path: Path) -> None:
    # A re-delivery carries a now-stale expected_prev_seq; dedupe must dominate CAS so
    # the benign duplicate is a no-op, not a CASConflict.
    log = _log(tmp_path)
    _paused(log, "a")  # seq 0
    _paused(log, "b")  # seq 1 → tail now 1
    stale = build_event(  # rebuilt as if the log were still empty (expected_prev_seq -1)
        plan_id="p1",
        prev_seq=Event.GENESIS_PREV_SEQ,
        type=EventType.GLOBAL_PAUSED,
        payload=GlobalPaused(reason="owner", actor="owner"),
        dedupe_key="a",  # same dedupe_key as the first event
        ts=TS,
    )
    res = log.append(stale)
    assert res.deduped and not res.appended  # dedupe, not CAS conflict


# --- CAS (concurrent double-write rejected) ----------------------------------


def test_cas_conflict_rejects_stale_writer(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _paused(log, "a")  # seq 0 → tail 0
    # A different event built against the empty log (expected_prev_seq -1) — a writer
    # that did not see the first append. New dedupe_key so dedupe does not mask it.
    stale = build_event(
        plan_id="p1",
        prev_seq=Event.GENESIS_PREV_SEQ,
        type=EventType.GLOBAL_PAUSED,
        payload=GlobalPaused(reason="disk", actor="diskguard"),
        dedupe_key="b",
        ts=TS,
    )
    with pytest.raises(CASConflict) as exc:
        log.append(stale)
    assert exc.value.expected == -1 and exc.value.actual == 0


def test_append_rejects_foreign_plan_id(tmp_path: Path) -> None:
    log = _log(tmp_path, "p1")
    foreign = build_event(
        plan_id="other",
        prev_seq=Event.GENESIS_PREV_SEQ,
        type=EventType.GLOBAL_PAUSED,
        payload=GlobalPaused(reason="owner", actor="owner"),
        dedupe_key="x",
        ts=TS,
    )
    from handoff_fanout.supervisor import SchemaError

    with pytest.raises(SchemaError):
        log.append(foreign)


# --- quarantine + fail-closed (design §4.2 "坏行→quarantine+fail-closed") ------


def test_malformed_json_line_quarantines_and_fails_closed(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _paused(log, "ok")
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n")
    with pytest.raises(QuarantinedLogError):
        log.read_all()
    # The bad line was preserved for forensics, not silently dropped.
    assert log.quarantine_path.exists()
    q = log.quarantine_path.read_text(encoding="utf-8")
    assert "not json" in q


def test_schema_violating_line_quarantines(tmp_path: Path) -> None:
    log = _log(tmp_path)
    # A JSON object that is not a valid Event (missing required fields).
    with open(log.path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 0, "type": "global_paused"}) + "\n")
    with pytest.raises(QuarantinedLogError):
        log.read_all()


def test_non_contiguous_seq_fails_closed(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _paused(log, "a")  # seq 0
    # Hand-write a line with seq 2 (gap at 1) — single writer can never do this.
    gap = build_event(
        plan_id="p1",
        prev_seq=1,  # claims to follow seq 1, but seq 1 is absent
        type=EventType.GLOBAL_PAUSED,
        payload=GlobalPaused(reason="owner", actor="owner"),
        dedupe_key="gap",
        ts=TS,
    )
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write(canonical_json(gap.to_dict()) + "\n")
    with pytest.raises(QuarantinedLogError):
        log.read_all()
    # R2 codex #2: the offending event is quarantined for forensics (not only raised).
    assert log.quarantine_path.exists()
    assert "non-contiguous" in log.quarantine_path.read_text(encoding="utf-8")


def test_cross_plan_line_quarantines(tmp_path: Path) -> None:
    log = _log(tmp_path, "p1")
    foreign = build_event(
        plan_id="p2",
        prev_seq=Event.GENESIS_PREV_SEQ,
        type=EventType.NODE_BLOCKED,
        payload=NodeReason(node="n1", reason="x"),
        dedupe_key="f",
        ts=TS,
    )
    with open(log.path, "w", encoding="utf-8") as fh:
        fh.write(canonical_json(foreign.to_dict()) + "\n")
    with pytest.raises(QuarantinedLogError):
        log.read_all()


def test_blank_lines_are_ignored_not_quarantined(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _paused(log, "a")
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write("\n\n")  # trailing blank lines (file formatting)
    assert len(log.read_all()) == 1  # no error, blanks skipped


def test_future_schema_version_quarantines(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _paused(log, "a")
    # S0 refuses a future schema_version; on disk it surfaces as a quarantined bad line.
    line = canonical_json(
        {
            "schema_version": 999,
            "event_id": "evt-x",
            "seq": 1,
            "ts": TS,
            "plan_id": "p1",
            "type": "global_paused",
            "expected_prev_seq": 0,
            "dedupe_key": "future",
            "writer": "supervisor",
            "run_id": None,
            "attempt_id": None,
            "payload": {"reason": "owner", "actor": "owner"},
            "provenance": None,
        }
    )
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    with pytest.raises(QuarantinedLogError):
        log.read_all()


# --- snapshot / compaction checkpoint interface ------------------------------


def test_snapshot_round_trips(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _paused(log, "a")
    log.write_snapshot(through_seq=0, state_hash="abc123", state={"plan_state": "RUNNING"})
    snap = log.read_snapshot()
    assert snap is not None
    assert snap["through_seq"] == 0 and snap["state_hash"] == "abc123"
    assert snap["state"] == {"plan_state": "RUNNING"}


def test_read_snapshot_none_when_absent(tmp_path: Path) -> None:
    assert _log(tmp_path).read_snapshot() is None


# --- real flock serialization under a threaded append storm ------------------


def test_concurrent_appends_serialize_without_loss_or_corruption(tmp_path: Path) -> None:
    """N threads each append one unique event via append_event (build-under-lock). flock
    serializes them; every event lands exactly once with a contiguous seq (no torn line,
    no lost update). This is the §9 "并发双写 → flock+CAS" guarantee end to end."""
    log = _log(tmp_path)
    n = 24
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()
            log.append_event(
                type=EventType.NODE_BLOCKED,
                payload=NodeReason(node=f"n{i}", reason=f"r{i}"),
                dedupe_key=f"k{i}",
                ts=TS,
            )
        except Exception as exc:  # noqa: BLE001 - record for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    events = log.read_all()  # raises if non-contiguous / dup dedupe_key / torn line
    assert len(events) == n
    assert [e.seq for e in events] == list(range(n))
    assert len({e.dedupe_key for e in events}) == n  # every unique event present once
