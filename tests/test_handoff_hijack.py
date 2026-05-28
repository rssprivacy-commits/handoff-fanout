"""Hijack defense (v5.3) — ported from the ERP scripts/tests/test_handoff_hijack.py.

The original tests drove a bash ``handoff-safe-commit.sh``. The Phase A1
port replaced the wrapper with a Python module, so these tests now invoke
``python -m handoff_fanout.safe_commit`` instead. The 8 cases are
otherwise the same, covering:

  1. Two serial commits succeed (functional baseline).
  2. A pre-commit hook that auto-adds an extra file is rejected
     (segment-5 check inside the hook).
  3. A stale lock dir (>5 min mtime) is force-cleared and the commit
     proceeds (the atomic helper now logs ``锁陈旧`` on stderr).
  4. Pre-existing staged paths in the index are tolerated; ``--only``
     keeps them out of the actual commit.
  5. ``HANDOFF_SAFE_COMMIT_BYPASS=1`` is a no-op on the happy path.
  6-8. The minimal segment-5 hook in isolation: reject when actual has
     extras, accept on subset, skip when env var unset.

The tests provide their own minimal ``pre-commit`` hook so they don't
depend on any project-level pre-commit infrastructure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"


# Minimal segment-5 logic — exactly the check that the v5.3 wrapper relies
# on inside ``install/git-hooks/pre-commit``. Kept inline so the tests
# don't depend on a host-installed hook.
MINIMAL_PRE_COMMIT = r"""#!/bin/bash
# Minimal pre-commit for v5.3 hijack defense test — segment 5 only.
FAIL=0
if [ -n "$HANDOFF_EXPECTED_FILES" ] && [ -f "$HANDOFF_EXPECTED_FILES" ]; then
  EXP=$(sort -u < "$HANDOFF_EXPECTED_FILES")
  ACTUAL=$(git diff --cached --name-only | sort -u)
  EXTRA=$(comm -23 <(echo "$ACTUAL") <(echo "$EXP") || true)
  if [ -n "$EXTRA" ]; then
    echo "PRECOMMIT_HIJACK_REJECT: extra=$EXTRA" >&2
    FAIL=1
  fi
