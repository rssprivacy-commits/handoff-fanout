"""Per-session git worktree isolation (worktree.py).

A real bare ``origin`` + working clone exercises the integration-branch resolution,
the published-HEAD merge-back gate, collision classification, file linking, and the
fail-safe removal path. Mode resolution is pure (env/sentinel/config).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from handoff_fanout import config as _config
from handoff_fanout import worktree as wt

# ─── git harness ─────────────────────────────────────────────────────────────


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo_config(ws: Path) -> None:
    for k, v in (
        ("user.email", "t@t.test"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
    ):
        _run(["git", "config", k, v], ws)


def _bare_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare ``origin`` (default branch main) + a working clone on main, pushed."""
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", str(bare), str(ws)], check=True, capture_output=True)
    _init_repo_config(ws)
    (ws / "README.md").write_text("base\n")
    _run(["git", "add", "."], ws)
    _run(["git", "commit", "-qm", "init"], ws)
    _run(["git", "push", "-q", "origin", "main"], ws)
    # Make origin/HEAD resolve to main (clone sets it; be explicit for robustness).
    subprocess.run(
        ["git", "remote", "set-head", "origin", "main"], cwd=str(ws), capture_output=True
    )
    return bare, ws


def _commit(ws: Path, fname: str, content: str, msg: str) -> str:
    (ws / fname).write_text(content)
    _run(["git", "add", "."], ws)
    _run(["git", "commit", "-qm", msg], ws)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ws), capture_output=True, text=True
    ).stdout.strip()


def _cfg(home: Path, **overrides) -> _config.Config:
    cfg = _config.Config(home=home)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "handoff"
    h.mkdir()
    return h


# ─── resolve_mode ────────────────────────────────────────────────────────────


def test_mode_default_off(home):
    assert wt.resolve_mode(_cfg(home), "proj", env={}) == wt.MODE_OFF


@pytest.mark.parametrize(
    "val,expected",
    [
        ("1", wt.MODE_ON),
        ("on", wt.MODE_ON),
        ("true", wt.MODE_ON),
        ("report", wt.MODE_REPORT),
        ("0", wt.MODE_OFF),
        ("off", wt.MODE_OFF),
    ],
)
def test_mode_env(home, val, expected):
    assert wt.resolve_mode(_cfg(home), "proj", env={"HANDOFF_WORKTREE_ISOLATION": val}) == expected


def test_mode_env_unknown_falls_through_to_config(home):
    cfg = _cfg(home, worktree_mode="report")
    assert (
        wt.resolve_mode(cfg, "proj", env={"HANDOFF_WORKTREE_ISOLATION": "garbage"})
        == wt.MODE_REPORT
    )


def test_mode_global_sentinel(home):
    (home / "worktree.enabled").touch()
    assert wt.resolve_mode(_cfg(home), "proj", env={}) == wt.MODE_ON


def test_mode_project_sentinel(home):
    (home / "proj").mkdir()
    (home / "proj" / "worktree.enabled").touch()
    assert wt.resolve_mode(_cfg(home), "proj", env={}) == wt.MODE_ON
    assert wt.resolve_mode(_cfg(home), "other", env={}) == wt.MODE_OFF


def test_mode_config_projects(home):
    cfg = _cfg(home, worktree_projects=["erp-system"])
    assert wt.resolve_mode(cfg, "erp-system", env={}) == wt.MODE_ON
    assert wt.resolve_mode(cfg, "other", env={}) == wt.MODE_OFF


def test_mode_project_report_sentinel(home):
    """Scoped report-only pilot: worktree.report sentinel → report for that project."""
    (home / "proj").mkdir()
    (home / "proj" / "worktree.report").touch()
    assert wt.resolve_mode(_cfg(home), "proj", env={}) == wt.MODE_REPORT
    assert wt.resolve_mode(_cfg(home), "other", env={}) == wt.MODE_OFF


def test_mode_global_report_sentinel(home):
    (home / "worktree.report").touch()
    assert wt.resolve_mode(_cfg(home), "anyproj", env={}) == wt.MODE_REPORT


def test_mode_enabled_wins_over_report(home):
    """enabled (on) beats report when both set for the same scope."""
    (home / "proj").mkdir()
    (home / "proj" / "worktree.enabled").touch()
    (home / "proj" / "worktree.report").touch()
    assert wt.resolve_mode(_cfg(home), "proj", env={}) == wt.MODE_ON


