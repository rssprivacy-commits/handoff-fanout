"""Step1 A-收口 — ``audit-close --coordinator --status active`` is THE coordinator
relay (中枢交棒) path. Locks the three tribrain MUSTs + the brief's five relay
scenarios (design 2026-06-10-coord-retro-enforcement-design.md / OWNER ruling Step 1):

  1. main-tree safety: a coordinator closing FROM the project main tree gets retro
     gate + successor intent ONLY — no destructive git op ever touches the main tree;
  2. cross-project relay (A 项目中枢派 B 项目中枢): expressible ONLY as the explicit
     ``--project <target> --workspace <target repo>`` pair; the implicit cwd default
     fails closed (never a silently misrouted intent);
  3. no-remote project: a worktree-isolation relay fails closed with an actionable
     remedy (never the silent shared-tree degrade); singlepane / isolation-off
     projects keep working without a remote;
  + the one-time succession authority (G4) is issued ONLY by a coordinator active
    close whose retro-gated dump succeeded, and the unpushed-HEAD fail-closed gate
    (前车之鉴) still blocks the relay.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from handoff_fanout import codex_audit, dump, handoff_precheck

PROJECT = "demo-proj"
TASK = "coord-leg-7"


# ─── fixtures / helpers ───────────────────────────────────────────────────────


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
    # C′ sandbox: no real notification center traffic from the suite.
    monkeypatch.setattr(dump, "_notify", lambda *a, **k: None)


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str = "{}") -> Path:
    home = tmp_path / "handoff"
    home.mkdir()
    (home / "config.json").write_text(config)
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    return home


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(ws: Path) -> None:
    _run(["git", "init", "--quiet", "--initial-branch=main"], ws)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _run(["git", "config", k, v], ws)


def _git_repo(tmp_path: Path, name: str = "ws") -> Path:
    """A standalone git repo with one commit and NO remote."""
    ws = tmp_path / name
    ws.mkdir()
    _init_repo(ws)
    (ws / "README.md").write_text("base\n")
    _run(["git", "add", "."], ws)
    _run(["git", "commit", "-qm", "init"], ws)
    return ws


def _bare_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare ``origin`` + a working clone with ``main`` pushed (worktree-mode ready)."""
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
    subprocess.run(
        ["git", "remote", "set-head", "origin", "main"], cwd=str(ws), capture_output=True
    )
    return bare, ws


def _head(ws: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, check=True
    ).stdout.strip()


def _close_argv(
    ws: Path,
    *,
    task: str = TASK,
    project: str | None = PROJECT,
    workspace: bool = True,
    coordinator: bool = True,
    status: str = "active",
) -> list[str]:
    argv = ["--task", task, "--next", "next coordinator leg"]
    if project is not None:
        argv += ["--project", project]
    if workspace:
        argv += ["--workspace", str(ws)]
    argv += [
        "--audit-mode",
        "empty_diff_attestation",
        "--audit-base",
        _head(ws),
        "--status",
        status,
    ]
    if coordinator:
        argv.append("--coordinator")
    for k in handoff_precheck.PHASE0_KEYS:
        argv += ["--phase0-status", f"{k}=✅"]
    for k in handoff_precheck.PHASE1_KEYS:
        argv += ["--phase1-status", f"{k}=✅"]
    return argv


def _tokens(home: Path, project: str = PROJECT) -> list[Path]:
    d = home / project / "authority"
    return sorted(d.glob("succession-*.token")) if d.is_dir() else []


# ─── scenario 1: worktree coordinator 交棒 (+ MUST 1 main-tree safety) ─────────


