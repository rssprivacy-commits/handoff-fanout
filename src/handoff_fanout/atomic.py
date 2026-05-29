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


def atomic_replace(path: Path, content: str) -> None:
    """Atomically replace ``path``'s contents with ``content``.

    Unlike :func:`write_with_fsync` (which uses ``O_TRUNC`` in place and
    therefore exposes a window where a concurrent reader sees a truncated /
    partial file), this writes to a same-directory temp file, fsyncs it, then
    ``os.replace``s it over the target — a reader always observes either the
    full old content or the full new content, never a partial state. Required
    wherever a hash-verified artifact (e.g. retro evidence) is overwritten
    while other tabs may be reading it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{time.monotonic_ns()}"
    data = content.encode("utf-8")
    try:
        # O_EXCL: the temp name embeds pid+monotonic_ns so it is unique; EXCL
        # turns any collision into a hard error rather than a silent overwrite.
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            mv = memoryview(data)
            while mv:
                written = os.write(fd, mv)  # handle short writes
                mv = mv[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
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
            # Capture the stale holder's owner, then re-verify staleness +
            # ownership immediately before clearing. This stops a second
            # reclaimer (which observed the SAME stale lock) from deleting a
            # fresh lock that a first reclaimer just acquired: the owner nonce
            # will have changed, so we skip the clear. (I6 split-brain narrowing)
            owner_before = _read_owner(lock_path)
            with contextlib.suppress(FileNotFoundError, OSError):
                still_stale = (time.time() - lock_path.stat().st_mtime) > stale_seconds
                if still_stale and _read_owner(lock_path) == owner_before:
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
        # Success. Stamp a fencing token (owner nonce) unique to THIS
        # acquisition so the release path can prove ownership before deleting
        # the dir — without it, a holder whose stale lock was reclaimed by a
        # sibling would delete the sibling's fresh lock on context exit (I6
        # split-brain).
        my_nonce = _new_owner_nonce()
        try:
            (lock_path / "owner").write_text(my_nonce + "\n")
        except OSError as e:
            # Without a readable owner token the release path can't prove
            # ownership, so it would fail-closed and leak the lock. Roll the
            # acquisition back instead of holding an unidentifiable lock.
            _force_clear_lock(lock_path)
            raise LockAcquisitionError(
                f"acquired {lock_path} but could not stamp owner token: {e}"
            ) from e
        with contextlib.suppress(OSError):
            (lock_path / "pid").write_text(f"{os.getpid()}\n")
        try:
            yield lock_path
        finally:
            _release_owned_lock(lock_path, my_nonce)
        return

    raise LockAcquisitionError(
        f"could not acquire lock at {lock_path} after {retries} attempts "
        f"(holder pid: {_read_pid(lock_path)})"
    )


def _new_owner_nonce() -> str:
    """Per-acquisition fencing token: pid + monotonic ns + random suffix."""
    return f"{os.getpid()}-{time.monotonic_ns()}-{os.urandom(6).hex()}"


def _read_owner(lock_path: Path) -> str | None:
    try:
        return (lock_path / "owner").read_text().strip()
    except OSError:
        return None


def _release_owned_lock(lock_path: Path, my_nonce: str) -> None:
    """Release a lock we acquired — only if we still own it.

    If the on-disk ``owner`` nonce no longer matches ours, our lock was
    stale-cleared and reclaimed by another holder; deleting it now would
    destroy *their* lock, so we leave it untouched (I6 fix). A missing owner
    file is treated as ours (legacy / best-effort) so old callers still clean
    up.
    """
    owner = _read_owner(lock_path)
    if owner != my_nonce:
        # Mismatch OR missing owner → we can't prove this lock is still ours
        # (stale-cleared + reclaimed by a sibling, or owner file lost). Deleting
        # it could destroy another holder's lock, so leave it. A genuinely
        # orphaned lock is later reclaimed via the stale-age path. (codex P0-1)
        return
    _force_clear_lock(lock_path)


def _force_clear_lock(lock_path: Path) -> None:
    """Unconditionally remove a lock dir + its marker files.

    Used for releasing our own lock and for reclaiming a stale foreign lock.
    Removes ``owner`` / ``pid`` first so the final ``rmdir`` succeeds.
    """
    for marker in ("owner", "pid"):
        with contextlib.suppress(FileNotFoundError, OSError):
            (lock_path / marker).unlink()
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
