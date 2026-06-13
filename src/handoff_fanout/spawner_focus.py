"""direct-jump-spawn (2026-06-13): the SINGLE security gate validating a *spawner focus path* —
the active coordinator / spawning window's own ``.handoff.code-workspace`` — before it is written
to a worker's ``queue/<task>.uri`` as the additive ``SPAWNER_FOCUS=`` line.

The watchdog exports that line as ``$HANDOFF_SPAWNER_FOCUS`` and ``code-router.sh`` runs
``code <SPAWNER_FOCUS>`` to NATIVELY jump to the spawner's desktop Space before opening the new
worker (so the worker is born next to whoever dispatched it). Because the value becomes an argument
to ``code <file>``, an unvalidated/forged path in a tampered ``.uri`` could open an arbitrary file —
hence the strict gate, kept in ONE place so ``spawn`` (CLI ``--spawner-focus-path``) and ``dump``
(``$HANDOFF_WINDOW_FOCUS_PATH`` env) share the EXACT same check (no drift, no second copy of a
security boundary).

FAIL-OPEN by contract: this is a UX hint, never load-bearing. An absent/invalid value returns
``None`` and the caller simply omits the line (the window still spawns, just without the jump) — it
must NEVER raise or block a spawn/dump.
"""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from handoff_fanout import config as _config


def validate_spawner_focus(raw_path: str | None, *, cfg: _config.Config) -> str | None:
    """Return the realpath-normalized ``.handoff.code-workspace`` IFF ``raw_path`` passes every
    gate, else ``None`` (FAIL-OPEN — never raises for an invalid hint).

    Gate (verbatim from the dual-brain-audited spawn.py original, now the single source):
      * realpath + ``~`` expansion (so symlink / relative tricks can't escape the allow-list);
      * absolute;
      * ends with ``.handoff.code-workspace`` (so a forged value can't make the router
        ``code <arbitrary file>``);
      * exists as a regular file;
      * lives under an allowed root: the handoff home, ``~/.claude-handoff``, the system temp dir /
        ``$TMPDIR`` (where ``dx-spawn --coordinator`` writes its out-of-tree WS_FILE), or ``/tmp`` /
        ``/private/tmp``.
    """
    if not raw_path:
        return None
    rp = os.path.realpath(os.path.expanduser(raw_path))
    allowed = {
        os.path.realpath(str(cfg.home)),
        os.path.realpath(os.path.expanduser("~/.claude-handoff")),
        os.path.realpath(tempfile.gettempdir()),
        os.path.realpath(os.environ.get("TMPDIR") or "/tmp"),
        "/tmp",
        "/private/tmp",
    }
    if (
        os.path.isabs(rp)
        and rp.endswith(".handoff.code-workspace")
        and os.path.isfile(rp)
        and any(rp == a or rp.startswith(a + os.sep) for a in allowed)
    ):
        return rp
    return None