def test_mode_config_on_beats_global_report_sentinel(home):
    """Dual-brain P1: a global worktree.report must NOT demote a config-ON project."""
    (home / "worktree.report").touch()  # global pilot sentinel
    cfg = _cfg(home, worktree_projects=["erp-system"])  # erp-system ON via config
    assert wt.resolve_mode(cfg, "erp-system", env={}) == wt.MODE_ON  # not demoted
    assert wt.resolve_mode(cfg, "other", env={}) == wt.MODE_REPORT  # off project observes


def test_mode_env_overrides_sentinel(home):
    (home / "worktree.enabled").touch()
    assert (
        wt.resolve_mode(_cfg(home), "proj", env={"HANDOFF_WORKTREE_ISOLATION": "off"})
        == wt.MODE_OFF
    )


# ─── resolve_integration_branch ──────────────────────────────────────────────


def test_int_branch_config_override(home, tmp_path):
    _, ws = _bare_and_clone(tmp_path)
    cfg = _cfg(home, worktree_default_branch="release")
    assert wt.resolve_integration_branch(ws, cfg) == "release"


def test_int_branch_origin_head(home, tmp_path):
    _, ws = _bare_and_clone(tmp_path)
    assert wt.resolve_integration_branch(ws, _cfg(home), allow_network=False) == "main"


def test_int_branch_never_a_task_branch(home, tmp_path):
    """In a worktree on handoff/<task>, must NOT pick the task branch (R1-X2)."""
    _, ws = _bare_and_clone(tmp_path)
    _run(["git", "checkout", "-qb", "handoff/some-task"], ws)
    # origin/HEAD still → main; abbrev-ref HEAD would be handoff/some-task (the trap).
    assert wt.resolve_integration_branch(ws, _cfg(home), allow_network=False) == "main"


def test_int_branch_local_main_fallback(home, tmp_path):
    ws = tmp_path / "local"
    ws.mkdir()
    subprocess.run(["git", "init", "-qb", "main", str(ws)], check=True, capture_output=True)
    _init_repo_config(ws)
    _commit(ws, "a.txt", "x", "init")
    assert wt.resolve_integration_branch(ws, _cfg(home), allow_network=False) == "main"


def test_int_branch_unresolvable(home, tmp_path):
    ws = tmp_path / "weird"
    ws.mkdir()
    subprocess.run(["git", "init", "-qb", "trunk-xyz", str(ws)], check=True, capture_output=True)
    _init_repo_config(ws)
    _commit(ws, "a.txt", "x", "init")
    assert wt.resolve_integration_branch(ws, _cfg(home), allow_network=False) is None


# ─── create_worktree ─────────────────────────────────────────────────────────


def test_create_off(home, tmp_path):
    _, ws = _bare_and_clone(tmp_path)
    r = wt.create_worktree(
        source_workspace=ws, project="proj", task="t1", cfg=_cfg(home), mode=wt.MODE_OFF
    )
    assert r.status == wt.ST_OFF
    assert r.spawn_workspace == ws


def test_create_report_mutates_nothing(home, tmp_path):
    _, ws = _bare_and_clone(tmp_path)
    cfg = _cfg(home)
    r = wt.create_worktree(
        source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_REPORT
    )
    assert r.status == wt.ST_REPORT
    assert r.spawn_workspace == ws  # shared tree
    assert r.branch == "handoff/t1"
    assert r.integration_branch == "main"
    assert "worktree add" in (r.planned_cmd or "")
    # Nothing created.
    assert not wt.worktree_path(cfg, "proj", "t1").exists()
    assert not (home / "proj" / "worktrees").exists()


def test_create_happy_path(home, tmp_path):
    _, ws = _bare_and_clone(tmp_path)
    (ws / ".env").write_text("SECRET=1\n")  # gitignored-style essential file
    (ws / ".claude").mkdir()  # gitignored dir
    (ws / ".claude" / "settings.json").write_text("{}\n")
    cfg = _cfg(home, worktree_link_files=[".env", ".claude"], worktree_link_venv=False)
    r = wt.create_worktree(source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON)
    assert r.status == wt.ST_CREATED, r.reason
    assert r.spawn_workspace == wt.worktree_path(cfg, "proj", "t1")
    assert r.spawn_workspace.exists()
    assert r.branch == "handoff/t1"
    assert r.integration_branch == "main"
    # The worktree is on its own branch at origin/main.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(r.spawn_workspace), capture_output=True, text=True
    ).stdout.strip()
    assert head == r.base_sha
    # Tracked file present.
    assert (r.spawn_workspace / "README.md").exists()
    # R2 P1-G: regular files are COPIED (Docker-mount portable), dirs are SYMLINKED.
    env_dst = r.spawn_workspace / ".env"
    assert env_dst.is_file() and not env_dst.is_symlink()
    assert env_dst.read_text() == "SECRET=1\n"
    assert (r.spawn_workspace / ".claude").is_symlink()
    assert {".env", ".claude"} <= set(r.linked)


