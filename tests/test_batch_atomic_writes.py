"""Gap 1 regression — batch fan-out must write launcher-visible files atomically.

The launchd WatchPaths watcher tails ``queue/*.uri`` and the spawned session
reads ``queue/*.md``; an in-place ``O_TRUNC`` write (``write_with_fsync``) exposes
a window where a reader observes a truncated/partial file. The single-task path
already uses ``atomic_replace`` (temp + ``os.replace``) for exactly this reason.

These tests pin that EVERY launcher-visible ``.uri``/``.md`` produced by the three
batch fan-out producers — ``handle_open_batch``, ``trigger_fan_in_if_ready``, and
the watchdog's ``_dump_degraded_fan_in`` — goes through ``atomic_replace``, while
non-launcher files (``.env``, ``manifest.json``) may stay ``write_with_fsync``.

Pure filesystem + a real throwaway git repo; no external services.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from handoff_fanout import atomic, dump, watchdog
from handoff_fanout import config as _config


def _init_git(ws: Path) -> None:
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=ws, check=True)
    (ws / "a.py").write_text("a\n")
    (ws / "b.py").write_text("b\n")
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)


def _manifest(batch_id: str = "test-batch") -> dict:
    return {
        "schema_version": dump.SCHEMA_VERSION,
        "batch_id": batch_id,
        "fan_in_task": "test-fanin",
        "sub_tasks": [
            {
                "id": "sub-a",
                "brief": "do a",
                "file_ownership": [{"type": "exact", "path": "a.py"}],
                "depends_on": [],
            },
            {
                "id": "sub-b",
                "brief": "do b",
                "file_ownership": [{"type": "exact", "path": "b.py"}],
                "depends_on": [],
            },
        ],
    }


@pytest.fixture
def record_atomic(monkeypatch):
    """Record (primitive, path) for every atomic write, delegating to the real impl."""
    calls: list[tuple[str, Path]] = []
    real_replace = atomic.atomic_replace
    real_fsync = atomic.write_with_fsync

    def rec_replace(path, content):
        calls.append(("replace", Path(path)))
        return real_replace(path, content)

    def rec_fsync(path, content):
        calls.append(("fsync", Path(path)))
        return real_fsync(path, content)

    monkeypatch.setattr(atomic, "atomic_replace", rec_replace)
    monkeypatch.setattr(atomic, "write_with_fsync", rec_fsync)
    # The 30s inter-spawn stagger would make the test crawl; collapse it.
    monkeypatch.setattr(dump, "STAGGER_SPAWN_SECONDS", 0)
    return calls


def _assert_launcher_files_atomic(calls: list[tuple[str, Path]]) -> None:
    """Every queue/*.uri and queue/*.md must have been written via atomic_replace."""
    launcher = [
        (prim, p)
        for prim, p in calls
        if p.parent.name == "queue" and p.suffix in (".uri", ".md")
    ]
    assert launcher, "expected at least one launcher-visible write"
    for prim, p in launcher:
        assert prim == "replace", f"{p.name} written via {prim}, expected atomic_replace"


def test_open_batch_launcher_files_use_atomic_replace(
    tmp_path, isolated_handoff_home, record_atomic
):
    ws = tmp_path / "ws"
    ws.mkdir()
    _init_git(ws)
    project = ws.name

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")

    rc = dump.main(["--open-batch", str(manifest_path), "--project", project, "--workspace", str(ws)])
    assert rc == 0

    queue_dir = isolated_handoff_home / project / "queue"
    # All four sub-task launcher files exist and went via atomic_replace.
    for name in ("sub-a.md", "sub-a.uri", "sub-b.md", "sub-b.uri"):
        assert (queue_dir / name).exists()
    _assert_launcher_files_atomic(record_atomic)

    # Non-launcher artifacts (manifest.json, *.env) may stay write_with_fsync.
    fsync_paths = {p.name for prim, p in record_atomic if prim == "fsync"}
    assert "manifest.json" in fsync_paths
    assert any(name.endswith(".env") for name in fsync_paths)

    # No leftover atomic_replace temp residue in the queue dir.
    assert not list(queue_dir.glob(".*.tmp.*"))


def test_trigger_fan_in_launcher_files_use_atomic_replace(
    tmp_path, isolated_handoff_home, record_atomic
):
    ws = tmp_path / "ws"
    ws.mkdir()
    _init_git(ws)
    project = ws.name

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    assert dump.main(["--open-batch", str(manifest_path), "--project", project, "--workspace", str(ws)]) == 0

    cfg = _config.load()
    queue_dir = cfg.queue_dir(project)
    batch_dir = dump.handoff_root() / project / "batches" / "test-batch"
    # Mark both sub-tasks done so the fan-in trigger fires.
    (batch_dir / "sub-a.done").write_text("done\n")
    (batch_dir / "sub-b.done").write_text("done\n")

    record_atomic.clear()  # focus on the fan-in writes only
    fired = dump.trigger_fan_in_if_ready(project, ws, "test-batch", queue_dir, cfg=cfg)
    assert fired is True

    assert (queue_dir / "test-fanin.md").exists()
    assert (queue_dir / "test-fanin.uri").exists()
    _assert_launcher_files_atomic(record_atomic)
    assert not list(queue_dir.glob(".*.tmp.*"))


def test_watchdog_degraded_fan_in_uses_atomic_replace(
    tmp_path, isolated_handoff_home, record_atomic
):
    ws = tmp_path / "ws"
    ws.mkdir()
    _init_git(ws)
    project = ws.name

    cfg = _config.load()
    batch_dir = dump.handoff_root() / project / "batches" / "test-batch"
    batch_dir.mkdir(parents=True)
    manifest = _manifest()

    record_atomic.clear()
    watchdog._dump_degraded_fan_in(
        cfg,
        project,
        ws,
        "test-batch",
        manifest,
        done={"sub-a"},
        blocked=set(),
        missing={"sub-b"},
    )

    queue_dir = cfg.queue_dir(project)
    assert (queue_dir / "test-fanin-watchdog.md").exists()
    assert (queue_dir / "test-fanin-watchdog.uri").exists()
    _assert_launcher_files_atomic(record_atomic)
    assert not list(queue_dir.glob(".*.tmp.*"))
