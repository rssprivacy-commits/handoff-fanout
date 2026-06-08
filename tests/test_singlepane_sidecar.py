"""Single-pane (non-worktree) spawn sidecar — ``maybe_write_singlepane_sidecar``.

Owner ruling S (2026-06-08): deliver a default single-editor-pane VS Code window for an
opted-in project WITHOUT git-worktree isolation. The dump generates an OUT-OF-TREE
``.handoff.code-workspace`` (so it never dirties the repo) + a ``queue/<task>.singlepane``
sidecar the watchdog opens; the handoff-helper extension collapses the side bars on load.
"""

from __future__ import annotations

import json
from pathlib import Path

from handoff_fanout import config as _config
from handoff_fanout import dump


def _cfg(home: Path, singlepane: list[str]) -> _config.Config:
    return _config._from_dict({"singlepane_projects": singlepane}, home=home)


def test_optin_writes_sidecar_and_out_of_tree_workspace(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ["wilde-hexe"])
    qd = tmp_path / "wilde-hexe" / "queue"
    qd.mkdir(parents=True)
    real_repo = tmp_path / "repo"
    real_repo.mkdir()

    dump.maybe_write_singlepane_sidecar(
        cfg, "wilde-hexe", "wh-foo", real_repo, qd, worktree_active=False
    )

    sidecar = qd / "wh-foo.singlepane"
    assert sidecar.exists()
    ws_file = Path(sidecar.read_text())
    # OUT-OF-TREE: lives under $HANDOFF_HOME/<project>/singlepane, NOT in the repo tree.
    assert ws_file.parent == tmp_path / "wilde-hexe" / "singlepane"
    assert real_repo not in ws_file.parents
    assert ws_file.name.endswith(".handoff.code-workspace")  # extension guard suffix

    spec = json.loads(ws_file.read_text())
    assert spec["folders"] == [{"path": str(real_repo)}]  # window opens the REAL repo
    assert "wh-foo" in spec["settings"]["window.title"]  # task token for the submit guard
    assert spec["settings"]["workbench.activityBar.location"] == "hidden"  # single pane


def test_worktree_active_removes_sidecar(tmp_path: Path) -> None:
    """A worktree spawn has its OWN .handoff.code-workspace and wins — no singlepane sidecar."""
    cfg = _cfg(tmp_path, ["wilde-hexe"])
    qd = tmp_path / "wilde-hexe" / "queue"
    qd.mkdir(parents=True)
    sidecar = qd / "wh-foo.singlepane"
    sidecar.write_text("stale")  # pretend a prior run left one
    dump.maybe_write_singlepane_sidecar(
        cfg, "wilde-hexe", "wh-foo", tmp_path, qd, worktree_active=True
    )
    assert not sidecar.exists()


def test_non_optin_project_no_sidecar_and_cleans_stale(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ["wilde-hexe"])  # erp-system NOT opted in
    qd = tmp_path / "erp-system" / "queue"
    qd.mkdir(parents=True)
    sidecar = qd / "erp-bar.singlepane"
    sidecar.write_text("stale-optin")  # a config flip-off must clean this up
    dump.maybe_write_singlepane_sidecar(
        cfg, "erp-system", "erp-bar", tmp_path, qd, worktree_active=False
    )
    assert not sidecar.exists()


def test_singlepane_projects_fail_open_on_degenerate(tmp_path: Path) -> None:
    # Degenerate values → empty list (no project opts in). Never enables single-pane on a typo.
    for bad in ("wilde-hexe", 123, None, [""], [1, 2]):
        assert _config._from_dict({"singlepane_projects": bad}, home=tmp_path).singlepane_projects == []
    assert _config._from_dict(
        {"singlepane_projects": ["wilde-hexe", "", "x"]}, home=tmp_path
    ).singlepane_projects == ["wilde-hexe", "x"]
