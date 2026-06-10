"""Single-pane (non-worktree) spawn sidecar — ``maybe_write_singlepane_sidecar``.

Owner ruling S (2026-06-08): deliver a default single-editor-pane VS Code window for an
opted-in project WITHOUT git-worktree isolation. The dump generates an OUT-OF-TREE
``.handoff.code-workspace`` (so it never dirties the repo) + a ``queue/<task>.singlepane``
sidecar the watchdog opens; the handoff-helper extension collapses the side bars on load.

Phase 2 (2026-06-09 spawn-window-unify R2 M1/M4): the ``window.title`` now binds
``project·task·role·spawn_nonce`` (via ``spawn_nonce.title_for``) so the watchdog can ATOMICALLY
prove the front window is the exact one we launched, and the sidecar is now **JSON** (breaking
migration from the old plain-path text) carrying ``role``/``close_policy``/``spawn_nonce``/
``predecessor_nonce`` for the watchdog read side + role-gated autoclose.
"""

from __future__ import annotations

import json
from pathlib import Path

from handoff_fanout import config as _config
from handoff_fanout import dump

# A fixed, unmistakable nonce for the assertions (real spawns use spawn_nonce.new_nonce()).
_NONCE = "deadbeefcafef00d"


def _cfg(home: Path, singlepane: list[str]) -> _config.Config:
    return _config._from_dict({"singlepane_projects": singlepane}, home=home)


def test_optin_writes_sidecar_and_out_of_tree_workspace(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ["wilde-hexe"])
    qd = tmp_path / "wilde-hexe" / "queue"
    qd.mkdir(parents=True)
    real_repo = tmp_path / "repo"
    real_repo.mkdir()

    dump.maybe_write_singlepane_sidecar(
        cfg,
        "wilde-hexe",
        "wh-foo",
        real_repo,
        qd,
        worktree_active=False,
        role="worker",
        close_policy="keep",
        spawn_nonce=_NONCE,
    )

    sidecar = qd / "wh-foo.singlepane"
    assert sidecar.exists()
    meta = json.loads(sidecar.read_text())  # JSON sidecar now (was plain path)
    ws_file = Path(meta["workspace"])
    # OUT-OF-TREE: lives under $HANDOFF_HOME/<project>/singlepane, NOT in the repo tree.
    assert ws_file.parent == tmp_path / "wilde-hexe" / "singlepane"
    assert real_repo not in ws_file.parents
    assert ws_file.name.endswith(".handoff.code-workspace")  # extension guard suffix

    spec = json.loads(ws_file.read_text())
    assert spec["folders"] == [{"path": str(real_repo)}]  # window opens the REAL repo
    assert "wh-foo" in spec["settings"]["window.title"]  # task token for the submit guard
    assert spec["settings"]["workbench.activityBar.location"] == "hidden"  # single pane
    # P0 THIN workspace: settings carry ONLY window.title + the single-pane UX keys + the
    # Step2 session env signal — never a coordinator/inject config block (per-project gating
    # must stay in the repo's own .vscode so v1.12.0 gating still governs it). Lock the exact
    # key set so a regression can't smuggle one in.
    assert set(spec) == {"folders", "settings"}
    assert set(spec["settings"]) == {
        "window.title",
        "workbench.activityBar.location",
        "workbench.startupEditor",
        "claudeCode.preferredLocation",
        "terminal.integrated.env.osx",
    }
    # Step2 B 轨二: a worker dump window carries the worker session identity.
    assert spec["settings"]["terminal.integrated.env.osx"] == {
        "HANDOFF_SESSION_ROLE": "worker",
        "HANDOFF_SESSION_TASK": "wh-foo",
    }


def test_worktree_active_removes_sidecar(tmp_path: Path) -> None:
    """A worktree spawn has its OWN .handoff.code-workspace and wins — no singlepane sidecar."""
    cfg = _cfg(tmp_path, ["wilde-hexe"])
    qd = tmp_path / "wilde-hexe" / "queue"
    qd.mkdir(parents=True)
    sidecar = qd / "wh-foo.singlepane"
    sidecar.write_text("stale")  # pretend a prior run left one
    dump.maybe_write_singlepane_sidecar(
        cfg,
        "wilde-hexe",
        "wh-foo",
        tmp_path,
        qd,
        worktree_active=True,
        role="worker",
        close_policy="keep",
        spawn_nonce=_NONCE,
    )
    assert not sidecar.exists()


