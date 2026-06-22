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
import sys
import tempfile
from dataclasses import dataclass
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


# ── spawn-unification Step 2 / Tier-3 SESSION IDENTITY (2026-06-22 / sw-su-step2) ────────────────
# The rf/sf wrong-desktop root cause: a coordinator dispatches a worker (``handoff dump --status
# active`` / ``handoff spawn``) but supplies NEITHER an explicit ``--spawner-focus-path`` NOR a
# ``--self-task``, and its cwd is the shared repo root (not a same-project worktree) → Tier-1 AND
# Tier-2 both miss → the .uri omits SPAWNER_FOCUS → code-router.sh falls back to the stale static
# desktop map → wrong desktop. dx-spawn-session.sh already auto-derives ``--self-task`` for ITS paths,
# but the direct ``handoff dump --status active`` / ERP / skills paths do not pass through it. Closing
# the gap IN THE ENGINE makes every produce path reliable regardless of which (or no) wrapper invoked
# it (design §8.6 Step 2 «收编中央 producer»). Reuses the SHARED ``dx_session_role`` resolver — the
# single cross-consumer identity source (memory-guard / coord-guard / dx-spawn all consume it) — rather
# than building a 2nd identity mechanism. STRICTLY ADDITIVE + FAIL-OPEN (warn-mode: best-effort, never
# blocks; Step 4 later flips a still-unresolved miss to fail-closed).
#
# HONEST SCOPE (sw-su-s2fix 2026-06-22): Tier-3 honors ONLY a ``definite``-confidence supervisor (the
# worktree marker / worktree sidecar / env role — same conservative predicate coord-guard.py uses),
# REJECTING the resolver's ``suspected`` singlepane-sidecar identity (whose cwd = the shared repo root is
# indistinguishable from the owner's everyday session → trusting it risks a REAL-BUT-WRONG anchor). So a
# SINGLEPANE coordinator dispatching from the repo root is NOT closed by Tier-3 (it resolves to owner/none
# or supervisor/suspected → both rejected → still MISSES). The fallback-to-0 goal is met by Tier-3
# (definite-identifiable coordinators) AND Step 4's fail-closed (force singlepane to pass ``--self-task``,
# i.e. Tier-2) TOGETHER — Tier-3 does NOT, and must not, close the singlepane gap by trusting a suspected
# identity. See ``_derive_self_from_session`` for the gate.


def _load_session_role_resolver():
    """Load the shared ``dx_session_role.resolve_session_role`` callable via robust importlib, or
    ``None`` (FAIL-OPEN). Path: ``$DX_SESSION_ROLE_PATH`` override, else
    ``~/.claude/scripts/dx_session_role.py`` — the SAME file dx-spawn-session.sh loads (single identity
    source, 不各造身份机制). Any absent file / import error → ``None`` so the caller fail-opens. The
    module is loaded fresh per call (cheap on the once-per-spawn produce path; avoids a stale resolver
    surviving a redeploy) and NEVER raises."""
    path = os.environ.get("DX_SESSION_ROLE_PATH") or os.path.expanduser(
        "~/.claude/scripts/dx_session_role.py"
    )
    if not os.path.isfile(path):
        return None
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_dx_session_role_engine", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "resolve_session_role", None)
    except Exception:
        return None