def test_create_injects_vscode_workspace(home, tmp_path):
    """option-C spawn-UX (2026-06-03 worktree-spawn-bug fix): create_worktree generates an
    identifiable ``<project>.code-workspace`` + symlinks ``.vscode``, and those engine artifacts
    do NOT make the worktree read dirty (else GC's fail-safe never reclaims it / remove retains
    it forever). REAL untracked WIP still → dirty (redline preserved)."""
    import json as _json

    _, ws = _bare_and_clone(tmp_path)
    (ws / ".vscode").mkdir()
    (ws / ".vscode" / "settings.json").write_text('{"editor.tabSize": 2}\n')
    cfg = _cfg(home, worktree_link_files=[".env"], worktree_link_venv=False)
    r = wt.create_worktree(
        source_workspace=ws, project="erp-system", task="stage1-10c", cfg=cfg, mode=wt.MODE_ON
    )
    assert r.status == wt.ST_CREATED, r.reason
    cw = r.spawn_workspace / wt.WORKTREE_VSCODE_FILE  # FIXED engine name (not <project>.code-workspace)
    assert cw.exists()
    assert r.vscode_workspace_file == str(cw)
    data = _json.loads(cw.read_text())
    assert data["folders"] == [{"path": "."}]
    # window.title carries project + task → window is identifiable (not the bare worktree dir name).
    assert "erp-system" in data["settings"]["window.title"]
    assert "stage1-10c" in data["settings"]["window.title"]
    # .vscode inherited from the source (symlink → project formatter/linter/launch).
    assert (r.spawn_workspace / ".vscode").is_symlink()
    # REDLINE: the injected artifacts are discounted → the fresh worktree is CLEAN (GC-safe),
    # both with the engine ignore set AND with no ignore (unconditional UX-artifact discount).
    assert not wt.is_dirty(r.spawn_workspace, ignore=set(wt._link_names(cfg)))
    assert not wt.is_dirty(r.spawn_workspace)
    # R2 Gemini P0-2: a USER's own untracked `*.code-workspace` is NOT the engine file → must still
    # count as dirty (else GC would reclaim it = data loss). Exact-name discount, not a suffix.
    (r.spawn_workspace / "my-wip.code-workspace").write_text("{}\n")
    assert wt.is_dirty(r.spawn_workspace)
    (r.spawn_workspace / "my-wip.code-workspace").unlink()
    # REAL untracked WIP still makes it dirty — the fail-safe is intact.
    (r.spawn_workspace / "real_wip.py").write_text("x = 1\n")
    assert wt.is_dirty(r.spawn_workspace)


# ─── 监管中枢窗口红顶防误关 (§五 / 2026-06-09 owner立法 / handoff-fanout 派窗路径普适化) ──

# The EXACT proven-rendering red-top spec, byte-parity with dx-spawn-session.sh --coordinator.
_COORD_RED = {
    "titleBar.activeBackground": "#8B0000",
    "titleBar.activeForeground": "#FFFFFF",
    "titleBar.inactiveBackground": "#5A0000",
    "titleBar.inactiveForeground": "#E0E0E0",
}
# Golden: the EXACT bytes a NON-coordinator worktree workspace must produce (captured from the
# pre-change engine). Locks zero-regression — any drift in the non-中枢 path fails this test.
_GOLDEN_NON_COORD = (
    '{\n  "folders": [\n    {\n      "path": "."\n    }\n  ],\n  "settings": {\n'
    '    "window.title": "erp-system \\u00b7 stage1-10c [worktree]${separator}${activeEditorShort}",\n'
    '    "workbench.activityBar.location": "hidden",\n'
    '    "workbench.startupEditor": "none",\n'
    '    "claudeCode.preferredLocation": "panel"\n  }\n}'
)


