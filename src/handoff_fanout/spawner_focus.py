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

import json
import os
import tempfile
from datetime import datetime, timezone
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


# ── mp-locate-return Part A / 去程 (2026-06-14 / sw-coord-p22 AUDITED-SPEC §1): self-report the
# spawner's OWN workspace PATH (NOT a desktop number) and emit it as ``SPAWNER_FOCUS=<PATH>`` so the
# watchdog code-router runs the EXISTING one-step ``focus-jump`` (``code <workspace>`` reactivates the
# already-open window → macOS slides to its Space in ONE native step). p22 reversal: the
# ``SPAWNER_DESKTOP=N`` → ``goto N`` route was ``Ctrl+arrow`` PER-STEP (逐格), violating the north
# star「一步原生、非逐格」. Two env-independent tiers, BOTH derived from data the engine already wrote:
#   * Tier 1 WORKTREE — ``<cwd>/.handoff.code-workspace`` (the worktree coordinator's own workspace).
#   * Tier 2 SINGLEPANE — :func:`derive_singlepane_focus` from the spawner's self-reported task
#     (``--self-task``) → the REAL ``<home>/<proj>/singlepane/<task>.handoff.code-workspace`` sidecar
#     (the marker-hook route is DROPPED: that marker base never existed → was always ``None``).
# Every candidate goes through the SAME :func:`validate_spawner_focus` security gate (single boundary).
# Strictly ADDITIVE + FAIL-OPEN: any miss → ``None`` → caller omits ``SPAWNER_FOCUS`` and the existing
# per-project goto is unchanged (字节级向后兼容).


def _tier1_cwd_belongs_to_project(rp: str, *, cfg: _config.Config, project: str | None) -> bool:
    """spawn-unification Step 1 hardening (2026-06-22 / sw-spawn-unify-s1fix): a Tier-1 ``cwd``
    workspace must belong to the TARGET ``project`` — not merely live under an allowed root.

    :func:`validate_spawner_focus` only proves the candidate is an existing ``.handoff.code-workspace``
    under a TRUSTED root (the handoff home / ``~/.claude-handoff`` / temp). But EVERY project's
    worktrees live under that SAME home, so the gate alone cannot tell project A's worktree from
    project B's. Running a spawn / succession from project B's worktree cwd while dispatching FOR
    project A would then let Tier-1 grab B's workspace (cross-project mis-resolution → the worker is
    born on the wrong project's coordinator desktop). This is a TIGHTENING-ONLY check: the normal flow
    — a same-project worktree coordinator dispatching a same-project worker — resolves its cwd UNDER
    ``worktrees_root(cfg, project)`` and passes unchanged; only a cross-project cwd is dropped (the
    resolver then falls through to Tier-2 / ``None``). When no target ``project`` is supplied there is
    nothing to bind to, so the historical Tier-1 behavior is preserved (the sole such caller is the
    raw unit test — every production caller passes ``project``). NEVER raises (fail-open)."""
    if not project:
        return True
    from handoff_fanout import worktree as _worktree

    try:
        root = os.path.realpath(str(_worktree.worktrees_root(cfg, project)))
    except Exception:
        # Can't determine where this project's worktrees live → DROP the Tier-1 candidate (the
        # resolver falls through to Tier-2 / None). Same fail-open guarantee as the rest of the module.
        return False
    return rp == root or rp.startswith(root + os.sep)


def resolve_spawner_focus_path(
    cwd: str | os.PathLike[str],
    *,
    cfg: _config.Config,
    home: str | os.PathLike[str] | None = None,
    project: str | None = None,
    self_task: str | None = None,
) -> str | None:
    """Return the SPAWNING coordinator's OWN ``.handoff.code-workspace`` realpath (validated through
    :func:`validate_spawner_focus`, the single security boundary), or ``None``. Two env-independent
    tiers; NEVER raises (fail-open → caller omits ``SPAWNER_FOCUS`` and the existing goto stands):

      * Tier 1 — WORKTREE: ``<cwd>/.handoff.code-workspace`` (a worktree coordinator always carries one;
        the engine creates worktrees under ``cfg.home/<project>/worktrees`` = an allowed root). The
        resolved candidate must ALSO belong to the target ``project`` (:func:`_tier1_cwd_belongs_to_project`)
        — a cross-project worktree cwd is dropped so it can't mis-resolve another project's workspace.
      * Tier 2 — SINGLEPANE: :func:`derive_singlepane_focus(home, project, self_task)` → the REAL engine
        sidecar ``<home>/<proj>/singlepane/<self_task>.handoff.code-workspace`` (the identity a singlepane
        window can't read from cwd — its cwd is the shared repo root). Requires ``home``/``project``/
        ``self_task`` (the spawner's OWN task, self-reported via ``--self-task``). Project-bound by
        construction (the path is built FROM ``project``), so it needs no extra binding check.

    Each candidate is re-validated by ``validate_spawner_focus`` (realpath + ``.handoff.code-workspace``
    suffix + allowed-root) before return, so a path outside the trusted roots is dropped (``None``),
    never written verbatim into the worker ``.uri``."""
    cand = os.path.join(str(cwd), ".handoff.code-workspace")
    if os.path.isfile(cand):
        rp = validate_spawner_focus(cand, cfg=cfg)
        if rp and _tier1_cwd_belongs_to_project(rp, cfg=cfg, project=project):
            return rp
    if home and project and self_task:
        sidecar_ws = derive_singlepane_focus(home, project, self_task)
        if sidecar_ws:
            rp = validate_spawner_focus(sidecar_ws, cfg=cfg)
            if rp:
                return rp
    return None


# ── spawn-unification Step 1 (2026-06-22 / sw-spawn-unify-step1) ─────────────────────────────────
# Telemetry: turn the otherwise-SILENT "no SPAWNER_FOCUS → code-router.sh falls back to the static
# projects.json desktop map" event (the root cause of workers landing on the wrong / owner's desktop)
# into a VISIBLE, COUNTABLE record. ``spawn`` and ``dump`` call this directly the moment their anchor
# resolution yields None; an audit-close succession records its miss via the ``run_spawn`` it invokes
# (which calls this when ITS own resolution yields None) — audit-close does not call it directly. The
# call is made BEFORE the existing fail-open omit-the-line behavior, which is left BYTE-IDENTICAL (this
# Step is warn-mode: observe, never block). The canary later proves this count trends to ~0 before
# Step 4 flips the resolution miss to fail-closed.


def log_anchor_miss(
    *,
    home: str | os.PathLike[str],
    project: str,
    task: str | None,
    cwd: str | os.PathLike[str],
    isolation: str | None,
    reason: str,
) -> None:
    """Append ONE JSON line ``{ts, task, project, cwd, isolation, reason}`` to
    ``<home>/<project>/spawn-anchor-miss.log``.

    STRICTLY ADDITIVE + NON-BLOCKING by contract (same fail-open guarantee as the resolver itself): a
    telemetry write must NEVER raise / block a spawn or dump, so every failure (unwritable dir, disk
    full, odd ``home``) is swallowed. The caller's omit-the-``SPAWNER_FOCUS``-line behavior is
    unchanged whether this logs or not."""
    try:
        log_dir = os.path.join(str(home), project)
        os.makedirs(log_dir, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task": task,
            "project": project,
            "cwd": str(cwd),
            "isolation": isolation,
            "reason": reason,
        }
        with open(os.path.join(log_dir, "spawn-anchor-miss.log"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # fail-open: telemetry is never load-bearing — a missed log line must not break a spawn/dump.
        pass
