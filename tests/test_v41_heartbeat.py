"""v4.1 single-task heartbeat — template injection + watchdog mode 6.

Root cause (主人 5/29 'API Error 会话裸跑'): v4.1 build_handoff_md emitted a
spawn prompt without any heartbeat instruction, so when the new tab wedged
on 529 / API Error there was nothing for the watchdog to notice. mode 4
only covered fan-out sub-tasks.

This module nails the symmetry shut:

  * ``build_handoff_md`` Step 1 now writes ``queue/<task>.heartbeat`` every 60s
  * ``watchdog.scan_single_task_heartbeats`` (mode 6) marks stale heartbeats
    ``queue/<task>.529-suspected``

Both halves are exercised here — drift on either side reopens the gap.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from handoff_fanout import templates, watchdog


# ─── A: build_handoff_md heartbeat injection ─────────────────────────────────


def _render_handoff(task: str = "fix-foo", project: str = "demo") -> str:
    return templates.build_handoff_md(
        task=task,
        project=project,
        workspace=Path("/tmp/ws"),
        next_brief="do the thing",
        status="active",
        tests=None,
        baseline={"git_head": "abc123", "last_3_commits": "abc123 do thing\n"},
        roadmap_excerpt="(none)",
        inject_blocks=[],
        handoff_home=Path("/home/x/.claude-handoff"),
        handoff_md_path=Path("/home/x/.claude-handoff/demo/queue/fix-foo.md"),
    )


def test_build_handoff_md_contains_heartbeat_step():
    md = _render_handoff()
    assert "第一步: 启动 heartbeat" in md
    assert "/home/x/.claude-handoff/demo/queue/fix-foo.heartbeat" in md
    assert "sleep 60" in md


def test_build_handoff_md_baseline_renumbered_to_step_two():
    md = _render_handoff()
    assert "第二步: Baseline 验证" in md
    assert md.index("第一步: 启动 heartbeat") < md.index("第二步: Baseline 验证")


def test_build_handoff_md_heartbeat_pid_kill_hint():
    """The kill hint matters — without it, sessions leave background pids around."""
    md = _render_handoff(task="task-x")
    assert "/tmp/heartbeat-task-x.pid" in md
    assert "kill $(cat /tmp/heartbeat-task-x.pid)" in md


# ─── B: watchdog mode 6 scan ────────────────────────────────────────────────


def _stale(path: Path, seconds_ago: float) -> None:
    new_t = time.time() - seconds_ago
    os.utime(path, (new_t, new_t))


def _setup_queue(root: Path, project: str = "demo") -> Path:
    queue = root / project / "queue"
    queue.mkdir(parents=True)
    return queue


def test_mode6_marks_529_when_heartbeat_stale(isolated_handoff_home):
    queue = _setup_queue(isolated_handoff_home)
    (queue / "task-a.md").write_text("# task")
    hb = queue / "task-a.heartbeat"
    hb.write_text("")
    _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    count = watchdog.scan_single_task_heartbeats()
    assert count == 1
    marker = queue / "task-a.529-suspected"
    assert marker.exists()
    body = marker.read_text()
    assert "task-a" in body
    assert "heartbeat stale" in body


def test_mode6_skips_when_heartbeat_fresh(isolated_handoff_home):
    queue = _setup_queue(isolated_handoff_home)
    (queue / "task-b.md").write_text("# task")
    (queue / "task-b.heartbeat").write_text("")  # fresh mtime

    assert watchdog.scan_single_task_heartbeats() == 0
    assert not (queue / "task-b.529-suspected").exists()


def test_mode6_skips_when_md_missing(isolated_handoff_home):
    """No .md means task is already gone — heartbeat is leftover noise."""
    queue = _setup_queue(isolated_handoff_home)
    hb = queue / "ghost.heartbeat"
    hb.write_text("")
    _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    assert watchdog.scan_single_task_heartbeats() == 0
    assert not (queue / "ghost.529-suspected").exists()


def test_mode6_skips_when_done(isolated_handoff_home):
    queue = _setup_queue(isolated_handoff_home)
    (queue / "task-c.md").write_text("# task")
    (queue / "task-c.done").write_text("")
    hb = queue / "task-c.heartbeat"
    hb.write_text("")
    _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    assert watchdog.scan_single_task_heartbeats() == 0


def test_mode6_skips_when_blocked(isolated_handoff_home):
    queue = _setup_queue(isolated_handoff_home)
    (queue / "task-d.md").write_text("# task")
    (queue / "task-d.BLOCKED.md").write_text("blocked")
    hb = queue / "task-d.heartbeat"
    hb.write_text("")
    _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    assert watchdog.scan_single_task_heartbeats() == 0


def test_mode6_idempotent(isolated_handoff_home):
    """Second scan must not re-flag the same task — atomic_create on marker."""
    queue = _setup_queue(isolated_handoff_home)
    (queue / "task-e.md").write_text("# task")
    hb = queue / "task-e.heartbeat"
    hb.write_text("")
    _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    assert watchdog.scan_single_task_heartbeats() == 1
    assert watchdog.scan_single_task_heartbeats() == 0  # marker exists


def test_mode6_cross_project_independent(isolated_handoff_home):
    qa = _setup_queue(isolated_handoff_home, "proj-a")
    qb = _setup_queue(isolated_handoff_home, "proj-b")
    for q in (qa, qb):
        (q / "t.md").write_text("# t")
        hb = q / "t.heartbeat"
        hb.write_text("")
        _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    assert watchdog.scan_single_task_heartbeats() == 2
    assert (qa / "t.529-suspected").exists()
    assert (qb / "t.529-suspected").exists()


def test_mode6_skips_special_dirs(isolated_handoff_home):
    """locks/ and _recovery/ are not project directories."""
    for special in ("locks", "_recovery"):
        d = isolated_handoff_home / special / "queue"
        d.mkdir(parents=True)
        (d / "t.md").write_text("# t")
        hb = d / "t.heartbeat"
        hb.write_text("")
        _stale(hb, seconds_ago=watchdog.SUB_TASK_HEARTBEAT_STALE_SECONDS + 60)

    assert watchdog.scan_single_task_heartbeats() == 0