def test_non_optin_project_no_sidecar_and_cleans_stale(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ["wilde-hexe"])  # erp-system NOT opted in
    qd = tmp_path / "erp-system" / "queue"
    qd.mkdir(parents=True)
    sidecar = qd / "erp-bar.singlepane"
    sidecar.write_text("stale-optin")  # a config flip-off must clean this up
    dump.maybe_write_singlepane_sidecar(
        cfg,
        "erp-system",
        "erp-bar",
        tmp_path,
        qd,
        worktree_active=False,
        role="worker",
        close_policy="keep",
        spawn_nonce=_NONCE,
    )
    assert not sidecar.exists()


def test_singlepane_projects_fail_open_on_degenerate(tmp_path: Path) -> None:
    # Degenerate values → empty list (no project opts in). Never enables single-pane on a typo.
    for bad in ("wilde-hexe", 123, None, [""], [1, 2]):
        assert _config._from_dict({"singlepane_projects": bad}, home=tmp_path).singlepane_projects == []
    assert _config._from_dict(
        {"singlepane_projects": ["wilde-hexe", "", "x"]}, home=tmp_path
    ).singlepane_projects == ["wilde-hexe", "x"]


def test_sidecar_json_carries_nonce_role(tmp_path: Path) -> None:
    """The sidecar is JSON now (breaking migration from plain-path): it carries the role,
    close_policy, spawn_nonce + predecessor_nonce the watchdog read side / role-gated autoclose
    need — not just the bare workspace path."""
    cfg = _cfg(tmp_path, ["wilde-hexe"])
    qd = tmp_path / "wilde-hexe" / "queue"
    qd.mkdir(parents=True)
    real_repo = tmp_path / "repo"
    real_repo.mkdir()

    dump.maybe_write_singlepane_sidecar(
        cfg,
        "wilde-hexe",
        "wh-foo",
        real_repo,
        qd,
        worktree_active=False,
        role="worker",
        close_policy="keep",
        spawn_nonce=_NONCE,
    )

    meta = json.loads((qd / "wh-foo.singlepane").read_text())
    assert meta["role"] == "worker"
    assert meta["close_policy"] == "keep"
    assert meta["spawn_nonce"] == _NONCE
    assert meta["predecessor_nonce"] is None  # a worker spawn has no predecessor to close
    assert Path(meta["workspace"]).name.endswith(".handoff.code-workspace")


def test_title_carries_nonce(tmp_path: Path) -> None:
    """window.title binds project·task·role·nonce so the watchdog can ATOMICALLY match the front
    window by the unguessable spawn_nonce (osascript substring `contains`), while KEEPING the task
    token for backward-compat with the existing task-match submit guard."""
    cfg = _cfg(tmp_path, ["wilde-hexe"])
    qd = tmp_path / "wilde-hexe" / "queue"
    qd.mkdir(parents=True)
    real_repo = tmp_path / "repo"
    real_repo.mkdir()

    dump.maybe_write_singlepane_sidecar(
        cfg,
        "wilde-hexe",
        "wh-foo",
        real_repo,
        qd,
        worktree_active=False,
        role="worker",
        close_policy="keep",
        spawn_nonce=_NONCE,
    )

    ws_file = Path(json.loads((qd / "wh-foo.singlepane").read_text())["workspace"])
    title = json.loads(ws_file.read_text())["settings"]["window.title"]
    assert _NONCE in title  # nonce — the strong atomic gate
    assert "worker" in title  # role
    assert "wilde-hexe" in title  # project
    assert "wh-foo" in title  # task token (backward-compat with the task-match submit guard)


# ─── §五·2 coordinator red-top (owner-caught gap 2026-06-10) ─────────────────
# `handoff dump --coordinator` previously red-topped ONLY the worktree path
# (create_worktree); the singlepane writer ignored the flag entirely, so the
# singlepane projects (wilde-hexe/sdgf/fb) could NEVER render a red 中枢 window.

