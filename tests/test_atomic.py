"""Tests for atomic_create / write_with_fsync / acquire_dir_lock."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from handoff_fanout.atomic import (
    LockAcquisitionError,
    acquire_dir_lock,
    atomic_create,
    write_with_fsync,
)


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


def test_acquire_dir_lock_round_trip(tmp_path: Path) -> None:
    lock = tmp_path / "my.lock"
    with acquire_dir_lock(lock) as acquired:
        assert acquired == lock
        assert lock.is_dir()
        assert (lock / "pid").exists()
        assert (lock / "pid").read_text().strip() == str(os.getpid())
    assert not lock.exists(), "lock should be released on context exit"


def test_acquire_dir_lock_contention_fails_after_retries(tmp_path: Path) -> None:
    lock = tmp_path / "busy.lock"
    lock.mkdir()  # simulate another process holding it
    (lock / "pid").write_text("99999\n")
    with pytest.raises(LockAcquisitionError) as exc:
        with acquire_dir_lock(lock, retries=2, wait_seconds=0.05):
            pass
    assert "99999" in str(exc.value)


def test_acquire_dir_lock_stale_lock_is_force_cleared(tmp_path: Path) -> None:
    lock = tmp_path / "stale.lock"
    lock.mkdir()
    (lock / "pid").write_text("12345\n")
    # Backdate mtime so the lock looks stale.
    old = time.time() - 3600
    os.utime(lock, (old, old))
    # With a short stale_seconds the lock should be reclaimed immediately.
    with acquire_dir_lock(lock, stale_seconds=10.0, retries=1, wait_seconds=0.01):
        assert (lock / "pid").read_text().strip() == str(os.getpid())
    assert not lock.exists()


def test_acquire_dir_lock_released_on_exception(tmp_path: Path) -> None:
    lock = tmp_path / "boom.lock"
    with pytest.raises(RuntimeError, match="user error"):
        with acquire_dir_lock(lock):
            raise RuntimeError("user error")
    assert not lock.exists(), "lock must be released even on exception"
