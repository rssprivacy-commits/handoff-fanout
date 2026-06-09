"""``handoff spawn`` — the fresh-spawn intent producer (Phase 6a / design §13 A').

A project-agnostic producer that emits the SAME artifacts the watchdog already consumes
(``install/auto-continue.sh``: ``queue/<task>.uri`` + the worktree's / out-of-tree
``.handoff.code-workspace`` + the ``queue/<task>.singlepane`` JSON sidecar) — but WITHOUT the
v5.4 retro-mandate gate (no ``--retro-evidence`` ⇒ never exit 4) and WITHOUT injecting the
roadmap excerpt. It REUSES ``worktree.create_worktree`` / ``inject_vscode_workspace`` /
``spawn_nonce.title_for`` / ``atomic`` rather than re-deriving the worktree+nonce-workspace
production logic, and ``dump`` is not invoked, imported, or modified.

Contract (design §13 / plan Task 6a.1):
  1. worktree (only when ``--isolation worktree``; via ``worktree.create_worktree``);
  2. ``.handoff.code-workspace`` whose ``window.title`` carries the unguessable ``spawn_nonce``;
  3. JSON sidecar ``{workspace, role, close_policy, spawn_nonce, isolation, predecessor_nonce}``;
  4. ``queue/<task>.uri`` (``vscode://anthropic.claude-code/open?prompt=`` + 🆔-prefixed prompt).
Fail-closed (never a partial intent) on unknown project / untrusted (corrupt) config / unsafe
worktree state; a worktree created then a later step failing is rolled back (no orphan).
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse
from pathlib import Path

import pytest

from handoff_fanout import spawn
from handoff_fanout import spawn_nonce as _spawn_nonce

PROJECT = "wilde-hexe"
TASK = "wh-frobnicate"
NONCE = "deadbeefcafef00d"  # fixed via monkeypatch so assertions can pin the title


# ─── fixtures / helpers ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _pin_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``new_nonce`` so every spawn in the suite uses a known, assertable nonce."""
    monkeypatch.setattr(_spawn_nonce, "new_nonce", lambda: NONCE)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No retro/audit mandate leaks in — spawn must never touch those gates anyway."""
    for var in (
        "HANDOFF_RETRO_MANDATE",
        "HANDOFF_RETRO_BYPASS",
        "HANDOFF_AUDIT_MANDATE",
        "HANDOFF_WORKTREE_ISOLATION",
    ):
        monkeypatch.delenv(var, raising=False)


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str = "{}") -> Path:
    home = tmp_path / "handoff"
    home.mkdir()
    (home / "config.json").write_text(config)
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    return home


def _plain_repo(tmp_path: Path) -> Path:
    """A bare directory standing in for the project workspace (singlepane needs no git)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n")
    return repo


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _bare_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A git repo with a bare remote + a published ``main`` — what create_worktree needs."""
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


def _argv(
    *,
    project: str = PROJECT,
    task: str = TASK,
    role: str = "worker",
    isolation: str,
    workspace: Path | None = None,
    prompt: str | None = "do the thing",
    brief: Path | None = None,
    close_policy: str | None = None,
    predecessor_nonce: str | None = None,
) -> list[str]:
    a = [
        "--project",
        project,
        "--task-id",
        task,
        "--role",
        role,
        "--isolation",
        isolation,
    ]
    if workspace is not None:
        a += ["--workspace", str(workspace)]
    if brief is not None:
        a += ["--brief", str(brief)]
    elif prompt is not None:
        a += ["--prompt", prompt]
    if close_policy is not None:
        a += ["--close-policy", close_policy]
    if predecessor_nonce is not None:
        a += ["--predecessor-nonce", predecessor_nonce]
    return a


def _sidecar(home: Path, project: str = PROJECT, task: str = TASK) -> dict:
    return json.loads((home / project / "queue" / f"{task}.singlepane").read_text())


def _uri_lines(home: Path, project: str = PROJECT, task: str = TASK) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (home / project / "queue" / f"{task}.uri").read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def _decoded_prompt(home: Path, project: str = PROJECT, task: str = TASK) -> str:
    uri = _uri_lines(home, project, task)["URI"]
    _, _, qs = uri.partition("?prompt=")
    return urllib.parse.unquote(qs)


# ─── singlepane path ────────────────────────────────────────────────────────


def test_singlepane_produces_workspace_sidecar_uri(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)

    rc = spawn.main(_argv(isolation="singlepane", workspace=repo))
    assert rc == 0

    # (3) sidecar
    sc = _sidecar(home)
    assert sc["role"] == "worker"
    assert sc["close_policy"] == "keep"
    assert sc["spawn_nonce"] == NONCE
    assert sc["isolation"] == "singlepane"
    assert sc["predecessor_nonce"] is None
    ws_file = Path(sc["workspace"])
    # OUT-OF-TREE: under $HANDOFF_HOME/<project>/singlepane, never dirtying the repo.
    assert ws_file.parent == home / PROJECT / "singlepane"
    assert repo not in ws_file.parents
    assert ws_file.name.endswith(".handoff.code-workspace")

    # (2) workspace file — folders→real repo, nonce in title, the 4 UX settings only
    spec = json.loads(ws_file.read_text())
    assert spec["folders"] == [{"path": str(repo)}]
    title = spec["settings"]["window.title"]
    assert NONCE in title and TASK in title and PROJECT in title and "worker" in title
    assert set(spec) == {"folders", "settings"}
    assert set(spec["settings"]) == {
        "window.title",
        "workbench.activityBar.location",
        "workbench.startupEditor",
        "claudeCode.preferredLocation",
    }

    # (4) .uri — WORKSPACE = the real repo (NOT under /worktrees/ ⇒ singlepane consumer path)
    uri = _uri_lines(home)
    assert uri["WORKSPACE"] == str(repo)
    assert uri["URI"].startswith("vscode://anthropic.claude-code/open?prompt=")


def test_prompt_has_id_prefix(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    assert spawn.main(_argv(isolation="singlepane", workspace=repo, prompt="do the thing")) == 0
    prompt = _decoded_prompt(home)
    assert prompt.startswith(f"🆔{TASK}")
    assert "do the thing" in prompt


def test_brief_path_referenced_in_prompt(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    brief = tmp_path / "mybrief.md"
    brief.write_text("# brief\n")
    assert spawn.main(_argv(isolation="singlepane", workspace=repo, prompt=None, brief=brief)) == 0
    prompt = _decoded_prompt(home)
    assert prompt.startswith(f"🆔{TASK}")
    assert str(brief) in prompt


# ─── singlepane concurrency hard-REJECT (design §5.4 / p6a-fix1 MUST 3) ─────


def test_singlepane_second_worker_same_project_rejected(tmp_path, monkeypatch):
    """MUST 3: `handoff spawn --isolation singlepane` is a public entry that bypasses dump's
    Task5.1 guard. Two singlepane workers on one project = two windows landing in the SAME
    real repo (index.lock clashes / overwrites). Design §5.4: the second dispatch is hard
    REJECTED (rc 2, no .uri) — never a soft 'it shouldn't be concurrent' assumption."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    assert spawn.main(_argv(task="wh-first", isolation="singlepane", workspace=repo)) == 0

    rc = spawn.main(_argv(task="wh-second", isolation="singlepane", workspace=repo))
    assert rc == 2
    qd = home / PROJECT / "queue"
    assert not (qd / "wh-second.uri").exists()
    assert not (qd / "wh-second.singlepane").exists()
    # the first worker's intent is untouched
    assert (qd / "wh-first.uri").exists()


