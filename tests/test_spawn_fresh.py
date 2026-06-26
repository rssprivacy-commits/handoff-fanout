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
import os
import subprocess
import urllib.parse
from pathlib import Path

import pytest

from handoff_fanout import spawn
from handoff_fanout import spawn_nonce as _spawn_nonce
from handoff_fanout import spawner_focus as _spawner_focus
from handoff_fanout import succession_authority as _authority

PROJECT = "wilde-hexe"
TASK = "wh-frobnicate"
NONCE = "deadbeefcafef00d"  # fixed via monkeypatch so assertions can pin the title

# The REAL resolver, captured at import (BEFORE the conftest autouse ``neutralize_spawner_self_report``
# pins it to None per-test). The dispatch end-to-end tests below re-set this to UN-neutralize and hit
# the real Tier-2 path (mirrors test_spawner_focus._REAL_RESOLVE).
_REAL_RESOLVE = _spawner_focus.resolve_spawner_focus_path


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


def _issue_token(home: Path, *, project: str = PROJECT, task: str = TASK) -> str:
    """A fresh one-time succession authority BOUND TO THE SUCCESSOR ``task`` (Step2 C
    契约: issuer's task = the designated successor; consume rejects any other), exactly
    as a retro-gated ``audit-close --coordinator --status active --task <succ>`` would
    issue (G4 收口: every succession spawn in this suite must hold one)."""
    return str(_authority.issue_token(home=home, project=project, task=task))


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
    succession_token: str | None = None,
    spawner_focus_path: str | None = None,
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
    if succession_token is not None:
        a += ["--succession-token", succession_token]
    if spawner_focus_path is not None:
        a += ["--spawner-focus-path", spawner_focus_path]
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

    # (2) workspace file — folders→real repo, nonce in title, the 4 UX settings + the
    # Step2 B 轨二 session-identity env signal only
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
        "terminal.integrated.env.osx",
    }
    assert spec["settings"]["terminal.integrated.env.osx"] == {
        "HANDOFF_SESSION_ROLE": "worker",
        "HANDOFF_SESSION_TASK": TASK,
        # direct-jump-spawn: the window's own focus path (realpath of this .code-workspace).
        "HANDOFF_WINDOW_FOCUS_PATH": os.path.realpath(str(ws_file)),
    }

    # (4) .uri — WORKSPACE = the real repo (NOT under /worktrees/ ⇒ singlepane consumer path)
    uri = _uri_lines(home)
    assert uri["WORKSPACE"] == str(repo)
    assert uri["URI"].startswith("vscode://anthropic.claude-code/open?prompt=")
    # direct-jump-spawn: no --spawner-focus-path passed ⇒ NO SPAWNER_FOCUS line (向后兼容).
    assert "SPAWNER_FOCUS" not in uri


def test_spawner_focus_path_valid_written_to_uri(tmp_path, monkeypatch):
    """direct-jump-spawn: a valid --spawner-focus-path (an existing .handoff.code-workspace under an
    allowed root) is realpath-normalized and written as the SPAWNER_FOCUS line the watchdog reads."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    # a plausible spawner identity: an existing .handoff.code-workspace under HANDOFF_HOME (an
    # allowed root). The trailing-slash / symlink norm is what the router matches against storage.json.
    spawner = home / "some-proj" / "singlepane" / "coord-x.handoff.code-workspace"
    spawner.parent.mkdir(parents=True)
    spawner.write_text("{}")
    rc = spawn.main(_argv(isolation="singlepane", workspace=repo, spawner_focus_path=str(spawner)))
    assert rc == 0
    uri = _uri_lines(home)
    assert uri["SPAWNER_FOCUS"] == os.path.realpath(str(spawner))


def test_spawner_focus_path_invalid_dropped_fail_open(tmp_path, monkeypatch):
    """Fail-open: an invalid --spawner-focus-path (wrong suffix / non-existent) is DROPPED — the
    worker still spawns (rc=0) and just gets NO SPAWNER_FOCUS line (no desktop jump), never a failure."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    # wrong suffix (not a .handoff.code-workspace) — would let the router `code <arbitrary file>`.
    bogus = home / "not-a-workspace.txt"
    bogus.write_text("x")
    rc = spawn.main(_argv(isolation="singlepane", workspace=repo, spawner_focus_path=str(bogus)))
    assert rc == 0  # fail-open: never fail a spawn over the UX hint
    assert "SPAWNER_FOCUS" not in _uri_lines(home)
    # non-existent path → also dropped (a SEPARATE project to avoid the one-worker-per-project guard).
    rc = spawn.main(
        _argv(
            project="focus-proj2",
            task="wh-focus2",
            isolation="singlepane",
            workspace=repo,
            spawner_focus_path=str(home / "ghost.handoff.code-workspace"),
        )
    )
    assert rc == 0
    assert "SPAWNER_FOCUS" not in _uri_lines(home, project="focus-proj2", task="wh-focus2")