# sha256 of the NON-coordinator workspace bytes for the fixed inputs in _write_fixed,
# captured from the PRE-Step2 writer (main @ cd28d4e — before the B 轨二 env signal).
# Kept as the byte-precision REFERENCE: stripping ONLY the env key from today's bytes
# must reproduce EXACTLY these bytes (proves the Step2 diff is the two env vars and
# nothing else — the brief's 精确断言).
_PRE_STEP2_GOLDEN_WS_SHA256 = "acbea9304b393bc6e02ebdcd8e34f9f209805bb2935c27437e11e3067af719ce"
# sha256 of the CURRENT non-coordinator workspace bytes (= pre-Step2 + the one
# terminal.integrated.env.osx key appended LAST). Locks byte-zero regression for every
# caller that does not pass is_coordinator=True — including the open-batch sub-task +
# fan-in call sites, which never thread the flag.
_GOLDEN_WS_SHA256 = "1419c0ef57bb3ee3e4e4a8cceed3219c345215104f7100aa7d3d02277c7ee6e9"


def _write_fixed(tmp_path: Path, **kw) -> tuple[Path, str]:
    """Drive the writer with fully deterministic inputs (fixed fake repo path + fixed
    nonce) so the workspace bytes are reproducible across machines/runs. Returns
    ``(ws_file, sidecar_text)``."""
    cfg = _cfg(tmp_path, ["wilde-hexe"])
    qd = tmp_path / "wilde-hexe" / "queue"
    qd.mkdir(parents=True, exist_ok=True)
    dump.maybe_write_singlepane_sidecar(
        cfg,
        "wilde-hexe",
        "wh-foo",
        Path("/repo"),
        qd,
        worktree_active=False,
        role="worker",
        close_policy="keep",
        spawn_nonce=_NONCE,
        **kw,
    )
    sidecar_text = (qd / "wh-foo.singlepane").read_text()
    return Path(json.loads(sidecar_text)["workspace"]), sidecar_text


def test_non_coordinator_workspace_byte_identical_golden(tmp_path: Path) -> None:
    """Byte-zero regression: omitting is_coordinator (legacy/batch/fan-in callers) AND
    passing an explicit False both reproduce the current golden bytes exactly."""
    import hashlib

    ws_omitted, _ = _write_fixed(tmp_path / "a")
    ws_false, _ = _write_fixed(tmp_path / "b", is_coordinator=False)
    assert hashlib.sha256(ws_omitted.read_bytes()).hexdigest() == _GOLDEN_WS_SHA256
    assert hashlib.sha256(ws_false.read_bytes()).hexdigest() == _GOLDEN_WS_SHA256


def test_step2_env_signal_is_the_only_byte_diff_vs_pre_step2(tmp_path: Path) -> None:
    """Step2 B 轨二 precision contract (the brief's 精确断言): today's artifact minus the
    ONE ``terminal.integrated.env.osx`` key re-serializes to EXACTLY the pre-Step2 golden
    bytes — i.e. the all-path additive change is the two env vars and zero other drift."""
    import hashlib

    ws_file, _ = _write_fixed(tmp_path)
    spec = json.loads(ws_file.read_text())
    env = spec["settings"].pop("terminal.integrated.env.osx")
    assert env == {"HANDOFF_SESSION_ROLE": "worker", "HANDOFF_SESSION_TASK": "wh-foo"}
    stripped = json.dumps(spec, indent=2).encode("utf-8")
    assert hashlib.sha256(stripped).hexdigest() == _PRE_STEP2_GOLDEN_WS_SHA256


