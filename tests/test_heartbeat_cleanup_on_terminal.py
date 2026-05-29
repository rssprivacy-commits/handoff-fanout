"""Terminal-state heartbeat cleanup — dump must not leak ``.heartbeat`` files.

Observed in erp-system on 2026-05-29: two tasks
(``publish-handoff-fanout-v131-pypi``, ``fix-26-batch6-sibling-test-flake``)
had a ``.done`` marker yet their ``queue/<task>.heartbeat`` was still on disk
6h+ later. A leaked heartbeat keeps reading stale, so watchdog mode 4/6
mis-flags an already-finished task as ``529-suspected`` and (with enforcement
on) could even hunt PIDs for a task that completed cleanly.

Root cause: ``build_handoff_md`` Step 1 / ``build_sub_task_handoff_md`` Step 2
touch a heartbeat every 60s, but the four terminal paths in ``dump`` only
removed the ``.uri`` sidecar — never the heartbeat. These tests pin the
cleanup on all four: single-task done/blocked + sub-task batch done/blocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from handoff_fanout import config as _config
from handoff_fanout import dump

PROJECT = "demo"


def _git_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=ws, check=True)
    (ws / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)
    return ws


def _queue(home: Path) -> Path:
    q = home / PROJECT / "queue"
    q.mkdir(parents=True, exist_ok=True)
    return q


def _run_single(home: Path, ws: Path, task: str, status: str, monkeypatch) -> int:
    monkeypatch.delenv("HANDOFF_RETRO_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_RETRO_BYPASS", raising=False)
    return dump.main(
        [
            "--task",
            task,
            "--next",
            "next thing",
            "--project",
            PROJECT,
            "--workspace",
            str(ws),
            "--status",
            status,
        ],
    )


def test_single_task_done_removes_heartbeat(isolated_handoff_home, tmp_path, monkeypatch):
    ws = _git_workspace(tmp_path)
    queue = _queue(isolated_handoff_home)
    task = "publish-handoff-fanout-v131-pypi"
    hb = queue / f"{task}.heartbeat"
    hb.write_text("")

    rc = _run_single(isolated_handoff_home, ws, task, "done", monkeypatch)

    assert rc == 0
    assert (queue / f"{task}.done").exists()
    assert not hb.exists(), "done path leaked the heartbeat"


def test_single_task_blocked_removes_heartbeat(isolated_handoff_home, tmp_path, monkeypatch):
    ws = _git_workspace(tmp_path)
    queue = _queue(isolated_handoff_home)
    task = "fix-26-batch6-sibling-test-flake"
    hb = queue / f"{task}.heartbeat"
    hb.write_text("")

    rc = _run_single(isolated_handoff_home, ws, task, "blocked", monkeypatch)

    assert rc == 0
    assert (queue / f"{task}.BLOCKED.md").exists()
    assert not hb.exists(), "blocked path leaked the heartbeat"


def test_single_task_done_no_heartbeat_is_noop(isolated_handoff_home, tmp_path, monkeypatch):
    """unlink(missing_ok=True): completing a task that never wrote a heartbeat is fine."""
    ws = _git_workspace(tmp_path)
    queue = _queue(isolated_handoff_home)
    task = "task-without-heartbeat"

    rc = _run_single(isolated_handoff_home, ws, task, "done", monkeypatch)

    assert rc == 0
    assert (queue / f"{task}.done").exists()
    assert not (queue / f"{task}.heartbeat").exists()


def _batch_dir(home: Path, batch_id: str) -> Path:
    d = home / PROJECT / "batches" / batch_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_batch_done_removes_sub_task_heartbeat(isolated_handoff_home, tmp_path):
    """Sub-task heartbeat lives in batch_dir; batch-done must drop it.

    No manifest is written, so ``trigger_fan_in_if_ready`` returns False
    harmlessly — the heartbeat cleanup runs before it regardless.
    """
    cfg = _config.load()
    queue = _queue(isolated_handoff_home)
    batch_id = "batch-x"
    batch_dir = _batch_dir(isolated_handoff_home, batch_id)
    sub_task_id = "sub-foo"
    hb = batch_dir / f"{sub_task_id}.heartbeat"
    hb.write_text("")

    args = SimpleNamespace(batch_id=batch_id, task=f"{sub_task_id}-done", next_brief="done")
    rc = dump.handle_batch_done(args, cfg, tmp_path, PROJECT, queue)

    assert rc == 0
    assert (batch_dir / f"{sub_task_id}.done").exists()
    assert not hb.exists(), "batch-done leaked the sub-task heartbeat"


def test_batch_blocked_removes_sub_task_heartbeat(isolated_handoff_home, tmp_path):
    cfg = _config.load()
    queue = _queue(isolated_handoff_home)
    batch_id = "batch-y"
    batch_dir = _batch_dir(isolated_handoff_home, batch_id)
    sub_task_id = "sub-bar"
    hb = batch_dir / f"{sub_task_id}.heartbeat"
    hb.write_text("")

    args = SimpleNamespace(batch_id=batch_id, task=f"{sub_task_id}-blocked", blocked_reason="stuck")
    rc = dump.handle_batch_blocked(args, cfg, tmp_path, PROJECT, queue)

    assert rc == 0
    assert (batch_dir / f"{sub_task_id}.blocked").exists()
    assert not hb.exists(), "batch-blocked leaked the sub-task heartbeat"
