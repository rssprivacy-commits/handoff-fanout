"""`handoff prune` — janitor for terminal-task sidecar leftovers.

Built after observing 81 .md / 8 heartbeat / 4 stale 529-suspected files piling
up in a real erp-system queue, with no command to clean them. prune removes
ONLY sidecars (heartbeat / 529-suspected / uri) of *terminal* tasks (.done or
.BLOCKED.md present), and must never touch history (.md/.done/.BLOCKED.md),
active tasks, or unknown tasks. Default is dry-run.
"""

from __future__ import annotations

from pathlib import Path

from handoff_fanout import prune

PROJECT = "demo"


def _queue(home: Path, project: str = PROJECT) -> Path:
    q = home / project / "queue"
    q.mkdir(parents=True, exist_ok=True)
    return q


def _make_task(queue: Path, task: str, *, terminal: str | None, sidecars=()) -> None:
    """terminal: None=active, 'done', or 'blocked'. sidecars: ext names to create."""
    (queue / f"{task}.md").write_text("# task")
    if terminal == "done":
        (queue / f"{task}.done").touch()
    elif terminal == "blocked":
        (queue / f"{task}.BLOCKED.md").write_text("blocked")
    for ext in sidecars:
        (queue / f"{task}.{ext}").write_text("")


def test_find_prunable_lists_only_terminal_with_sidecars(isolated_handoff_home):
    queue = _queue(isolated_handoff_home)
    _make_task(queue, "done-leftover", terminal="done", sidecars=["heartbeat", "529-suspected"])
    _make_task(queue, "blocked-leftover", terminal="blocked", sidecars=["uri"])
    _make_task(queue, "done-clean", terminal="done", sidecars=[])  # no sidecars → skip
    _make_task(queue, "active", terminal=None, sidecars=["heartbeat"])  # not terminal → skip

    records = prune.find_prunable(isolated_handoff_home)
    tasks = {r["task"] for r in records}
    assert tasks == {"done-leftover", "blocked-leftover"}


def test_dry_run_removes_nothing(isolated_handoff_home):
    queue = _queue(isolated_handoff_home)
    _make_task(queue, "t", terminal="done", sidecars=["heartbeat", "529-suspected", "uri"])

    rc = prune.main([])  # dry-run default

    assert rc == 0
    assert (queue / "t.heartbeat").exists()
    assert (queue / "t.529-suspected").exists()
    assert (queue / "t.uri").exists()


def test_execute_removes_sidecars_keeps_history(isolated_handoff_home):
    queue = _queue(isolated_handoff_home)
    _make_task(queue, "t", terminal="done", sidecars=["heartbeat", "529-suspected", "uri"])

    rc = prune.main(["--execute"])

    assert rc == 0
    assert not (queue / "t.heartbeat").exists()
    assert not (queue / "t.529-suspected").exists()
    assert not (queue / "t.uri").exists()
    # history preserved
    assert (queue / "t.md").exists()
    assert (queue / "t.done").exists()


def test_execute_never_touches_active_task(isolated_handoff_home):
    """An active (non-terminal) task's heartbeat must survive prune --execute."""
    queue = _queue(isolated_handoff_home)
    _make_task(queue, "active", terminal=None, sidecars=["heartbeat", "uri"])

    rc = prune.main(["--execute"])

    assert rc == 0
    assert (queue / "active.heartbeat").exists()
    assert (queue / "active.uri").exists()


def test_execute_keeps_blocked_marker_drops_its_sidecars(isolated_handoff_home):
    queue = _queue(isolated_handoff_home)
    _make_task(queue, "b", terminal="blocked", sidecars=["heartbeat", "529-suspected"])

    prune.main(["--execute"])

    assert (queue / "b.BLOCKED.md").exists()  # the blocked marker is history
    assert not (queue / "b.heartbeat").exists()
    assert not (queue / "b.529-suspected").exists()


def test_project_filter_scopes_to_one_project(isolated_handoff_home):
    qa = _queue(isolated_handoff_home, "proj-a")
    qb = _queue(isolated_handoff_home, "proj-b")
    _make_task(qa, "t", terminal="done", sidecars=["heartbeat"])
    _make_task(qb, "t", terminal="done", sidecars=["heartbeat"])

    prune.main(["--project", "proj-a", "--execute"])

    assert not (qa / "t.heartbeat").exists()
    assert (qb / "t.heartbeat").exists()  # untouched


def test_skips_special_dirs(isolated_handoff_home):
    """locks/ and _recovery/ are not projects — never scanned."""
    for special in ("locks", "_recovery"):
        q = isolated_handoff_home / special / "queue"
        q.mkdir(parents=True)
        _make_task(q, "t", terminal="done", sidecars=["heartbeat"])

    rc = prune.main(["--execute"])

    assert rc == 0
    for special in ("locks", "_recovery"):
        assert (isolated_handoff_home / special / "queue" / "t.heartbeat").exists()


def test_empty_home_is_noop(isolated_handoff_home):
    assert prune.main([]) == 0
    assert prune.main(["--execute"]) == 0


def test_cli_dispatch_routes_to_prune(isolated_handoff_home):
    """`handoff prune` via the unified dispatcher reaches prune.main."""
    from handoff_fanout import cli

    queue = _queue(isolated_handoff_home)
    _make_task(queue, "t", terminal="done", sidecars=["heartbeat"])

    rc = cli.main(["prune", "--execute"])

    assert rc == 0
    assert not (queue / "t.heartbeat").exists()
