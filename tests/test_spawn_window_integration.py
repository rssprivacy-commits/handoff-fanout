"""Phase 7 — P0 spawn-window integration tests (plan Task 7.1 / design §10).

End-to-end tests across the spawn-window-unify components, exercising the REAL
artifacts each side produces/consumes (no hand-rolled doubles of either side):

  ① concurrent worktree isolation — 3 workers spawned TRULY concurrently (separate
    OS processes released by a file barrier) on one source repo → 3 independent
    worktrees / workspaces / .uri intents, distinct nonces, no git lock clashes;
  ② singlepane concurrency hard-REJECT (design §5.4) — exactly one of two truly
    concurrent dispatches wins; the loser fail-closes and the real repo is never
    touched. (Boundary: the active-pane signal is the PRE-consumption contract —
    sidecar + unconsumed ``queue/<task>.uri``. Post-consumption blindness is the
    known dump-Task5.1 §5.4 gap, deliberately NOT re-tested here.)
  ③ autoclose does not mis-fire (design §6) — the engine-produced succession
    intent drives the REAL ``install/auto-continue.sh`` autoclose segment; the
    critical section interoperates with ``handoff_fanout.spawn_lock`` (same
    ``.spawn.lock`` dir); the design-§6 "no pending/inflight intent" gate is
    implemented (t41b — formerly pinned here as an xfail) and exercised as a
    regular passing test;
  ④ P0 safety — per-project gating (an ERP-gated config leaks NOTHING into a
    sibling project's spawn artifacts), THIN workspace (window.title + the 3
    single-pane UX keys only, never a coordinator settings block), and the full
    dx-spawn(p6b router) → ``handoff spawn`` chain (env ``DX_SPAWN_SH`` locates
    the router; skipped with an explicit reason when unset — no hardcoded
    machine paths).

C′ sandbox red line: every test runs against a tmp_path ``HANDOFF_HOME``/
``HANDOFF_ROOT`` + tmp git repos; every external exit (``open`` / ``osascript`` /
``code``) is stubbed to a recorder. No live queue/config, no real windows.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

from handoff_fanout import cli, handoff_precheck
from handoff_fanout.spawn_lock import project_spawn_lock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
WATCHDOG = REPO_ROOT / "install" / "auto-continue.sh"

PRED_NONCE = "0011223344556677"  # well-formed 16-hex predecessor nonce


# ─── shared harness ──────────────────────────────────────────────────────────


def _home(tmp_path: Path, config: str = "{}") -> Path:
    home = tmp_path / "handoff-home"
    home.mkdir()
    (home / "config.json").write_text(config)
    return home


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _bare_and_clone(tmp_path: Path, name: str = "sandbox-proj") -> tuple[Path, Path]:
    """A git repo with a bare remote + a published ``main`` — what create_worktree needs.
    ``name`` must be a kebab-valid engine slug (④(c) derives the slug from the basename)."""
    bare = tmp_path / f"{name}-origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    ws = tmp_path / name
    subprocess.run(["git", "clone", str(bare), str(ws)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _run_git(["config", k, v], ws)
    (ws / "README.md").write_text("base\n")
    _run_git(["add", "."], ws)
    _run_git(["commit", "-qm", "init"], ws)
    _run_git(["push", "-q", "origin", "main"], ws)
    subprocess.run(
        ["git", "remote", "set-head", "origin", "main"], cwd=str(ws), capture_output=True
    )
    return bare, ws


def _spawn_env(home: Path) -> dict[str, str]:
    """Env for a ``handoff spawn`` SUBPROCESS: this worktree's engine on PYTHONPATH,
    the sandbox HANDOFF_HOME, and no retro/audit/isolation env leaking in."""
    env = dict(os.environ)
    env["HANDOFF_HOME"] = str(home)
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    for var in (
        "HANDOFF_RETRO_MANDATE",
        "HANDOFF_RETRO_BYPASS",
        "HANDOFF_AUDIT_MANDATE",
        "HANDOFF_WORKTREE_ISOLATION",
        "HANDOFF_AUTOCLOSE_ENABLED",
    ):
        env.pop(var, None)
    return env


# A barrier-released runner: the process loads, then BLOCKS until the parent creates
# the barrier file, then calls spawn.main(argv). Releasing N pre-started processes off
# one file gives genuine simultaneous entry into the engine (not staggered Popen starts).
_BARRIER_RUNNER = (
    "import os, sys, time\n"
    "barrier = os.environ['SPAWN_BARRIER']\n"
    "deadline = time.time() + 30\n"
    "while not os.path.exists(barrier):\n"
    "    if time.time() > deadline:\n"
    "        sys.exit(99)\n"
    "    time.sleep(0.005)\n"
    "from handoff_fanout.spawn import main\n"
    "sys.exit(main(sys.argv[1:]))\n"
)


def _concurrent_spawns(
    home: Path, tmp_path: Path, argvs: list[list[str]]
) -> list[subprocess.CompletedProcess]:
    """Launch one spawn subprocess per argv, release them simultaneously, wait for all."""
    barrier = tmp_path / "barrier.flag"
    env = _spawn_env(home)
    env["SPAWN_BARRIER"] = str(barrier)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _BARRIER_RUNNER, *argv],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for argv in argvs
    ]
    time.sleep(0.3)  # let every process reach the barrier wait
    barrier.write_text("go\n")
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        results.append(subprocess.CompletedProcess(p.args, p.returncode, stdout=out, stderr=err))
    return results


def _worker_argv(project: str, task: str, *, isolation: str, workspace: Path) -> list[str]:
    return [
        "--project",
        project,
        "--task-id",
        task,
        "--role",
        "worker",
        "--isolation",
        isolation,
        "--workspace",
        str(workspace),
        "--prompt",
        f"work on {task}",
    ]


def _sidecar(home: Path, project: str, task: str) -> dict:
    return json.loads((home / project / "queue" / f"{task}.singlepane").read_text())


def _uri_lines(home: Path, project: str, task: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (home / project / "queue" / f"{task}.uri").read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def _decoded_prompt(home: Path, project: str, task: str) -> str:
    uri = _uri_lines(home, project, task)["URI"]
    _, _, qs = uri.partition("?prompt=")
    return urllib.parse.unquote(qs)


# ─── ① concurrent worktree isolation (design §10 "P0 隔离" / plan T7.1) ───────


def test_concurrent_three_workers_isolated(tmp_path: Path) -> None:
    """3 workers spawned TRULY concurrently (separate processes, one barrier release)
    on the same source repo → 3 independent worktrees, 3 workspaces, 3 intents;
    distinct nonces; per-task 🆔 prompts; no git lock clash; no cross-tree writes."""
    project = "iso-proj"
    home = _home(tmp_path)
    _, ws = _bare_and_clone(tmp_path, name=project)
    tasks = ["wk-alpha", "wk-beta", "wk-gamma"]

    results = _concurrent_spawns(
        home,
        tmp_path,
        [_worker_argv(project, t, isolation="worktree", workspace=ws) for t in tasks],
    )

    # All 3 succeed — concurrent worktree creation must not trip over a shared
    # git lock (a `index.lock` / `cannot lock ref` crash would surface as rc!=0).
    for t, r in zip(tasks, results, strict=True):
        assert r.returncode == 0, f"{t}: rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"

    nonces: set[str] = set()
    wt_dirs: list[Path] = []
    for t in tasks:
        # one worktree per task, opened via its own nonce-titled workspace
        wt = home / project / "worktrees" / t
        assert wt.is_dir(), f"{t}: worktree missing"
        wt_dirs.append(wt)
        uri = _uri_lines(home, project, t)
        assert Path(uri["WORKSPACE"]) == wt  # COLD_WINDOW path: WORKSPACE = worktree dir
        sc = _sidecar(home, project, t)
        assert sc["isolation"] == "worktree"
        assert sc["role"] == "worker"
        title = json.loads((wt / ".handoff.code-workspace").read_text())["settings"]["window.title"]
        assert t in title and sc["spawn_nonce"] in title  # title binds THIS task's nonce
        nonces.add(sc["spawn_nonce"])
        # each prompt carries its own 🆔 (owner window-identity contract)
        assert _decoded_prompt(home, project, t).startswith(f"🆔{t}")
        # each worktree sits on its own branch
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(wt),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head == f"handoff/{t}"
    assert len(nonces) == 3, f"nonces not distinct: {nonces}"

    # file isolation: a write in worktree A never appears in B/C or the source tree
    (wt_dirs[0] / "only-in-alpha.txt").write_text("alpha\n")
    for other in wt_dirs[1:]:
        assert not (other / "only-in-alpha.txt").exists()
    assert not (ws / "only-in-alpha.txt").exists()

    # no stray lock left behind in the shared source repo
    assert not (ws / ".git" / "index.lock").exists()
    # the source tree itself was never dirtied by the spawns
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(ws), capture_output=True, text=True, check=True
    ).stdout
    assert status.strip() == "", f"source repo dirtied by concurrent spawns: {status}"


def test_worktree_publish_holds_project_spawn_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worktree path's create+publish runs as ONE critical section under the
    project ``.spawn.lock`` — the watchdog's try_autoclose reads sidecars under that
    same lock and assumes every sidecar writer holds it (R2 TOCTOU contract)."""
    from handoff_fanout import spawn

    project = "lockwt-proj"
    home = _home(tmp_path)
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    _, ws = _bare_and_clone(tmp_path, name=project)

    seen: dict[str, bool] = {}
    real_write_uri = spawn._write_uri

    def probe(queue_dir: Path, task: str, *, workspace: Path, uri: str) -> None:
        seen["lock_held"] = (home / project / ".spawn.lock").is_dir()
        real_write_uri(queue_dir, task, workspace=workspace, uri=uri)

    monkeypatch.setattr(spawn, "_write_uri", probe)
    rc = cli.main(
        ["spawn", *_worker_argv(project, "lockwt-task", isolation="worktree", workspace=ws)]
    )
    assert rc == 0
    assert seen.get("lock_held") is True, (
        "worktree publish ran OUTSIDE the project spawn lock — try_autoclose's "
        "critical-section assumption (all sidecar writers hold the lock) is broken"
    )