def test_coordinator_workspace_is_redtopped(tmp_path: Path) -> None:
    """is_coordinator=True → 🧭中枢· prefix WRAPS the nonce-bound title (the watchdog's
    substring nonce/task gates must still hit) + the exact shared red-titleBar spec."""
    ws_file, sidecar_text = _write_fixed(tmp_path, is_coordinator=True)
    spec = json.loads(ws_file.read_text())
    title = spec["settings"]["window.title"]
    assert title.startswith("🧭中枢·")
    assert _NONCE in title  # nonce substring gate intact under the prefix
    assert "wh-foo" in title  # task token intact (task-match submit guard)
    # Exactly the THIN key set + the one visual red-top key + the Step2 env signal —
    # nothing else rides along.
    assert set(spec["settings"]) == {
        "window.title",
        "workbench.activityBar.location",
        "workbench.startupEditor",
        "claudeCode.preferredLocation",
        "workbench.colorCustomizations",
        "terminal.integrated.env.osx",
    }
    # Step2 B 轨二: the dump --coordinator window's SESSION role is the coordinator one
    # (supervisor_succession → the memory-guard matrix lets it write/sediment memory),
    # even though the watchdog SIDECAR keeps role="worker" (asserted below — that
    # contract is untouched).
    assert spec["settings"]["terminal.integrated.env.osx"] == {
        "HANDOFF_SESSION_ROLE": "supervisor_succession",
        "HANDOFF_SESSION_TASK": "wh-foo",
    }
    # Full 4-key red spec, byte-parity with worktree/dx-spawn (shared constants).
    assert spec["settings"]["workbench.colorCustomizations"] == {
        "titleBar.activeBackground": "#8B0000",
        "titleBar.activeForeground": "#FFFFFF",
        "titleBar.inactiveBackground": "#5A0000",
        "titleBar.inactiveForeground": "#E0E0E0",
    }
    # Sidecar contract: still compact single-line JSON (bash json_get line reader) with
    # the unchanged worker fields, PLUS the warmgap-B SHOULD observable marker
    # ``is_coordinator: true`` (watchdog/兜底/cleanup semantics) — added only on a 中枢.
    assert "\n" not in sidecar_text
    meta = json.loads(sidecar_text)
    assert meta["role"] == "worker"
    assert meta["close_policy"] == "keep"
    assert meta["spawn_nonce"] == _NONCE
    assert meta["is_coordinator"] is True


# ─── warmgap-B: coordinator ⇒ singlepane engine invariant (owner 批 B / 2026-06-10) ──
# The fourth red-top gap: a project in NEITHER singlepane_projects NOR worktree mode let
# `dump --coordinator` fall to the WARM path (reuse-a-window tab — no red-top, no nonce
# title, no independent window). MUST-1 forces singlepane production for a non-worktree
# coordinator regardless of config; MUST-2 makes a write failure on that path FAIL CLOSED.


def test_coordinator_forces_singlepane_for_non_optin_project(tmp_path: Path) -> None:
    """MUST-1: is_coordinator=True + project NOT in singlepane_projects → the full
    singlepane artifact set is produced anyway (out-of-tree workspace + red-top + nonce
    title + JSON sidecar with the coordinator marker) — the warm path is structurally
    unable to honour the 中枢 window invariant, so config cannot opt a coordinator out."""
    cfg = _cfg(tmp_path, [])  # NO project opts in — the exact warm-gap configuration
    qd = tmp_path / "handoff-fanout" / "queue"
    qd.mkdir(parents=True)
    real_repo = tmp_path / "repo"
    real_repo.mkdir()

    dump.maybe_write_singlepane_sidecar(
        cfg,
        "handoff-fanout",
        "hf-coord-1",
        real_repo,
        qd,
        worktree_active=False,
        role="worker",
        close_policy="keep",
        spawn_nonce=_NONCE,
        is_coordinator=True,
    )

    sidecar_text = (qd / "hf-coord-1.singlepane").read_text()
    assert "\n" not in sidecar_text  # compact single-line contract intact on the forced path
    meta = json.loads(sidecar_text)
    assert meta["is_coordinator"] is True
    ws_file = Path(meta["workspace"])
    assert ws_file.parent == tmp_path / "handoff-fanout" / "singlepane"  # out-of-tree
    spec = json.loads(ws_file.read_text())
    assert spec["folders"] == [{"path": str(real_repo)}]  # opens the real repo
    title = spec["settings"]["window.title"]
    assert title.startswith("🧭中枢·")  # red-top prefix
    assert _NONCE in title  # nonce gate intact
    assert (
        spec["settings"]["workbench.colorCustomizations"]["titleBar.activeBackground"] == "#8B0000"
    )
    assert spec["settings"]["terminal.integrated.env.osx"]["HANDOFF_SESSION_ROLE"] == (
        "supervisor_succession"
    )


