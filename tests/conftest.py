"""Shared pytest fixtures."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Initialise a throwaway git repo in ``tmp_path`` and chdir into it.

    The repo is configured with a deterministic user identity so commits
    succeed under CI (which doesn't have a global git user by default).
    """
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@handoff-fanout.local"], cwd=tmp_path, check=True)
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