def test_singlepane_lock_held_rejected(tmp_path, monkeypatch):
    """MUST 3: a concurrent spawn holding the project .spawn.lock (same lock dump Task5.1 /
    autoclose use) must hard-reject the second singlepane producer — rc 2, no .uri."""
    from handoff_fanout.spawn_lock import project_spawn_lock

    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    with project_spawn_lock(PROJECT, root=home):
        rc = spawn.main(_argv(isolation="singlepane", workspace=repo))
    assert rc == 2
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_singlepane_terminal_task_frees_pane(tmp_path, monkeypatch):
    """A terminal predecessor (.done) no longer holds the pane — the successor may spawn."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    assert spawn.main(_argv(task="wh-first", isolation="singlepane", workspace=repo)) == 0
    (home / PROJECT / "queue" / "wh-first.done").write_text("done\n")

    assert spawn.main(_argv(task="wh-second", isolation="singlepane", workspace=repo)) == 0
    assert (home / PROJECT / "queue" / "wh-second.uri").exists()


def test_singlepane_same_task_respawn_not_self_rejected(tmp_path, monkeypatch):
    """A same-task re-spawn (retry) must not reject itself off its own previous intent."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    assert spawn.main(_argv(isolation="singlepane", workspace=repo)) == 0
    assert spawn.main(_argv(isolation="singlepane", workspace=repo)) == 0
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_singlepane_succession_exempt_from_worker_guard(tmp_path, monkeypatch):
    """supervisor_succession REPLACES its predecessor window (design §6) — mirroring dump's
    singlepane_worker_guard, the active-worker REJECT applies to worker dispatches only."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    assert spawn.main(_argv(task="wh-worker", isolation="singlepane", workspace=repo)) == 0

    rc = spawn.main(
        _argv(
            task="wh-succession",
            isolation="singlepane",
            workspace=repo,
            role="supervisor_succession",
            predecessor_nonce="0123456789abcdef",
        )
    )
    assert rc == 0
    assert (home / PROJECT / "queue" / "wh-succession.uri").exists()


# ─── worktree path ──────────────────────────────────────────────────────────


def test_worktree_produces_worktree_workspace_sidecar_uri(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _, ws = _bare_and_clone(tmp_path)

    rc = spawn.main(_argv(isolation="worktree", workspace=ws))
    assert rc == 0

    # (4) .uri WORKSPACE points at the created worktree (under /worktrees/ ⇒ COLD_WINDOW)
    uri = _uri_lines(home)
    wt_workspace = Path(uri["WORKSPACE"])
    assert "/worktrees/" in str(wt_workspace)
    assert wt_workspace.is_dir()

    # (1)+(2) the worktree carries its own .handoff.code-workspace with the nonce title
    cws = wt_workspace / ".handoff.code-workspace"
    assert cws.exists()
    title = json.loads(cws.read_text())["settings"]["window.title"]
    assert NONCE in title and TASK in title and PROJECT in title

    # (3) sidecar declares isolation=worktree (read by autoclose for role/predecessor)
    sc = _sidecar(home)
    assert sc["isolation"] == "worktree"
    assert sc["role"] == "worker"
    assert sc["spawn_nonce"] == NONCE


# ─── no retro gate / no roadmap (the whole point of A') ─────────────────────


def test_no_retro_evidence_never_exits_4(tmp_path, monkeypatch):
    """A fresh spawn with NO --retro-evidence must succeed (0), never the retro RETRY exit 4."""
    _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_argv(isolation="singlepane", workspace=repo))
    assert rc == 0
    assert rc != 4


def test_does_not_inject_roadmap(tmp_path, monkeypatch):
    marker = "ROADMAP_MARKER_DO_NOT_LEAK_42"
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text(f"# roadmap\n\n{marker}\n")
    home = _home(tmp_path, monkeypatch, config=json.dumps({"roadmap": {"path": str(roadmap)}}))
    repo = _plain_repo(tmp_path)
    assert spawn.main(_argv(isolation="singlepane", workspace=repo)) == 0

    qd = home / PROJECT / "queue"
    blob = (
        (qd / f"{TASK}.uri").read_text()
        + (qd / f"{TASK}.singlepane").read_text()
        + Path(_sidecar(home)["workspace"]).read_text()
    )
    assert marker not in blob


# ─── supervisor_succession (autoclose) ──────────────────────────────────────


def test_succession_sidecar_carries_predecessor(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    rc = spawn.main(
        _argv(
            isolation="singlepane",
            workspace=repo,
            role="supervisor_succession",
            predecessor_nonce="0123456789abcdef",
        )
    )
    assert rc == 0
    sc = _sidecar(home)
    assert sc["role"] == "supervisor_succession"
    assert sc["predecessor_nonce"] == "0123456789abcdef"
    # default close policy for a succession is to close the predecessor
    assert sc["close_policy"] == "close_predecessor"


# ─── fail-closed ────────────────────────────────────────────────────────────


def test_unknown_project_slug_fails_closed(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_argv(project="Bad Slug!", isolation="singlepane", workspace=repo))
    assert rc == 2
    assert not (home / "Bad Slug!").exists()


def test_missing_workspace_dir_fails_closed(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    missing = tmp_path / "does-not-exist"
    rc = spawn.main(_argv(isolation="singlepane", workspace=missing))
    assert rc == 2
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_corrupt_config_fails_closed(tmp_path, monkeypatch):
    # untrusted config ⇒ unified_spawn_enabled=False ⇒ refuse to produce intent (禁止静默降级)
    home = _home(tmp_path, monkeypatch, config="{ this is not json")
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_argv(isolation="singlepane", workspace=repo))
    assert rc == 2
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()
    assert not (home / PROJECT / "singlepane").exists()


def test_unified_spawn_disabled_fails_closed(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, config=json.dumps({"unified_spawn_enabled": False}))
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_argv(isolation="singlepane", workspace=repo))
    assert rc == 2
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_worktree_on_non_git_dir_fails_closed_no_silent_downgrade(tmp_path, monkeypatch):
    """Explicit --isolation worktree on a non-git dir must NOT silently spawn on the shared
    tree (no-silent-downgrade red line) — fail closed, no intent."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)  # not a git repo ⇒ create_worktree degrades
    rc = spawn.main(_argv(isolation="worktree", workspace=repo))
    assert rc == 2
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_worktree_unpublished_head_blocks(tmp_path, monkeypatch):
    """A source HEAD ahead of origin/<int> is an UNSAFE state — create_worktree BLOCKs and
    spawn fails closed (no intent), never branches a successor off stale code."""
    home = _home(tmp_path, monkeypatch)
    _, ws = _bare_and_clone(tmp_path)
    # advance HEAD past origin/main without pushing
    (ws / "extra.txt").write_text("local only\n")
    _run(["git", "add", "."], ws)
    _run(["git", "commit", "-qm", "local-unpushed"], ws)
    rc = spawn.main(_argv(isolation="worktree", workspace=ws))
    assert rc == 2
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


