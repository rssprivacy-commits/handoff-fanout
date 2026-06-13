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


def derive_singlepane_focus(home: str | os.PathLike[str], project: str, task: str) -> str | None:
    """SELF-REPORT (djs-jump-return 2026-06-14): derive a singlepane coordinator's OWN
    ``.handoff.code-workspace`` path from the task IT self-reports — NOT from the env channel.

    p19 proved ``$HANDOFF_WINDOW_FOCUS_PATH`` (injected via ``terminal.integrated.env.osx``) does
    NOT reach the agent shell of an extension-panel auto-spawned singlepane coordinator, so the
    env-based ``_spawner_focus_line`` / ``--spawner-focus-path`` produce ``""`` from such a window
    and the worker never jumps to the coordinator's desktop. The agent, however, KNOWS its own task
    (passed as ``--self-task`` on the succession close) — so reconstruct the path the engine itself
    wrote when this coordinator was spawned: ``<home>/<project>/singlepane/<task>.handoff.code-workspace``
    (see ``dump.maybe_write_singlepane_sidecar`` — ``ws_file = sp_dir / f"{task}.handoff.code-workspace"``).

    Returns the path string only when it EXISTS as a regular file (so a bootstrap leg whose window
    was opened by dx-spawn out-of-tree — no engine singlepane file — yields ``None`` and the caller
    fail-opens to the existing per-project goto, no spurious "dropped" warning). The returned string
    is still re-validated by :func:`validate_spawner_focus` at the produce site (single security
    boundary — derivation does not bypass the gate). NEVER raises — a missing/odd value is ``None``.
    """
    if not project or not task:
        return None
    p = os.path.join(str(home), project, "singlepane", f"{task}.handoff.code-workspace")
    return p if os.path.isfile(p) else None
