"""Regression: pytest must never invoke the real ``pbcopy``.

Root cause (主人 2026-05-29): the ``handoff dump`` active path
unconditionally piped the rendered handoff markdown into ``pbcopy`` so
the user could paste it into a new IDE tab. When the dump path was
exercised under pytest (``test_retro.py`` runs ``dump.main()`` end-to-end
against tmpdir fixtures with ``project=demo`` / ``task=demo-task``), the
sample fixture markdown silently replaced whatever the user had on the
clipboard. The user hit this while pasting a handwritten BLOCKED report
and got the fixture text instead.

Fix: ``dump._maybe_pbcopy`` skips the real call when either env var is
set — ``PYTEST_CURRENT_TEST`` (auto-set by pytest for each running test)
or ``HANDOFF_NO_PBCOPY`` (manual opt-out for CI / headless / scripted
callers). Industry-standard Option C: zero-config inside test runs, one
env var for everyone else.

This file exercises the helper directly and the integration through
``dump.main()`` so the guard is tested at both layers. A session-level
``conftest`` sentinel (``no_pbcopy_during_tests``) wraps
``subprocess.Popen`` for the whole suite and fails any test that lets a
``pbcopy`` command escape.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from handoff_fanout import dump, handoff_precheck


# ─── C: unit — _maybe_pbcopy env guards ─────────────────────────────────────


def test_maybe_pbcopy_skips_when_pytest_env_set(monkeypatch):
    """The pytest auto-set env var is the primary guard."""
    calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        raise AssertionError(f"pbcopy invoked under pytest: argv={argv!r}")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y (call)")
    monkeypatch.delenv("HANDOFF_NO_PBCOPY", raising=False)

    dump._maybe_pbcopy("hello clipboard")
    assert calls == []


def test_maybe_pbcopy_skips_when_handoff_no_pbcopy_set(monkeypatch):
    """``HANDOFF_NO_PBCOPY`` is the manual opt-out for CI / scripts."""
    calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        raise AssertionError(f"pbcopy invoked despite HANDOFF_NO_PBCOPY: argv={argv!r}")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HANDOFF_NO_PBCOPY", "1")

    dump._maybe_pbcopy("hello clipboard")
    assert calls == []


def test_maybe_pbcopy_calls_pbcopy_when_both_unset(monkeypatch):
    """When neither guard is set the real call still runs (preserved behaviour)."""
    calls: list[tuple[list[str], bytes]] = []

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            self.argv = argv

        def communicate(self, input=None):
            calls.append((self.argv, input))
            return b"", b""

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HANDOFF_NO_PBCOPY", raising=False)

    dump._maybe_pbcopy("hello clipboard")

    assert len(calls) == 1
    argv, payload = calls[0]
    assert argv == ["pbcopy"]
    assert payload == b"hello clipboard"


def test_maybe_pbcopy_swallows_missing_pbcopy_binary(monkeypatch):
    """Non-macOS host: ``pbcopy`` missing is a soft no-op (preserved)."""

    def boom(*_a, **_k):
        raise FileNotFoundError("pbcopy")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HANDOFF_NO_PBCOPY", raising=False)

    # Must not raise.
    dump._maybe_pbcopy("hello clipboard")


def test_maybe_pbcopy_swallows_oserror(monkeypatch):
    """Stdin closed / broken pipe / EPIPE: still soft no-op."""

    def boom(*_a, **_k):
        raise OSError("broken pipe")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HANDOFF_NO_PBCOPY", raising=False)

    dump._maybe_pbcopy("hello clipboard")


def test_maybe_pbcopy_skips_when_pytest_env_empty_string(monkeypatch):
    """Presence-not-truthiness: ``PYTEST_CURRENT_TEST=`` (empty) still skips.

    Documented contract is "env var set to skip" — empty string is *set*
    per ``in os.environ``, so guard must honour it. Catches the codex R1
    P2 finding (truthiness check would let empty pass through to pbcopy).
    """

    def fake_popen(argv, **kwargs):
        raise AssertionError(f"pbcopy invoked despite PYTEST_CURRENT_TEST set (empty): argv={argv!r}")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "")
    monkeypatch.delenv("HANDOFF_NO_PBCOPY", raising=False)

    dump._maybe_pbcopy("hello clipboard")


def test_maybe_pbcopy_skips_when_handoff_no_pbcopy_empty_string(monkeypatch):
    """Same contract for ``HANDOFF_NO_PBCOPY=`` (empty) — must skip."""

    def fake_popen(argv, **kwargs):
        raise AssertionError(f"pbcopy invoked despite HANDOFF_NO_PBCOPY set (empty): argv={argv!r}")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HANDOFF_NO_PBCOPY", "")

    dump._maybe_pbcopy("hello clipboard")


# ─── I: integration — dump.main() active path never touches the clipboard ──


@pytest.fixture
def handoff_home(tmp_path, monkeypatch):
    home = tmp_path / "handoff"
    home.mkdir()
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.delenv("HANDOFF_RETRO_BYPASS", raising=False)
    monkeypatch.delenv("HANDOFF_RETRO_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_SAFE_COMMIT_LOCK", raising=False)
    monkeypatch.delenv("HANDOFF_SAFE_COMMIT_BYPASS", raising=False)
    return home


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=ws, check=True)
    (ws / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)
    monkeypatch.chdir(ws)
    return ws


PROJECT = "demo"
TASK = "demo-task"


def _make_evidence(home: Path, ws: Path) -> Path:
    p0 = {k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS}
    p1 = {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS}
    payload = handoff_precheck.build_evidence(
        task_id=TASK,
        project=PROJECT,
        workspace=ws,
        mode="normal",
        nonce=None,
        phase0=p0,
        phase1=p1,
    )
    path = home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, path)
    return path


def test_dump_main_active_does_not_touch_clipboard(handoff_home, workspace, monkeypatch):
    """End-to-end: dump.main() active path must not Popen pbcopy under pytest."""
    pbcopy_calls: list[list[str]] = []
    real_popen = subprocess.Popen

    def watching_popen(argv, *args, **kwargs):
        if isinstance(argv, list | tuple) and argv and "pbcopy" in str(argv[0]):
            pbcopy_calls.append(list(argv))
            raise AssertionError(f"pbcopy escaped during dump.main(): argv={argv!r}")
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", watching_popen)

    ev = _make_evidence(handoff_home, workspace)
    code = dump.main(
        [
            "--task",
            TASK,
            "--next",
            "test next",
            "--project",
            PROJECT,
            "--workspace",
            str(workspace),
            "--status",
            "active",
            "--retro-evidence",
            str(ev),
        ]
    )
    assert code == 0
    assert pbcopy_calls == []
    # The handoff markdown must still have been written — fix is opt-out of
    # pbcopy only, not opt-out of the rest of the active path.
    assert (handoff_home / PROJECT / "queue" / f"{TASK}.md").exists()
    assert (handoff_home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_dump_main_active_with_explicit_handoff_no_pbcopy(handoff_home, workspace, monkeypatch):
    """Manual ``HANDOFF_NO_PBCOPY=1`` (even outside pytest) also suppresses."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HANDOFF_NO_PBCOPY", "1")

    pbcopy_calls: list[list[str]] = []
    real_popen = subprocess.Popen

    def watching_popen(argv, *args, **kwargs):
        if isinstance(argv, list | tuple) and argv and "pbcopy" in str(argv[0]):
            pbcopy_calls.append(list(argv))
            raise AssertionError(f"pbcopy escaped despite HANDOFF_NO_PBCOPY: argv={argv!r}")
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", watching_popen)

    ev = _make_evidence(handoff_home, workspace)
    code = dump.main(
        [
            "--task",
            TASK,
            "--next",
            "test next",
            "--project",
            PROJECT,
            "--workspace",
            str(workspace),
            "--status",
            "active",
            "--retro-evidence",
            str(ev),
        ]
    )
    assert code == 0
    assert pbcopy_calls == []


def test_pytest_current_test_is_actually_set():
    """Sanity: pytest auto-sets PYTEST_CURRENT_TEST so guard C in production works."""
    assert os.environ.get("PYTEST_CURRENT_TEST", "").startswith(
        "tests/test_no_clipboard_pollution.py::test_pytest_current_test_is_actually_set"
    )