# ─── spawn-unification Step 1: anchor-miss telemetry (warn-mode, zero behavior change) ───────────


def _anchor_miss_lines(home: Path, project: str = PROJECT) -> list[dict]:
    log = home / project / "spawn-anchor-miss.log"
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]


def test_spawn_anchor_miss_logged_and_uri_unchanged(tmp_path, monkeypatch):
    """spawn-unification Step 1: a spawn with NO resolvable anchor (conftest pins the resolver to None)
    still produces a byte-compatible .uri WITHOUT SPAWNER_FOCUS (zero behavior change) AND records ONE
    anchor-miss telemetry line — turning the silent static-map fallback into a countable signal."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)

    rc = spawn.main(_argv(isolation="singlepane", workspace=repo))
    assert rc == 0
    # zero behavior change: the .uri is exactly the pre-feature byte-compat form (no SPAWNER_FOCUS).
    assert "SPAWNER_FOCUS" not in _uri_lines(home)
    # but the miss is now VISIBLE + COUNTABLE.
    misses = _anchor_miss_lines(home)
    assert len(misses) == 1
    assert misses[0]["project"] == PROJECT
    assert misses[0]["task"] == TASK
    assert misses[0]["isolation"] == "singlepane"
    assert misses[0]["reason"] == "spawn:anchor-unresolved"


def test_spawn_anchor_hit_logs_no_miss(tmp_path, monkeypatch):
    """Symmetric guard: when an anchor DOES resolve (valid --spawner-focus-path), NO miss is logged."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    spawner = home / "some-proj" / "singlepane" / "coord-x.handoff.code-workspace"
    spawner.parent.mkdir(parents=True)
    spawner.write_text("{}")
    rc = spawn.main(_argv(isolation="singlepane", workspace=repo, spawner_focus_path=str(spawner)))
    assert rc == 0
    assert _uri_lines(home)["SPAWNER_FOCUS"] == os.path.realpath(str(spawner))
    assert _anchor_miss_lines(home) == []  # hit → no telemetry


# ─── singlepane DISPATCH end-to-end: --self-task drives the Tier-2 SPAWNER_FOCUS ────────────────
# The conftest autouse ``neutralize_spawner_self_report`` pins ``resolve_spawner_focus_path`` to None
# for every dump/spawn integration test (suite hermeticity) — so NO existing test proved the REAL
# dispatch path (``handoff spawn --self-task`` → Tier-2 ``derive_singlepane_focus`` → the additive
# SPAWNER_FOCUS line) actually fires end-to-end. That blind spot is exactly why dx-spawn's missing
# ``--self-task`` (the singlepane去程 bug) was invisible to tests. These two RESTORE the real resolver
# (captured pre-neutralize) and exercise it through ``spawn.main`` with a REAL on-disk sidecar — no stub.


def _coord_singlepane_sidecar(home: Path, task: str, project: str = PROJECT) -> Path:
    """The engine wrote ``<home>/<proj>/singlepane/<task>.handoff.code-workspace`` when this singlepane
    coordinator was itself spawned — exactly what ``derive_singlepane_focus(--self-task)`` reconstructs."""
    ws = home / project / "singlepane" / f"{task}.handoff.code-workspace"
    ws.parent.mkdir(parents=True, exist_ok=True)
    ws.write_text("{}")
    return ws


