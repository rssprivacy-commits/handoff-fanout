"""Tests for atomic_create / write_with_fsync / acquire_dir_lock.

The lock tests target the Phase 4 ``fcntl.flock`` implementation (v6
concurrency design §14): the kernel releases the lock when the holding
process dies or closes the fd, so there is no ``stale_seconds`` heuristic
and no owner-nonce fencing token. The constraints exercised here map 1:1
to the R-flock codex audit (§14.5):

  - P0 #1  reentrant same-path acquire must not self-deadlock (registry)
  - P0 #2  lock fd is O_CLOEXEC + non-inheritable (no fork/exec leak)
  - P1     alive-but-hung holder is never force-broken (no stale break)
  - P1     only EAGAIN/EWOULDBLOCK/EACCES are retryable; other errno raise
  - P1     migrating over an old mkdir ``*.lockdir`` fails closed
  - P1     pid diagnostic written only after the flock succeeds
  - P1     failed acquire leaks no fd / registry entry
"""

from __future__ import annotations

import errno
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from handoff_fanout import atomic
from handoff_fanout.atomic import (
    _LOCK_REGISTRY,
    LockAcquisitionError,
    LockMigrationError,
    acquire_dir_lock,
    atomic_create,
    atomic_replace,
    write_with_fsync,
)

# ─── atomic_create / write_with_fsync / atomic_replace (unchanged) ──────────


def test_atomic_create_first_call_returns_true(tmp_path: Path) -> None:
    p = tmp_path / "marker"
    assert atomic_create(p) is True
    assert p.exists()


def test_atomic_create_second_call_returns_false(tmp_path: Path) -> None:
    p = tmp_path / "marker"
    assert atomic_create(p) is True
    assert atomic_create(p) is False  # race protection
    assert p.exists()


def test_atomic_create_creates_parent_dirs(tmp_path: Path) -> None:
    p = tmp_path / "a" / "b" / "c" / "marker"
    assert atomic_create(p) is True
    assert p.exists()
    assert p.parent.is_dir()


def test_write_with_fsync_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "out.txt"
    write_with_fsync(p, "hello\n")
    assert p.read_text() == "hello\n"


def test_write_with_fsync_overwrites(tmp_path: Path) -> None:
    p = tmp_path / "out.txt"
    write_with_fsync(p, "first")
    write_with_fsync(p, "second")
    assert p.read_text() == "second"


def test_write_with_fsync_creates_parent_dirs(tmp_path: Path) -> None:
    p = tmp_path / "deep" / "nest" / "out.txt"
    write_with_fsync(p, "x")
    assert p.read_text() == "x"


def test_atomic_replace_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "ev.json"
    atomic_replace(p, '{"a": 1}\n')
    assert p.read_text() == '{"a": 1}\n'


def test_atomic_replace_overwrites_existing(tmp_path: Path) -> None:
    p = tmp_path / "ev.json"
    atomic_replace(p, "old")
    atomic_replace(p, "new")
    assert p.read_text() == "new"


def test_atomic_replace_creates_parent_dirs(tmp_path: Path) -> None:
    p = tmp_path / "deep" / "nest" / "ev.json"
    atomic_replace(p, "x")
    assert p.read_text() == "x"


def test_atomic_replace_leaves_no_tmp_residue(tmp_path: Path) -> None:
    p = tmp_path / "ev.json"
    atomic_replace(p, "data")
    residue = [q for q in tmp_path.iterdir() if q.name != "ev.json"]
    assert residue == [], f"unexpected tmp residue: {residue}"


# ─── acquire_dir_lock: flock semantics ──────────────────────────────────────

# A child that grabs an exclusive flock on argv[1], signals readiness by
# creating argv[2], then sleeps — so the parent can test contention /
# crash-release at the OS level (no handoff internals in the child).
_HOLDER = (
    "import fcntl, os, sys, time;"
    "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o644);"
    "fcntl.flock(fd, fcntl.LOCK_EX);"
    "open(sys.argv[2], 'w').close();"
    "time.sleep(60)"
)


def _spawn_holder(lock: Path, ready: Path, **popen_kw) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock), str(ready)], **popen_kw)
    deadline = time.time() + 10
    while not ready.exists():
        if time.time() > deadline:
            proc.kill()
            raise AssertionError("holder subprocess never acquired the flock")
        time.sleep(0.02)
    return proc


