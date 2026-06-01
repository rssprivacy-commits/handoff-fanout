"""Orphan defense (v5.2) — ported from the ERP scripts/tests/test_handoff_orphan.py.

Same 15 cases, but driven through the ``handoff_fanout`` package APIs and
the ``isolated_handoff_home`` conftest fixture instead of the bash-era
``HANDOFF_ROOT`` monkeypatch trick.

Each test still covers one of three code paths:

  * ``dump.assert_batch_alive`` — the spawn-time invariant (C1).
  * ``dump.find_orphans`` + ``dump.handle_cleanup_orphan`` — orphan
    detection and the dry-run/--apply CLI (C4).
  * ``watchdog.scan_orphan_spawns`` — the cross-project sweep mode (C3 / mode 5).

No external services; pure filesystem.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from handoff_fanout import dump, watchdog

DEFAULT_PROJECT = "test-project"


def _setup_project(root: Path, project: str = DEFAULT_PROJECT) -> dict:
    """Build a fresh ``ack/ queue/ launched/ batches/`` tree under ``root/project``."""
    proj = root / project
    (proj / "ack").mkdir(parents=True)
    (proj / "queue").mkdir(parents=True)
    (proj / "launched").mkdir(parents=True)
    (proj / "batches").mkdir(parents=True)
    return {
        "proj": proj,
        "ack": proj / "ack",
        "queue": proj / "queue",
        "launched": proj / "launched",
        "batches": proj / "batches",
    }


def _stale(path: Path, seconds_ago: float) -> None:
    """Backdate file mtime so it falls outside the orphan grace window."""
    new_t = time.time() - seconds_ago
    os.utime(path, (new_t, new_t))


# ─── C1: assert_batch_alive ──────────────────────────────────────────────────


def test_assert_batch_alive_passes_when_alive(tmp_path):
    batch_dir = tmp_path / "b1"
    batch_dir.mkdir()
    (batch_dir / "manifest.json").write_text("{}")
    dump.assert_batch_alive(batch_dir, stage="test")  # should not raise


def test_assert_batch_alive_fails_when_dir_missing(tmp_path):
    batch_dir = tmp_path / "nonexistent"
    with pytest.raises(SystemExit) as ei:
        dump.assert_batch_alive(batch_dir, stage="test-stage")
    assert "batch_dir 在 spawn 期消失" in str(ei.value)
    assert "test-stage" in str(ei.value)


def test_assert_batch_alive_fails_when_manifest_missing(tmp_path):
    batch_dir = tmp_path / "b2"
    batch_dir.mkdir()
    with pytest.raises(SystemExit) as ei:
        dump.assert_batch_alive(batch_dir, stage="post-mkdir")
    assert "manifest.json 在 spawn 期消失" in str(ei.value)


# ─── C4: find_orphans ────────────────────────────────────────────────────────


def test_find_orphans_detects_spawned_without_md(isolated_handoff_home):
    p = _setup_project(isolated_handoff_home)
    (p["ack"] / "g2.spawned").write_text("(test)")
    (p["ack"] / "g2.submitted").write_text("(test)")
    found = dump.find_orphans()
    assert len(found) == 1
    assert found[0]["task"] == "g2"
    assert found[0]["project"] == DEFAULT_PROJECT


def test_find_orphans_skips_when_md_exists(isolated_handoff_home):
    p = _setup_project(isolated_handoff_home)
    (p["ack"] / "active-task.spawned").write_text("(test)")
    (p["queue"] / "active-task.md").write_text("# task")
    assert dump.find_orphans() == []


def test_find_orphans_skips_when_done(isolated_handoff_home):
    p = _setup_project(isolated_handoff_home)
    (p["ack"] / "closed.spawned").write_text("(test)")
    (p["queue"] / "closed.done").touch()
    assert dump.find_orphans() == []


def test_find_orphans_respects_project_filter(isolated_handoff_home):
    p1 = _setup_project(isolated_handoff_home, "proj-one")
    p2 = _setup_project(isolated_handoff_home, "proj-two")
    (p1["ack"] / "g2.spawned").write_text("(test)")
    (p2["ack"] / "h1.spawned").write_text("(test)")
    assert {o["task"] for o in dump.find_orphans()} == {"g2", "h1"}
    assert {o["task"] for o in dump.find_orphans(project_filter="proj-one")} == {"g2"}
    assert {o["task"] for o in dump.find_orphans(project_filter="proj-two")} == {"h1"}


def test_find_orphans_skips_special_dirs(isolated_handoff_home):
    _setup_project(isolated_handoff_home)
    (isolated_handoff_home / "locks").mkdir()
    (isolated_handoff_home / "_recovery").mkdir()
    # locks / _recovery have no ack/ subdir → naturally skipped
    assert dump.find_orphans() == []


# ─── C4: handle_cleanup_orphan ────────────────────────────────────────────────


def test_cleanup_orphan_dry_run_does_not_delete(isolated_handoff_home, capsys):
    p = _setup_project(isolated_handoff_home)
    spawned = p["ack"] / "g2.spawned"
    spawned.write_text("(test)")
    submitted = p["ack"] / "g2.submitted"
    submitted.write_text("(test)")
    launched_txt = p["launched"] / "g2-12345.txt"
    launched_txt.write_text("(test)")

    args = SimpleNamespace(project=None, apply=False, kill_spawned=False)
    dump.handle_cleanup_orphan(args)

    assert spawned.exists()
    assert submitted.exists()
    assert launched_txt.exists()
    out = capsys.readouterr().out
    assert "1 个孤儿" in out
    assert "g2" in out
    assert "dry-run" in out


def test_cleanup_orphan_apply_deletes_all(isolated_handoff_home):
    p = _setup_project(isolated_handoff_home)
    spawned = p["ack"] / "g2.spawned"
    spawned.write_text("(test)")
    submitted = p["ack"] / "g2.submitted"
    submitted.write_text("(test)")
    queued = p["ack"] / "g2.queued"
    queued.write_text("(test)")
    launched_txt = p["launched"] / "g2-12345.txt"
    launched_txt.write_text("(test)")
    blocked_md = p["queue"] / "g2.BLOCKED.md"
    blocked_md.write_text("# BLOCKED")

    args = SimpleNamespace(project=None, apply=True, kill_spawned=False)
    dump.handle_cleanup_orphan(args)

    assert not spawned.exists()
    assert not submitted.exists()
    assert not queued.exists()
    assert not launched_txt.exists()
    assert not blocked_md.exists()

    recovery_files = list((isolated_handoff_home / "_recovery").glob("orphans-*.json"))
    assert len(recovery_files) == 1
    record = json.loads(recovery_files[0].read_text())
    assert len(record) == 1
    assert record[0]["task"] == "g2"


def test_cleanup_orphan_apply_removes_old_ready_and_heartbeat(isolated_handoff_home):
    """Gap 2: --apply must also delete ack/<task>.old_ready + queue/<task>.heartbeat.

    A leaked heartbeat keeps ticking and watchdog mode 6 mis-flags the orphan as
    529-suspected; a leaked old_ready accumulates as stale audit metadata.
    """
    p = _setup_project(isolated_handoff_home)
    spawned = p["ack"] / "g2.spawned"
    spawned.write_text("(test)")
    old_ready = p["ack"] / "g2.old_ready"
    old_ready.write_text('{"task_id": "g2"}')
    heartbeat = p["queue"] / "g2.heartbeat"
    heartbeat.write_text("")

    args = SimpleNamespace(project=None, apply=True, kill_spawned=False)
    dump.handle_cleanup_orphan(args)

    assert not spawned.exists()
    assert not old_ready.exists()
    assert not heartbeat.exists()


def test_find_orphans_exposes_old_ready_and_heartbeat_paths(isolated_handoff_home):
    """Gap 2: the orphan dict carries the new residue paths for cleanup."""
    p = _setup_project(isolated_handoff_home)
    (p["ack"] / "g2.spawned").write_text("(test)")
    found = dump.find_orphans()
    assert len(found) == 1
    o = found[0]
    assert o["old_ready_path"] == p["ack"] / "g2.old_ready"
    assert o["heartbeat_path"] == p["queue"] / "g2.heartbeat"


def test_cleanup_orphan_no_orphans_no_recovery_record(isolated_handoff_home, capsys):
    _setup_project(isolated_handoff_home)
    args = SimpleNamespace(project=None, apply=True, kill_spawned=False)
    dump.handle_cleanup_orphan(args)
    out = capsys.readouterr().out
    assert "无孤儿残留" in out
    recovery = isolated_handoff_home / "_recovery"
    assert not recovery.exists() or not list(recovery.glob("*.json"))


# ─── C3: watchdog scan_orphan_spawns ─────────────────────────────────────────


def test_watchdog_orphan_scan_writes_blocked_md_after_grace(isolated_handoff_home):
    p = _setup_project(isolated_handoff_home)
    spawned = p["ack"] / "g2.spawned"
    spawned.write_text("(test)")
    _stale(spawned, seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)

    count = watchdog.scan_orphan_spawns()
    assert count == 1
    blocked_md = p["queue"] / "g2.BLOCKED.md"
    assert blocked_md.exists()
    body = blocked_md.read_text()
    assert "orphan" in body
    assert "g2" in body
    assert "watchdog mode 5" in body


def test_watchdog_orphan_scan_skips_within_grace(isolated_handoff_home):
    p = _setup_project(isolated_handoff_home)
    spawned = p["ack"] / "fresh.spawned"
    spawned.write_text("(test)")
    # Don't age — within grace window
    assert watchdog.scan_orphan_spawns() == 0
    assert not (p["queue"] / "fresh.BLOCKED.md").exists()


def test_watchdog_orphan_scan_skips_when_md_present(isolated_handoff_home):
    p = _setup_project(isolated_handoff_home)
    spawned = p["ack"] / "alive.spawned"
    spawned.write_text("(test)")
    _stale(spawned, seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)
    (p["queue"] / "alive.md").write_text("# task")
    assert watchdog.scan_orphan_spawns() == 0


def test_watchdog_orphan_scan_idempotent(isolated_handoff_home):
    """A second run must not re-flag the same orphan."""
    p = _setup_project(isolated_handoff_home)
    spawned = p["ack"] / "g2.spawned"
    spawned.write_text("(test)")
    _stale(spawned, seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)
    assert watchdog.scan_orphan_spawns() == 1
    # second pass: BLOCKED.md exists, so it's skipped
    assert watchdog.scan_orphan_spawns() == 0
