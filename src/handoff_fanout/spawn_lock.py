"""Project-level mutex covering spawn-intent decision + autoclose critical section
(R2 M2/M3). macOS has no flock on all fs → atomic ``mkdir``. A TTL break prevents a
crashed holder from deadlocking the whole project (Gemini R2r3-S2: release in finally).

Concurrency contract (R2 fix1): acquire is a BOUNDED retry loop, NOT a single
break-then-mkdir. When two workers race to break the SAME stale lock, exactly one
wins the re-``mkdir``; the loser's ``mkdir`` raises ``FileExistsError`` — that must
NOT crash the process (it is a normal race outcome, not a bug). The loser re-inspects
the lock: a rival's now-FRESH lock (``age < ttl``) yields a clean ``LockHeld``, and a
pathological churn is capped by ``max_stale_breaks`` so the loop can never livelock.
Anti-concurrency IS this primitive's entire job, so it must stay crash-free under it."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path


class LockHeld(Exception): ...


@contextlib.contextmanager
def project_spawn_lock(project: str, *, root: Path, ttl: float = 120.0, max_stale_breaks: int = 5):
    lockdir = Path(root) / project / ".spawn.lock"
    lockdir.parent.mkdir(parents=True, exist_ok=True)
    stale_breaks = 0
    while True:
        try:
            lockdir.mkdir()  # atomic acquire
            break
        except FileExistsError:
            # Someone holds the lock — OR a rival just grabbed it in a stale-break
            # race we lost. Inspect its age to tell "held" from "stale".
            try:
                age = time.time() - lockdir.stat().st_mtime
            except FileNotFoundError:
                # The holder released between our failed mkdir and the stat → the
                # lock is free now; retry the atomic mkdir immediately (clean, no
                # crash). Does not count as a stale-break.
                continue
            if age < ttl:
                # Genuinely held — OR a rival WON the stale-break race and now owns a
                # FRESH lock. Either way this is a deliberate signal, not an
                # error-in-handler: suppress the FileExistsError chain.
                raise LockHeld(f"{project} spawn lock held ({age:.0f}s)") from None
            # Stale (holder crashed). Break it, then RE-LOOP to re-acquire. The worker
            # that LOSES a concurrent stale-break sees the winner's fresh lock on the
            # next mkdir → clean LockHeld above (never an uncaught FileExistsError).
            stale_breaks += 1
            if stale_breaks > max_stale_breaks:
                # Bounded: a pathological churn (e.g. a rival re-creating a stale lock
                # every round) must terminate cleanly rather than spin forever.
                raise LockHeld(
                    f"{project} spawn lock contended "
                    f"(still stale after {max_stale_breaks} break attempts)"
                ) from None
            with contextlib.suppress(OSError):
                lockdir.rmdir()
            # loop → next mkdir
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lockdir.rmdir()  # ALWAYS release
