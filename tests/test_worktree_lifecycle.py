"""Worker worktree lifecycle state machine (§5.2/§5.3 of the spawn-window design).

Task 3.1 — ``WorktreeState`` enum + ``is_reclaimable_orphan`` (the 3-condition orphan
gate). Task 3.2 — ``add_worktree_or_reclaim_orphan`` (branch/worktree collision →
reclaim a confirmed orphan + rebuild, else fail-closed ``WorktreeConflict``).
"""

from __future__ import annotations

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