def _derive_self_from_session(cwd: str | os.PathLike[str]) -> tuple[str, str] | None:
    """Return the DISPATCHING coordinator's OWN ``(project, task)`` from the shared session-role
    resolver, or ``None`` (FAIL-OPEN). Only a ``definite``-confidence ``supervisor`` carrying both
    ``task`` and ``project`` is honored; everything else → ``None``. NEVER raises.

    CONFIDENCE GATE (sw-su-s2fix 2026-06-22): the shared ``dx_session_role`` resolver tags every
    identity ``definite`` | ``suspected``. A ``supervisor`` is ``definite`` ONLY when it is grounded in a
    confident signal — the worktree 🧭中枢 ``window.title`` marker, a worktree ``.singlepane`` sidecar, or
    an explicit ``HANDOFF_SESSION_ROLE`` env. A singlepane coordinator scanned by cwd, by contrast, is
    returned ``supervisor / suspected / singlepane-sidecar`` because its cwd (the shared repo root) is
    INDISTINGUISHABLE from the owner's everyday session at the same cwd (resolver docstring §3) — a
    UNIQUE sidecar match there is still only weak cwd evidence. Honoring such a ``suspected`` identity
    would let Tier-3 resolve a REAL-BUT-WRONG workspace (anchor the worker to the wrong desktop). So we
    accept ONLY ``confidence == "definite"`` — the SAME conservative predicate ``coord-guard.py`` uses to
    decide whether a session is confidently a coordinator (``role == "supervisor" and confidence ==
    "definite"``). worker / owner / solo / ambiguous (resolver returns ``None`` task for a ≥2-coordinator
    scan — uniqueness-or-fail) / contradiction / and now ``suspected`` supervisor all → ``None``.

    HONEST SCOPE (warn-mode Step 2): this makes Tier-3 a BEST-EFFORT closer that fires ONLY for a
    DEFINITE-identifiable coordinator — a worktree-marker coordinator, or one carrying the env role.
    A SINGLEPANE coordinator dispatching from the shared repo root resolves to ``owner/none`` (or
    ``supervisor/suspected`` when a sidecar uniquely matches), and BOTH are rejected here → Tier-3 does
    NOT fire → that singlepane dispatch still MISSES (omits ``SPAWNER_FOCUS``, falls back to the static
    desktop map). Closing the singlepane miss needs an explicit ``--self-task`` (Tier-2) or Step 4's
    fail-closed «force singlepane to pass ``--self-task``» — Tier-3 does not (and must not) close it by
    trusting a suspected identity. This is the deliberate boundary, not a defect.

    ⚠️ ``cfg.home`` caveat: the shared resolver scans ``~/.claude-handoff`` for its singlepane track
    (it does not honor ``$HANDOFF_HOME``); the deployed install pins ``HANDOFF_HOME=~/.claude-handoff``
    so PROD ``cfg.home`` matches. Either way the returned identity is RE-GROUNDED by the caller's
    :func:`derive_singlepane_focus(home=cfg.home, …)` (which DOES use the passed home), so an identity
    with no sidecar under ``cfg.home`` resolves no workspace → ``None`` → no mis-jump. This seam is a
    module-level function so the test suite can neutralize it for hermeticity (conftest autouse)."""
    resolver = _load_session_role_resolver()
    if resolver is None:
        return None
    try:
        res = resolver(str(cwd))
    except Exception:
        return None
    if not isinstance(res, dict):
        return None
    if (
        res.get("role") == "supervisor"
        and res.get("confidence") == "definite"
        and res.get("task")
        and res.get("project")
    ):
        return (str(res["project"]), str(res["task"]))
    return None


