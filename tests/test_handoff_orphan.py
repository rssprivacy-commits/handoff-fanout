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


# ─── C3 / mode 5: unified-spawn (`handoff spawn`) recognition ─────────────────
# A unified spawn (spawn.py) writes a compact-JSON `queue/<task>.singlepane` sidecar
# + a `.uri`, and NEVER a `queue/<task>.md` (that is the legacy dump path's product).
# The orphan predicate is `.md`-missing, so EVERY unified-spawn worker running past
# the 300s grace was systematically mis-flagged. mode 5 must recognise these.


def _write_spawn_sidecar(
    queue: Path,
    task: str,
    *,
    workspace: Path,
    isolation: str = "worktree",
    role: str = "worker",
) -> Path:
    """Mirror ``spawn._write_sidecar``'s COMPACT single-line JSON shape (the watchdog
    contract): workspace / role / close_policy / spawn_nonce / isolation / predecessor_nonce."""
    sidecar = queue / f"{task}.singlepane"
    sidecar.write_text(
        json.dumps(
            {
                "workspace": str(workspace),
                "role": role,
                "close_policy": "keep",
                "spawn_nonce": "deadbeef",
                "isolation": isolation,
                "predecessor_nonce": None,
            }
        )
    )
    return sidecar


def test_watchdog_orphan_scan_skips_active_unified_spawn(isolated_handoff_home):
    """A LIVE ``handoff spawn`` worker — valid non-terminal sidecar + its workspace
    still present, NO ``queue/.md`` by contract — must NOT be mis-flagged as a legacy
    orphan (the bug: BLOCKED.md misled the owner into closing an active tab)."""
    p = _setup_project(isolated_handoff_home)
    ws = p["proj"] / "worktrees" / "u1" / ".handoff.code-workspace"
    ws.parent.mkdir(parents=True)
    ws.write_text("{}")  # the worker's open target still exists ⇒ a live dispatch
    spawned = p["ack"] / "u1.spawned"
    spawned.write_text("(test)")
    _write_spawn_sidecar(p["queue"], "u1", workspace=ws)
    _stale(spawned, seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)

    assert watchdog.scan_orphan_spawns() == 0
    assert not (p["queue"] / "u1.BLOCKED.md").exists()
    assert not (p["queue"] / "u1.stale-spawn").exists()


def test_watchdog_orphan_scan_marks_stale_unified_spawn_distinctly(isolated_handoff_home):
    """A unified-spawn dispatch whose workspace is GONE (torn-down worktree / leaked
    residue) must NOT be masked — it gets a DISTINCT ``.stale-spawn`` marker, never the
    owner-misleading orphan BLOCKED.md."""
    p = _setup_project(isolated_handoff_home)
    ws = p["proj"] / "worktrees" / "u2" / ".handoff.code-workspace"  # deliberately never created
    spawned = p["ack"] / "u2.spawned"
    spawned.write_text("(test)")
    _write_spawn_sidecar(p["queue"], "u2", workspace=ws)
    _stale(spawned, seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)

    watchdog.scan_orphan_spawns()
    assert not (p["queue"] / "u2.BLOCKED.md").exists()  # never the misleading 'close tab' marker
    stale = p["queue"] / "u2.stale-spawn"
    assert stale.exists()  # not masked — a visible, auditable residue
    assert "u2" in stale.read_text()


def test_watchdog_orphan_scan_stale_unified_spawn_idempotent(isolated_handoff_home):
    """A second pass must not re-write the stale unified-spawn residue marker."""
    p = _setup_project(isolated_handoff_home)
    ws = p["proj"] / "worktrees" / "u3" / ".handoff.code-workspace"  # gone
    spawned = p["ack"] / "u3.spawned"
    spawned.write_text("(test)")
    _write_spawn_sidecar(p["queue"], "u3", workspace=ws)
    _stale(spawned, seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)

    watchdog.scan_orphan_spawns()
    stale = p["queue"] / "u3.stale-spawn"
    first = stale.stat().st_mtime
    watchdog.scan_orphan_spawns()
    assert stale.stat().st_mtime == first  # not re-marked


def test_watchdog_orphan_scan_corrupt_sidecar_not_legacy_orphan(isolated_handoff_home):
    """A corrupt/unparseable sidecar is STILL evidence of a unified-spawn dispatch (the
    legacy dump path always writes a ``queue/.md``, absent here) — so it must NOT get the
    legacy orphan BLOCKED.md; it is treated as stale residue instead."""
    p = _setup_project(isolated_handoff_home)
    spawned = p["ack"] / "u4.spawned"
    spawned.write_text("(test)")
    (p["queue"] / "u4.singlepane").write_text("{ not valid json")
    _stale(spawned, seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)

    watchdog.scan_orphan_spawns()
    assert not (p["queue"] / "u4.BLOCKED.md").exists()
    assert (p["queue"] / "u4.stale-spawn").exists()