def test_inject_vscode_workspace_coordinator_redtop(tmp_path):
    """A coordinator worktree's .handoff.code-workspace must carry the red title bar +
    🧭中枢· prefix (visual parity with dx-spawn-session.sh --coordinator) so the owner can't
    misclose the 中枢 among many windows — §五·2 (2026-06-09 owner立法)."""
    src = tmp_path / "src"
    src.mkdir()
    wtree = tmp_path / "wt"
    wtree.mkdir()
    p = wt.inject_vscode_workspace(
        src, wtree, "erp-system", "erp-dev-coord-14", is_coordinator=True
    )
    s = json.loads(Path(p).read_text())["settings"]
    # 🧭中枢· prefix, BUT project+task retained → window stays identifiable.
    assert s["window.title"].startswith("🧭中枢·")
    assert "erp-system" in s["window.title"]
    assert "erp-dev-coord-14" in s["window.title"]
    # exact red values — the proven-rendering spec (colors > text for non-technical owner).
    assert s["workbench.colorCustomizations"] == _COORD_RED
    # the singlepane cold-spawn fields are preserved (red-top is additive, not a rewrite).
    assert s["workbench.activityBar.location"] == "hidden"
    assert s["claudeCode.preferredLocation"] == "panel"


def test_inject_vscode_workspace_default_byte_identical_baseline(tmp_path):
    """ZERO REGRESSION (validation gate #2): a NON-coordinator worktree workspace is byte-identical
    to the pre-change golden AND identical whether is_coordinator is omitted (legacy callers) or
    explicitly False. No colorCustomizations, no 🧭 in title — non-中枢 windows look exactly as before."""
    src = tmp_path / "src"
    src.mkdir()
    wt_default = tmp_path / "wt_default"
    wt_default.mkdir()
    wt_false = tmp_path / "wt_false"
    wt_false.mkdir()
    p_default = wt.inject_vscode_workspace(src, wt_default, "erp-system", "stage1-10c")
    p_false = wt.inject_vscode_workspace(
        src, wt_false, "erp-system", "stage1-10c", is_coordinator=False
    )
    b_default = Path(p_default).read_text()
    b_false = Path(p_false).read_text()
    assert b_default == _GOLDEN_NON_COORD  # byte-identical to pre-change engine
    assert b_default == b_false  # default arg == explicit False
    s = json.loads(b_default)["settings"]
    assert "workbench.colorCustomizations" not in s
    assert "🧭" not in s["window.title"]


def test_create_worktree_coordinator_threads_redtop(home, tmp_path):
    """create_worktree threads is_coordinator → the generated .handoff.code-workspace is red-top.
    (covers the create_worktree → inject_vscode_workspace hop on the fresh-create path)."""
    _, ws = _bare_and_clone(tmp_path)
    cfg = _cfg(home, worktree_link_files=[".env"], worktree_link_venv=False)
    r = wt.create_worktree(
        source_workspace=ws,
        project="erp-system",
        task="erp-dev-coord-14",
        cfg=cfg,
        mode=wt.MODE_ON,
        is_coordinator=True,
    )
    assert r.status == wt.ST_CREATED, r.reason
    cw = r.spawn_workspace / wt.WORKTREE_VSCODE_FILE
    s = json.loads(cw.read_text())["settings"]
    assert s["window.title"].startswith("🧭中枢·")
    assert s["workbench.colorCustomizations"] == _COORD_RED


def test_inject_vscode_workspace_coordinator_repaints_existing_non_red(tmp_path):
    """REUSE path — §五·2 absolute invariant "只要是中枢窗口就必须红顶" (codex+gemini 双脑共识 finding).
    When a coordinator worktree REUSES a pre-existing .handoff.code-workspace that lacks red-top
    (pre-patch engine / first created without --coordinator), inject must PATCH the red-top in — not
    silently return the non-red file. Non-coordinator reuse stays a byte-identical no-op (user-file
    protection / R2 Gemini P0-2 preserved). Idempotent: re-running never double-prefixes the title."""
    src = tmp_path / "src"
    src.mkdir()
    wtree = tmp_path / "wt"
    wtree.mkdir()
    # pre-existing NON-red engine file (as a pre-patch / non-coordinator run would leave it).
    p0 = wt.inject_vscode_workspace(src, wtree, "erp-system", "role-flip")
    before = Path(p0).read_text()
    assert "workbench.colorCustomizations" not in json.loads(before)["settings"]
    # NON-coordinator reuse → byte-identical no-op (never clobber an existing file).
    wt.inject_vscode_workspace(src, wtree, "erp-system", "role-flip", is_coordinator=False)
    assert Path(p0).read_text() == before
    # COORDINATOR reuse → red-top patched into the existing file.
    wt.inject_vscode_workspace(src, wtree, "erp-system", "role-flip", is_coordinator=True)
    s = json.loads(Path(p0).read_text())["settings"]
    assert s["window.title"].startswith("🧭中枢·")
    assert s["workbench.colorCustomizations"] == _COORD_RED
    # idempotent: a 2nd coordinator reuse must NOT double-prefix the title.
    wt.inject_vscode_workspace(src, wtree, "erp-system", "role-flip", is_coordinator=True)
    s2 = json.loads(Path(p0).read_text())["settings"]
    assert s2["window.title"].count("🧭中枢·") == 1