# ─── partial-failure rollback ───────────────────────────────────────────────


def test_worktree_partial_failure_rolls_back(tmp_path, monkeypatch):
    """worktree created, then publishing the .uri raises → the worktree is removed and NO
    partial intent (.uri / sidecar) is left behind."""
    home = _home(tmp_path, monkeypatch)
    _, ws = _bare_and_clone(tmp_path)

    boom_calls = {"n": 0}

    def boom(*a, **k):
        boom_calls["n"] += 1
        raise RuntimeError("simulated publish failure")

    monkeypatch.setattr(spawn, "_write_uri", boom)
    rc = spawn.main(_argv(isolation="worktree", workspace=ws))
    assert rc == 2
    assert boom_calls["n"] >= 1
    qd = home / PROJECT / "queue"
    assert not (qd / f"{TASK}.uri").exists()
    assert not (qd / f"{TASK}.singlepane").exists()
    # the worktree dir was created then rolled back
    assert not (home / PROJECT / "worktrees" / TASK).exists()


def test_worktree_reuse_refreshes_nonce_title(tmp_path, monkeypatch):
    """MUST 1 (p6a-fix1): re-spawning the same task adopts the existing clean+published
    worktree — whose ``.handoff.code-workspace`` still carries the PREVIOUS spawn's nonce.
    The delivery contract is 'title carries THIS spawn's nonce' (design §4 atomic landing
    gate: the nonce is unguessable, the task token is not), so the reuse path must rewrite
    the engine-generated workspace title with the CURRENT nonce."""
    home = _home(tmp_path, monkeypatch)
    _, ws = _bare_and_clone(tmp_path)
    nonce1, nonce2 = "1111aaaa1111aaaa", "2222bbbb2222bbbb"
    nonces = iter([nonce1, nonce2])
    monkeypatch.setattr(_spawn_nonce, "new_nonce", lambda: next(nonces))

    assert spawn.main(_argv(isolation="worktree", workspace=ws)) == 0
    cws = home / PROJECT / "worktrees" / TASK / ".handoff.code-workspace"
    assert nonce1 in json.loads(cws.read_text())["settings"]["window.title"]

    # 2nd spawn REUSES the worktree → the title must now carry nonce2, not the stale nonce1.
    assert spawn.main(_argv(isolation="worktree", workspace=ws)) == 0
    title = json.loads(cws.read_text())["settings"]["window.title"]
    assert nonce2 in title
    assert nonce1 not in title
    # and the published artifacts agree with the workspace title (no mismatch possible)
    assert _sidecar(home)["spawn_nonce"] == nonce2