def test_watchdog_orphan_scan_mixed_active_and_legacy_orphan(isolated_handoff_home):
    """Zero-regression + per-task classification: in ONE scan, an active unified-spawn
    worker is skipped while a true legacy orphan (no sidecar) still gets its BLOCKED.md.
    The sidecar recognition only changes the ``no .md BUT sidecar present`` case — a
    sidecar-less ERP-style residue is detected exactly as before."""
    p = _setup_project(isolated_handoff_home)
    # active unified-spawn worker
    ws = p["proj"] / "worktrees" / "alive" / ".handoff.code-workspace"
    ws.parent.mkdir(parents=True)
    ws.write_text("{}")
    (p["ack"] / "alive.spawned").write_text("(test)")
    _write_spawn_sidecar(p["queue"], "alive", workspace=ws)
    _stale(p["ack"] / "alive.spawned", seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)
    # legacy orphan — no sidecar at all
    (p["ack"] / "legacy.spawned").write_text("(test)")
    _stale(p["ack"] / "legacy.spawned", seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)

    count = watchdog.scan_orphan_spawns()
    assert count == 1  # only the legacy orphan counts as an orphan
    assert (p["queue"] / "legacy.BLOCKED.md").exists()
    assert not (p["queue"] / "alive.BLOCKED.md").exists()
    assert not (p["queue"] / "alive.stale-spawn").exists()


# ─── fix1 MUST#1: the `.singlepane` sidecar is NOT spawn-exclusive ────────────
# dump.py's singlepane/coordinator path writes a `.singlepane` sidecar too — same keys
# but NO `isolation` field (that stamp is spawn.py-exclusive, see spawn._write_sidecar).
# A dump task's liveness contract IS its `queue/.md`; its sidecar + workspace routinely
# outlive a cleaned/crashed `.md`, so they must never mask the legacy orphan verdict.


def _write_dump_sidecar(
    queue: Path,
    task: str,
    *,
    workspace: Path,
    is_coordinator: bool = False,
) -> Path:
    """Mirror ``dump.maybe_write_singlepane_sidecar``'s payload: compact single-line JSON
    with workspace / role / close_policy / spawn_nonce / predecessor_nonce
    (+ ``is_coordinator`` only when true) and — crucially — NO ``isolation`` key."""
    payload: dict[str, object] = {
        "workspace": str(workspace),
        "role": "worker",
        "close_policy": "keep",
        "spawn_nonce": "cafebabe",
        "predecessor_nonce": None,
    }
    if is_coordinator:
        payload["is_coordinator"] = True
    sidecar = queue / f"{task}.singlepane"
    sidecar.write_text(json.dumps(payload))
    return sidecar


def test_watchdog_orphan_scan_dump_sidecar_missing_md_is_legacy_orphan(isolated_handoff_home):
    """Regression lock (codex MUST#1): a dump task whose ``.md`` is gone but whose
    dump-format sidecar + workspace file BOTH survive must still take the byte-identical
    legacy orphan path (BLOCKED.md + counted) — not be masked as an ACTIVE spawn."""
    p = _setup_project(isolated_handoff_home)
    ws = p["proj"] / ".handoff.code-workspace"
    ws.write_text("{}")  # survives the lost .md — proves nothing about dump-task liveness
    (p["ack"] / "d1.spawned").write_text("(test)")
    _write_dump_sidecar(p["queue"], "d1", workspace=ws)
    _stale(p["ack"] / "d1.spawned", seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)

    assert watchdog.scan_orphan_spawns() == 1
    assert (p["queue"] / "d1.BLOCKED.md").exists()
    assert not (p["queue"] / "d1.stale-spawn").exists()


def test_watchdog_orphan_scan_coordinator_dump_sidecar_is_legacy_orphan(isolated_handoff_home):
    """Coordinator dumps add ``is_coordinator: true`` to the same isolation-less payload;
    with the workspace gone they must fall to the legacy orphan path, not ``.stale-spawn``."""
    p = _setup_project(isolated_handoff_home)
    ws = p["proj"] / ".handoff.code-workspace"  # deliberately never created
    (p["ack"] / "d2.spawned").write_text("(test)")
    _write_dump_sidecar(p["queue"], "d2", workspace=ws, is_coordinator=True)
    _stale(p["ack"] / "d2.spawned", seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)

    assert watchdog.scan_orphan_spawns() == 1
    assert (p["queue"] / "d2.BLOCKED.md").exists()
    assert not (p["queue"] / "d2.stale-spawn").exists()


