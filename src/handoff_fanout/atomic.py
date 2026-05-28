"""Atomic filesystem primitives shared by dump / watchdog / heartbeat / safe-commit.

The handoff protocol relies on three POSIX guarantees on local filesystems:

  - ``open(O_CREAT | O_EXCL)`` is atomic (exclusive create).
  - ``mkdir()`` is atomic (only one process creates the directory).
  - ``fsync(fd)`` + ``fsync(dir_fd)`` together survive sudden power loss.

These guarantees do NOT hold on NFS / SMB / FUSE in general — handoff state
files must live on a local disk (the default ``~/.handoff/`` is fine).
"""
from __future__ import annotations

import contextlib
import errno
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path


def atomic_create(path: Path) -> bool:
    """Create an empty file atomically.

    Returns ``True`` if this process created the file, ``False`` if another
    process beat us to it (race-safe). Raises on any other ``OSError``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)
    return True


def write_with_fsync(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` and fsync both the file and its parent.

    Overwrites any existing file. The fsync-parent step is what makes the
    new file's directory entry durable after a power cut.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    dir_fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    except OSError as e:
        # Some filesystems (notably tmpfs in older kernels) reject fsync on
        # a directory fd. The write itself is still durable; ignore.
        if e.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise
    finally:
        os.close(dir_fd)


class LockAcquisitionError(RuntimeError):
    """Raised when ``acquire_dir_lock`` exhausts its retries."""


@contextlib.contextmanager
def acquire_dir_lock(
    lock_path: Path,
    *,
    stale_seconds: float = 300.0,
    retries: int = 5,
    wait_seconds: float = 10.0,
) -> Iterator[Path]:
    """Acquire a cross-process directory lock backed by ``mkdir()`` atomicity.

    A stale lock (mtime older than ``stale_seconds``) is force-cleared once
    before retrying — this recovers from crashed lock holders without needing
    an external sweeper.

    The lock directory contains a ``pid`` file naming the holder, which is
    useful when diagnosing deadlocks.

    Raises ``LockAcquisitionError`` after ``retries`` failed attempts.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        age = time.time() - lock_path.stat().st_mtime
        if age > stale_seconds:
            print(
                f"handoff-safe-commit: 锁陈旧 stale lock at {lock_path} "
                f"(age={age:.0f}s > {stale_seconds:.0f}s) — force clearing",
                file=sys.stderr,
            )
            _force_clear_lock(lock_path)

    for attempt in range(retries):
        try:
            lock_path.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            if attempt < retries - 1:
                time.sleep(wait_seconds)
            continue
        # Success.
        pid_file = lock_path / "pid"
        try:
            pid_file.write_text(f"{os.getpid()}\n")
        except OSError:
            # If we can't write the pid file something is wrong, but we still
            # hold the lock — proceed and let the caller surface the error.
            pass
        try:
            yield lock_path
        finally:
            _force_clear_lock(lock_path)
        return

    raise LockAcquisitionError(
        f"could not acquire lock at {lock_path} after {retries} attempts "
        f"(holder pid: {_read_pid(lock_path)})"
    )


def _force_clear_lock(lock_path: Path) -> None:
    pid_file = lock_path / "pid"
    if pid_file.exists():
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
    try:
        lock_path.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        # Lock dir non-empty (unexpected leftover files) — best effort.
        pass


def _read_pid(lock_path: Path) -> str:
    try:
        return (lock_path / "pid").read_text().strip()
    except OSError:
        return "?"