def test_worktree_tracked_user_workspace_fails_closed(tmp_path, monkeypatch):
    """MUST 1 (p6a-fix1, fail-closed leg): a USER-tracked .handoff.code-workspace is never
    overwritten — but then the title cannot carry this spawn's nonce, so producing an intent
    would bake in a title↔sidecar nonce mismatch. The spawn must fail closed (rc 2, no .uri)."""
    home = _home(tmp_path, monkeypatch)
    _, ws = _bare_and_clone(tmp_path)
    # the user tracked their own .handoff.code-workspace into the repo
    (ws / ".handoff.code-workspace").write_text(json.dumps({"folders": [{"path": "."}]}))
    _run(["git", "add", "."], ws)
    _run(["git", "commit", "-qm", "user tracks a workspace file"], ws)
    _run(["git", "push", "-q", "origin", "main"], ws)

    rc = spawn.main(_argv(isolation="worktree", workspace=ws))
    assert rc == 2
    qd = home / PROJECT / "queue"
    assert not (qd / f"{TASK}.uri").exists()
    assert not (qd / f"{TASK}.singlepane").exists()
    # the user's tracked file content was respected (never overwritten) in the worktree, if any
    wt_dir = home / PROJECT / "worktrees" / TASK
    if (wt_dir / ".handoff.code-workspace").exists():
        assert "window.title" not in (wt_dir / ".handoff.code-workspace").read_text()