def test_inject_vscode_workspace_coordinator_leaves_unparseable_file(tmp_path):
    """Best-effort guard: a pre-existing UNPARSEABLE .handoff.code-workspace (some user's odd file)
    is left untouched even for a coordinator — never destroy unknown content (codex: 'fail/warn,
    don't clobber'). The dump still proceeds (returns the path)."""
    src = tmp_path / "src"
    src.mkdir()
    wtree = tmp_path / "wt"
    wtree.mkdir()
    junk = "this is not json {{{"
    (wtree / wt.WORKTREE_VSCODE_FILE).write_text(junk)
    p = wt.inject_vscode_workspace(src, wtree, "erp-system", "role-flip", is_coordinator=True)
    assert p == str(wtree / wt.WORKTREE_VSCODE_FILE)
    assert (wtree / wt.WORKTREE_VSCODE_FILE).read_text() == junk  # untouched


def test_coordinator_redtop_merges_preserves_other_user_colors(tmp_path):
    """REUSE patch must MERGE the red titleBar keys into an existing colorCustomizations dict, NOT
    replace it — a user's unrelated colors (editor.background, …) must survive (gemini round-2 P0 /
    never destroy user content)."""
    src = tmp_path / "src"
    src.mkdir()
    wtree = tmp_path / "wt"
    wtree.mkdir()
    (wtree / wt.WORKTREE_VSCODE_FILE).write_text(
        json.dumps(
            {
                "folders": [{"path": "."}],
                "settings": {
                    "window.title": "x",
                    "workbench.colorCustomizations": {"editor.background": "#000000"},
                },
            },
            indent=2,
        )
    )
    wt.inject_vscode_workspace(src, wtree, "erp-system", "role-flip", is_coordinator=True)
    cc = json.loads((wtree / wt.WORKTREE_VSCODE_FILE).read_text())["settings"][
        "workbench.colorCustomizations"
    ]
    assert cc["editor.background"] == "#000000"  # user's unrelated color survives
    assert cc["titleBar.activeBackground"] == "#8B0000"  # red merged in
    assert cc["titleBar.inactiveBackground"] == "#5A0000"


def test_coordinator_redtop_warns_when_cannot_apply(tmp_path, capsys):
    """禁止静默降级铁律 (codex+gemini round-2 P1): when a coordinator worktree's existing workspace
    can't be red-topped (unparseable / non-engine), the dump still proceeds (never brick) BUT emits
    a VISIBLE stderr warning — a non-red 中枢 window must NOT slip out silently."""
    src = tmp_path / "src"
    src.mkdir()
    wtree = tmp_path / "wt"
    wtree.mkdir()
    (wtree / wt.WORKTREE_VSCODE_FILE).write_text("not json {{{")
    wt.inject_vscode_workspace(src, wtree, "erp-system", "role-flip", is_coordinator=True)
    err = capsys.readouterr().err
    assert "中枢" in err or "coordinator" in err.lower()
    assert "🧭" in err
    assert "WARN" in err  # round-3 codex: a literal token so logs/scripts can grep the degrade signal