def test_singlepane_dispatch_self_task_emits_spawner_focus(tmp_path, monkeypatch):
    """End-to-end: a singlepane coordinator self-reporting its task (``--self-task``) — its terminal env
    never reaching the agent shell (p19) so ``--spawner-focus-path`` is absent — resolves its OWN sidecar
    workspace via Tier-2 → the additive SPAWNER_FOCUS line the watchdog reads for the one-step focus-jump.
    Restores the real resolver (un-neutralizes the conftest autouse) so this hits the resolver for real."""
    monkeypatch.setattr(_spawner_focus, "resolve_spawner_focus_path", _REAL_RESOLVE)
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    coord_ws = _coord_singlepane_sidecar(home, "wh-coord-23")
    # A singlepane coordinator's cwd is the SHARED repo root (NO in-tree .handoff.code-workspace), so
    # Tier-1 (cwd workspace) misses and Tier-2 (self_task → sidecar) is the ONLY way SPAWNER_FOCUS appears.
    plain_cwd = tmp_path / "shared-repo-root"
    plain_cwd.mkdir()
    monkeypatch.chdir(plain_cwd)
    monkeypatch.delenv("HANDOFF_WINDOW_FOCUS_PATH", raising=False)  # env empty → exercise the self-report

    rc = spawn.main(_argv(isolation="singlepane", workspace=repo) + ["--self-task", "wh-coord-23"])
    assert rc == 0
    assert _uri_lines(home)["SPAWNER_FOCUS"] == os.path.realpath(str(coord_ws))


def test_singlepane_dispatch_no_self_task_omits_spawner_focus(tmp_path, monkeypatch):
    """Byte-compat / fail-open: WITHOUT ``--self-task`` the dispatch path emits NO SPAWNER_FOCUS line even
    though the sidecar EXISTS on disk — Tier-2 is gated on the self-reported task, so the .uri stays
    identical to today. Real resolver restored, so the omission proves the gate (self_task None), not the
    neutralize stub. This is the negative the missing-flag fail-open contract rests on."""
    monkeypatch.setattr(_spawner_focus, "resolve_spawner_focus_path", _REAL_RESOLVE)
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    _coord_singlepane_sidecar(home, "wh-coord-23")  # sidecar present, but no --self-task points at it
    plain_cwd = tmp_path / "shared-repo-root"
    plain_cwd.mkdir()
    monkeypatch.chdir(plain_cwd)
    monkeypatch.delenv("HANDOFF_WINDOW_FOCUS_PATH", raising=False)

    rc = spawn.main(_argv(isolation="singlepane", workspace=repo))  # no --self-task
    assert rc == 0
    assert "SPAWNER_FOCUS" not in _uri_lines(home)


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


# ─── req1: machine-enforced 大白话 purpose-echo (2026-06-27) ─────────────────
# Every WORKER prompt instructs the worker to FIRST state its plain-language purpose, so the owner
# sees a real task statement — not just a 🆔 echo. Applied to BOTH the --brief and --prompt paths
# (the live dx-spawn → handoff spawn dispatch always converts a brief into --prompt). Non-worker
# (supervisor_succession) prompts stay byte-identical.


def test_brief_path_worker_injects_purpose_echo(tmp_path, monkeypatch):
    """--brief worker: the prompt becomes the exact 大白话 purpose-echo form (brief §0/req1)."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    brief = tmp_path / "mybrief.md"
    brief.write_text("# brief\n")
    assert spawn.main(_argv(isolation="singlepane", workspace=repo, prompt=None, brief=brief)) == 0
    prompt = _decoded_prompt(home)
    assert prompt == (
        f"🆔{TASK} 🔴开张第一句先回显：🆔{TASK} ＋ 用一句大白话说明你这个会话要做什么"
        f"（读 `{brief}` 后用人话讲清，别只回显 🆔）。然后 "
        f"open `{brief}` and execute per its instructions."
    )


def test_prompt_path_worker_injects_purpose_echo(tmp_path, monkeypatch):
    """A bare literal --prompt (no purpose cue) gets the 大白话 instruction prepended, still leads
    with 🆔{task}, and the original literal is preserved."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    assert spawn.main(_argv(isolation="singlepane", workspace=repo, prompt="do the thing")) == 0
    prompt = _decoded_prompt(home)
    assert prompt.startswith(f"🆔{TASK} 🔴开张第一句先回显")
    assert "用一句大白话说明你这个会话要做什么" in prompt
    assert prompt.endswith("然后 do the thing")


