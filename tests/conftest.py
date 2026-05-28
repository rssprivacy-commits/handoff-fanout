"""Shared pytest fixtures."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_pbcopy_during_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense-in-depth: fail loudly if any test lets ``pbcopy`` escape.

    ``dump._maybe_pbcopy`` already short-circuits when ``PYTEST_CURRENT_TEST``
    is set, but this autouse wrapper around ``subprocess.Popen`` is the
    sentinel: any future code path (or test that monkey-patches the guard
    away) that pipes into ``pbcopy`` raises here instead of silently
    overwriting the user's clipboard.

    Non-``pbcopy`` ``Popen`` calls (git, osascript, etc.) pass through to
    the real implementation untouched.
    """
    real_popen = subprocess.Popen

    def guarded_popen(argv, *args, **kwargs):
        first = None
        if isinstance(argv, list | tuple) and argv:
            first = str(argv[0])
        elif isinstance(argv, str):
            first = argv.split()[0] if argv.split() else None
        if first and "pbcopy" in first.rsplit("/", 1)[-1]:
            raise AssertionError(
                f"pbcopy escaped during pytest run: argv={argv!r}. "
                "dump._maybe_pbcopy should have suppressed this — check "
                "PYTEST_CURRENT_TEST handling or new clipboard call sites."
            )
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Initialise a throwaway git repo in ``tmp_path`` and chdir into it.

    The repo is configured with a deterministic user identity so commits
    succeed under CI (which doesn't have a global git user by default).
    """
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@handoff-fanout.local"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "handoff-fanout test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture
def isolated_handoff_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HANDOFF_HOME at a tmp dir so tests never touch the real user's state."""
    home = tmp_path / "handoff"
    home.mkdir()
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    # Make sure no stale safe-commit lock path leaks in from the user env.
    monkeypatch.delenv("HANDOFF_SAFE_COMMIT_LOCK", raising=False)
    monkeypatch.delenv("HANDOFF_SAFE_COMMIT_BYPASS", raising=False)
    return home