def test_coordinator_redtop_missing_title_fallback(tmp_path):
    """Edge (gemini round-2 P2): a reused file whose window.title was deleted still gets a
    🧭中枢·-marked fallback title (project+task identifiable) — not just red, the 中枢 text marker too."""
    src = tmp_path / "src"
    src.mkdir()
    wtree = tmp_path / "wt"
    wtree.mkdir()
    (wtree / wt.WORKTREE_VSCODE_FILE).write_text(
        json.dumps({"folders": [{"path": "."}], "settings": {"workbench.startupEditor": "none"}}, indent=2)
    )
    wt.inject_vscode_workspace(src, wtree, "erp-system", "role-flip", is_coordinator=True)
    s = json.loads((wtree / wt.WORKTREE_VSCODE_FILE).read_text())["settings"]
    assert s["window.title"].startswith("🧭中枢·")
    assert "role-flip" in s["window.title"]  # fallback stays identifiable
    assert s["workbench.colorCustomizations"]["titleBar.activeBackground"] == "#8B0000"


def test_create_worktree_default_no_redtop(home, tmp_path):
    """create_worktree without is_coordinator → NO red-top (zero regression at the create layer)."""
    _, ws = _bare_and_clone(tmp_path)
    cfg = _cfg(home, worktree_link_files=[".env"], worktree_link_venv=False)
    r = wt.create_worktree(
        source_workspace=ws, project="erp-system", task="stage1-10c", cfg=cfg, mode=wt.MODE_ON
    )
    assert r.status == wt.ST_CREATED, r.reason
    s = json.loads((r.spawn_workspace / wt.WORKTREE_VSCODE_FILE).read_text())["settings"]
    assert "workbench.colorCustomizations" not in s
    assert "🧭" not in s["window.title"]


def test_create_blocks_on_unpublished_head(home, tmp_path):
    _, ws = _bare_and_clone(tmp_path)
    _commit(ws, "new.txt", "wip", "unpublished work")  # committed but NOT pushed
    r = wt.create_worktree(
        source_workspace=ws, project="proj", task="t1", cfg=_cfg(home), mode=wt.MODE_ON
    )
    assert r.status == wt.ST_BLOCKED
    assert "not published" in (r.reason or "")
    assert r.spawn_workspace == ws  # caller will abort, not spawn isolated


def test_create_degrades_without_remote(home, tmp_path):
    ws = tmp_path / "local"
    ws.mkdir()
    subprocess.run(["git", "init", "-qb", "main", str(ws)], check=True, capture_output=True)
    _init_repo_config(ws)
    _commit(ws, "a.txt", "x", "init")
    r = wt.create_worktree(
        source_workspace=ws, project="proj", task="t1", cfg=_cfg(home), mode=wt.MODE_ON
    )
    assert r.status == wt.ST_DEGRADED
    assert "remote" in (r.reason or "")
    assert r.spawn_workspace == ws


def test_create_degrades_when_not_a_repo(home, tmp_path):
    ws = tmp_path / "plain"
    ws.mkdir()
    r = wt.create_worktree(
        source_workspace=ws, project="proj", task="t1", cfg=_cfg(home), mode=wt.MODE_ON
    )
    assert r.status == wt.ST_DEGRADED
    assert r.spawn_workspace == ws


def test_recreate_clean_published_worktree(home, tmp_path):
    """A re-dump of the same task whose prior worktree is clean+published recreates it."""
    _, ws = _bare_and_clone(tmp_path)
    cfg = _cfg(home, worktree_link_venv=False)
    r1 = wt.create_worktree(
        source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON
    )
    assert r1.status == wt.ST_CREATED
    r2 = wt.create_worktree(
        source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON
    )
    assert r2.status == wt.ST_CREATED, r2.reason
    assert r2.spawn_workspace.exists()


def test_collision_dirty_worktree_blocks(home, tmp_path):
    """A same-task worktree with uncommitted work is retained + blocked (R1-R3)."""
    _, ws = _bare_and_clone(tmp_path)
    cfg = _cfg(home, worktree_link_venv=False)
    r1 = wt.create_worktree(
        source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON
    )
    assert r1.status == wt.ST_CREATED
    (r1.spawn_workspace / "dirty.txt").write_text("uncommitted")
    _run(["git", "add", "dirty.txt"], r1.spawn_workspace)  # staged, uncommitted
    r2 = wt.create_worktree(
        source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON
    )
    assert r2.status == wt.ST_BLOCKED
    assert r1.spawn_workspace.exists()  # retained, not destroyed


