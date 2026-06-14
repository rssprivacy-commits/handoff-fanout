"""Concurrent worktree-create safety + the dump-path spawn-lock symmetry fix (sw-coord-p21).

Two layers of evidence:

1. PRIMITIVE arms (verbatim from the central reproduction REPRO-concurrent-worktree-create.py):
   N real threads fire ``create_worktree`` against ONE shared source repo.
     * UNLOCKED arm = mimics the (pre-fix) dump path — create_worktree with no spawn lock.
     * LOCKED arm   = mimics the spawn path — create_worktree under ``project_spawn_lock``.
   Invariant either way: NO corruption (``git worktree list`` stays parseable, no unhandled
   exception). The UNLOCKED arm REPORTS its success count (a number < N is the spurious
   ``.git/config``-lock failure the missing handoff lock causes); the LOCKED arm asserts N/N.

2. DUMP-PATH arms (this task's strengthening): prove the fix is actually WIRED into
   ``dump.resolve_spawn_workspace`` — not merely that the lock primitive works.
     * a DETERMINISTIC spy (zero concurrency flakiness): inside a stubbed ``create_worktree``
       it tries to grab the SAME project lock non-blocking — post-fix the dump path already
       holds it (→ LockHeld), pre-fix it is free. This is the robust fail-without-fix sentinel.
     * an N-concurrent ``dump.main`` end-to-end: N parallel active dumps (worktree isolation
       ON, unique task each, shared source repo) must ALL create their worktree (created == N).
       Pre-fix the un-serialized ``git worktree add -b`` race degrades some to the shared tree.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from handoff_fanout import config as _config
from handoff_fanout import dump
from handoff_fanout import worktree as wt
from handoff_fanout.spawn_lock import LockHeld, project_spawn_lock

N = 8
PROJECT = "concur-proj"


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _bare_and_clone(tmp_path: Path) -> Path:
    """A bare origin (main) + a pushed working clone — the shape create_worktree accepts."""
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
    subprocess.run(["git", "remote", "set-head", "origin", "main"], cwd=str(ws),
                   capture_output=True)
    return ws


def _worktree_list_consistent(ws: Path) -> bool:
    """`git worktree list` must succeed (a corrupt .git/worktrees metadata makes it error)."""
    p = subprocess.run(["git", "-C", str(ws), "worktree", "list", "--porcelain"],
                       capture_output=True, text=True)
    return p.returncode == 0


def _fire(ws: Path, home: Path, *, use_lock: bool) -> dict:
    cfg = _config.Config(home=home)
    start = threading.Barrier(N)
    results: dict[int, dict] = {}
    lock_obj = threading.Lock()

    def worker(i: int) -> None:
        task = f"ct{i}"
        start.wait()  # release all N at the same instant → maximal contention
        rec: dict = {"task": task, "status": None, "blocked": None, "exc": None, "lockheld": False}
        try:
            if use_lock:
                try:
                    with project_spawn_lock(PROJECT, root=home, wait=30.0):
                        res = wt.create_worktree(
                            source_workspace=ws, project=PROJECT, task=task, cfg=cfg,
                            mode=wt.MODE_ON, spawn_nonce=f"nonce{i}", role="worker",
                        )
                        rec["status"] = res.status
                        rec["blocked"] = res.is_blocked
                except LockHeld:
                    rec["lockheld"] = True
            else:
                res = wt.create_worktree(
                    source_workspace=ws, project=PROJECT, task=task, cfg=cfg,
                    mode=wt.MODE_ON, spawn_nonce=f"nonce{i}", role="worker",
                )
                rec["status"] = res.status
                rec["blocked"] = res.is_blocked
        except Exception as e:  # noqa: BLE001 — we WANT to capture any crash as data
            rec["exc"] = f"{type(e).__name__}: {e}"
        with lock_obj:
            results[i] = rec

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)

    created = sum(1 for r in results.values() if r["status"] == wt.ST_CREATED)
    excs = [r["exc"] for r in results.values() if r["exc"]]
    return {
        "results": results,
        "created": created,
        "exceptions": excs,
        "list_ok": _worktree_list_consistent(ws),
    }


def test_concurrent_create_unlocked_never_corrupts(tmp_path: Path) -> None:
    """DUMP-path shape: create_worktree with NO spawn lock under N concurrent threads.

    Hard invariant (must hold regardless of the lock): no thread raises an unhandled
    exception AND the repo's worktree metadata stays consistent (git's own locks must
    prevent CORRUPTION even when the handoff lock is absent). The success COUNT is
    reported — a number < N is the spurious-failure symptom of the missing handoff lock.
    """
    ws = _bare_and_clone(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    out = _fire(ws, home, use_lock=False)
    # Corruption red line — git must never leave the shared repo in a broken state.
    assert out["list_ok"], "git worktree metadata CORRUPTED under unlocked concurrency"
    assert not out["exceptions"], f"unhandled crash under unlocked concurrency: {out['exceptions']}"
    # Diagnostic (not an assertion): how many actually succeeded vs spuriously failed.
    print(f"[UNLOCKED] created={out['created']}/{N}  list_ok={out['list_ok']}")
    for i, r in sorted(out["results"].items()):
        print(f"  ct{i}: status={r['status']} blocked={r['blocked']} exc={r['exc']}")


def test_concurrent_create_under_spawn_lock_all_succeed(tmp_path: Path) -> None:
    """SPAWN-path shape: create_worktree UNDER project_spawn_lock. The lock serializes the
    racy `git worktree add -b`, so ALL N must cleanly succeed (no spurious config-lock
    failures, no corruption). This is the control that proves the lock is the fix."""
    ws = _bare_and_clone(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    out = _fire(ws, home, use_lock=True)
    assert out["list_ok"], "git worktree metadata CORRUPTED even under the spawn lock"
    assert not out["exceptions"], f"unhandled crash under locked concurrency: {out['exceptions']}"
    print(f"[LOCKED] created={out['created']}/{N}  list_ok={out['list_ok']}")
    for i, r in sorted(out["results"].items()):
        print(f"  ct{i}: status={r['status']} blocked={r['blocked']} lockheld={r['lockheld']}")
    # The lock's whole purpose: serialized worktree creation all succeeds.
    assert out["created"] == N, (
        f"under the spawn lock only {out['created']}/{N} worktrees were created — "
        "the lock did not serialize create_worktree as designed"
    )


# ─── dump-path symmetry fix (sw-coord-p21): the lock must be WIRED into dump.main ─────


def _clean_dump_env(monkeypatch, home: Path) -> None:
    """The minimal env for an un-gated active dump with worktree isolation ON."""
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.setenv("HANDOFF_WORKTREE_ISOLATION", "on")
    for v in ("HANDOFF_RETRO_MANDATE", "HANDOFF_RETRO_BYPASS", "HANDOFF_AUDIT_MANDATE"):
        monkeypatch.delenv(v, raising=False)


def _dump_argv(task: str, ws: Path) -> list[str]:
    return [
        "--task", task, "--next", "brief", "--project", PROJECT,
        "--workspace", str(ws), "--status", "active",
    ]


def test_dump_main_holds_spawn_lock_around_create_worktree(tmp_path, monkeypatch) -> None:
    """DETERMINISTIC fail-without-fix sentinel (no concurrency, no flakiness).

    Stub ``create_worktree`` to probe the project spawn lock the instant the dump path
    calls it: a non-blocking (wait=0) acquire of the SAME lock must RAISE LockHeld,
    proving ``resolve_spawn_workspace`` is already holding it. Pre-fix the lock is free
    here, so ``LockHeld`` would NOT raise and this assertion fails — exactly the
    regression this guards. The stub still delegates to the real create so the dump
    completes normally (rc == 0)."""
    ws = _bare_and_clone(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _clean_dump_env(monkeypatch, home)

    real_create = wt.create_worktree
    captured: dict = {}

    def spy_create(**kwargs):
        try:
            with project_spawn_lock(PROJECT, root=home, wait=0.0):
                captured["lock_held_during_create"] = False  # lock was FREE → not wired
        except LockHeld:
            captured["lock_held_during_create"] = True  # dump path holds it → fix wired
        return real_create(**kwargs)

    monkeypatch.setattr(wt, "create_worktree", spy_create)
    rc = dump.main(_dump_argv("wired-task", ws))
    assert rc == 0
    assert captured.get("lock_held_during_create") is True, (
        "dump.resolve_spawn_workspace did NOT hold the project spawn lock while creating "
        "the worktree — the sw-coord-p21 symmetry fix is missing or reverted"
    )
    assert (home / PROJECT / "worktrees" / "wired-task").exists()


def test_dump_main_concurrent_all_create_worktrees(tmp_path, monkeypatch) -> None:
    """END-TO-END: N parallel ``dump.main`` active dumps (worktree isolation ON, unique
    task each) against ONE shared source repo. With the spawn lock wired into the dump
    path the racy ``git worktree add -b`` is serialized, so EVERY dump creates its
    worktree (created == N). Pre-fix the un-serialized ``.git/config`` race degrades some
    dumps to the shared tree (created < N) — this is the realistic regression the lock fixes.

    env is set once before the barrier release (process-global; every thread inherits it).
    """
    ws = _bare_and_clone(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _clean_dump_env(monkeypatch, home)

    start = threading.Barrier(N)
    results: dict[int, object] = {}
    guard = threading.Lock()

    def worker(i: int) -> None:
        task = f"dct{i}"
        start.wait()  # release all N together → maximal contention on .git/config
        try:
            rc: object = dump.main(_dump_argv(task, ws))
        except SystemExit as e:  # argparse / explicit SystemExit surfaces as the code
            rc = e.code
        except Exception as e:  # noqa: BLE001 — capture any crash as data, never hide it
            rc = f"EXC:{type(e).__name__}: {e}"
        with guard:
            results[i] = rc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)

    created = sum(1 for i in range(N) if (home / PROJECT / "worktrees" / f"dct{i}").exists())
    print(f"[DUMP-CONCURRENT] created={created}/{N} rcs={results}")
    # No dump may crash or fail-closed: the lock makes parallel worktree workers QUEUE,
    # never reject (120s wait >> the few serialized creates here).
    assert all(rc == 0 for rc in results.values()), f"a dump did not return 0: {results}"
    assert created == N, (
        f"only {created}/{N} concurrent dumps created their worktree — the dump path is "
        "not serializing create_worktree under the project spawn lock (fix missing/reverted)"
    )
