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
    atomic_replace,
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
    # No .tmp.* sibling left behind after the rename.
    residue = [q for q in tmp_path.iterdir() if q.name != "ev.json"]
    assert residue == [], f"unexpected tmp residue: {residue}"


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
    with (
        pytest.raises(LockAcquisitionError) as exc,
        acquire_dir_lock(lock, retries=2, wait_seconds=0.05),
    ):
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
    with pytest.raises(RuntimeError, match="user error"), acquire_dir_lock(lock):
        raise RuntimeError("user error")
    assert not lock.exists(), "lock must be released even on exception"


def test_acquire_dir_lock_writes_unique_owner_nonce(tmp_path: Path) -> None:
    lock = tmp_path / "a.lock"
    with acquire_dir_lock(lock):
        owner1 = (lock / "owner").read_text().strip()
        assert owner1, "lock dir must contain a non-empty owner nonce"
    with acquire_dir_lock(lock):
        owner2 = (lock / "owner").read_text().strip()
    assert owner1 != owner2, "each acquisition must get a distinct owner nonce"


def test_release_does_not_delete_foreign_reacquired_lock(tmp_path: Path) -> None:
    """I6 split-brain fix: if our lock was stale-cleared and another holder
    reacquired it, our context-exit release must NOT delete the new holder's
    lock. Owner nonce mismatch ⟹ leave it alone."""
    lock = tmp_path / "x.lock"
    with acquire_dir_lock(lock):
        # Simulate a sibling reclaiming the lock under a new owner nonce.
        (lock / "owner").write_text("a-different-holder-nonce\n")
    assert lock.exists(), "must not delete a lock now owned by another holder"
    assert (lock / "owner").read_text().strip() == "a-different-holder-nonce"
    # Correct owner-checking leaves the foreign lock FULLY intact (incl. pid);
    # the old path-only _force_clear unlinks pid first, which would fail here.
    assert (lock / "pid").exists(), "foreign holder's lock contents must be untouched"