def test_worktree_coordinator_relay_main_tree_safe_and_token_issued(tmp_path, monkeypatch, capsys):
    """Coordinator closes FROM the project main tree (no worktree of its own): the
    engine runs the retro gate + produces the successor's worktree intent, and the
    main tree comes through untouched — HEAD unchanged, uncommitted work intact, the
    tree never removed/pruned (tribrain MUST 1)."""
    home = _home(tmp_path, monkeypatch, config=json.dumps({"worktree_projects": [PROJECT]}))
    _, ws = _bare_and_clone(tmp_path)
    head_before = _head(ws)
    (ws / "uncommitted-coordinator-note.md").write_text("live WIP in the main tree\n")

    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 0, rc
    # successor intent: a worktree relay (COLD path) + .uri pointing AT the worktree
    wt = home / PROJECT / "worktrees" / TASK
    assert wt.is_dir(), "successor worktree must exist"
    uri = (home / PROJECT / "queue" / f"{TASK}.uri").read_text()
    assert f"WORKSPACE={wt}" in uri
    # §五·2: the relay window is a coordinator window → red-topped
    spec = json.loads((wt / ".handoff.code-workspace").read_text())
    assert spec["settings"]["window.title"].startswith("🧭中枢·")
    # retro proof drove the relay (old_ready written from the evidence)
    assert (home / PROJECT / "ack" / f"{TASK}.old_ready").exists()
    # ── MUST 1: main tree untouched ──
    assert _head(ws) == head_before, "main tree HEAD must not move"
    assert (ws / "uncommitted-coordinator-note.md").exists(), "main-tree WIP must survive"
    assert (ws / ".git").exists() and (ws / "README.md").exists()
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=ws, capture_output=True, text=True
    ).stdout
    assert str(ws) in listed, "the main tree must still be the primary worktree"
    # ── G4: the one-time succession authority was issued by the gated close ──
    toks = _tokens(home)
    assert len(toks) == 1 and (toks[0].stat().st_mode & 0o777) == 0o600
    assert "succession-authority-issued" in capsys.readouterr().out


def test_worktree_coordinator_relay_stale_worktree_reclaim_spares_main_tree(tmp_path, monkeypatch):
    """MUST 1, destructive-branch coverage: a SAME-task stale worktree (its base has
    been advanced past) makes create_worktree run its remove+recreate reclaim — the
    only destructive git ops on the relay path. They must target the stale worktree
    ONLY; the main tree the coordinator is standing in survives untouched."""
    home = _home(tmp_path, monkeypatch, config=json.dumps({"worktree_projects": [PROJECT]}))
    _, ws = _bare_and_clone(tmp_path)
    assert codex_audit.main_audit_close(_close_argv(ws)) == 0  # leg 1: worktree @ base1
    wt = home / PROJECT / "worktrees" / TASK
    base1 = _head(wt)
    # advance origin/main past the worktree's base → the worktree is now STALE
    (ws / "advance.txt").write_text("move main\n")
    _run(["git", "add", "advance.txt"], ws)
    _run(["git", "commit", "-qm", "advance"], ws)
    _run(["git", "push", "-q", "origin", "main"], ws)
    head_before = _head(ws)
    (ws / "uncommitted-wip.md").write_text("WIP\n")

    rc = codex_audit.main_audit_close(_close_argv(ws))  # leg 2: reclaim + recreate

    assert rc == 0, rc
    assert wt.is_dir() and _head(wt) == head_before != base1, (
        "stale worktree must be recreated at the advanced base"
    )
    # main tree untouched by the reclaim
    assert _head(ws) == head_before
    assert (ws / "uncommitted-wip.md").exists() and (ws / "README.md").exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ws, capture_output=True, text=True, check=True
    ).stdout
    assert "uncommitted-wip.md" in status, "the main tree must still be a working repo"


# ─── scenario 2: singlepane coordinator 交棒 ──────────────────────────────────


def test_singlepane_coordinator_relay_redtop_and_token(tmp_path, monkeypatch):
    """A singlepane project (worktree mode off) relays without any remote: red-topped
    out-of-tree workspace + sidecar + token. Locks that the no-remote fail-closed gate
    NEVER over-blocks the singlepane path (it needs no origin containment)."""
    home = _home(tmp_path, monkeypatch, config=json.dumps({"singlepane_projects": [PROJECT]}))
    ws = _git_repo(tmp_path)  # deliberately NO remote

    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 0, rc
    sidecar = json.loads((home / PROJECT / "queue" / f"{TASK}.singlepane").read_text())
    assert sidecar["role"] == "worker" and sidecar["close_policy"] == "keep"
    spec = json.loads(Path(sidecar["workspace"]).read_text())
    assert spec["settings"]["window.title"].startswith("🧭中枢·")
    assert spec["settings"]["workbench.colorCustomizations"]["titleBar.activeBackground"] == (
        "#8B0000"
    )
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()
    assert len(_tokens(home)) == 1