def test_worktree_spawn_lock_contention_fails_closed_after_bounded_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holder that never releases within the bounded wait → the worktree spawn
    fail-closes (rc 2, no partial intent) instead of blocking forever."""
    from handoff_fanout import spawn

    project = "lockwt-stuck"
    home = _home(tmp_path)
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.setattr(spawn, "_WORKTREE_LOCK_WAIT", 0.2)
    _, ws = _bare_and_clone(tmp_path, name=project)

    with project_spawn_lock(project, root=home):
        rc = cli.main(
            ["spawn", *_worker_argv(project, "stuck-task", isolation="worktree", workspace=ws)]
        )
    assert rc == 2
    qd = home / project / "queue"
    assert not (qd / "stuck-task.uri").exists()
    assert not (qd / "stuck-task.singlepane").exists()


# ─── ② singlepane concurrency hard-REJECT (design §5.4 / §10 "P0 并发硬拦") ────


def test_singlepane_second_dispatch_rejected_while_intent_inflight(tmp_path: Path) -> None:
    """Worker #1's intent is in flight (sidecar + unconsumed .uri) → dispatching
    worker #2 fail-closes (rc 2): no second intent, and the REAL repo dir is never
    written into (绝不落主目录直改)."""
    project = "sp-proj"
    home = _home(tmp_path)
    repo = tmp_path / "sp-repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n")
    before = sorted(p.name for p in repo.iterdir())
    env = _spawn_env(home)

    first = subprocess.run(
        [sys.executable, "-m", "handoff_fanout.spawn"]
        + _worker_argv(project, "sp-first", isolation="singlepane", workspace=repo),
        env=env,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr

    second = subprocess.run(
        [sys.executable, "-m", "handoff_fanout.spawn"]
        + _worker_argv(project, "sp-second", isolation="singlepane", workspace=repo),
        env=env,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 2
    assert "REJECT" in second.stderr
    qd = home / project / "queue"
    assert not (qd / "sp-second.uri").exists()
    assert not (qd / "sp-second.singlepane").exists()
    assert (qd / "sp-first.uri").exists()  # the winner's intent is untouched
    # the real repo was never touched by either dispatch (workspace lives OUT-OF-TREE)
    assert sorted(p.name for p in repo.iterdir()) == before


def test_singlepane_truly_concurrent_dispatch_exactly_one_wins(tmp_path: Path) -> None:
    """Two TRULY concurrent singlepane worker dispatches (barrier-released processes)
    → exactly ONE intent is produced; the loser hard-rejects (rc 2) whether it lost
    on the .spawn.lock or on the winner's already-published intent."""
    project = "sp-race"
    home = _home(tmp_path)
    repo = tmp_path / "sp-race-repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n")
    tasks = ["race-one", "race-two"]

    results = _concurrent_spawns(
        home,
        tmp_path,
        [_worker_argv(project, t, isolation="singlepane", workspace=repo) for t in tasks],
    )

    rcs = sorted(r.returncode for r in results)
    assert rcs == [0, 2], [(r.returncode, r.stderr) for r in results]
    qd = home / project / "queue"
    produced = [t for t in tasks if (qd / f"{t}.uri").exists()]
    assert len(produced) == 1, f"expected exactly one published intent, got {produced}"
    loser = next(t for t in tasks if t not in produced)
    assert not (qd / f"{loser}.singlepane").exists()  # no partial loser artifacts
    # the real repo dir was never written into by either contender
    assert sorted(p.name for p in repo.iterdir()) == ["README.md"]


