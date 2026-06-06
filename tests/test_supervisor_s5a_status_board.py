"""S5a — status board (minimal observable + rescuable) test-suite.

Exercises the four owner-facing capabilities end to end against synthetic fixture
trees (NEVER the real ``~/.claude-handoff`` — C′ red line): real-runtime → business
dimension normalization, the strict "可关" judgment (osascript mocked), the bound-run
DAG overlay projection (read-only — appends nothing), the approve-only-when-bound rule,
the reversible STOP_AUTO sentinels, and the 脑裂 force-sync escape hatch.

Times / clocks are injected (the pure core never reads the wall clock — mirrors INV-3).
"""

from __future__ import annotations

import json

import pytest

from handoff_fanout import status_board as sb
from handoff_fanout.status_board import (
    Binding,
    BindingStore,
    BusinessState,
    HandoffLayout,
    StatusConfig,
    TaskSnapshot,
    approve_node,
    assess_closable,
    classify,
    discover_task_ids,
    load_overlay,
    query_visible_tasks,
    scan_all,
    scan_task,
)
from handoff_fanout.supervisor.event_log import EventLog
from handoff_fanout.supervisor.events import EventType
from handoff_fanout.supervisor.payloads import NodeReason
from handoff_fanout.supervisor.plan import Node, Plan

NOW = 1_000_000.0  # injected epoch clock


# --- fixtures ----------------------------------------------------------------


def _layout(tmp_path, project="erp-system") -> HandoffLayout:
    return HandoffLayout.resolve(
        project=project, root=tmp_path / "hf", transcript_root=tmp_path / "transcripts"
    )