def test_prompt_live_dxspawn_shape_injected_without_id_duplication(tmp_path, monkeypatch):
    """The ACTUAL live path: dx-spawn builds '🆔{task} · …（开张先回显本窗口标识「🆔{task}」…）' and
    calls `handoff spawn --prompt`. That bare-🆔-echo prompt (no 大白话) MUST get the purpose-echo
    injected — this is the gap req1 closes — without duplicating the leading 🆔 identity token."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    live = (
        f"🆔{TASK} · 你被跨项目派来，现在在 wilde-hexe 项目。读 /tmp/b.md 全文，再执行其中任务。"
        f"（开张第一句先回显本窗口标识「🆔{TASK}」，方便主人对窗口）"
    )
    assert spawn.main(_argv(isolation="singlepane", workspace=repo, prompt=live)) == 0
    prompt = _decoded_prompt(home)
    # leading id is NOT duplicated — it's immediately followed by the instruction, not another 🆔
    assert prompt.startswith(f"🆔{TASK} 🔴开张第一句先回显")
    assert not prompt.startswith(f"🆔{TASK} 🆔{TASK}")
    assert "用一句大白话说明你这个会话要做什么" in prompt
    # original literal body preserved (the separator is folded away, the task description stays)
    assert "你被跨项目派来" in prompt


def test_prompt_with_existing_cue_left_verbatim(tmp_path, monkeypatch):
    """A literal --prompt that ALREADY carries the PRECISE 大白话 purpose marker (the distinctive
    injected phrase, not the bare noun) is used verbatim — no double-injection of the instruction."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    crafted = (
        f"🆔{TASK} 用一句大白话说明你这个会话要做什么：本会话给 X 加缓存。"
        f"open `/tmp/b.md` and execute."
    )
    assert spawn.main(_argv(isolation="singlepane", workspace=repo, prompt=crafted)) == 0
    prompt = _decoded_prompt(home)
    assert prompt == crafted  # verbatim, prefix + cue already present
    assert prompt.count("用一句大白话说明你这个会话要做什么") == 1  # not double-injected


def test_prompt_bare_noun_dabaihua_still_injected(tmp_path, monkeypatch):
    """FIX D (Codex): a literal --prompt that merely MENTIONS the noun '大白话' (e.g. asks the worker
    to write something 用大白话) but does NOT carry the precise injected marker is STILL injected — an
    incidental mention of the noun no longer false-skips the purpose echo (the bare-substring bug)."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    crafted = f"🆔{TASK} 把架构用大白话写给主人。open `/tmp/b.md` and execute."
    assert spawn.main(_argv(isolation="singlepane", workspace=repo, prompt=crafted)) == 0
    prompt = _decoded_prompt(home)
    assert prompt.startswith(f"🆔{TASK} 🔴开张第一句先回显")          # injected, leads with id + echo
    assert "用一句大白话说明你这个会话要做什么" in prompt            # the instruction WAS injected
    assert "把架构用大白话写给主人" in prompt                        # original literal preserved


def test_build_prompt_succession_role_not_injected():
    """Non-worker (supervisor_succession) prompts/briefs are NEVER injected — req1 is scoped to
    workers, and this keeps the succession continuation prompt byte-identical (the warmgap-C
    golden asserts the exact succession .uri)."""
    succ_prompt = spawn._build_prompt(
        "wh-succ", role=spawn.ROLE_SUCCESSION, brief=None, prompt="自动接续 / continue per baseline"
    )
    assert succ_prompt == "🆔wh-succ 自动接续 / continue per baseline"
    assert "大白话" not in succ_prompt
    succ_brief = spawn._build_prompt(
        "wh-succ", role=spawn.ROLE_SUCCESSION, brief="/tmp/b.md", prompt=None
    )
    assert succ_brief == "🆔wh-succ open `/tmp/b.md` and execute per its instructions."
    assert "大白话" not in succ_brief


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
            succession_token=_issue_token(home, task="wh-succession"),
        )
    )
    assert rc == 0
    assert (home / PROJECT / "queue" / "wh-succession.uri").exists()


def test_singlepane_succession_publish_holds_project_spawn_lock(tmp_path, monkeypatch):
    """t41b-fix1: the succession branch publishes under the SAME project .spawn.lock as
    the worker branch. The watchdog's §6 pending-intent gate scans queue/*.uri under
    that lock assuming every spawn-side publisher holds it — an unlocked succession
    publish could slip its .uri between the gate's scan and its close decision. The
    succession exemption covers the active-worker REJECT only, never the lock."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)

    seen: dict[str, bool] = {}
    real_write_uri = spawn._write_uri

    def probe(queue_dir: Path, task: str, *, workspace: Path, uri: str, spawner_focus=None) -> None:
        seen["lock_held"] = (home / PROJECT / ".spawn.lock").is_dir()
        real_write_uri(queue_dir, task, workspace=workspace, uri=uri, spawner_focus=spawner_focus)

    monkeypatch.setattr(spawn, "_write_uri", probe)
    rc = spawn.main(
        _argv(
            task="wh-succession",
            isolation="singlepane",
            workspace=repo,
            role="supervisor_succession",
            predecessor_nonce="0123456789abcdef",
            succession_token=_issue_token(home, task="wh-succession"),
        )
    )
    assert rc == 0
    assert seen.get("lock_held") is True, (
        "succession publish ran OUTSIDE the project spawn lock — the watchdog's "
        "pending-intent gate atomicity premise (all .uri publishers hold the lock) is broken"
    )