def test_acquire_dir_lock_round_trip(tmp_path: Path) -> None:
    lock = tmp_path / "my.lock"
    with acquire_dir_lock(lock) as acquired:
        assert acquired == lock
        # flock anchor is a regular FILE now, not a directory.
        assert lock.is_file()
    # File is intentionally left behind (reused; never unlinked), but the
    # lock is released so it can be re-acquired immediately.
    assert lock.exists()
    with acquire_dir_lock(lock, retries=1, wait_seconds=0.0):
        pass


def test_acquire_dir_lock_writes_pid_diagnostic(tmp_path: Path) -> None:
    lock = tmp_path / "diag.lock"
    with acquire_dir_lock(lock):
        body = lock.read_text()
        assert str(os.getpid()) in body, "lock file must record holder pid for diagnostics"


def test_acquire_dir_lock_released_on_exception(tmp_path: Path) -> None:
    lock = tmp_path / "boom.lock"
    with pytest.raises(RuntimeError, match="user error"), acquire_dir_lock(lock):
        raise RuntimeError("user error")
    # Released despite the exception → re-acquirable.
    with acquire_dir_lock(lock, retries=1, wait_seconds=0.0):
        pass


def test_acquire_dir_lock_contention_fails_after_retries(tmp_path: Path) -> None:
    lock = tmp_path / "busy.lock"
    ready = tmp_path / "busy.ready"
    proc = _spawn_holder(lock, ready)
    try:
        with (
            pytest.raises(LockAcquisitionError),
            acquire_dir_lock(lock, retries=2, wait_seconds=0.05),
        ):
            pass
    finally:
        proc.kill()
        proc.wait()


def test_alive_holder_is_never_force_broken(tmp_path: Path) -> None:
    """P1 honest trade-off: an alive (even hung) holder is NEVER broken.

    Unlike the old mkdir+stale path, flock has no staleness heuristic, so a
    long-lived holder keeps the lock until it dies. We must fail closed, not
    steal it.
    """
    lock = tmp_path / "alive.lock"
    ready = tmp_path / "alive.ready"
    proc = _spawn_holder(lock, ready)
    try:
        # Even with several retries, an alive holder is not broken.
        with (
            pytest.raises(LockAcquisitionError),
            acquire_dir_lock(lock, retries=3, wait_seconds=0.05),
        ):
            pass
        assert proc.poll() is None, "holder must still be alive (not killed/broken)"
    finally:
        proc.kill()
        proc.wait()


def test_crash_holder_auto_released_by_kernel(tmp_path: Path) -> None:
    """The flock root-fix: a SIGKILLed holder releases instantly via the
    kernel — no stale_seconds wait, no manual reclaim."""
    lock = tmp_path / "crash.lock"
    ready = tmp_path / "crash.ready"
    proc = _spawn_holder(lock, ready)
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    # Immediately acquirable — kernel released the dead holder's lock.
    with acquire_dir_lock(lock, retries=1, wait_seconds=0.0):
        pass


def test_reentrant_same_path_no_self_deadlock(tmp_path: Path) -> None:
    """P0 #1: nested acquisition of the SAME path in the SAME process must
    not EWOULDBLOCK against itself (flock is per-open-fd, not per-process)."""
    lock = tmp_path / "reentrant.lock"
    with acquire_dir_lock(lock):
        with acquire_dir_lock(lock, retries=1, wait_seconds=0.0):
            # Both critical sections active — no self-deadlock.
            rp = os.path.realpath(str(lock))
            assert _LOCK_REGISTRY[rp].depth == 2
        # Inner exit decremented depth but did NOT release the lock.
        assert _LOCK_REGISTRY[rp].depth == 1
    # Outermost exit released and cleared the registry entry.
    assert os.path.realpath(str(lock)) not in _LOCK_REGISTRY
    with acquire_dir_lock(lock, retries=1, wait_seconds=0.0):
        pass


def test_lock_fd_is_cloexec_and_non_inheritable(tmp_path: Path) -> None:
    """P0 #2: the lock fd must be close-on-exec and non-inheritable so it
    never leaks into git/subprocess children spawned inside the critical
    section."""
    lock = tmp_path / "cloexec.lock"
    with acquire_dir_lock(lock):
        rp = os.path.realpath(str(lock))
        fd = _LOCK_REGISTRY[rp].fd
        flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        assert flags & fcntl.FD_CLOEXEC, "lock fd must have FD_CLOEXEC"
        assert os.get_inheritable(fd) is False, "lock fd must be non-inheritable"


