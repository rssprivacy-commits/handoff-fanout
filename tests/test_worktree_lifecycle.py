"""Worker worktree lifecycle state machine (§5.2/§5.3 of the spawn-window design).

Task 3.1 — ``WorktreeState`` enum + ``is_reclaimable_orphan`` (the 3-condition orphan
gate). Task 3.2 — ``add_worktree_or_reclaim_orphan`` (branch/worktree collision →
reclaim a confirmed orphan + rebuild, else fail-closed ``WorktreeConflict``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from handoff_fanout import worktree as wt
from handoff_fanout.worktree import WorktreeState, is_reclaimable_orphan

# ─── Task 3.1: 3-condition orphan gate ───────────────────────────────────────


def test_orphan_needs_all_three_conditions():
    # 孤儿 = 进程死 ∧ 不在 pending 队列 ∧ 状态∈{merged,abandoned}
    assert (
        is_reclaimable_orphan(proc_alive=False, in_pending_queue=False, state=WorktreeState.MERGED)
        is True
    )
    assert (
        is_reclaimable_orphan(proc_alive=True, in_pending_queue=False, state=WorktreeState.MERGED)
        is False
    )  # 进程活
    assert (
        is_reclaimable_orphan(
            proc_alive=False, in_pending_queue=True, state=WorktreeState.ABANDONED
        )
        is False
    )  # 在队列
    assert (
        is_reclaimable_orphan(proc_alive=False, in_pending_queue=False, state=WorktreeState.ACTIVE)
        is False
    )  # active 绝不回收
    assert (
        is_reclaimable_orphan(
            proc_alive=False, in_pending_queue=False, state=WorktreeState.AWAITING_MERGE
        )
        is False
    )  # 业务层 merge-back 管


def test_abandoned_orphan_is_reclaimable():
    assert (
        is_reclaimable_orphan(
            proc_alive=False, in_pending_queue=False, state=WorktreeState.ABANDONED
        )
        is True
    )


def test_creating_state_never_reclaimed():
    # CREATING = mid-spawn; reclaiming it would race the very session being born.
    assert (
        is_reclaimable_orphan(
            proc_alive=False, in_pending_queue=False, state=WorktreeState.CREATING
        )
        is False
    )


def test_worktree_state_values_stable():
    # Persisted in sidecars/markers → string values must stay stable.
    assert {s.value for s in WorktreeState} == {
        "creating",
        "active",
        "awaiting-merge",
        "merged",
        "abandoned",
    }


# ─── Task 3.2: branch-conflict reclaim-or-fail-closed ─────────────────────────


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _bare_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare ``origin`` (default branch main) + a working clone on main, pushed.

    ``origin/main`` resolves → it can be the worktree base ref.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", str(bare), str(ws)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t.test"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _run(["git", "config", k, v], ws)
    (ws / "README.md").write_text("base\n")
    _run(["git", "add", "."], ws)
    _run(["git", "commit", "-qm", "init"], ws)
    _run(["git", "push", "-q", "origin", "main"], ws)
    return bare, ws


def test_branch_conflict_reclaims_orphan_and_rebuilds(tmp_path):
    """① 旧物=可回收孤儿 → 移除重建成功 (no raise; worktree rebuilt)."""
    _, ws = _bare_and_clone(tmp_path)
    branch = "handoff/t1"
    wt_dir = tmp_path / "wt_t1"
    # Pre-create a colliding worktree + branch (simulates a prior spawn's leftover).
    _run(["git", "worktree", "add", "-b", branch, str(wt_dir), "origin/main"], ws)
    assert wt_dir.exists()
    leftover = wt_dir / "stale.txt"
    leftover.write_text("stale work from the dead session")

    # Confirmed orphan (proc dead ∧ not queued ∧ MERGED) → reclaim + rebuild, no raise.
    wt.add_worktree_or_reclaim_orphan(
        source_workspace=ws,
        wt=wt_dir,
        branch=branch,
        base_ref="origin/main",
        proc_alive=False,
        in_pending_queue=False,
        state=WorktreeState.MERGED,
    )
    # Rebuilt: dir exists, on a fresh checkout (the stale leftover is gone), branch resolves.
    assert wt_dir.exists()
    assert not leftover.exists()
    assert wt.branch_head(ws, branch) is not None


def test_branch_only_conflict_reclaims_orphan(tmp_path):
    """A lingering branch (no worktree dir) that is an orphan → reclaimed + rebuilt."""
    _, ws = _bare_and_clone(tmp_path)
    branch = "handoff/t2"
    wt_dir = tmp_path / "wt_t2"
    _run(["git", "branch", branch, "origin/main"], ws)  # branch exists, NO worktree dir
    assert wt.branch_head(ws, branch) is not None
    assert not wt_dir.exists()

    wt.add_worktree_or_reclaim_orphan(
        source_workspace=ws,
        wt=wt_dir,
        branch=branch,
        base_ref="origin/main",
        proc_alive=False,
        in_pending_queue=False,
        state=WorktreeState.ABANDONED,
    )
    assert wt_dir.exists()
    assert wt.branch_head(ws, branch) is not None


def test_branch_conflict_active_fails_closed(tmp_path):
    """② 旧物=active/活会话 → fail-closed 抛 WorktreeConflict,不静默复用、不删。"""
    _, ws = _bare_and_clone(tmp_path)
    branch = "handoff/t3"
    wt_dir = tmp_path / "wt_t3"
    _run(["git", "worktree", "add", "-b", branch, str(wt_dir), "origin/main"], ws)
    keep = wt_dir / "live_work.txt"
    keep.write_text("a live session is editing this")

    with pytest.raises(wt.WorktreeConflict):
        wt.add_worktree_or_reclaim_orphan(
            source_workspace=ws,
            wt=wt_dir,
            branch=branch,
            base_ref="origin/main",
            proc_alive=True,  # live owner process
            in_pending_queue=False,
            state=WorktreeState.ACTIVE,
        )
    # Retained, untouched — the live session's work is intact.
    assert wt_dir.exists()
    assert keep.read_text() == "a live session is editing this"


def test_queued_task_conflict_fails_closed(tmp_path):
    """In-pending-queue collision → fail-closed even if process looks dead + state terminal."""
    _, ws = _bare_and_clone(tmp_path)
    branch = "handoff/t4"
    wt_dir = tmp_path / "wt_t4"
    _run(["git", "worktree", "add", "-b", branch, str(wt_dir), "origin/main"], ws)
    with pytest.raises(wt.WorktreeConflict):
        wt.add_worktree_or_reclaim_orphan(
            source_workspace=ws,
            wt=wt_dir,
            branch=branch,
            base_ref="origin/main",
            proc_alive=False,
            in_pending_queue=True,  # still queued → not an orphan
            state=WorktreeState.MERGED,
        )
    assert wt_dir.exists()


def test_no_collision_just_adds(tmp_path):
    """No pre-existing branch/worktree → plain add succeeds, no raise."""
    _, ws = _bare_and_clone(tmp_path)
    wt_dir = tmp_path / "wt_fresh"
    wt.add_worktree_or_reclaim_orphan(
        source_workspace=ws,
        wt=wt_dir,
        branch="handoff/fresh",
        base_ref="origin/main",
        proc_alive=False,
        in_pending_queue=False,
        state=WorktreeState.MERGED,
    )
    assert wt_dir.exists()
    assert wt.branch_head(ws, "handoff/fresh") is not None


def test_non_collision_failure_raises_add_error_not_conflict(tmp_path):
    """An environmental (non-collision) add failure raises WorktreeAddError — NOT
    WorktreeConflict — and never reclaims (caller degrades, doesn't treat as clash)."""
    _, ws = _bare_and_clone(tmp_path)
    wt_dir = tmp_path / "wt_badref"
    with pytest.raises(wt.WorktreeAddError):
        wt.add_worktree_or_reclaim_orphan(
            source_workspace=ws,
            wt=wt_dir,
            branch="handoff/badref",
            base_ref="origin/does-not-exist",  # unresolvable → not a name clash
            proc_alive=False,
            in_pending_queue=False,
            state=WorktreeState.MERGED,
        )
    # WorktreeAddError is not a WorktreeConflict (distinct caller handling).
    assert not issubclass(wt.WorktreeAddError, wt.WorktreeConflict)
