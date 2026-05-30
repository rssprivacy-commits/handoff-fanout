"""Tests for the launchd-owned headless runner (``handoff_fanout.headless_runner``).

Covers the design-spec §5 test plan at the Python level: stdin-prompt wiring,
finalize classification (submitted-headless / done / BLOCKED / timeout / nonzero),
safety precheck, PID-reuse-safe pidfile + janitor, halt + opt-in-revoke draining,
and the concurrency cap not hot-looping.

Every external binary is stubbed: ``HANDOFF_CLAUDE_HEADLESS_CMD`` points at a
shell stub, ``HANDOFF_CLAUDE_HEADLESS_FLAGS`` is emptied (so the stub gets a
clean argv), and ``HANDOFF_CAFFEINATE_CMD`` is emptied (no caffeinate on CI).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from handoff_fanout import headless_runner as hr

PROJECT = "demo"
TASK = "demo-task"


# ─── fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "claude-handoff"
    (r / PROJECT / "queue").mkdir(parents=True)
    (r / PROJECT / "ack").mkdir(parents=True)
    (r / PROJECT / "headless-req").mkdir(parents=True)
    return r


def _clean_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


def _seed_prompt(root: Path, task: str = TASK, text: str = "PROMPT-BODY-XYZZY") -> None:
    (root / PROJECT / "queue" / f"{task}.md").write_text(text, encoding="utf-8")


def _stub(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return p


def _use_stub(monkeypatch, stub: Path, flags: str = "") -> None:
    monkeypatch.setenv("HANDOFF_CLAUDE_HEADLESS_CMD", str(stub))
    monkeypatch.setenv("HANDOFF_CLAUDE_HEADLESS_FLAGS", flags)
    monkeypatch.setenv("HANDOFF_CAFFEINATE_CMD", "")  # no caffeinate on CI


# ─── finalize classification ─────────────────────────────────────────────────


def test_run_one_stdin_wiring_and_no_artifact_blocks(root, tmp_path, monkeypatch):
    """cat stub echoes the stdin prompt to its log; rc=0 with no produced .uri /
    .done ⇒ BLOCKED 'completed without handoff artifact'."""
    ws = _clean_repo(tmp_path / "ws")
    _seed_prompt(root)
    _use_stub(monkeypatch, _stub(tmp_path, "cat_stub", "exec cat\n"))
    outcome = hr.run_one(root, PROJECT, TASK, str(ws))
    assert outcome == "blocked-no-artifact"
    log = (root / PROJECT / "headless" / f"{TASK}.log").read_text()
    assert "PROMPT-BODY-XYZZY" in log, "prompt must reach the child via stdin"
    assert (root / PROJECT / "queue" / f"{TASK}.BLOCKED.md").exists()
    assert (root / PROJECT / "ack" / f"{TASK}.failed").exists()
    # pidfile cleaned up after the run.
    assert not (root / PROJECT / "headless" / f"{TASK}.pid").exists()


def test_finalize_submitted_when_next_uri_appears(root, tmp_path, monkeypatch):
    ws = _clean_repo(tmp_path / "ws")
    _seed_prompt(root)
    target = root / PROJECT / "queue" / "next-task.uri"
    monkeypatch.setenv("_STUB_NEW_URI", str(target))
    _use_stub(monkeypatch, _stub(tmp_path, "ok_stub", 'cat >/dev/null; : > "$_STUB_NEW_URI"\n'))
    outcome = hr.run_one(root, PROJECT, TASK, str(ws))
    assert outcome == "submitted-headless"
    ack = root / PROJECT / "ack" / f"{TASK}.submitted-headless"
    assert ack.exists()
    assert "next-task.uri" in ack.read_text()
    assert not (root / PROJECT / "queue" / f"{TASK}.BLOCKED.md").exists()


def test_finalize_done_when_task_marks_done(root, tmp_path, monkeypatch):
    ws = _clean_repo(tmp_path / "ws")
    _seed_prompt(root)
    target = root / PROJECT / "queue" / f"{TASK}.done"
    monkeypatch.setenv("_STUB_DONE", str(target))
    _use_stub(monkeypatch, _stub(tmp_path, "done_stub", 'cat >/dev/null; : > "$_STUB_DONE"\n'))
    outcome = hr.run_one(root, PROJECT, TASK, str(ws))
    assert outcome == "done"
    assert not (root / PROJECT / "queue" / f"{TASK}.BLOCKED.md").exists()


def test_nonzero_exit_blocks(root, tmp_path, monkeypatch):
    ws = _clean_repo(tmp_path / "ws")
    _seed_prompt(root)
    _use_stub(monkeypatch, _stub(tmp_path, "fail_stub", "cat >/dev/null; exit 4\n"))
    outcome = hr.run_one(root, PROJECT, TASK, str(ws))
    assert outcome == "blocked-rc"
    blocked = (root / PROJECT / "queue" / f"{TASK}.BLOCKED.md").read_text()
    assert "rc=4" in blocked


def test_timeout_blocks_and_kills(root, tmp_path, monkeypatch):
    ws = _clean_repo(tmp_path / "ws")
    _seed_prompt(root)
    _use_stub(monkeypatch, _stub(tmp_path, "sleep_stub", "sleep 60\n"))
    monkeypatch.setenv("HANDOFF_HEADLESS_TIMEOUT", "1")
    start = time.time()
    outcome = hr.run_one(root, PROJECT, TASK, str(ws))
    assert outcome == "blocked-timeout"
    assert time.time() - start < 40, "timeout path must not hang"
    blocked = (root / PROJECT / "queue" / f"{TASK}.BLOCKED.md").read_text()
    assert "timeout" in blocked


def test_missing_prompt_blocks(root, tmp_path, monkeypatch):
    ws = _clean_repo(tmp_path / "ws")
    _use_stub(monkeypatch, _stub(tmp_path, "cat_stub", "exec cat\n"))
    outcome = hr.run_one(root, PROJECT, TASK, str(ws))
    assert outcome == "blocked-no-prompt"
    assert (root / PROJECT / "queue" / f"{TASK}.BLOCKED.md").exists()


# ─── safety precheck ─────────────────────────────────────────────────────────


def test_safety_precheck_clean(tmp_path, monkeypatch):
    monkeypatch.delenv("HANDOFF_PROTECTED_BRANCHES", raising=False)
    ws = _clean_repo(tmp_path / "ws")
    ok, reason = hr.safety_precheck(tmp_path, ws)
    assert ok, reason


def test_safety_precheck_dirty_blocks(tmp_path, monkeypatch):
    monkeypatch.delenv("HANDOFF_PROTECTED_BRANCHES", raising=False)
    ws = _clean_repo(tmp_path / "ws")
    (ws / "README.md").write_text("modified\n", encoding="utf-8")  # tracked change
    ok, reason = hr.safety_precheck(tmp_path, ws)
    assert not ok
    assert "dirty" in reason


def test_safety_precheck_untracked_junk_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv("HANDOFF_PROTECTED_BRANCHES", raising=False)
    ws = _clean_repo(tmp_path / "ws")
    (ws / "junk.tmp").write_text("x", encoding="utf-8")  # untracked only
    ok, _ = hr.safety_precheck(tmp_path, ws)
    assert ok, "untracked-only junk must not block the relay"


def test_safety_precheck_protected_branch_blocks(tmp_path, monkeypatch):
    ws = _clean_repo(tmp_path / "ws")
    monkeypatch.setenv("HANDOFF_PROTECTED_BRANCHES", "main master")
    ok, reason = hr.safety_precheck(tmp_path, ws)
    assert not ok
    assert "protected branch" in reason


# ─── pidfile PID-reuse defence + janitor ─────────────────────────────────────


def test_pidfile_owns_live_child_self(root):
    """Our own pid with its real lstart/pgid validates as a live child."""
    pid = os.getpid()
    pgid = os.getpgid(0)
    lstart = hr._ps_field(pid, "lstart")
    meta = {
        "pid": str(pid),
        "pgid": str(pgid),
        "start_lstart": lstart or "",
        "start_epoch": str(int(time.time())),
        "task": "x",
    }
    assert hr.pidfile_owns_live_child(meta) is True


def test_pidfile_recycled_pid_rejected(root):
    """Same live pid but a wrong recorded start time ⇒ treated as recycled."""
    pid = os.getpid()
    meta = {
        "pid": str(pid),
        "pgid": str(os.getpgid(0)),
        "start_lstart": "Wed Jan  1 00:00:00 2020",  # deliberately wrong
        "start_epoch": "0",
        "task": "x",
    }
    assert hr.pidfile_owns_live_child(meta) is False


def test_janitor_clears_stale_pidfile(root):
    """A pidfile for a dead pid is cleared, and NOT killed (no killpg on a
    recycled/dead pgid)."""
    hl = root / PROJECT / "headless"
    hl.mkdir(parents=True)
    pf = hl / f"{TASK}.pid"
    hr.write_pidfile(pf, 2_000_000_000, 2_000_000_000, time.time(), "bogus lstart", TASK)
    assert pf.exists()
    hr.janitor_sweep(root)
    assert not pf.exists()


# ─── drain loop: halt / opt-in revoke / cap ──────────────────────────────────


def _seed_req(root: Path, ws: Path, task: str = TASK) -> Path:
    req = root / PROJECT / "headless-req" / f"{task}.req"
    req.write_text(f"WORKSPACE={ws}\ntask={task}\n", encoding="utf-8")
    return req


def test_drain_halts_on_stop_auto_leaves_req(root, tmp_path, monkeypatch):
    ws = _clean_repo(tmp_path / "ws")
    _seed_prompt(root)
    req = _seed_req(root, ws)
    monkeypatch.setenv("HANDOFF_HEADLESS_ENABLED", "1")
    (root / "STOP_AUTO").touch()
    assert hr.drain_once(root) == 0
    assert req.exists(), "halt must leave the .req for after resume"


def test_drain_opt_in_revoked_defers(root, tmp_path, monkeypatch):
    ws = _clean_repo(tmp_path / "ws")
    _seed_prompt(root)
    req = _seed_req(root, ws)
    monkeypatch.delenv("HANDOFF_HEADLESS_ENABLED", raising=False)  # not opted in
    assert hr.drain_once(root) == 0
    assert not req.exists(), "revoked opt-in drains (unlinks) the .req"
    assert (root / PROJECT / "ack" / f"{TASK}.deferred").exists()


def test_drain_cap_busy_leaves_req(root, tmp_path, monkeypatch):
    ws = _clean_repo(tmp_path / "ws")
    _seed_prompt(root)
    req = _seed_req(root, ws)
    monkeypatch.setenv("HANDOFF_HEADLESS_ENABLED", "1")
    monkeypatch.setenv("HANDOFF_MAX_HEADLESS", "1")
    # Make count_live_headless() == 1 with a *validated-live* pidfile (our own pid).
    hl = root / "other-proj" / "headless"
    hl.mkdir(parents=True)
    pid = os.getpid()
    hr.write_pidfile(
        hl / "busy.pid", pid, os.getpgid(0), time.time(), hr._ps_field(pid, "lstart") or "", "busy"
    )
    # Keep the backoff short so the test doesn't actually wait.
    monkeypatch.setattr(hr, "CAP_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(hr, "CAP_BACKOFF_MAX_TICKS", 2)
    assert hr.drain_once(root) == 0
    assert req.exists(), "over-cap must leave the .req for the next invocation"


def test_drain_runs_when_enabled(root, tmp_path, monkeypatch):
    ws = _clean_repo(tmp_path / "ws")
    _seed_prompt(root)
    req = _seed_req(root, ws)
    monkeypatch.setenv("HANDOFF_HEADLESS_ENABLED", "1")
    _use_stub(monkeypatch, _stub(tmp_path, "cat_stub", "exec cat\n"))
    assert hr.drain_once(root) == 1
    assert not req.exists(), "a drained req is unlinked"
