"""End-to-end: ``dump.main`` under per-session worktree isolation.

Verifies the source/spawn split, the ``.uri``/handoff/``.worktree`` artifacts point
at the worktree, old_ready stays anchored to the source HEAD, the merge-back gate
BLOCKs an unpublished source, degrade falls back byte-identically, and ``worktree
gc`` reclaims a terminal task's worktree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from handoff_fanout import config as _config
from handoff_fanout import dump, handoff_precheck
from handoff_fanout import worktree as wt

TASK = "wt-e2e-task"
PROJECT = "proj"


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _bare_and_clone(tmp_path: Path) -> tuple[Path, Path]:
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


def _home(tmp_path: Path) -> Path:
    h = tmp_path / "handoff"
    h.mkdir()
    (h / "config.json").write_text("{}")
    return h


def _dump(home: Path, ws: Path, monkeypatch, *, status="active", on=True, extra=None) -> int:
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.delenv("HANDOFF_RETRO_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_RETRO_BYPASS", raising=False)
    monkeypatch.delenv("HANDOFF_AUDIT_MANDATE", raising=False)
    monkeypatch.setenv("HANDOFF_WORKTREE_ISOLATION", "on" if on else "off")
    argv = [
        "--task",
        TASK,
        "--next",
        "brief",
        "--project",
        PROJECT,
        "--workspace",
        str(ws),
        "--status",
        status,
    ]
    return dump.main(argv + (extra or []))


def _uri_workspace(home: Path) -> str:
    text = (home / PROJECT / "queue" / f"{TASK}.uri").read_text()
    for line in text.splitlines():
        if line.startswith("WORKSPACE="):
            return line[len("WORKSPACE=") :]
    return ""


# ─── mode off: byte-identical ────────────────────────────────────────────────


def test_dump_off_is_shared_tree(tmp_path, monkeypatch):
    _, ws = _bare_and_clone(tmp_path)
    home = _home(tmp_path)
    rc = _dump(home, ws, monkeypatch, on=False)
    assert rc == 0
    assert _uri_workspace(home) == str(ws)  # shared tree, no worktree
    assert not (home / PROJECT / "ack" / f"{TASK}.worktree").exists()
    assert not (home / PROJECT / "worktrees").exists()


def test_active_dump_clears_stale_blocked_md(tmp_path, monkeypatch):
    """Dual-brain P0: a stale <task>.BLOCKED.md (from a prior merge-back-gate block)
    must be cleared on a successful active dump — else the launcher skips the .uri and
    the relay stalls."""
    _, ws = _bare_and_clone(tmp_path)
    home = _home(tmp_path)
    qdir = home / PROJECT / "queue"
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / f"{TASK}.BLOCKED.md").write_text("# BLOCKED (stale)\n")
    rc = _dump(home, ws, monkeypatch, on=False)
    assert rc == 0
    assert (qdir / f"{TASK}.uri").exists()
    assert not (qdir / f"{TASK}.BLOCKED.md").exists()  # cleared → launcher won't skip


# ─── mode on: worktree created + artifacts point at it ───────────────────────


def test_dump_on_creates_worktree(tmp_path, monkeypatch):
    _, ws = _bare_and_clone(tmp_path)
    (ws / ".env").write_text("SECRET=1\n")
    home = _home(tmp_path)
    # config: link only .env, no .venv (keep the test hermetic).
    (home / "config.json").write_text(
        json.dumps({"worktree_link_files": [".env"], "worktree_link_venv": False})
    )
    rc = _dump(home, ws, monkeypatch, on=True)
    assert rc == 0
    expected_wt = home / PROJECT / "worktrees" / TASK
    assert expected_wt.exists()
    assert _uri_workspace(home) == str(expected_wt)
    # .worktree sidecar records the worktree + source.
    sc = json.loads((home / PROJECT / "ack" / f"{TASK}.worktree").read_text())
    assert sc["status"] == "created"
    assert sc["branch"] == "handoff/" + TASK
    assert sc["integration_branch"] == "main"
    assert sc["source_workspace"] == str(ws)
    # handoff .md: cd into the worktree + isolation banner + --project injected (R1-X1).
    md = (home / PROJECT / "queue" / f"{TASK}.md").read_text()
    assert f"cd {expected_wt}" in md
    assert "隔离 worktree" in md
    assert f"--project {PROJECT}" in md
    # linked .env present in worktree as a real file (copied — Docker-mount portable).
    assert (expected_wt / ".env").is_file() and not (expected_wt / ".env").is_symlink()


def _wt_settings(home: Path):
    cw = home / PROJECT / "worktrees" / TASK / wt.WORKTREE_VSCODE_FILE
    return json.loads(cw.read_text())["settings"]


def test_dump_coordinator_flag_threads_redtop(tmp_path, monkeypatch):
    """Full CLI plumbing (§五 / 2026-06-09): `handoff dump --coordinator` → the spawned worktree's
    .handoff.code-workspace is red-top + 🧭中枢· (args.coordinator → create_worktree → inject)."""
    _, ws = _bare_and_clone(tmp_path)
    home = _home(tmp_path)
    (home / "config.json").write_text(json.dumps({"worktree_link_venv": False}))
    rc = _dump(home, ws, monkeypatch, on=True, extra=["--coordinator"])
    assert rc == 0
    s = _wt_settings(home)
    assert s["window.title"].startswith("🧭中枢·")
    assert s["workbench.colorCustomizations"]["titleBar.activeBackground"] == "#8B0000"
    assert s["workbench.colorCustomizations"]["titleBar.inactiveBackground"] == "#5A0000"


def test_dump_without_coordinator_no_redtop(tmp_path, monkeypatch):
    """Zero regression at the dump layer: no --coordinator → no red-top, title unchanged."""
    _, ws = _bare_and_clone(tmp_path)
    home = _home(tmp_path)
    (home / "config.json").write_text(json.dumps({"worktree_link_venv": False}))
    rc = _dump(home, ws, monkeypatch, on=True)
    assert rc == 0
    s = _wt_settings(home)
    assert "workbench.colorCustomizations" not in s
    assert "🧭" not in s["window.title"]


def _singlepane_ws(home: Path) -> tuple[dict, dict]:
    """(workspace settings, sidecar meta) of the singlepane spawn artifacts."""
    meta = json.loads((home / PROJECT / "queue" / f"{TASK}.singlepane").read_text())
    return json.loads(Path(meta["workspace"]).read_text())["settings"], meta


def test_dump_coordinator_singlepane_threads_redtop(tmp_path, monkeypatch):
    """Full CLI plumbing on the SINGLEPANE path (owner-caught gap 2026-06-10): `handoff dump
    --coordinator` on a singlepane project (worktree off) red-tops the OUT-OF-TREE workspace —
    args.coordinator → write_active_dump → maybe_write_singlepane_sidecar. Before the fix the
    flag only reached create_worktree, so wilde-hexe/sdgf/fb 中枢 windows could never go red."""
    _, ws = _bare_and_clone(tmp_path)
    home = _home(tmp_path)
    (home / "config.json").write_text(json.dumps({"singlepane_projects": [PROJECT]}))
    rc = _dump(home, ws, monkeypatch, on=False, extra=["--coordinator"])
    assert rc == 0
    s, meta = _singlepane_ws(home)
    title = s["window.title"]
    assert title.startswith("🧭中枢·")
    assert meta["spawn_nonce"] in title  # watchdog's atomic nonce substring gate intact
    assert TASK in title  # task token intact (task-match submit guard)
    assert s["workbench.colorCustomizations"]["titleBar.activeBackground"] == "#8B0000"
    assert s["workbench.colorCustomizations"]["titleBar.inactiveBackground"] == "#5A0000"


def test_dump_singlepane_without_coordinator_no_redtop(tmp_path, monkeypatch):
    """Zero regression on the singlepane path: no --coordinator → the locked THIN key set
    (precise set assertion, mirroring test_spawn_fresh's no-redtop pattern)."""
    _, ws = _bare_and_clone(tmp_path)
    home = _home(tmp_path)
    (home / "config.json").write_text(json.dumps({"singlepane_projects": [PROJECT]}))
    rc = _dump(home, ws, monkeypatch, on=False)
    assert rc == 0
    s, _ = _singlepane_ws(home)
    assert set(s) == {
        "window.title",
        "workbench.activityBar.location",
        "workbench.startupEditor",
        "claudeCode.preferredLocation",
    }
    assert not s["window.title"].startswith("🧭中枢·")


def test_audit_close_coordinator_threads_redtop(tmp_path, monkeypatch):
    """The ERP coordinator spawn path goes through `handoff audit-close`, not bare dump.
    audit-close must forward --coordinator → dump → red-top worktree (§五 / 2026-06-09)."""
    from handoff_fanout import codex_audit

    _, ws = _bare_and_clone(tmp_path)
    home = _home(tmp_path)
    (home / "config.json").write_text(json.dumps({"worktree_link_venv": False}))
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    for v in (
        "HANDOFF_RETRO_MANDATE",
        "HANDOFF_RETRO_BYPASS",
        "HANDOFF_AUDIT_MANDATE",
        "HANDOFF_SAFE_COMMIT_LOCK",
        "HANDOFF_SAFE_COMMIT_BYPASS",
    ):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HANDOFF_WORKTREE_ISOLATION", "on")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ws), capture_output=True, text=True
    ).stdout.strip()
    argv = [
        "--task", TASK, "--project", PROJECT, "--workspace", str(ws),
        "--next", "spawn next", "--audit-mode", "empty_diff_attestation",
        "--audit-base", head, "--status", "active", "--coordinator",
    ]
    for k in handoff_precheck.PHASE0_KEYS:
        argv += ["--phase0-status", f"{k}=✅"]
    for k in handoff_precheck.PHASE1_KEYS:
        argv += ["--phase1-status", f"{k}=✅"]
    rc = codex_audit.main_audit_close(argv)
    assert rc == 0, rc
    s = _wt_settings(home)
    assert s["window.title"].startswith("🧭中枢·")
    assert s["workbench.colorCustomizations"]["titleBar.activeBackground"] == "#8B0000"


