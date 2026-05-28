"""Regression tests for ``install/git-hooks/pre-commit``.

The hook is the Layer 2 invariant check: it rejects a commit when the staged
file set is not a subset of ``$HANDOFF_EXPECTED_FILES``. These tests invoke
the script directly with various stagings to lock in its contract.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "install" / "git-hooks" / "pre-commit"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Initialise a throwaway git repo with one tracked file."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    # Minimum identity so commits don't fail (the hook itself runs without a real commit).
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    return tmp_path


def _stage(repo: Path, name: str, content: str = "x\n") -> None:
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)


def _run_hook(repo: Path, expected: str | None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("HANDOFF_EXPECTED_FILES", None)
    if expected is not None:
        env["HANDOFF_EXPECTED_FILES"] = expected
    return subprocess.run(
        ["bash", str(HOOK)], cwd=str(repo), env=env,
        capture_output=True, text=True,
    )


def test_hook_passthrough_when_env_unset(repo: Path) -> None:
    """No HANDOFF_EXPECTED_FILES => manual commit, hook is a no-op."""
    _stage(repo, "a.py")
    _stage(repo, "b.py")
    r = _run_hook(repo, expected=None)
    assert r.returncode == 0, r.stderr


def test_hook_passes_when_staged_equals_expected(repo: Path) -> None:
    _stage(repo, "a.py")
    r = _run_hook(repo, expected="a.py")
    assert r.returncode == 0, r.stderr


def test_hook_passes_when_staged_is_subset_of_expected(repo: Path) -> None:
    _stage(repo, "a.py")
    r = _run_hook(repo, expected="a.py:b.py:c.py")
    assert r.returncode == 0, r.stderr


def test_hook_blocks_when_staged_has_extra_file(repo: Path) -> None:
    _stage(repo, "a.py")
    _stage(repo, "rogue.py")
    r = _run_hook(repo, expected="a.py")
    assert r.returncode != 0
    assert "rogue.py" in r.stderr
    assert "Layer 2" in r.stderr


def test_hook_blocks_with_multi_file_diagnostic(repo: Path) -> None:
    _stage(repo, "a.py")
    _stage(repo, "rogue1.py")
    _stage(repo, "rogue2.py")
    r = _run_hook(repo, expected="a.py")
    assert r.returncode != 0
    assert "rogue1.py" in r.stderr
    assert "rogue2.py" in r.stderr


def test_hook_ignores_empty_entries_in_expected_list(repo: Path) -> None:
    """``a.py::b.py`` should be parsed as {a.py, b.py}, the empty entry skipped."""
    _stage(repo, "a.py")
    r = _run_hook(repo, expected="a.py::b.py")
    assert r.returncode == 0, r.stderr


def test_hook_is_executable() -> None:
    """install.sh chmods it; verify the file itself ships executable."""
    assert HOOK.exists(), f"hook script missing: {HOOK}"
    assert os.access(HOOK, os.X_OK), f"hook script not +x: {HOOK}"


def test_hook_passes_when_nothing_staged(repo: Path) -> None:
    """Empty staging area + non-empty expected list — vacuously a subset."""
    r = _run_hook(repo, expected="a.py:b.py")
    assert r.returncode == 0, r.stderr


def test_bash_available_for_hook() -> None:
    """The hook uses bash-only features (associative arrays); fail loud if missing."""
    assert shutil.which("bash") is not None