def resolve_spawner_focus_path(
    cwd: str | os.PathLike[str],
    *,
    cfg: _config.Config,
    home: str | os.PathLike[str] | None = None,
    project: str | None = None,
    self_task: str | None = None,
) -> str | None:
    """Return the SPAWNING coordinator's OWN ``.handoff.code-workspace`` realpath (validated through
    :func:`validate_spawner_focus`, the single security boundary), or ``None``. THREE env-independent
    tiers, tried in order; NEVER raises (fail-open → caller omits ``SPAWNER_FOCUS`` and the existing
    goto stands):

      * Tier 1 — WORKTREE: ``<cwd>/.handoff.code-workspace`` (a worktree coordinator always carries one;
        the engine creates worktrees under ``cfg.home/<project>/worktrees`` = an allowed root). The
        resolved candidate must ALSO belong to the target ``project`` (:func:`_tier1_cwd_belongs_to_project`)
        — a cross-project worktree cwd is dropped so it can't mis-resolve another project's workspace.
      * Tier 2 — SINGLEPANE (explicit): :func:`derive_singlepane_focus(home, project, self_task)` → the
        REAL engine sidecar ``<home>/<proj>/singlepane/<self_task>.handoff.code-workspace`` (the identity
        a singlepane window can't read from cwd — its cwd is the shared repo root). Requires ``home``/
        ``project``/``self_task`` (the spawner's OWN task, self-reported via ``--self-task``). Project-
        bound by construction (the path is built FROM ``project``), so it needs no extra binding check.
      * Tier 3 — SESSION IDENTITY (spawn-unification Step 2): when the dispatcher passed no ``--self-task``
        and cwd is not a same-project worktree, auto-derive the coordinator's OWN ``(project, task)`` from
        the shared session-role resolver (:func:`_derive_self_from_session`) and rebuild ITS singlepane
        workspace. BEST-EFFORT: honors ONLY a ``definite``-confidence coordinator (worktree marker / env
        role); a SINGLEPANE coordinator from the repo root is ``suspected`` and is REJECTED (real-but-wrong
        guard), so Tier-3 does NOT close the singlepane miss — that needs explicit ``--self-task`` (Tier-2)
        / Step 4 fail-closed. Requires only ``home``.

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
    # Tier 3 — SESSION IDENTITY (spawn-unification Step 2): neither an explicit anchor, a same-project
    # worktree cwd (Tier-1), nor a ``--self-task`` (Tier-2) resolved — the rf/sf root cause «协调员 dump
    # 一个 worker 却没带中枢身份». Auto-derive the DISPATCHING coordinator's OWN ``(project, task)`` from
    # the shared session-role resolver and reconstruct ITS singlepane workspace. Uses the coordinator's
    # OWN project (from the resolver, NOT the target ``project``), so a cross-project dispatch resolves
    # too. ``derive_singlepane_focus`` re-grounds the identity against ``home`` (= cfg.home) before the
    # SAME validate gate, so a foreign/stale identity with no sidecar under this home → ``None``. The
    # ``_derive_self_from_session`` seam is module-level (conftest neutralizes it for suite hermeticity).
    if home:
        derived = _derive_self_from_session(cwd)
        if derived:
            coord_project, coord_task = derived
            sidecar_ws = derive_singlepane_focus(home, coord_project, coord_task)
            if sidecar_ws:
                rp = validate_spawner_focus(sidecar_ws, cfg=cfg)
                if rp:
                    return rp
    return None


# ── spawn-unification Step 4 (2026-06-22 / sw-s4-impl) — fail-closed machinery ──────────────────
# Turn an anchor-resolution MISS on a coordinator dispatch from today's SILENT fail-open (omit
# SPAWNER_FOCUS → code-router.sh static-map fallback → wrong desktop) into an explicit fail-CLOSED
# refuse (design Step 4). DEFAULT = warn (config enforce lists empty) = BYTE-IDENTICAL to Step 1+2.
# The machinery here is PURE decision (origin trust matrix + per-project enforcement) — the actual
# resolution still happens ONCE per produce path (spawn resolves its own path; dump resolves at its
# command entry), and the writers consume the decision verbatim (design §2.4/§2.5 single-parse).
#
# ORIGIN TRUST MODEL (design §2.2 v3/v4 铁律): leniency (allow-no-anchor) comes ONLY from
# NON-inheritable sources — a config allow-list (system), a physical front TTY (interactive), or
# in-process pytest (test). An env var can only ADD strictness (HANDOFF_UNATTENDED demotes
# interactive), NEVER grant an exemption → an inherited env can only make a dispatch STRICTER, so the
# "inherited exemption env" backdoor is structurally impossible. Any un-provable signal → coordinator
# (the strictest origin = anchor required).

ORIGIN_COORDINATOR = "coordinator"
ORIGIN_INTERACTIVE = "interactive"
ORIGIN_SYSTEM = "system"
ORIGIN_TEST = "test"
ORIGINS = (ORIGIN_COORDINATOR, ORIGIN_INTERACTIVE, ORIGIN_SYSTEM, ORIGIN_TEST)

ENFORCE_WARN = "warn"
ENFORCE_DRY_RUN = "dry_run"
ENFORCE_BLOCK = "block"

# anchor-unresolved is kept DISTINCT from Step 6's isolation-unresolved (design §4.2): the two
# fail-closed reasons / error messages must never be conflated.
MISS_REASON_ANCHOR = "anchor-unresolved"


@dataclass(frozen=True)
class AnchorDecision:
    """The once-computed verdict for a single produce path (design §2.4). Immutable so the 4 dump
    writers can only CONSUME it (never re-derive). ``origin_source`` is LOG-ONLY (``cli`` | ``default``)
    — v3 deleted env-reported origin trust, so it carries no authority, only provenance for the log."""

    focus_line: str | None  # validated "SPAWNER_FOCUS=<path>\n" (the single resolution result) or None
    required: bool  # effective origin == coordinator AND project under an enforce phase (§2.4)
    origin: str  # the EFFECTIVE origin after the §4.2 trust matrix (post-demotion)
    origin_source: str  # cli | default (log-only; no trust use — §2.2 v3)
    enforcement: str  # warn | dry_run | block (§4.1 per-project phase)
    miss_reason: str | None  # MISS_REASON_ANCHOR when focus_line is None, else None


def _front_tty() -> bool:
    """True IFF BOTH stdin and stdout are a real terminal — a NECESSARY (non-sufficient) gate for the
    interactive exemption (design §2.2 v4 / R3 codex). A headless chain (no controlling tty) physically
    cannot pass this, so it can never obtain the interactive exemption even if it forgets to set
    HANDOFF_UNATTENDED — root-cause fix for the §7-2a "forgot the strictness env" weak point. NEVER
    raises (a closed/odd fd → not a tty)."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def _in_process_pytest() -> bool:
    """True IFF running INSIDE pytest (the ``test`` origin's exemption signal). Deliberately an
    in-process probe (``sys.modules``), NOT an env var: a production-inheritable ``HANDOFF_TEST_MODE``
    would violate «env only授 strictness» and let a leaked env grant leniency (design §2.2 v4 codex R3)."""
    return "pytest" in sys.modules