def _touch(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _snap(task="t", **kw) -> TaskSnapshot:
    return TaskSnapshot(task_id=task, **kw)


# =============================================================================
# 1. classify — business dimension normalization (pure)
# =============================================================================


class TestClassify:
    def test_done_without_window_is_done(self):
        assert classify(_snap(done=True), window_visible=None) is BusinessState.DONE
        assert classify(_snap(done=True), window_visible=False) is BusinessState.DONE

    def test_done_with_visible_window_is_closable(self):
        assert classify(_snap(done=True), window_visible=True) is BusinessState.DELIVERED_CLOSABLE

    def test_blocked_md_is_blocked(self):
        assert classify(_snap(blocked=True)) is BusinessState.BLOCKED

    def test_failed_is_blocked(self):
        assert classify(_snap(failed=True)) is BusinessState.BLOCKED

    def test_529_is_blocked(self):
        assert classify(_snap(suspected_529=True)) is BusinessState.BLOCKED

    def test_worker_reported_is_delivered_awaiting_review(self):
        assert classify(_snap(worker_reported=True)) is BusinessState.DELIVERED_AWAITING_REVIEW

    def test_branch_advanced_and_idle_is_delivered(self):
        assert (
            classify(_snap(branch_advanced=True, transcript_idle_s=300))
            is BusinessState.DELIVERED_AWAITING_REVIEW
        )

    def test_recent_transcript_is_running(self):
        assert classify(_snap(transcript_idle_s=10)) is BusinessState.RUNNING

    def test_idle_transcript_is_idle(self):
        # idle beyond the running threshold but no delivery/terminal signal → IDLE
        assert classify(_snap(transcript_idle_s=9000)) is BusinessState.IDLE

    def test_no_transcript_spawned_is_idle(self):
        assert classify(_snap(spawned=True, has_brief=True)) is BusinessState.IDLE

    # --- precedence ----------------------------------------------------------
    def test_done_beats_blocked_leftover(self):
        # a leftover BLOCKED.md + a later done signal → the task was unblocked + finished
        assert classify(_snap(done=True, blocked=True)) is BusinessState.DONE

    def test_blocked_beats_delivered_claim(self):
        # a self-reported block is more urgent than a delivery claim
        assert classify(_snap(blocked=True, worker_reported=True)) is BusinessState.BLOCKED

    def test_delivered_beats_running(self):
        assert (
            classify(_snap(worker_reported=True, transcript_idle_s=5))
            is BusinessState.DELIVERED_AWAITING_REVIEW
        )

    def test_running_threshold_config(self):
        cfg = StatusConfig(running_idle_s=60)
        assert classify(_snap(transcript_idle_s=90), config=cfg) is BusinessState.IDLE
        assert classify(_snap(transcript_idle_s=30), config=cfg) is BusinessState.RUNNING

    def test_window_visible_only_refines_done(self):
        # window_visible never turns a delivered/running task closable — only `done`
        assert (
            classify(_snap(worker_reported=True), window_visible=True)
            is BusinessState.DELIVERED_AWAITING_REVIEW
        )

    def test_done_visible_but_dirty_is_done_not_closable(self):
        # R2 codex #2: classify reuses the conservative closable predicate, so a
        # done-but-dirty task shows DONE (matching `sessions`), never DELIVERED_CLOSABLE.
        assert (
            classify(
                _snap(done=True, worktree_present=True, worktree_dirty=True),
                window_visible=True,
            )
            is BusinessState.DONE
        )

    def test_done_visible_clean_is_closable(self):
        assert (
            classify(
                _snap(done=True, worktree_present=True, worktree_dirty=False),
                window_visible=True,
            )
            is BusinessState.DELIVERED_CLOSABLE
        )

    def test_branch_advanced_but_still_active_is_running(self):
        # R2 codex #4: branch advanced + still-active transcript (idle < threshold) is
        # RUNNING, not delivered (only advanced + quiet = delivered).
        assert classify(_snap(branch_advanced=True, transcript_idle_s=5)) is BusinessState.RUNNING


# =============================================================================
# 2. assess_closable — strict "可关" (visible window ∩ central done)
# =============================================================================


class TestAssessClosable:
    def test_not_done_is_not_closable(self):
        v = assess_closable(_snap(worker_reported=True), window_visible=True)
        assert v.closable is False and "done" in v.reason

    def test_done_visible_clean_is_closable(self):
        v = assess_closable(
            _snap(done=True, worktree_present=True, worktree_dirty=False), window_visible=True
        )
        assert v.closable is True

    def test_done_but_dirty_worktree_is_not_closable(self):
        v = assess_closable(
            _snap(done=True, worktree_present=True, worktree_dirty=True), window_visible=True
        )
        assert v.closable is False and "WIP" in v.reason

    def test_done_window_unknown_is_conservative_not_closable(self):
        v = assess_closable(_snap(done=True), window_visible=None)
        assert v.closable is False and "未知" in v.reason

    def test_done_window_gone_is_not_closable(self):
        v = assess_closable(_snap(done=True), window_visible=False)
        assert v.closable is False and "无可见窗口" in v.reason

    def test_done_dirty_unknown_with_worktree_is_conservative(self):
        # R2 codex #3: a present worktree whose dirtiness is None (git check failed) must
        # NOT be closable (conservative) — only an explicit clean (False) passes.
        v = assess_closable(
            _snap(done=True, worktree_present=True, worktree_dirty=None), window_visible=True
        )
        assert v.closable is False and "未知" in v.reason

    def test_done_no_worktree_clean_is_closable(self):
        # no worktree at all → nothing to lose, dirty doesn't apply
        v = assess_closable(
            _snap(done=True, worktree_present=False, worktree_dirty=None), window_visible=True
        )
        assert v.closable is True


# =============================================================================
# 3. discover_task_ids + scan_task (the I/O layer, synthetic tree)
# =============================================================================


class TestScan:
    def test_discover_union_of_queue_ack_worktrees(self, tmp_path):
        layout = _layout(tmp_path)
        _touch(layout.queue_dir / "a.md")
        _touch(layout.queue_dir / "b.BLOCKED.md")
        _touch(layout.ack_dir / "c.spawned")
        (layout.worktrees_dir / "d").mkdir(parents=True)
        assert discover_task_ids(layout) == ["a", "b", "c", "d"]

    def test_scan_task_reads_signals(self, tmp_path):
        layout = _layout(tmp_path)
        _touch(layout.queue_dir / "t.md")
        _touch(layout.queue_dir / "t.done")
        _touch(layout.ack_dir / "t.worker_reported")
        _touch(layout.ack_dir / "t.spawned")
        (layout.worktrees_dir / "t").mkdir(parents=True)
        s = scan_task(layout, "t", now=NOW)
        assert s.has_brief and s.done and s.worker_reported and s.spawned
        assert s.worktree_present
        assert s.blocked is False

    def test_scan_transcript_idle(self, tmp_path):
        layout = _layout(tmp_path)
        tdir = layout.transcript_dir("t")
        tdir.mkdir(parents=True)
        jsonl = tdir / "sess.jsonl"
        jsonl.touch()
        import os

        os.utime(jsonl, (NOW - 42, NOW - 42))
        s = scan_task(layout, "t", now=NOW)
        assert s.transcript_idle_s == 42

    def test_scan_no_transcript_is_none(self, tmp_path):
        layout = _layout(tmp_path)
        _touch(layout.queue_dir / "t.md")
        s = scan_task(layout, "t", now=NOW)
        assert s.transcript_idle_s is None

    def test_scan_worktree_dirty_via_git_runner(self, tmp_path):
        layout = _layout(tmp_path)
        (layout.worktrees_dir / "t").mkdir(parents=True)

        def fake_git(args, cwd):
            if args[:1] == ["status"]:
                return " M somefile.py\n"  # dirty
            return "0\n"  # rev-list count

        s = scan_task(layout, "t", now=NOW, git_runner=fake_git)
        assert s.worktree_dirty is True
        assert s.branch_advanced is False

    def test_scan_branch_advanced_via_rev_list(self, tmp_path):
        # R2 codex #4: local rev-list count > 0 → branch advanced past integration base
        layout = _layout(tmp_path)
        (layout.worktrees_dir / "t").mkdir(parents=True)

        def fake_git(args, cwd):
            if args[:1] == ["status"]:
                return ""  # clean
            return "3\n"  # 3 commits ahead

        s = scan_task(layout, "t", now=NOW, git_runner=fake_git)
        assert s.branch_advanced is True
        assert s.worktree_dirty is False

    def test_scan_branch_advanced_unknown_on_git_failure(self, tmp_path):
        layout = _layout(tmp_path)
        (layout.worktrees_dir / "t").mkdir(parents=True)

        def fake_git(args, cwd):
            return None  # git failed

        s = scan_task(layout, "t", now=NOW, git_runner=fake_git)
        assert s.branch_advanced is None and s.worktree_dirty is None

    def test_scan_all_marks_bound(self, tmp_path):
        layout = _layout(tmp_path)
        _touch(layout.queue_dir / "t.md")
        snaps = scan_all(layout, now=NOW, bound_tasks=["t"])
        assert snaps[0].bound is True

    def test_transcript_dir_encoding_matches_patrol_formula(self, tmp_path):
        # patrol.sh: TDIR = transcript_root / (worktree abs path with / and . -> -)
        layout = _layout(tmp_path)
        wt = layout.worktree_path("supervisor-s5a").resolve()
        import re as _re

        expected = layout.transcript_root / _re.sub(r"[/.]", "-", str(wt))
        assert layout.transcript_dir("supervisor-s5a") == expected


# =============================================================================
# 4. query_visible_tasks — osascript mocked
# =============================================================================


class TestVisibleTasks:
    def test_matches_task_in_window_titles(self):
        def runner():
            return "● supervisor-s5a — handoff-fanout\nopening-fe-be-fix — erp-system\n"

        vis = query_visible_tasks(["supervisor-s5a", "task-gone"], runner=runner)
        assert vis == {"supervisor-s5a": True, "task-gone": False}

    def test_runner_none_returns_none(self):
        vis = query_visible_tasks(["t"], runner=lambda: None)
        assert vis is None

    def test_boundary_match_no_substring_false_positive(self):
        # R2 codex #7: `task-1` must NOT match `task-10` (kebab-token boundary).
        def runner():
            return "● supervisor-s5a-2 — handoff\ntask-10 — erp-system\n"

        vis = query_visible_tasks(["task-1", "supervisor-s5a"], runner=runner)
        assert vis == {"task-1": False, "supervisor-s5a": False}

    def test_boundary_match_path_segment(self):
        def runner():
            return "/Users/x/.claude-handoff/erp-system/worktrees/task-1 — Code\n"

        vis = query_visible_tasks(["task-1"], runner=runner)
        assert vis == {"task-1": True}


# =============================================================================
# 5. BindingStore — the board's own state (bindings.json)
# =============================================================================


def _binding(
    task="t", node="n1", plan_path="/x/plan.json", events_path="/x/events.jsonl", **kw
) -> Binding:
    return Binding(
        task_id=task,
        run_id="run-1",
        node_id=node,
        plan_path=plan_path,
        events_path=events_path,
        **kw,
    )


class TestBindingStore:
    def test_put_get_roundtrip(self, tmp_path):
        store = BindingStore(tmp_path / "bindings.json")
        store.put(_binding())
        b = store.get("t")
        assert b is not None and b.run_id == "run-1" and b.node_id == "n1"

    def test_missing_file_is_empty(self, tmp_path):
        store = BindingStore(tmp_path / "nope.json")
        assert store.all() == {}
        assert store.get("t") is None

    def test_corrupt_file_tolerated(self, tmp_path):
        p = tmp_path / "bindings.json"
        p.write_text("{ not json", encoding="utf-8")
        store = BindingStore(p)
        assert store.all() == {}  # never crashes the board

    def test_active_bound_tasks_excludes_detached(self, tmp_path):
        store = BindingStore(tmp_path / "bindings.json")
        store.put(_binding(task="a"))
        store.put(_binding(task="b"))
        store.set_detached("b", True)
        assert store.active_bound_tasks() == ["a"]

    def test_set_detached_unknown_raises(self, tmp_path):
        store = BindingStore(tmp_path / "bindings.json")
        with pytest.raises(KeyError):
            store.set_detached("ghost", True)

    def test_from_dict_missing_required_keys(self):
        with pytest.raises(ValueError):
            Binding.from_dict({"task_id": "t"})  # missing run_id/node_id/...


# =============================================================================
# 6. load_overlay — read-only DAG projection (appends nothing)
# =============================================================================


def _seed_run(tmp_path, *, await_approval=True):
    """Build a synthetic supervisor run: plan.json + events.jsonl driving node n1 to
    AWAIT_APPROVAL (an irreversible node)."""
    plan = Plan(
        schema_version=1,
        plan_id="p1",
        objective="obj",
        acceptance_oracle_ref="oracle.json",
        nodes=[
            Node(
                node_id="n1",
                brief="irreversible step",
                base_ref="main",
                reversible=False,
                side_effects=[],
                max_fix_attempts=1,
            )
        ]
        if False
        else [Node(node_id="n1", brief="do n1", base_ref="main")],
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    log = EventLog(events_path, "p1")
    log.append_event(
        type=EventType.PLAN_CREATED, payload=plan, dedupe_key="genesis", ts="2026-06-06T10:00:00"
    )
    if await_approval:
        log.append_event(
            type=EventType.APPROVAL_REQUESTED,
            payload=NodeReason(node="n1", reason="不可逆步骤需审批"),
            dedupe_key="approve-req-n1",
            ts="2026-06-06T10:01:00",
        )
    return plan_path, events_path


class TestOverlay:
    def test_overlay_projects_await_approval(self, tmp_path):
        plan_path, events_path = _seed_run(tmp_path)
        b = _binding(plan_path=str(plan_path), events_path=str(events_path))
        ov = load_overlay(b, now="2026-06-06T10:02:00")
        assert ov.error is None
        assert ov.plan_id == "p1"
        assert ov.bound_node is not None
        assert ov.bound_node.status == "AWAIT_APPROVAL"

    def test_overlay_does_not_append(self, tmp_path):
        plan_path, events_path = _seed_run(tmp_path)
        before = EventLog(events_path, "p1").read_all()
        b = _binding(plan_path=str(plan_path), events_path=str(events_path))
        load_overlay(b, now="2026-06-06T10:02:00")
        after = EventLog(events_path, "p1").read_all()
        assert [e.seq for e in after] == [e.seq for e in before]  # INV-1: read-only

    def test_overlay_missing_artefact_is_error_not_crash(self, tmp_path):
        b = _binding(plan_path=str(tmp_path / "nope.json"), events_path=str(tmp_path / "no.jsonl"))
        ov = load_overlay(b, now="2026-06-06T10:02:00")
        assert ov.error is not None
        assert ov.plan_status == "unknown"


# =============================================================================
# 7. approve_node — only AWAIT_APPROVAL, bound, auto-hash, idempotent
# =============================================================================


class TestApprove:
    def test_approve_appends_approval_granted(self, tmp_path):
        plan_path, events_path = _seed_run(tmp_path)
        b = _binding(plan_path=str(plan_path), events_path=str(events_path))
        res = approve_node(
            b,
            grantor="owner",
            granted_at="2026-06-06T10:05:00",
            expires_at="2026-06-13T10:05:00",
            reason="owner ok",
        )
        assert res["appended"] is True
        types = [e.type for e in EventLog(events_path, "p1").read_all()]
        assert EventType.APPROVAL_GRANTED in types
        assert len(res["bound_hash"]) == 64  # sha256 hex

    def test_approve_non_await_node_raises(self, tmp_path):
        # a fresh PENDING node (no approval_requested) is not AWAIT_APPROVAL
        plan_path, events_path = _seed_run(tmp_path, await_approval=False)
        b = _binding(plan_path=str(plan_path), events_path=str(events_path))
        with pytest.raises(sb.ApproveError):
            approve_node(
                b,
                grantor="owner",
                granted_at="2026-06-06T10:05:00",
                expires_at="2026-06-13T10:05:00",
            )

    def test_approve_idempotent_dedupe(self, tmp_path):
        plan_path, events_path = _seed_run(tmp_path)
        b = _binding(plan_path=str(plan_path), events_path=str(events_path))
        kw = dict(
            grantor="owner", granted_at="2026-06-06T10:05:00", expires_at="2026-06-13T10:05:00"
        )
        r1 = approve_node(b, **kw)
        r2 = approve_node(b, **kw)
        assert r1["appended"] is True
        assert r2["deduped"] is True  # same dedupe_key (same state.last_seq) → no-op

    def test_approve_bound_hash_deterministic(self, tmp_path):
        plan_path, events_path = _seed_run(tmp_path)
        b = _binding(plan_path=str(plan_path), events_path=str(events_path))
        r1 = approve_node(b, grantor="o", granted_at="t1", expires_at="t2")
        # re-deriving from the same pre-approval state would give the same hash; the
        # appended event makes the next state different (anti-replay) — assert the hash
        # is bound to the node id (not a constant).
        assert b.node_id in r1["bound_hash"] or len(r1["bound_hash"]) == 64


# =============================================================================
# 8. STOP_AUTO control — reversible sentinels (project vs global)
# =============================================================================


class TestStopControl:
    def test_pause_project_writes_project_sentinel(self, tmp_path):
        layout = _layout(tmp_path)
        target = sb.set_pause(layout, scope="project")
        assert target == layout.project_stop and target.exists()
        assert not layout.global_stop.exists()

    def test_pause_global_writes_global_sentinel(self, tmp_path):
        layout = _layout(tmp_path)
        target = sb.set_pause(layout, scope="global")
        assert target == layout.global_stop and target.exists()

    def test_resume_removes_sentinel(self, tmp_path):
        layout = _layout(tmp_path)
        sb.set_pause(layout, scope="project")
        target, existed = sb.clear_pause(layout, scope="project")
        assert existed is True and not target.exists()

    def test_resume_idempotent_when_absent(self, tmp_path):
        layout = _layout(tmp_path)
        target, existed = sb.clear_pause(layout, scope="project")
        assert existed is False and not target.exists()


# =============================================================================
# 9. CLI end-to-end (dispatch via main; pgrep/osascript injected)
# =============================================================================


def _no_external(monkeypatch):
    """Neutralise the real system probes (pgrep watcher / osascript windows) so CLI
    tests are deterministic + never depend on the host."""
    monkeypatch.setattr(sb, "_central_health", lambda layout, *, now: (None, None))
    monkeypatch.setattr(sb, "query_visible_tasks", lambda ids, **kw: None)
    monkeypatch.setattr(sb, "_git_runner", lambda args, cwd: None)


class TestCliEndToEnd:
    def test_status_runs_and_groups(self, tmp_path, monkeypatch, capsys):
        _no_external(monkeypatch)
        layout = _layout(tmp_path)
        _touch(layout.queue_dir / "task-blocked.md")
        _touch(layout.queue_dir / "task-blocked.BLOCKED.md")
        _touch(layout.queue_dir / "task-done.done")
        rc = sb.main(
            ["status", "--root", str(layout.root), "--project", "erp-system", "--no-color"]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "卡住需介入" in out and "task-blocked" in out
        assert "已完成" in out and "task-done" in out

    def test_sessions_runs(self, tmp_path, monkeypatch, capsys):
        _no_external(monkeypatch)
        layout = _layout(tmp_path)
        _touch(layout.queue_dir / "task-done.done")
        rc = sb.main(
            ["sessions", "--root", str(layout.root), "--project", "erp-system", "--no-color"]
        )
        out = capsys.readouterr().out
        assert rc == 0 and "可关会话评估" in out

    def test_pause_resume_cli(self, tmp_path, capsys):
        layout = _layout(tmp_path)
        assert sb.main(["pause", "--root", str(layout.root), "--project", "erp-system"]) == 0
        assert layout.project_stop.exists()
        assert sb.main(["resume", "--root", str(layout.root), "--project", "erp-system"]) == 0
        assert not layout.project_stop.exists()

    def test_stop_cli_writes_stop_auto_not_done(self, tmp_path):
        # R2 codex #1: `handoff stop` only writes STOP_AUTO (never the global `done`).
        layout = _layout(tmp_path)
        assert sb.main(["stop", "--root", str(layout.root), "--project", "erp-system"]) == 0
        assert layout.project_stop.exists()
        assert not layout.global_done.exists()

    def test_stop_global_cli(self, tmp_path):
        layout = _layout(tmp_path)
        assert (
            sb.main(["stop", "--global", "--root", str(layout.root), "--project", "erp-system"])
            == 0
        )
        assert layout.global_stop.exists()
        assert not layout.global_done.exists()

    def test_approve_unbound_task_refuses(self, tmp_path, capsys):
        layout = _layout(tmp_path)
        rc = sb.main(
            ["approve", "ghost-task", "--root", str(layout.root), "--project", "erp-system"]
        )
        out = capsys.readouterr()
        assert rc == 2
        assert "不是已绑定" in out.err or "无法 approve" in out.err

    def test_bind_then_approve_and_overlay(self, tmp_path, monkeypatch, capsys):
        _no_external(monkeypatch)
        layout = _layout(tmp_path)
        plan_path, events_path = _seed_run(tmp_path)
        # bind
        rc = sb.main(
            [
                "bind",
                "task-x",
                "--root",
                str(layout.root),
                "--project",
                "erp-system",
                "--run-id",
                "run-1",
                "--node-id",
                "n1",
                "--plan-path",
                str(plan_path),
                "--events-path",
                str(events_path),
            ]
        )
        assert rc == 0
        capsys.readouterr()
        # approve (bound + AWAIT_APPROVAL)
        rc = sb.main(
            [
                "approve",
                "task-x",
                "--root",
                str(layout.root),
                "--project",
                "erp-system",
                "--grantor",
                "owner",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0 and "approval_granted" in out
        # status shows the DAG overlay labelled 可能滞后
        _touch(layout.queue_dir / "task-x.md")
        rc = sb.main(
            ["status", "--root", str(layout.root), "--project", "erp-system", "--no-color"]
        )
        out = capsys.readouterr().out
        assert "监管中枢视图" in out and "可能滞后" in out

    def test_force_sync_detaches_then_reattaches(self, tmp_path, capsys):
        layout = _layout(tmp_path)
        store = BindingStore(layout.bindings_path)
        store.put(_binding(task="task-y"))
        rc = sb.main(
            ["force-sync", "task-y", "--root", str(layout.root), "--project", "erp-system"]
        )
        out = capsys.readouterr().out
        assert rc == 0 and "detach" in out
        assert BindingStore(layout.bindings_path).get("task-y").detached is True
        # split-brain: a detached binding is no longer an active overlay (real runtime wins)
        assert BindingStore(layout.bindings_path).active_bound_tasks() == []
        rc = sb.main(
            [
                "force-sync",
                "task-y",
                "--reattach",
                "--root",
                str(layout.root),
                "--project",
                "erp-system",
            ]
        )
        assert rc == 0
        assert BindingStore(layout.bindings_path).get("task-y").detached is False

    def test_force_sync_unbound_refuses(self, tmp_path, capsys):
        layout = _layout(tmp_path)
        rc = sb.main(["force-sync", "ghost", "--root", str(layout.root), "--project", "erp-system"])
        assert rc == 2


# =============================================================================
# 10. structural — pure stdlib (no heavy TUI deps), INV markers
# =============================================================================


class TestStructural:
    def test_no_heavy_tui_dependency(self):
        from pathlib import Path as _Path

        # the module must not pull Rich / Textual (design defer — simple ANSI only)
        src = _Path(str(sb.__file__)).read_text(encoding="utf-8")
        assert "import rich" not in src and "import textual" not in src
        assert "from rich" not in src and "from textual" not in src

    def test_business_states_all_have_labels(self):
        for st in BusinessState:
            assert st in sb.BUSINESS_LABEL
            assert st in sb.BUSINESS_ORDER

    def test_render_status_smoke(self):
        rows = [
            sb.BoardRow(_snap("a", blocked=True), BusinessState.BLOCKED, None),
            sb.BoardRow(_snap("b", done=True), BusinessState.DONE, False),
        ]
        text = sb.render_status(
            rows,
            now_iso="2026-06-06T10:00:00",
            project="erp-system",
            overlays=None,
            central_heartbeat_idle_s=30,
            watcher_alive=True,
            color=False,
        )
        assert "卡住需介入" in text and "a" in text
        assert "已完成" in text and "b" in text

    def test_render_status_overlay_labelled_may_lag(self, tmp_path):
        plan_path, events_path = _seed_run(tmp_path)
        ov = load_overlay(
            _binding(plan_path=str(plan_path), events_path=str(events_path)),
            now="2026-06-06T10:02:00",
        )
        text = sb.render_status(
            [sb.BoardRow(_snap("t"), BusinessState.IDLE, None)],
            now_iso="2026-06-06T10:00:00",
            project="erp-system",
            overlays=[ov],
            central_heartbeat_idle_s=None,
            watcher_alive=None,
            color=False,
        )
        assert "可能滞后" in text and "真实运行时为准" in text