fi
exit $FAIL
"""


@pytest.fixture
def gitrepo(tmp_path):
    """Initialise a tmp git repo with the minimal segment-5 pre-commit hook."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@hijack.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "HijackTest"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)

    # one initial commit so HEAD exists
    (repo / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init", "-q"], cwd=repo, check=True)

    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(MINIMAL_PRE_COMMIT)
    hook.chmod(0o755)

    lock = tmp_path / "lock"

    env = os.environ.copy()
    env["HANDOFF_SAFE_COMMIT_LOCK"] = str(lock)
    env["HANDOFF_SAFE_COMMIT_RETRY_LIMIT"] = "3"
    env["HANDOFF_SAFE_COMMIT_RETRY_WAIT"] = "1"
    # Make sure the package is importable from the subprocess regardless
    # of whether the user installed it editable.
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    env.pop("HANDOFF_ROLE", None)
    env.pop("HANDOFF_EXPECTED_FILES", None)
    env.pop("HANDOFF_SAFE_COMMIT_BYPASS", None)

    return {"repo": repo, "lock": lock, "env": env}


def _safe_commit(gitrepo, message, files, extra_env=None, expect_rc=0):
    env = dict(gitrepo["env"])
    if extra_env:
        env.update(extra_env)
    cmd = [
        sys.executable,
        "-m",
        "handoff_fanout.safe_commit",
        "-m",
        message,
        "--",
    ] + files
    result = subprocess.run(
        cmd,
        cwd=gitrepo["repo"],
        env=env,
        capture_output=True,
        text=True,
    )
    if expect_rc is not None:
        assert result.returncode == expect_rc, (
            f"safe-commit rc={result.returncode} (want {expect_rc}). "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return result


def _git(gitrepo, *args, check=True):
    result = subprocess.run(
        ["git", *args],
        cwd=gitrepo["repo"],
        env=gitrepo["env"],
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout


# ─── 1: serial commits ──────────────────────────────────────────────────────


def test_safe_commit_serial_two_calls_both_succeed(gitrepo):
    repo = gitrepo["repo"]
    (repo / "file1.txt").write_text("first\n")
    _safe_commit(gitrepo, "commit 1", ["file1.txt"])

    (repo / "file2.txt").write_text("second\n")
    _safe_commit(gitrepo, "commit 2", ["file2.txt"])

    log = _git(gitrepo, "log", "--oneline").strip().splitlines()
    assert len(log) == 3  # init + 2
    assert "commit 2" in log[0]
    assert "commit 1" in log[1]

    show2 = _git(gitrepo, "show", "--stat", "--name-only", "--pretty=format:", "HEAD")
    assert "file2.txt" in show2
    assert "file1.txt" not in show2


# ─── 2: hijack rejection via segment 5 ──────────────────────────────────────


def test_safe_commit_rejects_when_hook_auto_adds_extra_file(gitrepo):
    """A pre-commit hook that ``git add``s an unexpected file must trigger segment 5."""
    repo = gitrepo["repo"]
    (repo / "wanted.txt").write_text("want\n")
    (repo / "leaked.txt").write_text("leak\n")

    malicious_hook = r"""#!/bin/bash
# Inject auto-add to simulate hook hijack
git add leaked.txt
""" + MINIMAL_PRE_COMMIT.replace("#!/bin/bash\n", "")
    (repo / ".git" / "hooks" / "pre-commit").write_text(malicious_hook)
    (repo / ".git" / "hooks" / "pre-commit").chmod(0o755)

    result = _safe_commit(gitrepo, "should fail", ["wanted.txt"], expect_rc=1)
    assert "PRECOMMIT_HIJACK_REJECT" in result.stderr or "hijack" in result.stderr.lower()

    log = _git(gitrepo, "log", "--oneline").strip().splitlines()
    assert len(log) == 1  # init only


# ─── 3: stale lock auto-cleared ─────────────────────────────────────────────


def test_safe_commit_clears_stale_lock_and_proceeds(gitrepo):
    """A lock dir whose mtime is older than the stale window should be reclaimed."""
    lock = gitrepo["lock"]
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.mkdir()
    (lock / "pid").write_text("99999\n")
    stale_time = time.time() - 360  # 6 min
    os.utime(lock / "pid", (stale_time, stale_time))
    os.utime(lock, (stale_time, stale_time))

    repo = gitrepo["repo"]
    (repo / "after_stale.txt").write_text("ok\n")
    result = _safe_commit(gitrepo, "after stale lock", ["after_stale.txt"])
    assert "锁陈旧" in result.stderr, f"expected stale-clear stderr: {result.stderr!r}"

    log = _git(gitrepo, "log", "--oneline").strip().splitlines()
    assert len(log) == 2
    assert "after stale lock" in log[0]


# ─── 4: pre-existing staged file is tolerated, --only contains the commit ───


def test_safe_commit_tolerates_index_before_but_only_commits_expected(gitrepo):
    """Bystander staged files in the index don't ride along — ``--only`` excludes them."""
    repo = gitrepo["repo"]
    (repo / "bystander.txt").write_text("by\n")
    (repo / "wanted.txt").write_text("want\n")
    subprocess.run(["git", "add", "bystander.txt"], cwd=repo, env=gitrepo["env"], check=True)

    result = _safe_commit(gitrepo, "with bystander left", ["wanted.txt"])
    assert result.returncode == 0, f"should pass: {result.stderr!r}"

    show = _git(gitrepo, "show", "--stat", "--name-only", "--pretty=format:", "HEAD")
    assert "wanted.txt" in show
    assert "bystander.txt" not in show

    staged = _git(gitrepo, "diff", "--cached", "--name-only").strip().splitlines()
    assert "bystander.txt" in staged


# ─── 5: BYPASS env var (regression / no-op on happy path) ───────────────────


def test_safe_commit_bypass_env_var_skips_self_check(gitrepo, tmp_path):
    """``HANDOFF_SAFE_COMMIT_BYPASS=1`` must not break a normal commit."""
    repo = gitrepo["repo"]
    (repo / "with_bypass.txt").write_text("bypass\n")
    result = _safe_commit(
        gitrepo,
        "with bypass",
        ["with_bypass.txt"],
        extra_env={"HANDOFF_SAFE_COMMIT_BYPASS": "1"},
    )
    assert result.returncode == 0

    show = _git(gitrepo, "show", "--stat", "--name-only", "--pretty=format:", "HEAD")
    assert "with_bypass.txt" in show


# ─── 6-8: segment-5 hook in isolation ───────────────────────────────────────


def test_pre_commit_seg5_rejects_when_actual_has_extras(gitrepo, tmp_path):
    """Directly invoke the hook with HANDOFF_EXPECTED_FILES listing only a subset."""
    repo = gitrepo["repo"]
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    subprocess.run(["git", "add", "a.txt", "b.txt"], cwd=repo, env=gitrepo["env"], check=True)

    expected_file = tmp_path / "expected.txt"
    expected_file.write_text("a.txt\n")

    env = dict(gitrepo["env"])
    env["HANDOFF_EXPECTED_FILES"] = str(expected_file)

    hook = repo / ".git" / "hooks" / "pre-commit"
    result = subprocess.run(
        ["bash", str(hook)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "segment 5 must reject (actual b.txt outside expected)"
    assert "PRECOMMIT_HIJACK_REJECT" in result.stderr or "hijack" in result.stderr.lower()
    assert "b.txt" in result.stderr


def test_pre_commit_seg5_passes_when_actual_subset_of_expected(gitrepo, tmp_path):
    """expected = {a, b}, staged = {a} → subset, pass."""
    repo = gitrepo["repo"]
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, env=gitrepo["env"], check=True)

    expected_file = tmp_path / "expected.txt"
    expected_file.write_text("a.txt\nb.txt\n")

    env = dict(gitrepo["env"])
    env["HANDOFF_EXPECTED_FILES"] = str(expected_file)

    hook = repo / ".git" / "hooks" / "pre-commit"
    result = subprocess.run(
        ["bash", str(hook)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"segment 5 should pass on subset: stderr={result.stderr!r}"


def test_pre_commit_seg5_skipped_when_env_var_unset(gitrepo):
    """HANDOFF_EXPECTED_FILES unset → segment 5 short-circuits, plain commits work."""
    repo = gitrepo["repo"]
    (repo / "anything.txt").write_text("x\n")
    subprocess.run(["git", "add", "anything.txt"], cwd=repo, env=gitrepo["env"], check=True)

    env = dict(gitrepo["env"])
    env.pop("HANDOFF_EXPECTED_FILES", None)

    hook = repo / ".git" / "hooks" / "pre-commit"
    result = subprocess.run(
        ["bash", str(hook)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
