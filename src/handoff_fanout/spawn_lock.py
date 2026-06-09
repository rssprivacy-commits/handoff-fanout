"""Project-level mutex covering spawn-intent decision + autoclose critical section
(R2 M2/M3). macOS has no flock on all fs → atomic ``mkdir``. TTL break prevents a
crashed holder from deadlocking the whole project (Gemini R2r3-S2: release in finally)."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path


class LockHeld(Exception): ...


@contextlib.contextmanager
def project_spawn_lock(project: str, *, root: Path, ttl: float = 120.0):
    lockdir = Path(root) / project / ".spawn.lock"
    lockdir.parent.mkdir(parents=True, exist_ok=True)
    try:
        lockdir.mkdir()  # atomic acquire
    except FileExistsError:
        age = time.time() - lockdir.stat().st_mtime
        if age < ttl:
            # LockHeld is a deliberate signal (lock is genuinely held), not an
            # error-in-handler — suppress the FileExistsError chain.
            raise LockHeld(f"{project} spawn lock held ({age:.0f}s)") from None
        # stale (holder crashed) → break and re-acquire
        with contextlib.suppress(OSError):
            lockdir.rmdir()
        lockdir.mkdir()
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lockdir.rmdir()  # ALWAYS release