# ─── ③ autoclose does not mis-fire (design §6 / §10 "竞态原子") ────────────────
#
# These drive the REAL install/auto-continue.sh autoclose segment with the intent
# the REAL engine (`handoff spawn`) produced — the cross-component contract — with
# HANDOFF_SKIP_SPAWN=1 (autoclose segment only) and open/osascript stubbed.


def _write_stub(path: Path, sink: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{sink}"\nexit 0\n')
    path.chmod(0o755)


def _watchdog_env(home: Path, tmp_path: Path) -> dict[str, str]:
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(exist_ok=True)
    open_stub, osa_stub = stub_dir / "open", stub_dir / "osascript"
    _write_stub(open_stub, tmp_path / "open.log")
    _write_stub(osa_stub, tmp_path / "osascript.log")
    env = dict(os.environ)
    env.update(
        {
            "HANDOFF_ROOT": str(home),
            "HANDOFF_OPEN_CMD": str(open_stub),
            "HANDOFF_OSASCRIPT_CMD": str(osa_stub),
            "HANDOFF_SKIP_SPAWN": "1",
            "HANDOFF_VSCODE_CHECK": "0",
            "HANDOFF_AUTOCLOSE_ENABLED": "1",
        }
    )
    env.pop("HANDOFF_HOME", None)
    env["_OPEN_SINK"] = str(tmp_path / "open.log")
    return env


def _run_watchdog(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(WATCHDOG)], env=env, capture_output=True, text=True, timeout=20
    )


