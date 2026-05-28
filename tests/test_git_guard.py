"""Role enforcement tests for the bundled git wrapper.

Covers:
  - sub-task role: 10 mutating commands all reject with exit 99
  - sub-task role: read-only commands pass through (e.g. status, log)
  - fan-in role:   all commands pass through (sole committer)
  - main / unset:  all commands pass through
"""

from __future__ import annotations

import os
import subprocess

import pytest

from handoff_fanout.git_guard import git_guard_dir

WRAPPER = git_guard_dir() / "git"

BLOCKED_COMMANDS = [
    "commit",
    "push",
    "rebase",
    "cherry-pick",
    "reset",
    "revert",
    "tag",
    "am",
    "format-patch",
    "merge",
]


def _run(args: list[str], role: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "HANDOFF_ROLE"}
    if role is not None:
        env["HANDOFF_ROLE"] = role
    return subprocess.run(
        [str(WRAPPER), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_wrapper_is_executable() -> None:
    assert WRAPPER.exists(), f"wrapper missing: {WRAPPER}"
    assert os.access(WRAPPER, os.X_OK), f"wrapper not executable: {WRAPPER}"


@pytest.mark.parametrize("cmd", BLOCKED_COMMANDS)
def test_sub_task_role_blocks_mutating_command(cmd: str) -> None:
    result = _run([cmd], role="sub-task")
    assert result.returncode == 99, (
        f"expected exit 99 for `git {cmd}` under sub-task role, "
        f"got {result.returncode}; stderr={result.stderr!r}"
    )
    assert "handoff-fanout git-guard" in result.stderr
    assert f"`git {cmd}`" in result.stderr


def test_sub_task_role_passes_through_read_only() -> None:
    # `git --version` is always safe and always available; it should never
    # be blocked regardless of role.
    result = _run(["--version"], role="sub-task")
    assert result.returncode == 0, (
        f"`git --version` should pass through under sub-task role, "
        f"got {result.returncode}; stderr={result.stderr!r}"
    )
    assert "git version" in result.stdout


@pytest.mark.parametrize("role", ["fan-in", "main"])
def test_non_sub_task_roles_pass_through(role: str) -> None:
    result = _run(["--version"], role=role)
    assert result.returncode == 0, (
        f"`git --version` should pass through under {role}, "
        f"got {result.returncode}; stderr={result.stderr!r}"
    )


def test_unset_role_passes_through() -> None:
    result = _run(["--version"], role=None)
    assert result.returncode == 0
    assert "git version" in result.stdout