def test_dump_on_old_ready_anchored_to_source(tmp_path, monkeypatch):
    """old_ready.commit_hash must be the CLOSING session's HEAD (R1-C1)."""
    _, ws = _bare_and_clone(tmp_path)
    home = _home(tmp_path)
    (home / "config.json").write_text(json.dumps({"worktree_link_venv": False}))
    source_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ws), capture_output=True, text=True
    ).stdout.strip()
    # Build a valid retro evidence file so old_ready is written.
    ev = handoff_precheck.build_evidence(
        task_id=TASK,
        project=PROJECT,
        workspace=ws,
        nonce=None,
        phase0={k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS},
        phase1={k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
    )
    ev_path = home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    handoff_precheck.write_evidence(ev, ev_path)
    rc = _dump(home, ws, monkeypatch, on=True, extra=["--retro-evidence", str(ev_path)])
    assert rc == 0
    old_ready = json.loads((home / PROJECT / "ack" / f"{TASK}.old_ready").read_text())
    assert old_ready["commit_hash"] == source_head


# ─── merge-back gate: unpublished source BLOCKs ──────────────────────────────


def test_dump_on_blocks_unpublished_source(tmp_path, monkeypatch):
    _, ws = _bare_and_clone(tmp_path)
    # Commit but do NOT push → source HEAD not on origin/main.
    (ws / "wip.txt").write_text("x")
    _run(["git", "add", "."], ws)
    _run(["git", "commit", "-qm", "unpublished"], ws)
    home = _home(tmp_path)
    rc = _dump(home, ws, monkeypatch, on=True)
    assert rc == 2  # ERR-BLOCKED
    assert (home / PROJECT / "queue" / f"{TASK}.BLOCKED.md").exists()
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()  # no successor spawn
    assert not (home / PROJECT / "worktrees" / TASK).exists()


# ─── degrade: no remote → shared tree fallback ───────────────────────────────


def test_dump_on_degrades_without_remote(tmp_path, monkeypatch):
    ws = tmp_path / "local"
    ws.mkdir()
    subprocess.run(["git", "init", "-qb", "main", str(ws)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _run(["git", "config", k, v], ws)
    (ws / "a.txt").write_text("x")
    _run(["git", "add", "."], ws)
    _run(["git", "commit", "-qm", "init"], ws)
    home = _home(tmp_path)
    rc = _dump(home, ws, monkeypatch, on=True)
    assert rc == 0
    assert _uri_workspace(home) == str(ws)  # fell back to shared tree
    assert not (home / PROJECT / "ack" / f"{TASK}.worktree").exists()
    md = (home / PROJECT / "queue" / f"{TASK}.md").read_text()
    assert "降级" in md  # degrade banner visible to successor (R1-G3)


# ─── gc: reclaim terminal task worktree ──────────────────────────────────────


def test_worktree_gc_reclaims_terminal(tmp_path, monkeypatch):
    _, ws = _bare_and_clone(tmp_path)
    home = _home(tmp_path)
    (home / "config.json").write_text(json.dumps({"worktree_link_venv": False}))
    _dump(home, ws, monkeypatch, on=True)
    wt_path = home / PROJECT / "worktrees" / TASK
    assert wt_path.exists()
    # Mark the task terminal.
    (home / PROJECT / "queue" / f"{TASK}.done").touch()
    cfg = _config.load(home)
    # dry-run: nothing removed.
    wt.gc(cfg, PROJECT, execute=False)
    assert wt_path.exists()
    # execute: clean + published → reclaimed.
    wt.gc(cfg, PROJECT, execute=True)
    assert not wt_path.exists()
    assert not (home / PROJECT / "ack" / f"{TASK}.worktree").exists()


def test_worktree_gc_retains_dirty_terminal(tmp_path, monkeypatch):
    _, ws = _bare_and_clone(tmp_path)
    home = _home(tmp_path)
    (home / "config.json").write_text(json.dumps({"worktree_link_venv": False}))
    _dump(home, ws, monkeypatch, on=True)
    wt_path = home / PROJECT / "worktrees" / TASK
    (wt_path / "uncommitted.txt").write_text("dirty")  # leave WIP
    (home / PROJECT / "queue" / f"{TASK}.done").touch()
    cfg = _config.load(home)
    wt.gc(cfg, PROJECT, execute=True)
    assert wt_path.exists()  # retained — never destroy WIP


def test_worktree_gc_reclaims_without_done_marker(tmp_path, monkeypatch):
    """R2 P0-C: the serial relay never writes <task>.done — a clean+published
    worktree with NO live heartbeat is reclaimed anyway (session gone)."""
    _, ws = _bare_and_clone(tmp_path)
    home = _home(tmp_path)
    (home / "config.json").write_text(json.dumps({"worktree_link_venv": False}))
    _dump(home, ws, monkeypatch, on=True)
    wt_path = home / PROJECT / "worktrees" / TASK
    assert wt_path.exists()
    # No .done, no heartbeat → session gone.
    cfg = _config.load(home)
    wt.gc(cfg, PROJECT, execute=True)
    assert not wt_path.exists()  # reclaimed despite no .done marker


def test_worktree_gc_skips_live_heartbeat(tmp_path, monkeypatch):
    """A fresh heartbeat = live session → GC must NOT pull its worktree out."""
    _, ws = _bare_and_clone(tmp_path)
    home = _home(tmp_path)
    (home / "config.json").write_text(json.dumps({"worktree_link_venv": False}))
    _dump(home, ws, monkeypatch, on=True)
    wt_path = home / PROJECT / "worktrees" / TASK
    (home / PROJECT / "queue" / f"{TASK}.heartbeat").touch()  # fresh heartbeat = live
    cfg = _config.load(home)
    wt.gc(cfg, PROJECT, execute=True)
    assert wt_path.exists()  # retained — live session protected