# ─── scenario 3: cross-project 交棒 (A 项目中枢派 B 项目中枢) ──────────────────


def test_cross_project_relay_explicit_pair_works(tmp_path, monkeypatch):
    """1.2.2 conclusion: the semantics CAN express A→B — as the EXPLICIT pair
    ``--project <target> --workspace <target repo>``. Artifacts (queue/evidence/token)
    all land under the TARGET project, bound to the TARGET tree."""
    home = _home(tmp_path, monkeypatch)
    ws_a = _git_repo(tmp_path, name="proj-a")
    ws_b = _git_repo(tmp_path, name="proj-b")
    monkeypatch.chdir(ws_a)  # the A coordinator's cwd

    rc = codex_audit.main_audit_close(_close_argv(ws_b, project="target-proj", task="b-coord-1"))

    assert rc == 0, rc
    queue = home / "target-proj" / "queue"
    assert (queue / "b-coord-1.uri").exists() and (queue / "b-coord-1.md").exists()
    assert f"WORKSPACE={ws_b}" in (queue / "b-coord-1.uri").read_text()
    ev = json.loads(
        (home / "target-proj" / "precheck" / "b-coord-1.retro.evidence.json").read_text()
    )
    assert ev["workspace"] == str(ws_b), "evidence must bind the TARGET tree"
    assert len(_tokens(home, "target-proj")) == 1
    assert not (home / "proj-a").exists(), "nothing may leak into the launching project"