def test_watchdog_orphan_scan_unknown_isolation_value_falls_to_legacy(isolated_handoff_home):
    """``isolation`` is enum-locked to spawn.py's {worktree, singlepane}: any other value
    is no writer's legal product ⇒ not a recognised unified spawn ⇒ legacy orphan path."""
    p = _setup_project(isolated_handoff_home)
    ws = p["proj"] / "worktrees" / "d3" / ".handoff.code-workspace"
    ws.parent.mkdir(parents=True)
    ws.write_text("{}")
    (p["ack"] / "d3.spawned").write_text("(test)")
    _write_spawn_sidecar(p["queue"], "d3", workspace=ws, isolation="tmux")
    _stale(p["ack"] / "d3.spawned", seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)

    assert watchdog.scan_orphan_spawns() == 1
    assert (p["queue"] / "d3.BLOCKED.md").exists()
    assert not (p["queue"] / "d3.stale-spawn").exists()


def test_watchdog_orphan_scan_skips_active_singlepane_isolation_spawn(isolated_handoff_home):
    """The OTHER valid spawn ``isolation`` value ("singlepane") must also be recognised as
    a unified spawn — a live singlepane worker is skipped, never legacy-routed."""
    p = _setup_project(isolated_handoff_home)
    ws = p["proj"] / ".handoff.code-workspace"
    ws.write_text("{}")  # singlepane spawns target the real repo's workspace file
    (p["ack"] / "s1.spawned").write_text("(test)")
    _write_spawn_sidecar(p["queue"], "s1", workspace=ws, isolation="singlepane")
    _stale(p["ack"] / "s1.spawned", seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)

    assert watchdog.scan_orphan_spawns() == 0
    assert not (p["queue"] / "s1.BLOCKED.md").exists()
    assert not (p["queue"] / "s1.stale-spawn").exists()


# ─── fix1 MUST#2: `.stale-spawn` marker is either ABSENT or COMPLETE ──────────
# The old two-step (atomic_create empty file → write content) could crash in between,
# leaving a permanent EMPTY marker that made every later tick early-return — losing the
# cleanup note (the marker's entire value) forever.


def test_watchdog_stale_spawn_marker_absent_after_write_failure(isolated_handoff_home, monkeypatch):
    """If landing the marker content fails, NOTHING may remain at the marker path (no
    empty-file residue, no temp litter) — and the next healthy tick writes the full note."""
    p = _setup_project(isolated_handoff_home)
    ws = p["proj"] / "worktrees" / "m1" / ".handoff.code-workspace"  # gone ⇒ STALE
    (p["ack"] / "m1.spawned").write_text("(test)")
    _write_spawn_sidecar(p["queue"], "m1", workspace=ws)
    _stale(p["ack"] / "m1.spawned", seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)

    def _boom(src, dst):
        raise OSError("simulated crash while landing the marker")

    # context(): scoped patch — a bare monkeypatch.undo() would also tear down the
    # isolated_handoff_home fixture's HANDOFF_HOME patch (same function-scoped instance)
    # and point the recovery scan below at the REAL user tree.
    with monkeypatch.context() as mp:
        mp.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            watchdog.scan_orphan_spawns()

    marker = p["queue"] / "m1.stale-spawn"
    assert not marker.exists()  # either absent or complete — never an empty marker
    assert not list(p["queue"].glob(".m1.stale-spawn.tmp.*"))  # temp cleaned up too

    watchdog.scan_orphan_spawns()  # nothing was poisoned: the next tick lands the note
    assert "task_id: m1" in marker.read_text()


def test_watchdog_stale_spawn_empty_marker_residue_is_healed(isolated_handoff_home):
    """A zero-byte ``.stale-spawn`` (exactly the residue the old create-then-write crash
    left behind) must not wedge: the next tick rewrites the COMPLETE note instead of
    early-returning forever on mere existence."""
    p = _setup_project(isolated_handoff_home)
    ws = p["proj"] / "worktrees" / "m2" / ".handoff.code-workspace"  # gone ⇒ STALE
    (p["ack"] / "m2.spawned").write_text("(test)")
    _write_spawn_sidecar(p["queue"], "m2", workspace=ws)
    _stale(p["ack"] / "m2.spawned", seconds_ago=watchdog.ORPHAN_GRACE_SECONDS + 60)
    marker = p["queue"] / "m2.stale-spawn"
    marker.write_text("")  # pre-fix crash residue: marker exists but the note is lost

    watchdog.scan_orphan_spawns()
    text = marker.read_text()
    assert "task_id: m2" in text
    assert "## Cleanup" in text and "rm " in text  # the cleanup note IS the marker's value
