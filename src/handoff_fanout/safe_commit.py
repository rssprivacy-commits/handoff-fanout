"""Hijack-safe ``git commit`` wrapper.

Defends against cross-tab git-index hijack (where Tab A's ``git add`` gets
swept into Tab B's ``git commit`` because ``.git/index`` is repo-shared
state). Four cooperating layers:

  1. **Cross-process lock** (this module) — ``mkdir``-based directory lock
     prevents two ``handoff-safe-commit`` invocations from racing on the
     same repo.
  2. **Expected-files invariant** (this module) — after ``git add`` the
     staged set is compared against the user-supplied file list; unexpected
     staged files abort the commit *before* it runs.
  3. **Pre-commit hook check** (``install/git-hooks/pre-commit``) — the
     hook reads ``HANDOFF_EXPECTED_FILES`` (exported by this wrapper) and
     re-verifies the staged set inside the commit pipeline. This is the
     only layer that catches *hook-auto-stage* hijack (a pre-commit hook
     calling ``git add foo.txt``), because ``git commit --only`` does NOT
     prevent hooks from mutating the index.
  4. **Post-audit** (this module) — after ``git commit``, ``git show
     --stat HEAD`` is compared against the expected list. Surface any
     extra files with exit code 2 so the user can ``git reset --soft
     HEAD~1`` and recommit cleanly.

Emergency bypass: set ``HANDOFF_SAFE_COMMIT_BYPASS=1`` to skip the
expected-files check at layer 2 (lock + post-audit still apply).

Emergency bypass: set ``HANDOFF_SAFE_COMMIT_BYPASS=1`` to skip the
expected-files check (lock is still acquired, audit is still recorded).

CLI::

    handoff-safe-commit -m "MSG" -- file1 file2 ...
    handoff-safe-commit -F /path/to/msg.txt --allow-empty -- file1
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from handoff_fanout.atomic import LockAcquisitionError, acquire_dir_lock


def _default_lock_path() -> Path:
    override = os.environ.get("HANDOFF_SAFE_COMMIT_LOCK")
    if override:
        return Path(override)
    home = os.environ.get("HANDOFF_HOME", str(Path.home() / ".handoff"))
    return Path(home) / "git-commit.lockdir"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="handoff-safe-commit",
        description="Hijack-safe git commit wrapper (cross-process lock + expected-files invariant).",
    )
    msg_group = p.add_mutually_exclusive_group(required=True)
    msg_group.add_argument("-m", "--message", help="Commit message (passed to git commit -m).")
    msg_group.add_argument(
        "-F", "--file", help="Read commit message from file (passed to git commit -F)."
    )
    p.add_argument(
        "extra_args",
        nargs=argparse.REMAINDER,
        help="Files (after `--`) and optional git commit flags. Format: [extra-git-flags] -- file1 file2 ...",
    )
    return p


def _split_files(extra: list[str]) -> tuple[list[str], list[str]]:
    """Split ``extra`` at the first standalone ``--`` into (git_args, files)."""
    if "--" not in extra:
        return extra, []
    idx = extra.index("--")
    return extra[:idx], extra[idx + 1 :]


def _run_git(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    # core.quotepath=false: emit non-ASCII (e.g. CJK) paths verbatim UTF-8 rather
    # than git's default octal-escaped form. Without this the staged/landed name
    # sets come back escaped while ``expected`` (from the user-supplied file list)
    # is plain UTF-8, so the subset checks raise a spurious hijack false-positive
    # on any Chinese-named file.
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )


def _staged_files() -> set[str]:
    r = _run_git(["diff", "--cached", "--name-only"])
    return {line for line in r.stdout.splitlines() if line.strip()}


def _validate_file_exists(f: str) -> bool:
    """A file is acceptable if it exists on disk OR git is tracking some state for it.

    The second branch covers staged deletions (``git add`` on a deleted file
    removes it from the index, so plain ``git ls-files`` won't find it) as
    well as renames and other half-applied changes.
    """
    if Path(f).exists():
        return True
    status = _run_git(["status", "--porcelain", "--", f])
    return status.returncode == 0 and bool(status.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    git_extra, files = _split_files(args.extra_args or [])

    if not files:
        print(
            "handoff-safe-commit: must specify files after `--`\n"
            "  e.g. handoff-safe-commit -m 'msg' -- src/foo.py tests/test_foo.py",
            file=sys.stderr,
        )
        return 1

    for f in files:
        if not _validate_file_exists(f):
            print(f"handoff-safe-commit: file not found and not tracked: {f}", file=sys.stderr)
            return 1

    lock_path = _default_lock_path()
    try:
        retries = int(os.environ.get("HANDOFF_SAFE_COMMIT_RETRY_LIMIT", "5"))
        wait = float(os.environ.get("HANDOFF_SAFE_COMMIT_RETRY_WAIT", "10"))
        with acquire_dir_lock(lock_path, retries=retries, wait_seconds=wait):
            return _commit_under_lock(args, git_extra, files)
    except LockAcquisitionError as e:
        print(f"handoff-safe-commit: {e}", file=sys.stderr)
        return 1


def _commit_under_lock(args: argparse.Namespace, git_extra: list[str], files: list[str]) -> int:
    expected = set(files)
    index_before = _staged_files()

    for f in files:
        if f in index_before:
            # Already staged (e.g. user pre-staged a deletion). Re-running
            # ``git add`` would fail with "pathspec did not match any files"
            # because the path no longer exists in either the working tree
            # or the working-tree-vs-index diff. Honor the user's staging.
            continue
        # ``-A`` is per-pathspec ``--all``: stages add / modify / delete
        # uniformly for ``f`` only, without touching the rest of the index.
        r = _run_git(["add", "-A", "--", f])
        if r.returncode != 0:
            print(f"handoff-safe-commit: `git add -A -- {f}` failed: {r.stderr}", file=sys.stderr)
            return r.returncode

    index_after = _staged_files()
    unexpected = index_after - expected - index_before
    bypass = os.environ.get("HANDOFF_SAFE_COMMIT_BYPASS") == "1"
    if unexpected and not bypass:
        print(
            "handoff-safe-commit: index contains unexpected files after staging.\n"
            f"  expected:    {sorted(expected)}\n"
            f"  index_after: {sorted(index_after)}\n"
            f"  unexpected:  {sorted(unexpected)}\n"
            "  Likely cause: another tab `git add` outside the lock, or a pre-add hook auto-staged.\n"
            "  Emergency bypass: HANDOFF_SAFE_COMMIT_BYPASS=1 handoff-safe-commit ...",
            file=sys.stderr,
        )
        return 1

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="handoff-expected-", delete=False, encoding="utf-8"
    ) as tf:
        tf.write("\n".join(sorted(expected)) + "\n")
        expected_file = tf.name

    try:
        commit_args = ["commit"]
        if args.message:
            commit_args += ["-m", args.message]
        else:
            commit_args += ["-F", args.file]
        commit_args += git_extra
        commit_args += ["--only", "--"]
        commit_args += files

        env = {**os.environ, "HANDOFF_EXPECTED_FILES": expected_file}
        r = subprocess.run(["git", *commit_args], env=env, check=False)
        if r.returncode != 0:
            print(
                f"handoff-safe-commit: git commit failed (exit {r.returncode}). "
                "Run `git status` to inspect.",
                file=sys.stderr,
            )
            return r.returncode
    finally:
        with contextlib.suppress(OSError):
            os.unlink(expected_file)

    return _post_audit(expected)


def _post_audit(expected: set[str]) -> int:
    """After commit, verify landed file set is a subset of expected."""
    landed_proc = _run_git(["show", "--stat", "--name-only", "--pretty=format:", "HEAD"])
    landed = {ln for ln in landed_proc.stdout.splitlines() if ln.strip()}
    extra = landed - expected
    if extra:
        print(
            "handoff-safe-commit: post-audit found unexpected files in HEAD commit!\n"
            f"  landed:   {sorted(landed)}\n"
            f"  expected: {sorted(expected)}\n"
            f"  extra:    {sorted(extra)}\n"
            "  Commit is already in place. Inspect and decide whether to `git reset --soft HEAD~1` "
            "and re-run with corrected file list.",
            file=sys.stderr,
        )
        return 2

    head_proc = _run_git(["rev-parse", "--short", "HEAD"])
    head = head_proc.stdout.strip()
    print(
        f"handoff-safe-commit: {head} — {len(landed)} file(s) landed (within expected set).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
