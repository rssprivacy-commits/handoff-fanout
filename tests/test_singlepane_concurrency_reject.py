"""Singlepane worker concurrency hard-REJECT (design §5.4 / R2 M5 / Task 5.1).

A project whose EXPLICIT ``worker_isolation`` is ``"singlepane"`` shares ONE editor
pane in the main project tree (no git-worktree isolation). Two workers there would
clobber each other's files. So a SECOND concurrent worker dispatch must be physically
REJECTED — never silently fall back to a concurrent main-dir spawn (that is the very
clobber the isolation mode forbids). The rejection is fail-closed + an owner-readable
``ack/<task>.singlepane_busy.txt``, and rests on the SAME project ``.spawn.lock`` that
guards the autoclose critical section (design §7: one lock, no sub-lock races).

The gate fires on TWO signals (both via the project spawn lock):
  1. lock contention — a concurrent spawn already holds the lock (the named test);
  2. an active singlepane worker for a DIFFERENT task already occupies the pane
     (a `*.singlepane` sidecar whose `<task>.uri` is present and non-terminal — the
     same file-based "active tab" signal as count_global_active_tabs).
It is a NO-OP for non-singlepane projects and for non-worker roles (the central /
succession path is exempt — design §6).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from handoff_fanout import config as _config
from handoff_fanout import dump
from handoff_fanout.spawn_lock import project_spawn_lock

PROJECT = "wilde-hexe"


def _cfg(home: Path) -> _config.Config:
    # EXPLICIT singlepane isolation (worker_isolation) drives the gate; singlepane_projects
    # makes maybe_write_singlepane_sidecar actually drop the sidecar so the sequential-
    # overspawn signal (an active sidecar + .uri) is exercised end-to-end.
    return _config._from_dict(
        {
            "worker_isolation": {PROJECT: "singlepane"},
            "singlepane_projects": [PROJECT],
        },
        home=home,
    )


def _queue(home: Path) -> Path:
    qd = home / PROJECT / "queue"
    qd.mkdir(parents=True, exist_ok=True)
    return qd


def _mark_active(queue: Path, task: str) -> None:
    """Simulate a live singlepane worker: a sidecar + a non-terminal `.uri`."""
    (queue / f"{task}.singlepane").write_text('{"role": "worker"}', encoding="utf-8")
    (queue / f"{task}.uri").write_text("WORKSPACE=/x\nURI=vscode://x\n", encoding="utf-8")


# ── 1. lock contention (the brief's named test) ─────────────────────────────


def test_lock_held_rejects_second_concurrent_worker(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _queue(tmp_path)
    # First worker's dump is "in progress" — it holds the project spawn lock.
    with project_spawn_lock(PROJECT, root=cfg.home):
        with pytest.raises(dump.SinglepaneBusy):
            with dump.singlepane_worker_guard(cfg, project=PROJECT, task="wh-second"):
                pass  # must never reach the body — the lock is held


def test_lock_contention_writes_owner_readable_ack(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _queue(tmp_path)
    with project_spawn_lock(PROJECT, root=cfg.home):
        with pytest.raises(dump.SinglepaneBusy):
            with dump.singlepane_worker_guard(cfg, project=PROJECT, task="wh-second"):
                pass
    ack = tmp_path / PROJECT / "ack" / "wh-second.singlepane_busy.txt"
    assert ack.exists()
    body = ack.read_text()
    assert "wh-second" in body and "reason" in body  # owner can read WHY it was rejected


# ── 2. sequential over-spawn while a worker is still active ──────────────────


def test_active_worker_rejects_second_different_task(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    queue = _queue(tmp_path)
    _mark_active(queue, "wh-first")  # a live worker occupies the single pane
    with pytest.raises(dump.SinglepaneBusy):
        with dump.singlepane_worker_guard(cfg, project=PROJECT, task="wh-second"):
            pass


def test_terminal_worker_frees_the_pane(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    queue = _queue(tmp_path)
    _mark_active(queue, "wh-first")
    (queue / "wh-first.done").write_text("", encoding="utf-8")  # first finished
    # Pane is free now → a new worker may enter the guard body.
    entered = False
    with dump.singlepane_worker_guard(cfg, project=PROJECT, task="wh-second"):
        entered = True
    assert entered


def test_same_task_redump_not_rejected(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    queue = _queue(tmp_path)
    _mark_active(queue, "wh-first")  # the SAME task re-publishing must not reject itself
    entered = False
    with dump.singlepane_worker_guard(cfg, project=PROJECT, task="wh-first"):
        entered = True
    assert entered


# ── 3. no-op for non-singlepane / non-worker (must not lock or reject) ───────


def test_non_singlepane_project_is_noop(tmp_path: Path) -> None:
    cfg = _config._from_dict({}, home=tmp_path)  # no worker_isolation → None → no gate
    _queue(tmp_path)
    with project_spawn_lock("other", root=cfg.home):  # lock held, but project isn't singlepane
        entered = False
        with dump.singlepane_worker_guard(cfg, project="other", task="t1"):
            entered = True
        assert entered  # unrelated project's held lock is irrelevant


def test_non_worker_role_is_noop(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    queue = _queue(tmp_path)
    _mark_active(queue, "wh-first")
    # A supervisor_succession is exempt (design §6) — it replaces the predecessor, not a
    # concurrent worker. It must NOT be rejected even with an active worker present.
    entered = False
    with dump.singlepane_worker_guard(
        cfg, project=PROJECT, task="wh-succ", role="supervisor_succession"
    ):
        entered = True
    assert entered


# ── 4. end-to-end through dump.main (the "dump.py 入口" wiring) ───────────────


def _git_repo(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(d), "config", k, v], check=True, capture_output=True)
    (d / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(d), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True, capture_output=True)
    return d


def test_main_active_dump_rejected_when_pane_busy(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"worker_isolation": {PROJECT: "singlepane"}, "singlepane_projects": [PROJECT]})
    )
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.delenv("HANDOFF_RETRO_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_WORKTREE_ISOLATION", raising=False)
    queue = home / PROJECT / "queue"
    queue.mkdir(parents=True)
    _mark_active(queue, "wh-first")  # a live worker holds the pane

    ws = _git_repo(tmp_path / "repo")
    rc = dump.main(
        ["--project", PROJECT, "--task", "wh-second", "--next", "do x",
         "--status", "active", "--workspace", str(ws)]
    )
    assert rc == 2  # fail-closed BLOCKED-ish exit, NOT a silent concurrent spawn
    assert (home / PROJECT / "ack" / "wh-second.singlepane_busy.txt").exists()
    # And it must NOT have published a second .uri (no concurrent spawn smuggled in).
    assert not (queue / "wh-second.uri").exists()
