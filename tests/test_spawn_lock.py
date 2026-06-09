import os
import time
from pathlib import Path

import pytest

from handoff_fanout.spawn_lock import LockHeld, project_spawn_lock


def test_lock_excludes_second_holder(tmp_path):
    # While the first holder is active, the SECOND acquire's __enter__ raises
    # LockHeld — caught by pytest.raises; the body never runs.
    with (
        project_spawn_lock("erp", root=tmp_path, ttl=60),
        pytest.raises(LockHeld),
        project_spawn_lock("erp", root=tmp_path, ttl=60),
    ):
        pass


def test_lock_released_on_exit_even_on_error(tmp_path):
    with pytest.raises(ValueError), project_spawn_lock("erp", root=tmp_path, ttl=60):
        raise ValueError("boom")
    # 锁应在异常后释放(finally)→ 可再获取
    with project_spawn_lock("erp", root=tmp_path, ttl=60):
        pass


def test_stale_lock_broken_after_ttl(tmp_path):
    (tmp_path / "erp").mkdir()
    lockdir = tmp_path / "erp" / ".spawn.lock"
    lockdir.mkdir()
    os.utime(lockdir, (time.time() - 999, time.time() - 999))  # 伪造陈旧
    with project_spawn_lock("erp", root=tmp_path, ttl=60):  # 过期 → 破锁获取
        pass


def test_concurrent_stale_break_no_crash(tmp_path, monkeypatch):
    # R2 fix1 — the core concurrency bug. Two workers race to break the SAME stale
    # lock: one wins the re-mkdir, the other's mkdir COLLIDES. The loser must get a
    # clean LockHeld — NOT an uncaught FileExistsError that crashes the process.
    #
    # Deterministic rival model: at the instant THIS worker removes the stale lock
    # during its break, a rival re-creates a FRESH lock, so this worker's retry
    # mkdir() collides with the rival's fresh lock.  Pre-fix (bare mkdir after the
    # rmdir) this would raise FileExistsError → pytest would ERROR; the bounded
    # retry loop re-inspects, sees age < ttl, and raises a clean LockHeld instead.
    (tmp_path / "erp").mkdir()
    lockdir = tmp_path / "erp" / ".spawn.lock"
    lockdir.mkdir()
    os.utime(lockdir, (time.time() - 999, time.time() - 999))  # stale

    real_rmdir = Path.rmdir

    def racing_rmdir(self):
        real_rmdir(self)
        if self == lockdir:
            self.mkdir()  # rival instantly grabs a FRESH lock (age ~0)

    monkeypatch.setattr(Path, "rmdir", racing_rmdir, raising=True)

    with pytest.raises(LockHeld), project_spawn_lock("erp", root=tmp_path, ttl=60):
        pass


def test_wait_zero_default_is_nonblocking(tmp_path):
    # The default (wait=0) keeps the original semantics: a held lock raises
    # IMMEDIATELY (this is what the singlepane §5.4 hard-REJECT relies on).
    t0 = time.monotonic()
    with (
        project_spawn_lock("erp", root=tmp_path, ttl=60),
        pytest.raises(LockHeld),
        project_spawn_lock("erp", root=tmp_path, ttl=60),
    ):
        pass
    assert time.monotonic() - t0 < 1.0


def test_wait_acquires_after_holder_releases(tmp_path):
    # Phase 7 concurrency fix: a worktree spawn WAITS for a concurrent same-project
    # spawn (parallel workers are legitimate, design §2.2) instead of rejecting.
    # The holder releases shortly; the waiter must then acquire, not raise.
    import threading

    acquired = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with project_spawn_lock("erp", root=tmp_path, ttl=60):
            acquired.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    assert acquired.wait(10)
    threading.Timer(0.3, release.set).start()
    with project_spawn_lock("erp", root=tmp_path, ttl=60, wait=10.0):
        pass  # acquired after the holder released — no LockHeld
    t.join(10)


def test_wait_bounded_gives_up_with_lockheld(tmp_path):
    # The wait is BOUNDED: a holder that never releases within the budget yields a
    # clean LockHeld (fail-closed), never an unbounded block.
    with project_spawn_lock("erp", root=tmp_path, ttl=60):
        t0 = time.monotonic()
        with (
            pytest.raises(LockHeld),
            project_spawn_lock("erp", root=tmp_path, ttl=60, wait=0.3),
        ):
            pass
        assert 0.25 <= time.monotonic() - t0 < 5.0


def test_stale_break_bounded_no_livelock(tmp_path, monkeypatch):
    # A pathological rival that re-creates a STALE lock after EVERY break would spin
    # the retry loop forever without a bound. max_stale_breaks caps it: after N break
    # attempts the worker gives up CLEANLY with LockHeld (no livelock, no crash).
    (tmp_path / "erp").mkdir()
    lockdir = tmp_path / "erp" / ".spawn.lock"
    lockdir.mkdir()
    os.utime(lockdir, (time.time() - 999, time.time() - 999))  # stale

    real_rmdir = Path.rmdir

    def adversary_rmdir(self):
        real_rmdir(self)
        if self == lockdir:
            self.mkdir()
            os.utime(self, (time.time() - 999, time.time() - 999))  # STALE again

    monkeypatch.setattr(Path, "rmdir", adversary_rmdir, raising=True)

    with (
        pytest.raises(LockHeld),
        project_spawn_lock("erp", root=tmp_path, ttl=60, max_stale_breaks=3),
    ):
        pass