def _effective_origin(origin: str | None, *, cfg: _config.Config, project: str) -> str:
    """Apply the design §4.2 trust matrix → the EFFECTIVE origin (coordinator after any demotion).

    Leniency only from NON-inheritable sources; an unknown / un-provable signal → coordinator (most
    strict). Demotions: interactive without a front TTY OR with HANDOFF_UNATTENDED set → coordinator;
    system whose project is not in the config allow-list → coordinator; test outside in-process pytest
    → coordinator. NEVER raises."""
    o = origin if origin in ORIGINS else ORIGIN_COORDINATOR
    if o == ORIGIN_COORDINATOR:
        return ORIGIN_COORDINATOR
    if o == ORIGIN_INTERACTIVE:
        # HANDOFF_UNATTENDED present (ANY value) = the automated-chain marker → demote (env adds
        # strictness only). No TTY → demote (headless chain physically can't be exempt).
        if _front_tty() and "HANDOFF_UNATTENDED" not in os.environ:
            return ORIGIN_INTERACTIVE
        return ORIGIN_COORDINATOR
    if o == ORIGIN_SYSTEM:
        allow = getattr(cfg, "spawner_anchor_system_allow", ()) or ()
        return ORIGIN_SYSTEM if project in allow else ORIGIN_COORDINATOR
    if o == ORIGIN_TEST:
        return ORIGIN_TEST if _in_process_pytest() else ORIGIN_COORDINATOR
    return ORIGIN_COORDINATOR


def _anchor_enforcement(cfg: _config.Config, project: str) -> str:
    """Per-project enforcement phase (design §4.1): ``warn`` | ``dry_run`` | ``block``.

    CONFIG FAIL-SAFE (codex R2 «一个 config 坏 = 防线静默消失»): a present-but-corrupt config
    (``config_trusted=False``) → ``block`` — NEVER silently degrade an enforce-able dispatch to warn
    off a config we couldn't parse. Otherwise: an explicit ``enforce`` list membership → ``block``;
    a ``dry_run`` list membership → ``dry_run``; else ``warn``. Overlap (a project in BOTH lists) →
    ``block`` wins (the stricter — checked first; design §4.1 table)."""
    if not getattr(cfg, "config_trusted", True):
        return ENFORCE_BLOCK
    if project in (getattr(cfg, "spawner_anchor_enforce_projects", ()) or ()):
        return ENFORCE_BLOCK
    if project in (getattr(cfg, "spawner_anchor_dry_run_projects", ()) or ()):
        return ENFORCE_DRY_RUN
    return ENFORCE_WARN