def test_cross_project_relay_without_workspace_fails_closed(tmp_path, monkeypatch, capsys):
    """1.2.2 fail-closed: --project <target> riding the implicit cwd default would bind
    the target's queue to the LAUNCHING tree — rejected before any artifact exists."""
    home = _home(tmp_path, monkeypatch)
    ws_a = _git_repo(tmp_path, name="proj-a")
    monkeypatch.chdir(ws_a)

    rc = codex_audit.main_audit_close(
        _close_argv(ws_a, project="target-proj", task="b-coord-1", workspace=False)
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "cross-project-needs-workspace" in err
    assert "--workspace" in err, "the error must name the remedy"
    assert not (home / "target-proj").exists(), "no artifact may be written for the target"


# ─── scenario 4: no-remote 项目交棒 (1.2.3 conclusion: fail-closed for worktree mode) ──


def test_no_remote_worktree_mode_relay_fails_closed(tmp_path, monkeypatch, capsys):
    """A worktree-isolation project with no 'origin': create_worktree would silently
    degrade the relay to a shared-tree spawn — audit-close fails closed FIRST, with an
    actionable remedy, publishing nothing and issuing no authority (empty-diff relay)."""
    home = _home(tmp_path, monkeypatch, config=json.dumps({"worktree_projects": [PROJECT]}))
    ws = _git_repo(tmp_path)  # no remote

    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 1
    err = capsys.readouterr().err
    assert "no-remote-coordinator-relay" in err
    assert "git remote add origin" in err, "the error must name the remedy"
    assert not (home / PROJECT / "queue").exists(), "no intent may be published"
    assert _tokens(home) == [], "no authority may be issued"


def test_not_a_repo_worktree_mode_relay_fails_closed(tmp_path, monkeypatch, capsys):
    """Same degrade class, other branch: a non-git workspace under worktree mode."""
    home = _home(tmp_path, monkeypatch, config=json.dumps({"worktree_projects": [PROJECT]}))
    ws = tmp_path / "plain"
    ws.mkdir()
    (ws / "README.md").write_text("not a repo\n")
    argv = ["--task", TASK, "--next", "n", "--project", PROJECT, "--workspace", str(ws)]
    argv += ["--audit-mode", "empty_diff_attestation", "--audit-base", "HEAD"]
    argv += ["--status", "active", "--coordinator"]
    for k in handoff_precheck.PHASE0_KEYS:
        argv += ["--phase0-status", f"{k}=✅"]
    for k in handoff_precheck.PHASE1_KEYS:
        argv += ["--phase1-status", f"{k}=✅"]

    rc = codex_audit.main_audit_close(argv)

    assert rc == 1
    assert "coordinator-workspace-not-git" in capsys.readouterr().err
    assert not (home / PROJECT / "queue").exists()


def test_no_remote_isolation_off_relay_still_works(tmp_path, monkeypatch):
    """1.2.3 non-over-blocking control: with isolation off (default), a no-remote
    project relays fine — origin containment is a worktree-mode requirement only."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)  # no remote, default config (mode off)

    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 0, rc
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()
    assert len(_tokens(home)) == 1


# ─── scenario 5: succession close_predecessor (full retro-gated chain) ────────


def test_succession_close_predecessor_full_chain(tmp_path, monkeypatch):
    """D1 ① + ④ (Step2 contract): retro-gated coordinator close → one-time authority →
    a succession spawn FOR THE SAME SUCCESSOR TASK (the audit-close ``--task``) consumes
    it and closes the predecessor window. The whole dump/token chain runs on a repo
    WITHOUT any git remote (SHOULD#9 锁回归: the chain must not assume origin exists)."""
    home = _home(tmp_path, monkeypatch, config=json.dumps({"singlepane_projects": [PROJECT]}))
    ws = _git_repo(tmp_path)  # deliberately NO remote (D1 ④ / SHOULD#9)
    assert (
        subprocess.run(["git", "remote"], cwd=ws, capture_output=True, text=True).stdout.strip()
        == ""
    )
    assert codex_audit.main_audit_close(_close_argv(ws)) == 0
    [token] = _tokens(home)
    predecessor = json.loads((home / PROJECT / "queue" / f"{TASK}.singlepane").read_text())[
        "spawn_nonce"
    ]

    from handoff_fanout import spawn

    rc = spawn.main(
        [
            "--project",
            PROJECT,
            "--task-id",
            TASK,  # Step2 C: the token designates THIS successor — same id as --task above
            "--role",
            "supervisor_succession",
            "--isolation",
            "singlepane",
            "--workspace",
            str(ws),
            "--prompt",
            "succeed the coordinator",
            "--predecessor-nonce",
            predecessor,
            "--succession-token",
            str(token),
        ]
    )

    assert rc == 0
    sc = json.loads((home / PROJECT / "queue" / f"{TASK}.singlepane").read_text())
    assert sc["role"] == "supervisor_succession"
    assert sc["close_policy"] == "close_predecessor"
    assert sc["predecessor_nonce"] == predecessor
    assert not token.exists(), "authority consumed by the succession spawn"


def test_succession_spawn_for_other_task_rejected(tmp_path, monkeypatch, capsys):
    """D1 ② (Step2 contract): the audit-close-issued token designates ONE successor —
    a spawn for any other task is REJECTED (task-mismatch) and the authority survives
    for the designated successor."""
    home = _home(tmp_path, monkeypatch, config=json.dumps({"singlepane_projects": [PROJECT]}))
    ws = _git_repo(tmp_path)
    assert codex_audit.main_audit_close(_close_argv(ws)) == 0
    [token] = _tokens(home)

    from handoff_fanout import spawn

    rc = spawn.main(
        [
            "--project",
            PROJECT,
            "--task-id",
            "coord-leg-8",  # ≠ the audit-close --task (the designated successor)
            "--role",
            "supervisor_succession",
            "--isolation",
            "singlepane",
            "--workspace",
            str(ws),
            "--prompt",
            "hijack the relay",
            "--predecessor-nonce",
            "feedfacecafebeef",
            "--succession-token",
            str(token),
        ]
    )

    assert rc == 2
    assert "task-mismatch" in capsys.readouterr().err
    assert token.exists(), "a mismatching spawn must not burn the designated authority"
    assert not (home / PROJECT / "queue" / "coord-leg-8.uri").exists()
    log = (home / PROJECT / "authority" / "succession-audit.log").read_text()
    assert "task-mismatch" in log


# ─── guards that must NOT regress / leak ──────────────────────────────────────


def test_unpushed_head_relay_still_blocked_and_no_token(tmp_path, monkeypatch):
    """前车之鉴 (brief §1.2 ⚠️): main-tree HEAD not pushed to origin/<int> → the
    worktree gate BLOCKS the relay (fail-closed, BLOCKED.md names the push), and a
    blocked relay must never issue succession authority."""
    home = _home(tmp_path, monkeypatch, config=json.dumps({"worktree_projects": [PROJECT]}))
    _, ws = _bare_and_clone(tmp_path)
    (ws / "new.txt").write_text("wip\n")
    _run(["git", "add", "new.txt"], ws)
    _run(["git", "commit", "-qm", "unpushed work"], ws)  # committed, NOT pushed

    rc = codex_audit.main_audit_close(_close_argv(ws))

    assert rc == 2, "the unpushed-HEAD gate must fail the relay closed"
    blocked = home / PROJECT / "queue" / f"{TASK}.BLOCKED.md"
    assert blocked.exists() and "push" in blocked.read_text()
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()
    assert _tokens(home) == [], "a blocked relay must not issue authority"


def test_non_coordinator_close_issues_no_token(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)

    rc = codex_audit.main_audit_close(_close_argv(ws, coordinator=False))

    assert rc == 0, rc
    assert _tokens(home) == [], "only a --coordinator close may issue authority"


def test_coordinator_done_close_issues_no_token(tmp_path, monkeypatch):
    """A terminal close has no successor to authorize — no authority on --status done."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)

    rc = codex_audit.main_audit_close(_close_argv(ws, status="done"))

    assert rc == 0, rc
    assert (home / PROJECT / "queue" / f"{TASK}.done").exists()
    assert _tokens(home) == []


# ─── retrieval-pull L1: --predecessor-lesson-backref plumbed into evidence ─────


def test_audit_close_plumbs_backref_into_evidence(tmp_path, monkeypatch):
    """audit-close --predecessor-lesson-backref folds the structured back-reference
    into the retro evidence it builds (the coordinator's own consumption record)."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)

    argv = _close_argv(ws) + [
        "--predecessor-lesson-backref",
        "lesson-sw-coord-p61=applied",
        "--predecessor-lesson-backref",
        "lesson-old=superseded:lesson-new replaces it",
    ]
    rc = codex_audit.main_audit_close(argv)
    assert rc == 0, rc

    evidence = json.loads(
        (home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json").read_text()
    )
    assert evidence["predecessor_lesson_backref"] == [
        {"predecessor_lesson": "lesson-sw-coord-p61", "disposition": "applied"},
        {
            "predecessor_lesson": "lesson-old",
            "disposition": "superseded",
            "reason": "lesson-new replaces it",
        },
    ]
    # surfaced into old_ready too (additive)
    old_ready = json.loads((home / PROJECT / "ack" / f"{TASK}.old_ready").read_text())
    assert old_ready["predecessor_lesson_backref"] == evidence["predecessor_lesson_backref"]


def test_audit_close_without_backref_omits_field(tmp_path, monkeypatch):
    """Byte-stable: a close WITHOUT the flag produces evidence with no backref field."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)

    assert codex_audit.main_audit_close(_close_argv(ws)) == 0
    evidence = json.loads(
        (home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json").read_text()
    )
    assert "predecessor_lesson_backref" not in evidence


def test_audit_close_backref_malformed_clean_exit(tmp_path, monkeypatch, capsys):
    """A malformed CLI backref → clean nonzero exit (not a traceback) and no evidence
    written."""
    home = _home(tmp_path, monkeypatch)
    ws = _git_repo(tmp_path)

    argv = _close_argv(ws) + ["--predecessor-lesson-backref", "lesson-x=bogus"]
    rc = codex_audit.main_audit_close(argv)
    assert rc != 0
    assert not (home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json").exists()
