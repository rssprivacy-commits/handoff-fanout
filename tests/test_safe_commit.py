"""Integration tests for the hijack-safe commit wrapper.

These tests run real ``git`` commands inside a per-test tmp repo. They
cover the three defense layers: expected-files invariant, ``--only``
restriction, and the cross-process lock.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from handoff_fanout import safe_commit


def _write(p: Path, content: str = "x") -> None:
    p.write_text(content)


def _last_commit_files() -> set[str]:
    r = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {ln for ln in r.stdout.splitlines() if ln.strip()}


@pytest.fixture
def lock_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the safe-commit lock at a tmp file isolated from the real user."""
    lock = tmp_path / "safe-commit.lockdir"
    monkeypatch.setenv("HANDOFF_SAFE_COMMIT_LOCK", str(lock))
    monkeypatch.setenv("HANDOFF_SAFE_COMMIT_RETRY_LIMIT", "2")
    monkeypatch.setenv("HANDOFF_SAFE_COMMIT_RETRY_WAIT", "0.05")
    monkeypatch.delenv("HANDOFF_SAFE_COMMIT_BYPASS", raising=False)
    return lock


def test_happy_path_commits_only_specified_files(git_repo: Path, lock_in_tmp: Path) -> None:
    _write(git_repo / "a.txt", "alpha")
    _write(git_repo / "b.txt", "beta")
    _write(git_repo / "c.txt", "gamma")  # NOT in our expected list

    rc = safe_commit.main(["-m", "add a,b", "--", "a.txt", "b.txt"])
    assert rc == 0
    landed = _last_commit_files()
    assert landed == {"a.txt", "b.txt"}, f"unexpected landed set: {landed}"


def test_cjk_filename_commits_cleanly(git_repo: Path, lock_in_tmp: Path) -> None:
    """CJK (non-ASCII) filenames must not trip the expected-files invariant.

    git's default ``core.quotepath=true`` octal-escapes non-ASCII paths in
    ``diff --cached --name-only`` (e.g. ``部署任务栈.md`` →
    ``\\351\\203\\250...``). Without normalization the staged set comes back
    escaped while ``expected`` is plain UTF-8, so the subset check raises a
    spurious hijack false-positive on every Chinese-named file.
    """
    fname = "部署任务栈.md"
    _write(git_repo / fname, "x")
    _write(git_repo / "b.txt", "beta")  # NOT in expected list

    rc = safe_commit.main(["-m", "add cjk", "--", fname])
    assert rc == 0, "CJK filename must commit cleanly (no quotepath false-positive)"

    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", "show", "--name-only",
         "--pretty=format:", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    landed = {ln for ln in r.stdout.splitlines() if ln.strip()}
    assert landed == {fname}, f"unexpected landed set: {landed}"


def test_pre_commit_hook_hijack_is_caught_by_post_audit(git_repo: Path, lock_in_tmp: Path) -> None:
    """A pre-commit hook that auto-stages an extra file slips past the
    layer-2 snapshot AND past ``git commit --only`` (git docs: hooks
    modify the temporary index that ``--only`` builds). The layer-4
    post-audit must catch it and surface exit code 2 so the user can
    ``git reset --soft HEAD~1`` and recommit cleanly. The bundled
    pre-commit hook (layer 3, installed by ``install/git-hooks``) is
    the layer that prevents the commit from landing in the first
    place — that path is exercised by the hooks' own test suite.
    """
    _write(git_repo / "a.txt")
    _write(git_repo / "intruder.txt")
    hook = git_repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ngit add intruder.txt\n")
    hook.chmod(0o755)

    rc = safe_commit.main(["-m", "add a only", "--", "a.txt"])
    assert rc == 2, "post-audit must return exit 2 when hijack lands"
    landed = _last_commit_files()
    assert landed == {"a.txt", "intruder.txt"}, (
        f"documents the limitation: --only does NOT block hook auto-stage; got {landed}"
    )


def test_concurrent_add_between_snapshots_is_caught(
    git_repo: Path, lock_in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layer-2 snapshot invariant: if a sibling process stages a file
    *between* the wrapper's INDEX_BEFORE snapshot and the post-add
    snapshot, the wrapper aborts before commit.
    """
    _write(git_repo / "a.txt")
    _write(git_repo / "intruder.txt")

    # Patch _staged_files to inject the intruder ONLY on the second call
    # (post-add snapshot), simulating a concurrent `git add` from another tab.
    call_count = {"n": 0}
    real = safe_commit._staged_files

    def fake_staged() -> set[str]:
        call_count["n"] += 1
        s = real()
        if call_count["n"] >= 2:
            s.add("intruder.txt")
        return s

    monkeypatch.setattr(safe_commit, "_staged_files", fake_staged)

    rc = safe_commit.main(["-m", "add a only", "--", "a.txt"])
    assert rc == 1, "snapshot diff must abort before commit"


def test_pre_existing_staged_file_is_preserved(git_repo: Path, lock_in_tmp: Path) -> None:
    _write(git_repo / "a.txt")
    _write(git_repo / "preexist.txt")
    subprocess.run(["git", "add", "preexist.txt"], cwd=git_repo, check=True)

    # Even though preexist.txt is staged, our --only commit should ignore it.
    rc = safe_commit.main(["-m", "add a only", "--", "a.txt"])
    assert rc == 0
    landed = _last_commit_files()
    assert landed == {"a.txt"}
    # preexist.txt remains staged for a future commit.
    staged_after = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "preexist.txt" in staged_after


def test_missing_untracked_file_is_rejected(git_repo: Path, lock_in_tmp: Path) -> None:
    rc = safe_commit.main(["-m", "msg", "--", "does-not-exist.txt"])
    assert rc != 0


def test_deleted_but_tracked_file_is_accepted(git_repo: Path, lock_in_tmp: Path) -> None:
    _write(git_repo / "doomed.txt")
    subprocess.run(["git", "add", "doomed.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed", "--quiet"], cwd=git_repo, check=True)
    (git_repo / "doomed.txt").unlink()
    subprocess.run(["git", "add", "doomed.txt"], cwd=git_repo, check=True)  # stage the deletion

    rc = safe_commit.main(["-m", "remove", "--", "doomed.txt"])
    assert rc == 0
    # File is gone from HEAD.
    lstree = subprocess.run(
        ["git", "ls-tree", "HEAD"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "doomed.txt" not in lstree


def test_lock_contention_fails_when_held(git_repo: Path, lock_in_tmp: Path) -> None:
    # Pre-create the lock dir to simulate another process holding it.
    lock_in_tmp.mkdir()
    (lock_in_tmp / "pid").write_text("99999\n")

    _write(git_repo / "a.txt")
    rc = safe_commit.main(["-m", "msg", "--", "a.txt"])
    assert rc != 0


def test_must_specify_files_after_double_dash(git_repo: Path, lock_in_tmp: Path) -> None:
    rc = safe_commit.main(["-m", "no files"])
    assert rc == 1


def test_message_and_message_file_are_mutually_exclusive(git_repo: Path, lock_in_tmp: Path) -> None:
    with pytest.raises(SystemExit):
        safe_commit.main(["-m", "x", "-F", "y", "--", "a.txt"])
