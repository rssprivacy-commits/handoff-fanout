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
import fcntl
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
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
    """Raised when ``acquire_dir_lock`` exhausts its retries against a live holder."""


class LockMigrationError(LockAcquisitionError):
    """Raised when an old mkdir-era ``*.lockdir`` directory blocks the new flock file.

    Fail closed: the operator must remove the legacy directory manually. Auto
    ``rmdir`` would reintroduce the very acquire/clear TOCTOU that the flock
    migration removes, so it is intentionally not done. Subclasses
    ``LockAcquisitionError`` so existing ``except LockAcquisitionError``
    handlers still catch it.
    """


@dataclass
class _LockEntry:
    fd: int
    depth: int


# Process-wide registry: ``realpath(lock file) -> _LockEntry``. ``flock`` is
# keyed by the open file description, so a second ``os.open()`` + ``LOCK_EX`` on
# the SAME path within this process would block against itself (EWOULDBLOCK).
# The registry makes re-entrant acquisition reuse the already-held fd via depth
# counting instead of self-deadlocking. (R-flock P0 #1)
_LOCK_REGISTRY: dict[str, _LockEntry] = {}
_REGISTRY_LOCK = threading.Lock()

# errno values meaning "another holder has the lock; retrying may help".
# Everything else (ENOENT/EISDIR/ENOLCK/ESTALE/...) is a hard error that must
# propagate immediately rather than be mistaken for contention. (R-flock P1)
_RETRYABLE_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})


@contextlib.contextmanager
def acquire_dir_lock(
    lock_path: Path,
    *,
    stale_seconds: float = 300.0,
    retries: int = 5,
    wait_seconds: float = 10.0,
) -> Iterator[Path]:
    """Acquire a cross-process exclusive lock backed by ``fcntl.flock``.

    The kernel releases the lock automatically when the holding process dies
    or its fd closes, so there is **no staleness heuristic** (``stale_seconds``
    is accepted only for call-site compatibility and ignored) and **no
    owner-nonce fencing** — the kernel is the fencing authority. This roots out
    the acquire/stale-clear TOCTOU that the previous ``mkdir`` lock had.

    Re-entrant acquisition of the same path within one process reuses the held
    fd (depth-counted) rather than self-blocking. The depth counter assumes
    strict LIFO nesting within a single thread — which every consumer satisfies
    (they are single-threaded CLI ``with``-blocks). Concurrent acquisition of
    the *same* path from multiple threads is not supported: a second thread
    would either be mis-counted as re-entrant or self-block on ``flock``; the
    consumers never do this. The lock fd is ``O_CLOEXEC`` + non-inheritable so
    it never leaks into subprocesses (e.g. the ``git`` calls dump makes while
    holding the lock).

    Trade-off (R-flock P1): an alive-but-hung holder is **never** force-broken
    (breaking it would reintroduce split-brain). flock root-fixes *crashed*
    holders only; alive-hang recovery belongs to the watchdog / operation
    timeouts, not here.

    Raises ``LockMigrationError`` if a legacy mkdir lock directory occupies the
    path, or ``LockAcquisitionError`` after ``retries`` failed attempts against
    a live holder.
    """
    del stale_seconds  # flock needs no staleness heuristic; kept for API compat
    rp = os.path.realpath(str(lock_path))

    # Re-entrant fast path: this process already holds the lock → reuse the fd.
    with _REGISTRY_LOCK:
        entry = _LOCK_REGISTRY.get(rp)
        reentrant = entry is not None
        if reentrant:
            entry.depth += 1
    if reentrant:
        try:
            yield lock_path
        finally:
            with _REGISTRY_LOCK:
                e = _LOCK_REGISTRY.get(rp)
                if e is not None:
                    e.depth -= 1
        return

    fd = _flock_acquire(lock_path, retries=retries, wait_seconds=wait_seconds)
    with _REGISTRY_LOCK:
        _LOCK_REGISTRY[rp] = _LockEntry(fd=fd, depth=1)
    try:
        yield lock_path
    finally:
        with _REGISTRY_LOCK:
            e = _LOCK_REGISTRY.get(rp)
            release = e is not None and e.depth <= 1
            if e is not None:
                if release:
                    del _LOCK_REGISTRY[rp]
                else:
                    e.depth -= 1
        if release:
            _release_flock(fd)


def _flock_acquire(lock_path: Path, *, retries: int, wait_seconds: float) -> int:
    """Open the anchor file and take an exclusive ``flock``; return the held fd.

    On any failure path the fd is closed (no leak). Caller owns the returned fd
    and must release it via :func:`_release_flock`.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Migration fail-closed: a legacy mkdir lock dir at this path would make
    # ``os.open`` raise EISDIR and silently break mutual exclusion. Refuse
    # rather than auto-rmdir (which brings back the TOCTOU we removed).
    if lock_path.is_dir():
        raise LockMigrationError(
            f"refusing to lock {lock_path}: a legacy mkdir lock directory occupies "
            f"this path. Remove it manually once no holder is active, then retry "
            f"(auto-removal is intentionally not done to avoid reintroducing TOCTOU)."
        )

    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(lock_path), flags, 0o644)
    os.set_inheritable(fd, False)  # belt-and-suspenders over O_CLOEXEC
    try:
        for attempt in range(retries):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                if e.errno in _RETRYABLE_ERRNOS:
                    if attempt < retries - 1:
                        time.sleep(wait_seconds)
                        continue
                    raise LockAcquisitionError(
                        f"could not acquire lock at {lock_path} after {retries} "
                        f"attempts (holder: {_read_holder(lock_path)})"
                    ) from e
                raise  # hard errno (ENOLCK/EISDIR/ESTALE/...) — propagate as-is
            else:
                # Locked. Stamp diagnostics AFTER the flock succeeds; the
                # content is for humans only — the kernel guarantees exclusion.
                _stamp_holder(fd)
                return fd
        # retries < 1 — guard against an fd leak.
        raise LockAcquisitionError(f"could not acquire lock at {lock_path}")
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


def _release_flock(fd: int) -> None:
    """Release the lock: ``LOCK_UN`` then ``close`` (close alone also releases).

    The anchor file is intentionally **not** unlinked — it is reused, and
    unlink/create would reintroduce a creation race. Its content (the last
    holder's diagnostics) is left in place. Errors are suppressed so a release
    failure never shadows the critical section's original exception. (P2)
    """
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


def _stamp_holder(fd: int) -> None:
    """Best-effort diagnostics written into the locked anchor file.

    For human deadlock diagnosis only — never read back for correctness.
    Written only after the flock is held.
    """
    try:
        info = (
            f"pid={os.getpid()} host={socket.gethostname()} "
            f"start={time.time():.0f} cmd={' '.join(sys.argv)}\n"
        )
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, info.encode("utf-8", "replace"))
        os.fsync(fd)
    except OSError:
        pass


def _read_holder(lock_path: Path) -> str:
    try:
        return lock_path.read_text(errors="replace").strip() or "?"
    except OSError:
        return "?"