def make_anchor_decision(
    resolved_path: str | None,
    *,
    cfg: _config.Config,
    home: str | os.PathLike[str],
    project: str,
    origin: str = ORIGIN_COORDINATOR,
    origin_source: str = "cli",
    cwd: str | os.PathLike[str],
    callsite: str = "spawn",
) -> AnchorDecision:
    """Build the :class:`AnchorDecision` from an ALREADY-resolved+validated focus path (or ``None``).

    PURE decision — NO resolution here, so ``spawn`` (which resolved its own ``--spawner-focus-path`` /
    self-id path) and ``dump`` (which resolves once at its command entry) feed the SAME machinery
    without a second cwd/env/cfg read (design §2.5 TOCTOU). Computes the effective origin (§4.2 matrix),
    the per-project enforcement (§4.1), ``required`` (a coordinator dispatch under an enforce phase),
    and the miss reason. A system-origin无锚 pass-through is AUDIT-logged here (design §2.2 codex R3:
    every system exemption must be observable). NEVER raises (the decision must not break a spawn/dump)."""
    focus_line = f"SPAWNER_FOCUS={resolved_path}\n" if resolved_path else None
    eff = _effective_origin(origin, cfg=cfg, project=project)
    enforcement = _anchor_enforcement(cfg, project)
    required = eff == ORIGIN_COORDINATOR and enforcement != ENFORCE_WARN
    miss_reason = MISS_REASON_ANCHOR if resolved_path is None else None
    if eff == ORIGIN_SYSTEM and resolved_path is None:
        log_system_anchor_audit(home=home, project=project, callsite=callsite, cwd=cwd)
    return AnchorDecision(
        focus_line=focus_line,
        required=required,
        origin=eff,
        origin_source=origin_source if origin_source in ("cli", "default") else "default",
        enforcement=enforcement,
        miss_reason=miss_reason,
    )


def resolve_anchor_decision(
    cwd: str | os.PathLike[str],
    *,
    cfg: _config.Config,
    home: str | os.PathLike[str],
    project: str,
    self_task: str | None = None,
    origin: str = ORIGIN_COORDINATOR,
    origin_source: str = "cli",
    env_focus_path: str | None = None,
    callsite: str = "dump",
) -> AnchorDecision:
    """The SINGLE resolution+decision point for the dump command entry (design §2.4): validate the env
    focus hint (``$HANDOFF_WINDOW_FOCUS_PATH``), else self-resolve via
    :func:`resolve_spawner_focus_path` EXACTLY ONCE, then build the decision. The 4 dump writers consume
    the returned object verbatim — no second read of cwd/env/cfg downstream (TOCTOU eliminated). NEVER
    raises."""
    rp = validate_spawner_focus(env_focus_path, cfg=cfg) if env_focus_path else None
    if not rp:
        rp = resolve_spawner_focus_path(
            cwd, cfg=cfg, home=home, project=project, self_task=self_task
        )
    return make_anchor_decision(
        rp,
        cfg=cfg,
        home=home,
        project=project,
        origin=origin,
        origin_source=origin_source,
        cwd=cwd,
        callsite=callsite,
    )


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


def log_block_intent(
    *,
    home: str | os.PathLike[str],
    project: str,
    task: str | None,
    cwd: str | os.PathLike[str],
    origin: str,
    enforcement: str,
    reason: str,
) -> None:
    """Append ONE JSON line to ``<home>/<project>/spawn-anchor-block-intent.log`` — the dry_run /
    shadow record (design §4.1 phase 2): a dispatch that the ENFORCE phase WOULD have blocked, logged
    WITHOUT changing behavior so the ≥24-48h buffer can prove would-block→0 before flipping real block.
    Same fail-open / non-blocking contract as :func:`log_anchor_miss`."""
    try:
        log_dir = os.path.join(str(home), project)
        os.makedirs(log_dir, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task": task,
            "project": project,
            "cwd": str(cwd),
            "origin": origin,
            "enforcement": enforcement,
            "would_block": True,
            "reason": reason,
        }
        with open(
            os.path.join(log_dir, "spawn-anchor-block-intent.log"), "a", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_system_anchor_audit(
    *,
    home: str | os.PathLike[str],
    project: str,
    callsite: str,
    cwd: str | os.PathLike[str],
) -> None:
    """Append ONE JSON line ``{ts, project, callsite, cwd}`` to
    ``<home>/<project>/spawn-anchor-system-audit.log`` — every system-origin无锚 exemption (design
    §2.2 codex R3: a system pass-through must be observable so over-broad use is catchable, enabling
    a future ``(project, callsite)`` tightening). Same fail-open / non-blocking contract as
    :func:`log_anchor_miss`."""
    try:
        log_dir = os.path.join(str(home), project)
        os.makedirs(log_dir, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "project": project,
            "callsite": callsite,
            "cwd": str(cwd),
        }
        with open(
            os.path.join(log_dir, "spawn-anchor-system-audit.log"), "a", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