def test_singlepane_succession_lock_held_rejected(tmp_path, monkeypatch, capsys):
    """t41b-fix1: a held project .spawn.lock fail-closes a succession spawn too (rc 2,
    no partial intent, readable reason) — same semantics as the worker branch."""
    from handoff_fanout.spawn_lock import project_spawn_lock

    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    with project_spawn_lock(PROJECT, root=home):
        rc = spawn.main(
            _argv(
                task="wh-succession",
                isolation="singlepane",
                workspace=repo,
                role="supervisor_succession",
                predecessor_nonce="0123456789abcdef",
                succession_token=_issue_token(home, task="wh-succession"),
            )
        )
    assert rc == 2
    qd = home / PROJECT / "queue"
    assert not (qd / "wh-succession.uri").exists()
    assert not (qd / "wh-succession.singlepane").exists()
    assert "REJECTED" in capsys.readouterr().err


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
            succession_token=_issue_token(home),
        )
    )
    assert rc == 0
    sc = _sidecar(home)
    assert sc["role"] == "supervisor_succession"
    assert sc["predecessor_nonce"] == "0123456789abcdef"
    # default close policy for a succession is to close the predecessor
    assert sc["close_policy"] == "close_predecessor"


# ─── §五·2 red-top: succession = the next coordinator window ─────────────────
# (semantic-merge gap: red-top forked before spawn.py existed; dual-brain codex+gemini
#  both flagged the omission — closed by deriving is_coordinator from role.)


def test_succession_singlepane_workspace_is_redtopped(tmp_path, monkeypatch):
    """A succession's singlepane workspace must carry the 🧭中枢· prefix (WRAPPING the
    nonce-bound title — the watchdog's substring nonce gate must still hit) + red titleBar."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    rc = spawn.main(
        _argv(
            isolation="singlepane",
            workspace=repo,
            role="supervisor_succession",
            predecessor_nonce="0123456789abcdef",
            succession_token=_issue_token(home),
        )
    )
    assert rc == 0
    _ws_path = _sidecar(home)["workspace"]
    ws = json.loads(Path(_ws_path).read_text())
    title = ws["settings"]["window.title"]
    assert title.startswith("🧭中枢·")
    assert NONCE in title  # nonce substring gate intact under the prefix
    colors = ws["settings"]["workbench.colorCustomizations"]
    assert colors["titleBar.activeBackground"] == "#8B0000"
    assert colors["titleBar.inactiveBackground"] == "#5A0000"
    # Step2 B 轨二: a succession window's SESSION role is the coordinator one.
    assert ws["settings"]["terminal.integrated.env.osx"] == {
        "HANDOFF_SESSION_ROLE": "supervisor_succession",
        "HANDOFF_SESSION_TASK": TASK,
        # direct-jump-spawn: the coordinator window's own focus path (realpath of its .code-workspace).
        "HANDOFF_WINDOW_FOCUS_PATH": os.path.realpath(_ws_path),
    }


def test_worker_singlepane_workspace_has_no_redtop(tmp_path, monkeypatch):
    """Zero regression: a worker singlepane workspace keeps the locked THIN key set
    (+ the Step2 env signal, which is all-path additive) — no 🧭 prefix, no
    colorCustomizations."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    assert spawn.main(_argv(isolation="singlepane", workspace=repo)) == 0
    ws = json.loads(Path(_sidecar(home)["workspace"]).read_text())
    assert set(ws["settings"]) == {
        "window.title",
        "workbench.activityBar.location",
        "workbench.startupEditor",
        "claudeCode.preferredLocation",
        "terminal.integrated.env.osx",
    }
    assert not ws["settings"]["window.title"].startswith("🧭中枢·")