def test_collision_branch_only_unpublished_blocks(home, tmp_path):
    """R2 P0-A: a lingering branch (worktree dir gone) with unpublished commits must
    NOT be force-deleted — re-dump BLOCKs instead of `git branch -D`."""
    _, ws = _bare_and_clone(tmp_path)
    cfg = _cfg(home, worktree_link_venv=False)
    r1 = wt.create_worktree(
        source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON
    )
    assert r1.status == wt.ST_CREATED
    # Commit unpublished work on the branch, then remove the worktree DIR (branch ref lingers).
    _init_repo_config(r1.spawn_workspace)
    _commit(r1.spawn_workspace, "feat.txt", "work", "unpublished branch work")
    _run(["git", "worktree", "remove", "--force", str(r1.spawn_workspace)], ws)
    assert not r1.spawn_workspace.exists()
    assert wt.branch_head(ws, "handoff/t1") is not None  # branch ref survives
    r2 = wt.create_worktree(
        source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON
    )
    assert r2.status == wt.ST_BLOCKED
    assert "unpublished" in (r2.reason or "")
    assert wt.branch_head(ws, "handoff/t1") is not None  # branch NOT deleted


def test_dirty_source_warns_not_blocks(home, tmp_path):
    """R2 P0-B: a dirty source worktree WARNs (benign hook dirt must not brick the
    relay) — the worktree is still created, with the advisory surfaced."""
    _, ws = _bare_and_clone(tmp_path)
    (ws / "uncommitted.txt").write_text("benign hook dirt")  # untracked dirt in source
    cfg = _cfg(home, worktree_link_venv=False)
    r = wt.create_worktree(source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON)
    assert r.status == wt.ST_CREATED
    assert any("uncommitted" in w for w in r.warnings)


# ─── classify / remove ───────────────────────────────────────────────────────


def test_remove_clean_published(home, tmp_path):
    _, ws = _bare_and_clone(tmp_path)
    cfg = _cfg(home, worktree_link_venv=False)
    r = wt.create_worktree(source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON)
    removed, reason = wt.remove_worktree(ws, r.spawn_workspace, r.branch, "main")
    assert removed, reason
    assert not r.spawn_workspace.exists()


def test_remove_retains_dirty(home, tmp_path):
    _, ws = _bare_and_clone(tmp_path)
    cfg = _cfg(home, worktree_link_venv=False)
    r = wt.create_worktree(source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON)
    (r.spawn_workspace / "x.txt").write_text("uncommitted")
    removed, reason = wt.remove_worktree(ws, r.spawn_workspace, r.branch, "main")
    assert not removed
    assert "uncommitted" in reason
    assert r.spawn_workspace.exists()


def test_remove_retains_committed_unpublished(home, tmp_path):
    """Clean but committed-unpushed → retained (the redline: never lose work)."""
    _, ws = _bare_and_clone(tmp_path)
    cfg = _cfg(home, worktree_link_venv=False)
    r = wt.create_worktree(source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON)
    # Commit inside the worktree but do NOT push → clean tree, unpublished commit.
    _init_repo_config(r.spawn_workspace)
    _commit(r.spawn_workspace, "feat.txt", "work", "isolated work, unpushed")
    removed, reason = wt.remove_worktree(ws, r.spawn_workspace, r.branch, "main")
    assert not removed
    assert "unpublished" in reason
    assert r.spawn_workspace.exists()


def test_linked_files_do_not_count_as_dirty(home, tmp_path):
    """R-ON: engine-linked .env/.claude (untracked symlinks/copies) must NOT make a
    fresh worktree read as dirty — else GC's fail-safe leaks every worktree. Real WIP
    still does."""
    _, ws = _bare_and_clone(tmp_path)
    (ws / ".env").write_text("X=1\n")
    (ws / ".claude").mkdir()
    (ws / ".claude" / "s.json").write_text("{}\n")
    cfg = _cfg(home, worktree_link_files=[".env", ".claude"], worktree_link_venv=False)
    r = wt.create_worktree(source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON)
    assert r.status == wt.ST_CREATED
    # Raw status IS non-empty (the linked files show as untracked), but the
    # link-aware dirtiness check discounts them → clean → removable.
    assert wt.is_dirty(r.spawn_workspace)  # unfiltered: dirty
    assert not wt.is_dirty(r.spawn_workspace, ignore={".env", ".claude"})  # filtered: clean
    removed, reason = wt.remove_worktree(
        ws, r.spawn_workspace, r.branch, "main", {".env", ".claude"}
    )
    assert removed, reason
    assert not r.spawn_workspace.exists()