def test_non_coordinator_non_optin_still_skips(tmp_path: Path) -> None:
    """MUST-1 regression guard: the forced path is coordinator-ONLY — a plain dump for a
    non-opted-in project still produces nothing (and cleans a stale sidecar), byte-identical
    legacy behavior."""
    cfg = _cfg(tmp_path, [])
    qd = tmp_path / "handoff-fanout" / "queue"
    qd.mkdir(parents=True)
    sidecar = qd / "hf-solo-1.singlepane"
    sidecar.write_text("stale")
    dump.maybe_write_singlepane_sidecar(
        cfg,
        "handoff-fanout",
        "hf-solo-1",
        tmp_path,
        qd,
        worktree_active=False,
        role="solo",
        close_policy="keep",
        spawn_nonce=_NONCE,
        is_coordinator=False,
    )
    assert not sidecar.exists()
    assert not (tmp_path / "handoff-fanout" / "singlepane").exists()


def test_coordinator_worktree_active_unchanged(tmp_path: Path) -> None:
    """MUST-1 boundary: a worktree-CREATED coordinator keeps its worktree window (which
    already carries the red-top via create_worktree) — the invariant only takes over the
    would-otherwise-be-warm case, so no singlepane sidecar is produced."""
    cfg = _cfg(tmp_path, [])
    qd = tmp_path / "handoff-fanout" / "queue"
    qd.mkdir(parents=True)
    dump.maybe_write_singlepane_sidecar(
        cfg,
        "handoff-fanout",
        "hf-coord-wt",
        tmp_path,
        qd,
        worktree_active=True,
        role="worker",
        close_policy="keep",
        spawn_nonce=_NONCE,
        is_coordinator=True,
    )
    assert not (qd / "hf-coord-wt.singlepane").exists()


def test_coordinator_write_failure_fails_closed(tmp_path: Path) -> None:
    """MUST-2: a coordinator whose singlepane workspace cannot be written must RAISE
    (CoordinatorSinglepaneError → dump aborts before the .uri publish), never degrade to
    the warm path. A FILE squatting the singlepane dir path makes mkdir raise OSError."""
    import pytest

    cfg = _cfg(tmp_path, [])
    qd = tmp_path / "handoff-fanout" / "queue"
    qd.mkdir(parents=True)
    (tmp_path / "handoff-fanout" / "singlepane").write_text("squatter")  # mkdir → OSError

    with pytest.raises(dump.CoordinatorSinglepaneError):
        dump.maybe_write_singlepane_sidecar(
            cfg,
            "handoff-fanout",
            "hf-coord-2",
            tmp_path,
            qd,
            worktree_active=False,
            role="worker",
            close_policy="keep",
            spawn_nonce=_NONCE,
            is_coordinator=True,
        )
    assert not (qd / "hf-coord-2.singlepane").exists()  # no partial sidecar left behind


def test_non_coordinator_write_failure_stays_non_fatal(tmp_path: Path, capsys) -> None:
    """MUST-2 boundary: the SAME write failure on a non-coordinator dump keeps the legacy
    best-effort contract (print + clean up, never raise) — singlepane stays UX polish for
    everyone but the 中枢."""
    cfg = _cfg(tmp_path, ["wilde-hexe"])
    qd = tmp_path / "wilde-hexe" / "queue"
    qd.mkdir(parents=True)
    (tmp_path / "wilde-hexe" / "singlepane").write_text("squatter")

    dump.maybe_write_singlepane_sidecar(
        cfg,
        "wilde-hexe",
        "wh-solo-2",
        tmp_path,
        qd,
        worktree_active=False,
        role="solo",
        close_policy="keep",
        spawn_nonce=_NONCE,
        is_coordinator=False,
    )
    assert "(non-fatal) could not write singlepane workspace" in capsys.readouterr().out
    assert not (qd / "wh-solo-2.singlepane").exists()