def test_worker_worktree_workspace_has_no_redtop(tmp_path, monkeypatch):
    """codex SHOULD (redtop-succ round): assert no-redtop DIRECTLY on the spawn-worktree
    worker path (not only via the lower create_worktree/inject golden tests)."""
    home = _home(tmp_path, monkeypatch)
    _, ws_repo = _bare_and_clone(tmp_path)
    assert spawn.main(_argv(isolation="worktree", workspace=ws_repo)) == 0
    wt_workspace = Path(_uri_lines(home)["WORKSPACE"])
    spec = json.loads((wt_workspace / ".handoff.code-workspace").read_text())
    assert not spec["settings"]["window.title"].startswith("🧭中枢·")
    assert "workbench.colorCustomizations" not in spec["settings"]


def test_explicit_close_policy_contradicting_role_is_rejected(tmp_path, monkeypatch):
    """codex SHOULD (redtop-succ round): succession+keep / worker+close_predecessor are
    contradictory metadata (consumers act on role) — fail closed, produce no intent."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    token = _issue_token(home)
    rc = spawn.main(
        _argv(
            isolation="singlepane",
            workspace=repo,
            role="supervisor_succession",
            predecessor_nonce="0123456789abcdef",
            succession_token=token,
        )
        + ["--close-policy", "keep"]
    )
    assert rc != 0
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()
    # an earlier semantic rejection must never burn the one-time authority
    assert Path(token).exists()

    rc = spawn.main(
        _argv(isolation="singlepane", workspace=repo) + ["--close-policy", "close_predecessor"]
    )
    assert rc != 0
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_succession_worktree_workspace_is_redtopped(tmp_path, monkeypatch):
    """The worktree isolation path passes is_coordinator into create_worktree — a
    succession's worktree .handoff.code-workspace carries the same red-top."""
    home = _home(tmp_path, monkeypatch)
    _, ws_repo = _bare_and_clone(tmp_path)
    rc = spawn.main(
        _argv(
            isolation="worktree",
            workspace=ws_repo,
            role="supervisor_succession",
            predecessor_nonce="0123456789abcdef",
            succession_token=_issue_token(home),
        )
    )
    assert rc == 0
    wt_workspace = Path(_uri_lines(home)["WORKSPACE"])
    spec = json.loads((wt_workspace / ".handoff.code-workspace").read_text())
    title = spec["settings"]["window.title"]
    assert title.startswith("🧭中枢·") and NONCE in title
    colors = spec["settings"]["workbench.colorCustomizations"]
    assert colors["titleBar.activeBackground"] == "#8B0000"


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


def test_invalid_close_policy_fails_closed(tmp_path, monkeypatch):
    """SHOULD (p6a-fix1): close_policy is an enum the watchdog acts on — an unknown value
    must fail closed, not flow into the sidecar for the consumer to mis-read."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_argv(isolation="singlepane", workspace=repo, close_policy="nuke-all"))
    assert rc == 2
    qd = home / PROJECT / "queue"
    assert not (qd / f"{TASK}.uri").exists()
    assert not (qd / f"{TASK}.singlepane").exists()


def test_succession_without_predecessor_nonce_fails_closed(tmp_path, monkeypatch):
    """SHOULD (p6a-fix1): a supervisor_succession's purpose is closing its predecessor
    window — without --predecessor-nonce the window cannot be identified, so the intent
    would be unactionable. Fail closed."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    rc = spawn.main(_argv(isolation="singlepane", workspace=repo, role="supervisor_succession"))
    assert rc == 2
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


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