def test_real_wip_still_dirty_despite_link_ignore(home, tmp_path):
    """A non-linked untracked file is still WIP → retained even with the link filter."""
    _, ws = _bare_and_clone(tmp_path)
    (ws / ".env").write_text("X=1\n")
    cfg = _cfg(home, worktree_link_files=[".env"], worktree_link_venv=False)
    r = wt.create_worktree(source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON)
    (r.spawn_workspace / "real_wip.txt").write_text("important")  # genuine WIP
    removed, reason = wt.remove_worktree(ws, r.spawn_workspace, r.branch, "main", {".env"})
    assert not removed
    assert "uncommitted" in reason
    assert r.spawn_workspace.exists()


def test_is_dirty_only_discounts_untracked_links(home, tmp_path):
    """REDLINE (codex R-ON P1/P2): ignore discounts ONLY untracked link-named files.
    Tracked changes + weird ' -> ' filenames are still genuine WIP → dirty."""
    _, ws = _bare_and_clone(tmp_path)
    # (1) untracked link-named file → discounted → clean.
    (ws / ".env").write_text("x")
    assert wt.is_dirty(ws, ignore={".env"}) is False
    # (2) a weird untracked filename containing ' -> ' must NOT be mis-parsed as a
    # rename to '.env' and discounted — it is real WIP.
    (ws / "a -> .env").write_text("y")
    assert wt.is_dirty(ws, ignore={".env"}) is True
    (ws / "a -> .env").unlink()
    # (3) a TRACKED modification of a link-named file is genuine WIP → dirty despite ignore.
    # ``-f``: a user's GLOBAL gitignore (``~/.config/git/ignore``) commonly lists ``.env``,
    # so a bare ``git add .env`` is REFUSED (rc 1) and the file is never tracked → step (3)
    # silently degenerates into the untracked case + the test fails on this machine. Force
    # past the global ignore so the test is hermetic regardless of the user's git config.
    _run(["git", "add", "-f", ".env"], ws)
    _run(["git", "commit", "-qm", "track env"], ws)
    (ws / ".env").write_text("modified")
    assert wt.is_dirty(ws, ignore={".env"}) is True


def test_list_worktrees(home, tmp_path):
    _, ws = _bare_and_clone(tmp_path)
    cfg = _cfg(home, worktree_link_venv=False)
    wt.create_worktree(source_workspace=ws, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON)
    items = wt.list_worktrees(ws)
    paths = [i["path"] for i in items]
    assert any("worktrees/t1" in p for p in paths)


def test_gc_source_fallback_when_predecessor_gone(home, tmp_path):
    """Dual-brain P0 (cascade leak): a worktree whose recorded source (a predecessor's
    worktree) was GC'd must STILL be reclaimable via the main-repo fallback — not
    leaked forever as 'unresolved'."""
    # Main repo named 'proj' under workspace_root=tmp_path (so the fallback resolves).
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    proj = tmp_path / "proj"
    subprocess.run(["git", "clone", str(bare), str(proj)], check=True, capture_output=True)
    _init_repo_config(proj)
    (proj / "README.md").write_text("x")
    _run(["git", "add", "."], proj)
    _run(["git", "commit", "-qm", "init"], proj)
    _run(["git", "push", "-q", "origin", "main"], proj)
    subprocess.run(
        ["git", "remote", "set-head", "origin", "main"], cwd=str(proj), capture_output=True
    )
    cfg = _cfg(home, worktree_link_venv=False, workspace_root=tmp_path)
    r = wt.create_worktree(
        source_workspace=proj, project="proj", task="t1", cfg=cfg, mode=wt.MODE_ON
    )
    assert r.status == wt.ST_CREATED
    # Sidecar records a DEAD predecessor source (simulating task-A GC'd).
    ack = home / "proj" / "ack"
    ack.mkdir(parents=True, exist_ok=True)
    (ack / "t1.worktree").write_text(
        json.dumps(
            {
                "status": "created",
                "path": str(r.spawn_workspace),
                "branch": r.branch,
                "integration_branch": "main",
                "linked": r.linked,
                "source_workspace": str(tmp_path / "worktrees" / "dead-predecessor"),
            }
        )
    )
    recs = wt.find_reclaimable(cfg, "proj")
    assert len(recs) == 1
    assert not recs[0]["classification"].get("unresolved")  # resolved via fallback
    assert recs[0]["source"] == proj  # fell back to the main repo
    wt.gc(cfg, "proj", execute=True)
    assert not r.spawn_workspace.exists()  # reclaimed, not leaked
