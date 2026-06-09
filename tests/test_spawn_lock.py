import os
import time

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
