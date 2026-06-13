"""``spawner_focus.validate_spawner_focus`` — the SINGLE security gate shared by ``spawn``
(CLI ``--spawner-focus-path``) and ``dump`` (``$HANDOFF_WINDOW_FOCUS_PATH`` env).

direct-jump-spawn (2026-06-13): the validated value becomes an argument to ``code <file>`` in
``code-router.sh``, so the gate must reject anything that isn't an existing ``.handoff.code-workspace``
under a trusted root — and FAIL-OPEN (return ``None``, never raise) for every reject so a bad UX hint
never blocks a spawn/dump.

``isolated_handoff_home`` (conftest) points ``$HANDOFF_HOME`` at a tmp dir, so ``config.load().home``
— an allowed root — is that tmp dir; every test builds cfg via ``config.load()`` after it ran.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from handoff_fanout import config as _config
from handoff_fanout import spawner_focus


def _valid_ws(home: Path) -> Path:
    """An existing ``.handoff.code-workspace`` under the handoff home (an allowed root)."""
    ws = home / "some-proj" / "singlepane" / "coord-x.handoff.code-workspace"
    ws.parent.mkdir(parents=True)
    ws.write_text("{}")
    return ws


def test_valid_under_home_returns_realpath(isolated_handoff_home):
    ws = _valid_ws(isolated_handoff_home)
    got = spawner_focus.validate_spawner_focus(str(ws), cfg=_config.load())
    assert got == os.path.realpath(str(ws))


def test_valid_under_tmpdir_returns_realpath(isolated_handoff_home, tmp_path):
    """``dx-spawn --coordinator`` writes its out-of-tree WS_FILE under $TMPDIR — also allowed."""
    ws = tmp_path / "coord-tmp.handoff.code-workspace"
    ws.write_text("{}")
    got = spawner_focus.validate_spawner_focus(str(ws), cfg=_config.load())
    assert got == os.path.realpath(str(ws))


@pytest.mark.parametrize("raw", [None, ""])
def test_absent_input_returns_none(isolated_handoff_home, raw):
    assert spawner_focus.validate_spawner_focus(raw, cfg=_config.load()) is None


def test_wrong_suffix_dropped(isolated_handoff_home):
    """A non-``.handoff.code-workspace`` would let the router ``code <arbitrary file>`` — reject."""
    bogus = isolated_handoff_home / "not-a-workspace.txt"
    bogus.write_text("x")
    assert spawner_focus.validate_spawner_focus(str(bogus), cfg=_config.load()) is None


def test_nonexistent_dropped(isolated_handoff_home):
    ghost = isolated_handoff_home / "ghost.handoff.code-workspace"
    assert spawner_focus.validate_spawner_focus(str(ghost), cfg=_config.load()) is None


def test_directory_not_file_dropped(isolated_handoff_home):
    """Right suffix but a directory (not a regular file) → dropped (isfile gate)."""
    d = isolated_handoff_home / "dir.handoff.code-workspace"
    d.mkdir()
    assert spawner_focus.validate_spawner_focus(str(d), cfg=_config.load()) is None


def test_outside_allowed_roots_dropped(isolated_handoff_home):
    """An absolute ``.handoff.code-workspace`` OUTSIDE every allowed root → dropped (root check)."""
    assert (
        spawner_focus.validate_spawner_focus(
            "/etc/forged.handoff.code-workspace", cfg=_config.load()
        )
        is None
    )


# ─── derive_singlepane_focus (djs-jump-return: SELF-REPORT from self-task, no env) ──────


def test_derive_returns_path_when_singlepane_workspace_exists(isolated_handoff_home):
    """The engine wrote ``<home>/<proj>/singlepane/<task>.handoff.code-workspace`` when this
    coordinator spawned — derive reconstructs it from the self-reported task (no env channel)."""
    home = isolated_handoff_home
    ws = home / "demo-proj" / "singlepane" / "coord-leg-7.handoff.code-workspace"
    ws.parent.mkdir(parents=True)
    ws.write_text("{}")
    got = spawner_focus.derive_singlepane_focus(home, "demo-proj", "coord-leg-7")
    assert got == str(ws)
    # and the derived path round-trips through the SAME security gate (single boundary)
    assert spawner_focus.validate_spawner_focus(got, cfg=_config.load()) == os.path.realpath(str(ws))


def test_derive_returns_none_when_workspace_missing(isolated_handoff_home):
    """Bootstrap leg (dx-spawn-launched coordinator, no engine singlepane file) → None →
    caller fail-opens to today's per-project goto, no spurious 'dropped' warning."""
    assert spawner_focus.derive_singlepane_focus(isolated_handoff_home, "demo-proj", "nope") is None


@pytest.mark.parametrize(("project", "task"), [("", "t"), ("p", ""), ("", "")])
def test_derive_returns_none_on_empty_identity(isolated_handoff_home, project, task):
    assert spawner_focus.derive_singlepane_focus(isolated_handoff_home, project, task) is None