def test_lock_not_retained_by_exec_child(tmp_path: Path) -> None:
    """P0 #2 behavioural: a fork+exec child spawned *while we hold the lock*
    must NOT inherit the lock fd. After we release, a still-running child must
    not keep the lock alive (which it would if the fd had leaked into it).

    The child MUST be spawned inside the critical section — only then does the
    lock fd exist at fork time, so the test actually exercises inheritance
    (codex R-flock follow-up: spawning before acquire proves nothing)."""
    lock = tmp_path / "leak.lock"
    child = None
    try:
        with acquire_dir_lock(lock):
            # Spawn WHILE the lock fd is open. close_fds=False is the dangerous
            # path; O_CLOEXEC + non-inheritable are what stop the fd from
            # surviving into the exec'd child regardless.
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                close_fds=False,
            )
            time.sleep(0.2)  # let the child finish fork+exec before we release
        # Lock released by us; child still alive. If it had inherited the lock
        # fd, the flock would persist and this re-acquire would fail.
        with acquire_dir_lock(lock, retries=1, wait_seconds=0.0):
            pass
    finally:
        if child is not None:
            child.kill()
            child.wait()


def test_migration_old_lockdir_fails_closed(tmp_path: Path) -> None:
    """P1: an old mkdir-era ``*.lockdir`` directory left on disk must make
    acquisition fail closed (manual cleanup), never auto-rmdir (TOCTOU)."""
    lock = tmp_path / "legacy.lock"
    lock.mkdir()
    (lock / "pid").write_text("123\n")  # old marker file inside the dir
    with pytest.raises(LockMigrationError), acquire_dir_lock(lock, retries=1, wait_seconds=0.0):
        pass
    # The old directory is left untouched for the operator to remove.
    assert lock.is_dir()
    assert (lock / "pid").exists()


def test_migration_error_is_lock_acquisition_error(tmp_path: Path) -> None:
    """Consumers catch LockAcquisitionError; the migration error must remain
    catchable by that handler (subclass) while staying distinguishable."""
    assert issubclass(LockMigrationError, LockAcquisitionError)


def test_hard_errno_propagates_not_retried(tmp_path: Path, monkeypatch) -> None:
    """P1: errno outside the retryable set (e.g. ENOLCK) must propagate
    immediately, not be swallowed as 'lock busy, retry'."""
    lock = tmp_path / "enolck.lock"

    def boom(fd, op):
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(atomic.fcntl, "flock", boom)
    with pytest.raises(OSError) as exc, acquire_dir_lock(lock, retries=3, wait_seconds=0.0):
        pass
    assert exc.value.errno == errno.ENOLCK


def test_would_block_errno_retries_then_raises(tmp_path: Path, monkeypatch) -> None:
    """P1: EWOULDBLOCK is retryable; exhausting retries raises
    LockAcquisitionError (not a bare OSError)."""
    lock = tmp_path / "ewouldblock.lock"
    calls = {"n": 0}

    def busy(fd, op):
        calls["n"] += 1
        raise OSError(errno.EWOULDBLOCK, "would block")

    monkeypatch.setattr(atomic.fcntl, "flock", busy)
    with (
        pytest.raises(LockAcquisitionError),
        acquire_dir_lock(lock, retries=3, wait_seconds=0.0),
    ):
        pass
    assert calls["n"] == 3, "should retry the full count before giving up"


def test_failed_acquire_leaves_no_registry_entry(tmp_path: Path, monkeypatch) -> None:
    """P1: a failed acquisition must not leak an fd or a registry entry."""
    lock = tmp_path / "noleak.lock"

    def busy(fd, op):
        raise OSError(errno.EWOULDBLOCK, "would block")

    monkeypatch.setattr(atomic.fcntl, "flock", busy)
    with (
        pytest.raises(LockAcquisitionError),
        acquire_dir_lock(lock, retries=1, wait_seconds=0.0),
    ):
        pass
    assert os.path.realpath(str(lock)) not in _LOCK_REGISTRY


def test_stale_seconds_kwarg_accepted_for_compat(tmp_path: Path) -> None:
    """API compat: the 5 consumers still pass stale_seconds=... — it must be
    accepted (and ignored) so the call sites need no edit."""
    lock = tmp_path / "compat.lock"
    with acquire_dir_lock(lock, stale_seconds=300, retries=1, wait_seconds=0.0):
        pass