def test_worktree_reuse_publish_failure_does_not_remove_worktree(tmp_path, monkeypatch):
    """MUST 2 (p6a-fix1): a publish failure on a REUSED worktree (not created by this spawn —
    it may belong to another live session / the previous relay leg) must roll back ONLY this
    spawn's sidecar/.uri, NEVER remove the worktree itself (data-loss class)."""
    home = _home(tmp_path, monkeypatch)
    _, ws = _bare_and_clone(tmp_path)

    # 1st spawn creates the worktree + publishes fine.
    assert spawn.main(_argv(isolation="worktree", workspace=ws)) == 0
    wt_dir = home / PROJECT / "worktrees" / TASK
    assert wt_dir.is_dir()

    # 2nd spawn for the SAME task REUSES that clean+published worktree; its publish fails.
    def boom(*a, **k):
        raise RuntimeError("simulated publish failure")

    monkeypatch.setattr(spawn, "_write_uri", boom)
    rc = spawn.main(_argv(isolation="worktree", workspace=ws))
    assert rc == 2
    # this spawn's partial intent is rolled back …
    qd = home / PROJECT / "queue"
    assert not (qd / f"{TASK}.uri").exists()
    assert not (qd / f"{TASK}.singlepane").exists()
    # … but the REUSED worktree (not ours to destroy) survives.
    assert wt_dir.is_dir()


# ─── arg validation ─────────────────────────────────────────────────────────


def test_both_brief_and_prompt_rejected(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    brief = tmp_path / "b.md"
    brief.write_text("x")
    argv = [
        "--project",
        PROJECT,
        "--task-id",
        TASK,
        "--role",
        "worker",
        "--isolation",
        "singlepane",
        "--workspace",
        str(repo),
        "--prompt",
        "p",
        "--brief",
        str(brief),
    ]
    rc = spawn.main(argv)
    assert rc == 2
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_neither_brief_nor_prompt_rejected(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    argv = [
        "--project",
        PROJECT,
        "--task-id",
        TASK,
        "--role",
        "worker",
        "--isolation",
        "singlepane",
        "--workspace",
        str(repo),
    ]
    rc = spawn.main(argv)
    assert rc == 2


# ─── CLI dispatch (handoff spawn) ───────────────────────────────────────────


def test_cli_subcommand_dispatches(tmp_path, monkeypatch):
    from handoff_fanout import cli

    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    rc = cli.main(["spawn", *_argv(isolation="singlepane", workspace=repo)])
    assert rc == 0
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()
