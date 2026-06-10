"""Step2 契约 A — G3 真沉淀机器证明 (memory snapshot baseline + WARN-mode verification).

Locks the amendment's A.1–A.5 + SHOULD#2/#3/#7:

  * baseline write on ALL THREE coordinator dispatch paths (``dump --coordinator``
    singlepane + worktree, ``spawn --role supervisor_succession``) — 0600, O_EXCL
    half-write proof, ``coordinator_task`` field naming, keep-first on a re-dispatch;
  * verification at the coordinator's own relay (``audit-close --self-task``):
    added/changed *.md = proof; deletions are not; WARN never blocks (rc unchanged);
  * SHOULD#3 project 对位断言 (a cross-project baseline is never compared);
  * A.4 fallback chain: launched-mtime weak proof (SHOULD#7 hit files + mtimes in the
    audit log) → no-evidence WARN-pass with an audit line (绝不静默).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from handoff_fanout import codex_audit, dump, handoff_precheck, spawn
from handoff_fanout import memory_baseline as mb
from handoff_fanout import succession_authority as _authority

PROJECT = "demo-proj"
SELF_TASK = "coord-leg-7"  # the CLOSING coordinator (has a dispatch baseline)
NEXT_TASK = "coord-leg-8"  # the successor its relay dispatches


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "HANDOFF_RETRO_BYPASS",
        "HANDOFF_RETRO_MANDATE",
        "HANDOFF_AUDIT_MANDATE",
        "HANDOFF_WORKTREE_ISOLATION",
        "HANDOFF_SAFE_COMMIT_LOCK",
        "HANDOFF_SAFE_COMMIT_BYPASS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(dump, "_notify", lambda *a, **k: None)


@pytest.fixture
def claude_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the Claude Code projects root so the suite never touches the real
    ``~/.claude/projects`` (memory dirs are derived from it)."""
    root = tmp_path / "claude-projects"
    root.mkdir()
    monkeypatch.setattr(mb, "claude_projects_root", lambda: root)
    return root


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str = "{}") -> Path:
    home = tmp_path / "handoff"
    home.mkdir()
    (home / "config.json").write_text(config)
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    return home


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _git_repo(tmp_path: Path, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    _run(["git", "init", "--quiet", "--initial-branch=main"], ws)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _run(["git", "config", k, v], ws)
    (ws / "README.md").write_text("base\n")
    _run(["git", "add", "."], ws)
    _run(["git", "commit", "-qm", "init"], ws)
    return ws


def _memory_dir(claude_root: Path, workspace: Path) -> Path:
    d = claude_root / mb.claude_project_slug(workspace) / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _head(ws: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, check=True
    ).stdout.strip()


def _close_argv(ws: Path, *, task: str = NEXT_TASK, self_task: str | None = None) -> list[str]:
    """A minimal valid coordinator active close (mirrors test_audit_close_coordinator)."""
    argv = [
        "--task",
        task,
        "--next",
        "next coordinator leg",
        "--project",
        PROJECT,
        "--workspace",
        str(ws),
        "--audit-mode",
        "empty_diff_attestation",
        "--audit-base",
        _head(ws),
        "--status",
        "active",
        "--coordinator",
    ]
    if self_task is not None:
        argv += ["--self-task", self_task]
    for k in handoff_precheck.PHASE0_KEYS:
        argv += ["--phase0-status", f"{k}=✅"]
    for k in handoff_precheck.PHASE1_KEYS:
        argv += ["--phase1-status", f"{k}=✅"]
    return argv


def _audit_log(home: Path) -> str:
    p = home / PROJECT / "authority" / _authority.AUDIT_LOG_NAME
    return p.read_text() if p.exists() else ""


# ─── slug / memory-dir derivation ─────────────────────────────────────────────


def test_slug_flattens_slashes_and_dots() -> None:
    assert (
        mb.claude_project_slug("/Users/o/Projects/handoff-fanout")
        == "-Users-o-Projects-handoff-fanout"
    )
    # Claude Code's real flattening maps '.' too (实证: ~/.claude-handoff → --claude-handoff).
    assert (
        mb.claude_project_slug("/Users/o/.claude-handoff/wt/leg")
        == "-Users-o--claude-handoff-wt-leg"
    )


# ─── baseline write (unit) ────────────────────────────────────────────────────


def test_write_baseline_schema_perms_and_recursive_snapshot(
    tmp_path, monkeypatch, claude_root
) -> None:
    home = _home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    mem = _memory_dir(claude_root, ws)
    (mem / "MEMORY.md").write_text("index\n")
    (mem / "sub").mkdir()
    (mem / "sub" / "lesson.md").write_text("deep lesson\n")
    (mem / "notes.txt").write_text("not markdown\n")  # excluded: *.md only

    mb.write_baseline(home=home, project=PROJECT, coordinator_task=SELF_TASK, workspace=ws)

    path = mb.baseline_path(home, PROJECT, SELF_TASK)
    assert (path.stat().st_mode & 0o777) == 0o600  # owner-only, issue_token 同款
    payload = json.loads(path.read_text())
    assert payload["schema"] == 1
    assert payload["project"] == PROJECT
    assert payload["coordinator_task"] == SELF_TASK  # SHOULD#2 naming
    assert set(payload["files"]) == {"MEMORY.md", "sub/lesson.md"}  # recursive, md-only
    assert "G3-BASELINE-WRITTEN" in _audit_log(home)


def test_write_baseline_keeps_first_on_re_dispatch(tmp_path, monkeypatch, claude_root) -> None:
    """A same-task retry re-dispatch must NOT absorb sedimentation that happened in
    between (the EARLIEST dispatch state is the honest baseline)."""
    home = _home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    mem = _memory_dir(claude_root, ws)
    mb.write_baseline(home=home, project=PROJECT, coordinator_task=SELF_TASK, workspace=ws)
    (mem / "lesson.md").write_text("written between the two dispatches\n")

    mb.write_baseline(home=home, project=PROJECT, coordinator_task=SELF_TASK, workspace=ws)

    payload = json.loads(mb.baseline_path(home, PROJECT, SELF_TASK).read_text())
    assert payload["files"] == {}, "the FIRST (empty) snapshot must win"
    assert "G3-BASELINE-KEPT" in _audit_log(home)


# ─── verification (unit) ──────────────────────────────────────────────────────


def _baseline_then(home, claude_root, ws, mutate) -> tuple[str, str]:
    mem = _memory_dir(claude_root, ws)
    (mem / "MEMORY.md").write_text("v1\n")
    mb.write_baseline(home=home, project=PROJECT, coordinator_task=SELF_TASK, workspace=ws)
    mutate(mem)
    return mb.verify_sedimentation(home=home, project=PROJECT, self_task=SELF_TASK, workspace=ws)


def test_verify_changed_hash_is_proof(tmp_path, monkeypatch, claude_root) -> None:
    home = _home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    status, detail = _baseline_then(
        home, claude_root, ws, lambda mem: (mem / "MEMORY.md").write_text("v2 — lesson added\n")
    )
    assert status == mb.VERIFY_OK
    assert "MEMORY.md" in detail
    assert "G3-SEDIMENTATION-OK" in _audit_log(home)


def test_verify_added_file_is_proof(tmp_path, monkeypatch, claude_root) -> None:
    home = _home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    status, _ = _baseline_then(
        home, claude_root, ws, lambda mem: (mem / "lesson-new.md").write_text("fresh lesson\n")
    )
    assert status == mb.VERIFY_OK


def test_verify_no_change_warns_and_deletion_is_not_proof(
    tmp_path, monkeypatch, claude_root
) -> None:
    home = _home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    mem = _memory_dir(claude_root, ws)
    (mem / "MEMORY.md").write_text("v1\n")
    (mem / "old.md").write_text("old\n")
    mb.write_baseline(home=home, project=PROJECT, coordinator_task=SELF_TASK, workspace=ws)
    (mem / "old.md").unlink()  # deletion only — not sedimentation

    status, detail = mb.verify_sedimentation(
        home=home, project=PROJECT, self_task=SELF_TASK, workspace=ws
    )

    assert status == mb.VERIFY_WARN
    assert "交棒前必复盘沉淀" in detail
    assert "G3-NO-SEDIMENTATION" in _audit_log(home)


def test_verify_cross_project_baseline_never_compared(tmp_path, monkeypatch, claude_root) -> None:
    """SHOULD#3 对位断言: a baseline whose recorded project differs (copied/misplaced
    file) is logged + treated as absent — the fallback chain runs instead."""
    home = _home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    _memory_dir(claude_root, ws)
    mb.write_baseline(home=home, project=PROJECT, coordinator_task=SELF_TASK, workspace=ws)
    path = mb.baseline_path(home, PROJECT, SELF_TASK)
    payload = json.loads(path.read_text())
    payload["project"] = "some-other-proj"
    path.write_text(json.dumps(payload))

    status, _ = mb.verify_sedimentation(
        home=home, project=PROJECT, self_task=SELF_TASK, workspace=ws
    )

    log = _audit_log(home)
    assert "G3-BASELINE-PROJECT-MISMATCH" in log
    assert status == mb.VERIFY_WARN  # fallback found no launched artifact either
    assert "G3-FALLBACK-NO-EVIDENCE" in log


def test_fallback_weak_mtime_proof_logs_hits(tmp_path, monkeypatch, claude_root) -> None:
    """A.4 + SHOULD#7: no baseline → launched-timestamp weak proof, audit-logging the
    concrete hit files + mtimes (cuts observe-period triage cost)."""
    home = _home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    launched = home / PROJECT / "launched"
    launched.mkdir(parents=True)
    marker = launched / f"{SELF_TASK}-1781000000000000000.txt"
    marker.write_text("launch\n")
    past = time.time() - 3600
    os.utime(marker, (past, past))
    mem = _memory_dir(claude_root, ws)
    (mem / "lesson.md").write_text("written during the session\n")  # mtime now > launch

    status, detail = mb.verify_sedimentation(
        home=home, project=PROJECT, self_task=SELF_TASK, workspace=ws
    )

    assert status == mb.VERIFY_WEAK_OK
    assert "lesson.md" in detail and "mtime=" in detail
    log = _audit_log(home)
    assert "G3-FALLBACK-WEAK-PASS" in log and "lesson.md" in log


def test_fallback_no_evidence_warn_passes_with_audit_line(
    tmp_path, monkeypatch, claude_root
) -> None:
    home = _home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    status, detail = mb.verify_sedimentation(
        home=home, project=PROJECT, self_task=SELF_TASK, workspace=ws
    )
    assert status == mb.VERIFY_WARN
    assert "WARN-pass" in detail or "cannot prove" in detail
    assert "G3-FALLBACK-NO-EVIDENCE" in _audit_log(home)


# ─── dispatch-path integration: the three baseline writers ────────────────────


def test_dump_coordinator_singlepane_writes_baseline(tmp_path, monkeypatch, claude_root) -> None:
    home = _home(tmp_path, monkeypatch, config=json.dumps({"singlepane_projects": [PROJECT]}))
    ws = _git_repo(tmp_path)
    mem = _memory_dir(claude_root, ws)
    (mem / "MEMORY.md").write_text("index\n")

    rc = dump.main(
        [
            "--task",
            NEXT_TASK,
            "--next",
            "relay leg",
            "--project",
            PROJECT,
            "--workspace",
            str(ws),
            "--status",
            "active",
            "--coordinator",
        ]
    )

    assert rc == 0
    payload = json.loads(mb.baseline_path(home, PROJECT, NEXT_TASK).read_text())
    assert payload["coordinator_task"] == NEXT_TASK
    assert payload["files"] == {"MEMORY.md": payload["files"]["MEMORY.md"]}


def test_dump_non_coordinator_writes_no_baseline(tmp_path, monkeypatch, claude_root) -> None:
    home = _home(tmp_path, monkeypatch, config=json.dumps({"singlepane_projects": [PROJECT]}))
    ws = _git_repo(tmp_path)
    rc = dump.main(
        [
            "--task",
            NEXT_TASK,
            "--next",
            "worker leg",
            "--project",
            PROJECT,
            "--workspace",
            str(ws),
            "--status",
            "active",
        ]
    )
    assert rc == 0
    assert not mb.baseline_path(home, PROJECT, NEXT_TASK).exists()


def test_dump_coordinator_worktree_writes_baseline_from_source_tree(
    tmp_path, monkeypatch, claude_root
) -> None:
    """The worktree relay derives the memory dir from the SOURCE tree (the real project
    workspace) — never from the successor's worktree path (whose slug has no memory)."""
    home = _home(tmp_path, monkeypatch, config=json.dumps({"worktree_projects": [PROJECT]}))
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", str(bare), str(ws)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _run(["git", "config", k, v], ws)
    (ws / "README.md").write_text("base\n")
    _run(["git", "add", "."], ws)
    _run(["git", "commit", "-qm", "init"], ws)
    _run(["git", "push", "-q", "origin", "main"], ws)
    mem = _memory_dir(claude_root, ws)
    (mem / "MEMORY.md").write_text("index\n")

    rc = dump.main(
        [
            "--task",
            NEXT_TASK,
            "--next",
            "relay leg",
            "--project",
            PROJECT,
            "--workspace",
            str(ws),
            "--status",
            "active",
            "--coordinator",
        ]
    )

    assert rc == 0
    assert (home / PROJECT / "worktrees" / NEXT_TASK).is_dir()  # the worktree relay ran
    payload = json.loads(mb.baseline_path(home, PROJECT, NEXT_TASK).read_text())
    assert payload["memory_dir"] == str(mem)  # source-tree slug, not the worktree's
    assert "MEMORY.md" in payload["files"]


def test_spawn_succession_writes_baseline_worker_does_not(
    tmp_path, monkeypatch, claude_root
) -> None:
    home = _home(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n")
    _memory_dir(claude_root, repo)

    assert (
        spawn.main(
            [
                "--project",
                PROJECT,
                "--task-id",
                "succ-leg",
                "--role",
                "supervisor_succession",
                "--isolation",
                "singlepane",
                "--workspace",
                str(repo),
                "--prompt",
                "succeed",
                "--predecessor-nonce",
                "feedfacecafebeef",
                "--succession-token",
                str(_authority.issue_token(home=home, project=PROJECT, task="succ-leg")),
            ]
        )
        == 0
    )
    assert mb.baseline_path(home, PROJECT, "succ-leg").exists()

    assert (
        spawn.main(
            [
                "--project",
                PROJECT,
                "--task-id",
                "succ-leg",  # same task: pane is its own (exclude_task) → allowed
                "--role",
                "worker",
                "--isolation",
                "singlepane",
                "--workspace",
                str(repo),
                "--prompt",
                "work",
            ]
        )
        == 0
    )
    # a worker spawn never writes a baseline — the one above is the succession's (KEPT).
    payload = json.loads(mb.baseline_path(home, PROJECT, "succ-leg").read_text())
    assert payload["coordinator_task"] == "succ-leg"


# ─── audit-close --self-task: the G3 verification point (WARN-only) ───────────


def test_audit_close_self_task_reports_ok_after_sedimentation(
    tmp_path, monkeypatch, claude_root, capsys
) -> None:
    """End-to-end A.2: leg N's dispatch (audit-close) baselines the successor; the
    successor sediments a lesson; ITS relay (audit-close --self-task) proves it (OK
    line), and rc stays 0 throughout (WARN mode never gates)."""
    home = _home(tmp_path, monkeypatch, config=json.dumps({"singlepane_projects": [PROJECT]}))
    ws = _git_repo(tmp_path)
    mem = _memory_dir(claude_root, ws)
    (mem / "MEMORY.md").write_text("index v1\n")

    # leg N closes → dispatches SELF_TASK as the next coordinator (baseline written).
    assert codex_audit.main_audit_close(_close_argv(ws, task=SELF_TASK)) == 0
    assert mb.baseline_path(home, PROJECT, SELF_TASK).exists()
    capsys.readouterr()

    (mem / "lesson-leg7.md").write_text("the distilled lesson\n")  # SELF_TASK sediments

    # SELF_TASK relays → its own close proves the sedimentation.
    rc = codex_audit.main_audit_close(_close_argv(ws, task=NEXT_TASK, self_task=SELF_TASK))
    out = capsys.readouterr()
    assert rc == 0
    assert "OK G3-sedimentation" in out.out
    assert "lesson-leg7.md" in out.out


def test_audit_close_self_task_warns_without_blocking(
    tmp_path, monkeypatch, claude_root, capsys
) -> None:
    _home(tmp_path, monkeypatch, config=json.dumps({"singlepane_projects": [PROJECT]}))
    ws = _git_repo(tmp_path)
    mem = _memory_dir(claude_root, ws)
    (mem / "MEMORY.md").write_text("index v1\n")
    assert codex_audit.main_audit_close(_close_argv(ws, task=SELF_TASK)) == 0
    capsys.readouterr()

    # NO sedimentation between dispatch and relay → loud WARN, rc still 0, token still issued.
    rc = codex_audit.main_audit_close(_close_argv(ws, task=NEXT_TASK, self_task=SELF_TASK))
    out = capsys.readouterr()
    assert rc == 0, "WARN mode must never block the relay"
    assert "WARN G3-no-sedimentation" in out.err
    assert "succession-authority-issued" in out.out


def test_audit_close_without_self_task_warns_identityless(
    tmp_path, monkeypatch, claude_root, capsys
) -> None:
    home = _home(tmp_path, monkeypatch, config=json.dumps({"singlepane_projects": [PROJECT]}))
    ws = _git_repo(tmp_path)
    _memory_dir(claude_root, ws)
    rc = codex_audit.main_audit_close(_close_argv(ws, task=NEXT_TASK))
    out = capsys.readouterr()
    assert rc == 0
    assert "WARN G3-no-self-task" in out.err
    assert "G3-NO-SELF-TASK" in _audit_log(home)


def test_audit_close_self_task_requires_coordinator(tmp_path, monkeypatch, capsys) -> None:
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)
    argv = _close_argv(ws, task=NEXT_TASK, self_task=SELF_TASK)
    argv.remove("--coordinator")
    rc = codex_audit.main_audit_close(argv)
    assert rc == 1
    assert "self-task-needs-coordinator" in capsys.readouterr().err
    assert not (home / PROJECT / "queue" / f"{NEXT_TASK}.uri").exists()