def _open_log(env: dict[str, str]) -> str:
    sink = Path(env["_OPEN_SINK"])
    return sink.read_text() if sink.exists() else ""


def _succession_ready(home: Path, project: str, task: str) -> None:
    """The on-disk state of a succession whose successor window already landed:
    its own .uri consumed (mv → launched/, exactly what the watchdog does), retro
    evidence + old_ready + .submitted in place."""
    queue = home / project / "queue"
    launched = home / project / "launched"
    launched.mkdir(parents=True, exist_ok=True)
    (queue / f"{task}.uri").rename(launched / f"{task}-consumed.txt")

    payload = handoff_precheck.build_evidence(
        task_id=task,
        project=project,
        workspace=Path("/tmp"),
        nonce=None,
        phase0={k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS},
        phase1={k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
    )
    evidence = home / project / "precheck" / f"{task}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, evidence)

    ack = home / project / "ack"
    ack.mkdir(parents=True, exist_ok=True)
    (ack / f"{task}.old_ready").write_text(
        json.dumps(
            {
                "schema_version": handoff_precheck.EVIDENCE_SCHEMA_VERSION,
                "task_id": task,
                "nonce": "it-old-nonce",
                "session_id": "it-session",
                "session_id_kind": "fallback-fingerprint",
                "commit_hash": "abc1234",
                "push_completed_at": "2026-06-10T10:00:00+00:00",
                "tests_passed": True,
                "memory_updated": True,
                "dump_success": True,
                "retro_evidence_hash": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                "retro_evidence_path": str(evidence.relative_to(home / project)),
                "retro_evidence_path_absolute": str(evidence),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    (ack / f"{task}.submitted").write_text("2026-06-10 10:00:00\nstubbed submit\n")


def _spawn_succession(home: Path, project: str, task: str, repo: Path) -> dict:
    """Produce a REAL succession intent via the engine CLI; returns its sidecar.

    G4 收口 (Step1): a succession spawn is no longer a bare CLI path — issue the
    one-time authority token first, exactly as a retro-gated
    ``audit-close --coordinator --status active`` would. Step2 C binds the token to the
    SUCCESSOR ``task`` (the audit-close --task), so issue for the task we spawn."""
    from handoff_fanout import succession_authority as _authority

    token = _authority.issue_token(home=home, project=project, task=task)
    rc = cli.main(
        [
            "spawn",
            "--project",
            project,
            "--task-id",
            task,
            "--role",
            "supervisor_succession",
            "--isolation",
            "singlepane",
            "--workspace",
            str(repo),
            "--prompt",
            "take over coordination",
            "--predecessor-nonce",
            PRED_NONCE,
            "--succession-token",
            str(token),
        ]
    )
    assert rc == 0
    return _sidecar(home, project, task)


@pytest.fixture
def sp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "succ-repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n")
    return repo


def test_engine_succession_intent_drives_autoclose_after_consumption(
    tmp_path: Path, sp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-component happy path: the ENGINE-produced succession sidecar (not a
    hand-written double) is what the watchdog reads; once the succession's own
    intent is consumed and retro evidence is intact, autoclose fires ONE URI
    carrying the engine's spawn_nonce + the predecessor's nonce."""
    project, task = "succ-proj", "succ-take-over"
    home = _home(tmp_path)
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    sc = _spawn_succession(home, project, task, sp_repo)
    assert sc["role"] == "supervisor_succession"
    _succession_ready(home, project, task)

    env = _watchdog_env(home, tmp_path)
    proc = _run_watchdog(env)
    assert proc.returncode == 0, proc.stderr

    log = _open_log(env)
    assert log.count("task_id=") == 1
    assert "vscode://dharmaxis.handoff-helper/autoclose" in log
    assert f"task_id={task}" in log
    assert "role=supervisor_succession" in log
    assert f"predecessor_nonce={PRED_NONCE}" in log
    assert f"nonce={sc['spawn_nonce']}" in log  # the ENGINE's nonce, end to end
    assert (home / project / "ack" / f"{task}.autoclose_done").exists()


def test_autoclose_skips_while_python_spawn_lock_held_then_fires_after_release(
    tmp_path: Path, sp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock interop (design §6 R2r2-R2): the bash autoclose critical section and
    handoff_fanout.spawn_lock share ONE ``<project>/.spawn.lock``. While a spawn
    producer (the Python CM) holds it, the watchdog must SKIP — no URI, no marker;
    after release the next tick fires."""
    project, task = "lock-proj", "lock-succ"
    home = _home(tmp_path)
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    _spawn_succession(home, project, task, sp_repo)
    _succession_ready(home, project, task)
    env = _watchdog_env(home, tmp_path)

    with project_spawn_lock(project, root=home):
        proc = _run_watchdog(env)
        assert proc.returncode == 0, proc.stderr
        assert "task_id=" not in _open_log(env)  # withheld while the producer holds the lock
        ack = home / project / "ack"
        assert not (ack / f"{task}.autoclose_done").exists()
        assert not (ack / f"{task}.autoclose_failed.txt").exists()  # skip, not a failure

    proc = _run_watchdog(env)  # lock released → next tick completes the close
    assert proc.returncode == 0, proc.stderr
    assert _open_log(env).count("task_id=") == 1
    assert (home / project / "ack" / f"{task}.autoclose_done").exists()


def test_autoclose_withholds_while_worker_intent_inflight(
    tmp_path: Path, sp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design §6 临界区①: an unconsumed worker intent (queue/<other>.uri) dispatched by
    the predecessor coordinator must withhold the predecessor's autoclose until the
    intent is consumed."""
    project, task = "infl-proj", "infl-succ"
    home = _home(tmp_path)
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    # the OLD coordinator dispatched a worker whose intent is still in flight
    rc = cli.main(
        [
            "spawn",
            *_worker_argv(project, "infl-worker", isolation="singlepane", workspace=sp_repo),
        ]
    )
    assert rc == 0
    sc = _spawn_succession(home, project, task, sp_repo)
    assert sc["role"] == "supervisor_succession"
    _succession_ready(home, project, task)
    assert (home / project / "queue" / "infl-worker.uri").exists()  # still in flight

    env = _watchdog_env(home, tmp_path)
    proc = _run_watchdog(env)
    assert proc.returncode == 0, proc.stderr

    # design §6: the close must be withheld while the worker intent is in flight
    assert "task_id=" not in _open_log(env), (
        "autoclose fired while a worker intent was still in flight — design §6 临界区① "
        "pending-intent gate missing from try_autoclose"
    )
    assert not (home / project / "ack" / f"{task}.autoclose_done").exists()


# ─── ④ P0 safety: per-project gating + thin workspace + full chain (§10 "P0 安全") ─

ERP_MARKERS = (
    "paid_amount",
    "journal_service",
    "V3.6",
    "ROADMAP_MARKER_DO_NOT_LEAK",
)


def _gated_config(tmp_path: Path) -> str:
    """A config shaped like the live ERP-gated one: red lines + roadmap scoped to
    erp-system ONLY, plus one truly-global block."""
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("# roadmap\n\nROADMAP_MARKER_DO_NOT_LEAK_42\n")
    return json.dumps(
        {
            "inject_blocks": ["## Global\n- never force-push a shared branch"],
            "project_inject_blocks": {
                "erp-system": [
                    "## V3.6 红线 (不可破)\n- paid_amount 裸写 / journal_service.post_entry()"
                ]
            },
            "roadmap": {"path": str(roadmap), "projects": ["erp-system"]},
        }
    )


def _assert_no_erp_leak(home: Path, project: str, task: str) -> None:
    """Every artifact of this spawn intent — decoded prompt, .uri, sidecar, workspace
    file(s) — carries ZERO ERP-gated content."""
    blobs = {
        "prompt": _decoded_prompt(home, project, task),
        "uri": (home / project / "queue" / f"{task}.uri").read_text(),
        "sidecar": (home / project / "queue" / f"{task}.singlepane").read_text(),
    }
    ws_file = Path(_sidecar(home, project, task)["workspace"])
    blobs["workspace"] = ws_file.read_text()
    for name, blob in blobs.items():
        for marker in ERP_MARKERS:
            assert marker not in blob, f"ERP leak: {marker!r} in {name} of {project}/{task}"


def test_engine_gating_no_erp_leak_singlepane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-project gating regression (the v1.12.0 leak class): with an ERP-gated
    config in HANDOFF_HOME, a singlepane spawn for a SIBLING project produces
    artifacts with zero ERP content."""
    project = "wilde-hexe"
    home = _home(tmp_path, config=_gated_config(tmp_path))
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    repo = tmp_path / "wh-repo"
    repo.mkdir()
    rc = cli.main(
        ["spawn", *_worker_argv(project, "wh-task", isolation="singlepane", workspace=repo)]
    )
    assert rc == 0
    _assert_no_erp_leak(home, project, "wh-task")


def test_engine_gating_no_erp_leak_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same gating regression on the worktree path — including the worktree's own
    .handoff.code-workspace open target."""
    project = "gated-proj"
    home = _home(tmp_path, config=_gated_config(tmp_path))
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    _, ws = _bare_and_clone(tmp_path, name=project)
    rc = cli.main(
        ["spawn", *_worker_argv(project, "gated-task", isolation="worktree", workspace=ws)]
    )
    assert rc == 0
    _assert_no_erp_leak(home, project, "gated-task")
    wt_ws = home / project / "worktrees" / "gated-task" / ".handoff.code-workspace"
    blob = wt_ws.read_text()
    for marker in ERP_MARKERS:
        assert marker not in blob, f"ERP leak: {marker!r} in worktree workspace"


def test_worktree_workspace_settings_are_thin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0 thin workspace (design §4): the worktree .handoff.code-workspace carries
    ONLY folders + window.title + the 3 single-pane UX keys — never a coordinator
    settings block (red titleBar / colorCustomizations / inject content), so
    per-project gating stays in the target repo's own .vscode."""
    project = "thin-proj"
    home = _home(tmp_path)
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    _, ws = _bare_and_clone(tmp_path, name=project)
    rc = cli.main(
        ["spawn", *_worker_argv(project, "thin-task", isolation="worktree", workspace=ws)]
    )
    assert rc == 0
    spec = json.loads(
        (home / project / "worktrees" / "thin-task" / ".handoff.code-workspace").read_text()
    )
    assert set(spec) == {"folders", "settings"}
    assert set(spec["settings"]) == {
        "window.title",
        "workbench.activityBar.location",
        "workbench.startupEditor",
        "claudeCode.preferredLocation",
        "terminal.integrated.env.osx",  # Step2 B 轨二: all-path additive session signal
    }
    assert spec["settings"]["terminal.integrated.env.osx"] == {
        "HANDOFF_SESSION_ROLE": "worker",
        "HANDOFF_SESSION_TASK": "thin-task",
    }
    # the coordinator-only red-top block must never ride along
    assert "workbench.colorCustomizations" not in spec["settings"]


# ─── ④(c) full chain: dx-spawn (p6b thin router) → handoff spawn ──────────────


def _dx_spawn_sh() -> Path | None:
    p = os.environ.get("DX_SPAWN_SH", "")
    return Path(p) if p and Path(p).is_file() else None


@pytest.mark.skipif(
    _dx_spawn_sh() is None,
    reason=(
        "DX_SPAWN_SH not set or not a file — export DX_SPAWN_SH=<dharmaxis worktree>/"
        "scripts/dx-spawn-session.sh to run the dx-spawn→handoff-spawn full chain "
        "(no hardcoded machine paths in the committed suite)"
    ),
)
def test_full_chain_dx_spawn_routes_to_engine_with_gating(tmp_path: Path) -> None:
    """The REAL p6b router invokes the REAL engine: registry routes worker_isolation,
    the unified_spawn query runs through the engine's own config parser (shebang
    path, no query stub), the engine produces the worktree intent in the sandbox
    HANDOFF_HOME, and the chain leaks zero ERP content + opens zero real windows.

    Sandbox seal: the stub bin dir is PREPENDED to PATH so a bare `open`/`code`/
    `osascript` (the regression that once opened 12 real tabs) lands on a stub, and
    a positive-control probe proves the shadow works BEFORE the negative "log
    absent == never called" assertions are trusted. Residual risk: a hardcoded
    absolute `/usr/bin/open` bypasses both PATH and the seam env — that layer is
    owned by p6b's source-level tripwire test (dharmaxis
    tests/test_dx_spawn_routing.py) and deliberately not duplicated here."""
    project, task = "chain-proj", "chain-task"
    home = _home(tmp_path, config=_gated_config(tmp_path))
    _, ws = _bare_and_clone(tmp_path, name=project)

    registry = tmp_path / "project-registry.json"
    registry.write_text(
        json.dumps(
            {
                "projects": {
                    "chain": {
                        "name": "Chain",
                        "paths": {"root": str(ws)},
                        "worker_isolation": "worktree",
                    }
                }
            }
        )
    )

    # `handoff` CLI wrapper running THIS worktree's engine; its shebang is the live
    # interpreter so the router's unified_spawn shebang-query path runs for real.
    handoff_bin = tmp_path / "bin" / "handoff"
    handoff_bin.parent.mkdir()
    handoff_bin.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from handoff_fanout.cli import main\n"
        "sys.exit(main(sys.argv[1:]))\n"
    )
    handoff_bin.chmod(0o755)

    records = tmp_path / "records"
    records.mkdir()
    stubs = {}
    for name in ("open", "code", "osascript"):
        stub = tmp_path / "bin" / name
        _write_stub(stub, records / f"{name}.log")
        stubs[name] = stub

    env = {
        # stub bin dir FIRST: bare `open`/`code`/`osascript` resolve to the stubs,
        # never /usr/bin; real git/python3 still resolve from the system dirs.
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": os.environ["HOME"],
        "PYTHONPATH": str(SRC_DIR),
        "HANDOFF_HOME": str(home),
        "DX_PROJECT_REGISTRY": str(registry),
        "HANDOFF_BIN": str(handoff_bin),
        "OPEN_BIN": str(stubs["open"]),
        "CODE_BIN": str(stubs["code"]),
        "HANDOFF_OSASCRIPT_CMD": str(stubs["osascript"]),
        # watchdog seam, not exercised here — but if chain code ever falls back to
        # `${HANDOFF_OPEN_CMD:-/usr/bin/open}` (absolute default that PATH cannot
        # shadow), the seam still lands on the stub instead of the real binary.
        "HANDOFF_OPEN_CMD": str(stubs["open"]),
    }

    # positive control (tripwire): prove the PATH shadow really intercepts bare
    # invocations before trusting the negative assertions at the end. A silently
    # broken shadow would otherwise let a regressed router invoke the REAL
    # /usr/bin/open during the test run and still pass.
    for name in ("open", "code", "osascript"):
        probe = subprocess.run(
            ["/bin/bash", "-c", f"{name} --handoff-shadow-probe"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        log = records / f"{name}.log"
        assert probe.returncode == 0 and log.is_file(), f"PATH shadow inactive for {name}"
        assert "--handoff-shadow-probe" in log.read_text()
        log.unlink()  # clean slate: from here on, "log absent" == "never invoked"

    proc = subprocess.run(
        [
            "/bin/bash",
            str(_dx_spawn_sh()),
            "--project",
            str(ws),
            "--prompt",
            "do gated work",
            "--task-id",
            task,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "unified_spawn=enabled" in proc.stdout

    # the engine REALLY produced the worktree intent in the sandbox home
    assert (home / project / "worktrees" / task).is_dir()
    prompt = _decoded_prompt(home, project, task)
    assert prompt.startswith(f"🆔{task}")
    assert "do gated work" in prompt
    sc = _sidecar(home, project, task)
    assert sc["isolation"] == "worktree"
    assert sc["role"] == "worker"
    _assert_no_erp_leak(home, project, task)

    # the unified path NEVER opens windows / fires URIs / keystrokes itself
    for name in ("open", "code", "osascript"):
        assert not (records / f"{name}.log").exists(), f"{name} was invoked on the unified path"
