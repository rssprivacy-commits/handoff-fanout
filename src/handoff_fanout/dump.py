"""Generate handoff queue files for a project's next task or batch.

Two operating modes:

  * **Single-task** (default): write ``$HANDOFF_HOME/<project>/queue/<task>.md``
    plus a ``.uri`` sidecar consumed by the IDE auto-spawn helper.
  * **Batch / fan-out** (``--open-batch manifest.json``): write a manifest
    plus per-sub-task ``.md``/``.uri``/``.env`` files, applying the v5
    safety gates (N_max≤3, global active-tab limit, file_ownership
    intersection check, staggered spawn).

State transitions during a batch's lifetime are written by sub-task tabs
calling back into this module with ``--batch-done`` / ``--batch-blocked``;
the last-one-out triggers the fan-in handoff.

This module has zero ERP-specific content. Markdown blocks like the V3.6
redlines or in-house legislation are injected via
``config.Config.inject_blocks``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

from handoff_fanout import atomic, retro_gate, templates
from handoff_fanout import config as _config
from handoff_fanout import memory_baseline as _memory_baseline
from handoff_fanout import spawn_nonce as _spawn_nonce
from handoff_fanout import spawner_focus as _spawner_focus
from handoff_fanout import worktree as _worktree
from handoff_fanout.git_guard import git_guard_dir
from handoff_fanout.handoff_precheck import (
    EVIDENCE_SCHEMA_VERSION,
    compute_retro_evidence_hash,
    resolve_session_id,
)
from handoff_fanout.spawn_lock import LockHeld, project_spawn_lock

# v5.4 old_ready schema (§7.6). Bumped together with retro_evidence schema.
OLD_READY_SCHEMA_VERSION = EVIDENCE_SCHEMA_VERSION

# v5 protocol constants
SCHEMA_VERSION = 2
SPECIAL_MARKERS = {
    "_fanin_triggered",
    "_fanin_blocked",  # Step 4 (sw-s4-fix): a fan-in refused fail-closed by the anchor gate (codex #3)
    "_fan_in_started",
    "_fan_in_heartbeat",
    "_fan_in_done",
    "_watchdog_triggered",
    "_aborted",
    "_corrupted",
}
HANDOFF_ROLE_MAIN = "main"
HANDOFF_ROLE_SUB_TASK = "sub-task"
HANDOFF_ROLE_FAN_IN = "fan-in"

# E3 role taxonomy (warmgap-B / 2a gate 双脑一致): the NON-coordinator single-task active
# dump — the solo auto-relay chain — is NOT a true worker (batch sub-task / fan-in tabs and
# ``handoff spawn`` workers are). Its watchdog sidecar ``role`` + workspace env
# ``HANDOFF_SESSION_ROLE`` carry this value so downstream identity consumers (memory-guard
# role matrix, Step3 hooks) can tell the relay mainline apart from fenced workers. Distinct
# from the batch HANDOFF_ROLE_* env above (different mechanism: queue .env files). The
# watchdog open path is role-agnostic and autoclose acts only on ``supervisor_succession``,
# so this value rides through both safely (test-locked).
ROLE_SOLO = "solo"

# v5.1 spawn-storm defenders (carried over from the v5.1 / 5.2 audit).
SUB_TASK_N_MAX = 3
STAGGER_SPAWN_SECONDS = 30
GLOBAL_ACTIVE_LIMIT = 5

# Bounded wait for the project spawn lock on the dump WORKTREE-create path (sw-coord-p21
# symmetry fix). Kept equal to spawn.py's ``_WORKTREE_LOCK_WAIT`` (120.0): parallel
# worktree workers are LEGITIMATE (design §2.2), so a concurrent same-project dump QUEUES
# on the shared source repo's ``.spawn.lock`` instead of rejecting — see
# ``resolve_spawn_workspace``. A duplicated literal (not an import of spawn.py's private
# constant) keeps dump→spawn decoupled while documenting the intended parity.
_WORKTREE_LOCK_WAIT = 120.0

TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


# ─── lazy paths ──────────────────────────────────────────────────────────────


def handoff_root() -> Path:
    """Resolve HANDOFF_HOME at call time so tests can monkeypatch the env var."""
    return _config.home_dir()


# ─── validation ──────────────────────────────────────────────────────────────


def validate_task_id(task_id: str) -> None:
    if not TASK_ID_RE.match(task_id):
        raise SystemExit(f"❌ task-id must be kebab-case (a-z 0-9 -). got: {task_id!r}")
    if len(task_id) > 60:
        raise SystemExit(f"❌ task-id too long ({len(task_id)} > 60): {task_id}")


def validate_project_slug(slug: str) -> None:
    if not TASK_ID_RE.match(slug):
        raise SystemExit(f"❌ project-slug must be kebab-case. got: {slug!r}")


# ─── small helpers ──────────────────────────────────────────────────────────


def run(cmd: list[str], cwd: Path, timeout: float = 10.0) -> str:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
        )
        return (r.stdout or "").strip()
    except Exception as e:
        return f"<error: {e}>"


def run_preflight_gates(cfg, *, workspace: Path, project: str, status: str) -> int:
    """Run project-configured ``dump_preflight_commands`` (generic 2C gate).

    Returns a non-zero exit code to BLOCK the dump, or 0 when no gate applies /
    all pass. FAIL-CLOSED: a gate that exits non-zero, times out, or cannot be
    launched blocks the dump. The engine is progress-agnostic — it only runs
    whatever the project configured (e.g. ``progress_pending.py --gate``).

    A spec runs only when ``project`` is in its ``projects`` list (empty list =
    every project) AND ``status`` is in its ``statuses``. The project filter
    matters because one ``$HANDOFF_HOME/config.json`` is SHARED by all projects
    under that home — a project-bound gate must not run for siblings.

    Skipped entirely for projects with no ``dump_preflight_commands`` config
    (zero impact on non-opted-in projects).
    """
    specs = getattr(cfg, "dump_preflight_commands", None) or []
    for spec in specs:
        if spec.projects and project not in spec.projects:
            continue
        if status not in spec.statuses:
            continue
        warn = spec.on_error == "warn"
        try:
            r = subprocess.run(
                spec.command,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=spec.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            if warn:
                print(
                    f"⚠️ [preflight:{spec.name}] timed out after {spec.timeout}s "
                    f"(on_error=warn → dump allowed, gate DEGRADED)",
                    file=sys.stderr,
                )
                continue
            print(
                f"❌ [preflight:{spec.name}] timed out after {spec.timeout}s "
                f"(fail-closed, dump blocked)",
                file=sys.stderr,
            )
            return 3
        except (OSError, subprocess.SubprocessError) as e:
            if warn:
                print(
                    f"⚠️ [preflight:{spec.name}] could not run {spec.command!r}: {e} "
                    f"(on_error=warn → dump allowed, gate DEGRADED)",
                    file=sys.stderr,
                )
                continue
            print(
                f"❌ [preflight:{spec.name}] could not run {spec.command!r}: {e} "
                f"(fail-closed, dump blocked)",
                file=sys.stderr,
            )
            return 3
        if r.returncode != 0:
            if (r.stdout or "").strip():
                print(r.stdout.rstrip(), file=sys.stderr)
            if (r.stderr or "").strip():
                print(r.stderr.rstrip(), file=sys.stderr)
            print(
                f"❌ [preflight:{spec.name}] exit {r.returncode} (fail-closed, dump blocked)",
                file=sys.stderr,
            )
            return r.returncode or 3
    return 0


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def load_manifest(batch_dir: Path) -> dict | None:
    """Read ``manifest.json``; mark the batch corrupted and return None on parse failure."""
    manifest_path = batch_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        atomic.atomic_create(batch_dir / "_corrupted")
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    return data


def assert_batch_alive(batch_dir: Path, stage: str) -> None:
    """v5.2 spawn-time invariant guarding against silently re-created shells.

    ``atomic.write_with_fsync`` calls ``mkdir(parents=True, exist_ok=True)``,
    which means an externally ``rm``-ed batch dir gets transparently rebuilt
    as an empty shell, leaving sub-task tabs running in an env-less husk.
    This helper aborts spawn the moment that happens so partial writes don't
    create orphans.
    """
    if not batch_dir.exists():
        raise SystemExit(
            f"❌ batch_dir 在 spawn 期消失 (stage={stage}): {batch_dir}\n"
            f"   batch_dir vanished mid-spawn; already-written sub-tasks may now be orphans.\n"
            f"   recover with `handoff dump --cleanup-orphan`."
        )
    if not (batch_dir / "manifest.json").exists():
        raise SystemExit(
            f"❌ manifest.json 在 spawn 期消失 (stage={stage}): {batch_dir}/manifest.json\n"
            f"   manifest vanished mid-spawn; already-written sub-tasks may now be orphans."
        )


def _run_retro_gate(
    args: argparse.Namespace,
    workspace: Path,
    project: str,
    cfg,
) -> retro_gate.GateResult | None:
    """Decide whether the v5.4 retro gate runs and return its verdict.

    Returns ``None`` when the gate is intentionally skipped (legacy mode):
    no ``--retro-evidence`` was passed and neither ``HANDOFF_RETRO_MANDATE``
    nor ``HANDOFF_RETRO_BYPASS`` is set. Batch-mode sub commands also skip
    the gate — fan-out / fan-in dumps are governed by their own protocol
    (manifest + role.env) and are out of scope for v5.4 Phase 4a.
    """
    if args.batch_done or args.batch_blocked or args.open_batch or args.batch_fan_in:
        return None

    evidence_path = Path(args.retro_evidence) if args.retro_evidence else None

    # Narrow terminal-status exemption: a ``done`` / ``blocked`` closure with NO
    # explicit evidence has no successor task — retro ("did you retro before the
    # NEXT task") and audit ("don't propagate defects to the next session") both
    # presuppose a successor. ``batch_done`` / ``batch_blocked`` are already exempt
    # above for the same reason; this extends it to plain terminal dumps so marking
    # a task done — or honestly reporting it blocked, when a stuck session may not
    # even be able to produce clean evidence — is never itself gated. NARROW (codex
    # R1): if ``--retro-evidence`` IS supplied (e.g. ``handoff audit-close
    # --status done``), fall through and validate it — an attested closure must not
    # silently skip validation.
    if args.status in ("done", "blocked") and evidence_path is None:
        return None

    bypass = os.environ.get("HANDOFF_RETRO_BYPASS") == "1"
    mandate = os.environ.get("HANDOFF_RETRO_MANDATE") == "1"
    audit_mandate = os.environ.get("HANDOFF_AUDIT_MANDATE") == "1"

    # Project-scoped mandate roll-out (R1 cross-project blast-radius mitigation):
    # a shared ``$HANDOFF_HOME/config.json`` may list ``mandate_projects`` (a NON-EMPTY
    # list of slugs — see config.py for the fail-closed parsing of empty/typo values).
    # When configured, only listed projects enforce the env mandate on a no-evidence
    # dump — unlisted siblings take the legacy path so routing the global dump entry
    # to the engine doesn't brick not-yet-migrated projects. NOT applied when
    # ``HANDOFF_RETRO_BYPASS`` is set (codex R2-P1): a bypass must always reach the gate
    # so its override.json validation + bypass-debt recording run, even for an unlisted
    # project. An explicit ``--retro-evidence`` likewise always runs the gate (handled
    # by the ``evidence_path is None`` guard) — opt-in evidence is never ignored.
    if (
        evidence_path is None
        and not bypass
        and getattr(cfg, "mandate_projects_configured", False)
        and project not in getattr(cfg, "mandate_projects", [])
    ):
        return None

    # A listed project is configured to expect the env mandate ON. Used both for the
    # §F#9 total-drift guard below and threaded to the gate for the partial-drift guard.
    audit_mandate_expected = bool(
        getattr(cfg, "mandate_projects_configured", False)
        and project in getattr(cfg, "mandate_projects", [])
    )

    # §F#9 silent-downgrade guard — TOTAL drift (policy B, owner-ruled): a listed
    # project expects the mandate ON, but BOTH env mandates are missing on a
    # no-evidence/no-bypass dump → it would silently take the legacy (no-gate) path
    # below. WARN + durable sentinel, then continue legacy (NON-fatal — a fail-closed
    # reject would break the config.py:564 "unset env to disable" escape hatch and could
    # brick the listed project). Fires only for listed projects (audit_mandate_expected).
    if (
        evidence_path is None
        and not bypass
        and not mandate
        and not audit_mandate
        and audit_mandate_expected
    ):
        retro_gate.write_mandate_drift_sentinel(
            project,
            args.task,
            workspace=workspace,
            classification="total_missing",
            retro_mandate=mandate,
            audit_mandate=audit_mandate,
            mandate_projects=getattr(cfg, "mandate_projects", []),
        )

    if evidence_path is None and not bypass and not mandate and not audit_mandate:
        # legacy path: no gate, preserve pre-v5.4 ERP shim behaviour
        return None

    sid, _ = resolve_session_id()
    return retro_gate.check_retro_gate(
        project=project,
        task=args.task,
        workspace=workspace,
        evidence_path=evidence_path,
        bypass_enabled=bypass,
        mandate_enabled=mandate,
        audit_mandate_enabled=audit_mandate,
        audit_mandate_expected=audit_mandate_expected,
        # ship-live closure gate (DEFAULT-ON / §13.6): additive + narrow (fires only on a
        # structured closeout_obligations.release=✅), with env / config / sentinel off-switches.
        closure_mandate_enabled=_closure_attestation_mandate_enabled(cfg, project),
        nonce=args.nonce,
        session_id=sid,
    )


def any_stop_auto(project: str, batch_id: str | None = None) -> str | None:
    """Multi-layer STOP check (global, project, batch). Returns triggered path or None."""
    root = handoff_root()
    paths = [
        root / "done",
        root / "STOP_AUTO",
    ]
    if project:
        paths.append(root / project / "STOP_AUTO")
    if batch_id and project:
        paths.append(root / project / "batches" / batch_id / "STOP")
    for p in paths:
        if p.exists():
            return str(p)
    return None


# ─── file_ownership (Gate A — physical collision check) ─────────────────────


def expand_ownership(spec: dict, workspace: Path) -> set[str]:
    """Resolve a file_ownership spec into a set of workspace-relative paths.

    Supported spec types: ``exact`` (one file), ``prefix`` (directory tree),
    ``glob`` (workspace-rooted glob). ``..`` is rejected; all resolved paths
    must stay inside ``workspace``.
    """
    raw_path = spec["path"]
    typ = spec["type"]

    if ".." in raw_path.split("/"):
        raise ValueError(f"file_ownership path contains '..': {raw_path}")

    ws_resolved = workspace.resolve()

    if typ == "exact":
        target = (workspace / raw_path).resolve()
        if not str(target).startswith(str(ws_resolved)):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return {str(target.relative_to(ws_resolved))}
    if typ == "prefix":
        if not raw_path.endswith("/"):
            raise ValueError(f"prefix must end with /: {raw_path}")
        target_dir = (workspace / raw_path.rstrip("/")).resolve()
        if not str(target_dir).startswith(str(ws_resolved)):
            raise ValueError(f"path escapes workspace: {raw_path}")
        if not target_dir.is_dir():
            return set()
        return {str(p.relative_to(ws_resolved)) for p in target_dir.rglob("*") if p.is_file()}
    if typ == "glob":
        return {str(p.relative_to(ws_resolved)) for p in workspace.glob(raw_path) if p.is_file()}
    raise ValueError(f"unknown ownership type: {typ}")


def validate_ownership_no_overlap(sub_tasks: list[dict], workspace: Path) -> None:
    """Pairwise file_ownership intersection check (v5 Gate A)."""
    expanded = []
    for st in sub_tasks:
        files: set[str] = set()
        for spec in st.get("file_ownership", []):
            files |= expand_ownership(spec, workspace)
        expanded.append((st["id"], files))
    for i in range(len(expanded)):
        for j in range(i + 1, len(expanded)):
            inter = expanded[i][1] & expanded[j][1]
            if inter:
                raise ValueError(
                    f"file_ownership overlap ({expanded[i][0]} ∩ {expanded[j][0]}): {inter}"
                )


def count_global_active_tabs() -> int:
    """Count `.uri` files across all projects that aren't yet `.done`/`.BLOCKED`."""
    root = handoff_root()
    if not root.exists():
        return 0
    n = 0
    for uri in root.glob("*/queue/*.uri"):
        task = uri.stem
        proj_queue = uri.parent
        if (proj_queue / f"{task}.done").exists():
            continue
        if (proj_queue / f"{task}.BLOCKED.md").exists():
            continue
        n += 1
    return n


# ─── baseline detection (extensible via config.baseline_hooks) ──────────────


def detect_baseline(workspace: Path, cfg: _config.Config | None = None, *, project: str) -> dict:
    # ``project`` is keyword-only + REQUIRED (not Optional-default) on purpose: a baseline
    # hook can be project-scoped (``HookSpec.projects``), so a caller that forgot to pass
    # the project must fail LOUD, not silently run an ERP-only hook (``docker compose exec
    # api alembic current``) against — and leak its output into — a sibling project's dump.
    git_head = run(["git", "rev-parse", "--short", "HEAD"], workspace)
    last_3_commits = run(["git", "log", "--oneline", "-3"], workspace)
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], workspace)
    baseline: dict = {
        "git_head": git_head or "(unknown)",
        "branch": branch or "main",
        "last_3_commits": last_3_commits,
    }
    if cfg is None:
        cfg = _config.load()
    for hook in cfg.baseline_hooks:
        # Project gate (mirror PreflightSpec): EMPTY ``projects`` = all projects (legacy);
        # a non-empty list runs the hook ONLY for the listed projects.
        if hook.projects and project not in hook.projects:
            continue
        raw = run(hook.command, workspace)
        if hook.regex:
            m = re.search(hook.regex, raw)
            baseline[hook.name] = m.group(1) if m else "(N/A)"
        else:
            baseline[hook.name] = raw
    return baseline


def get_roadmap_excerpt(cfg: _config.Config, project: str) -> str:
    rm = cfg.roadmap
    # Project gate: a roadmap scoped to other projects (e.g. ERP's accounting roadmap) is
    # NOT excerpted into this project's prompt. EMPTY ``projects`` = all (legacy).
    if rm.projects and project not in rm.projects:
        return "(no roadmap configured for this project)"
    if not rm.path:
        return "(no roadmap configured; set roadmap.path in config.json)"
    path = Path(rm.path).expanduser()
    if not path.exists():
        return f"(roadmap not found at {path})"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return "(roadmap unreadable)"
    matches = list(re.finditer(rm.section_regex, content, re.DOTALL))
    if matches:
        slice_ = matches[-rm.max_sections :]
        return "\n\n".join(m.group(0)[: rm.max_chars_per_section] for m in slice_)
    return content[-rm.fallback_tail_chars :]


# ─── single-pane (non-worktree) spawn workspace ─────────────────────────────


class CoordinatorSinglepaneError(Exception):
    """A coordinator relay's forced-singlepane workspace/sidecar could not be written.

    warmgap-B MUST-2 (owner 批 B / 2026-06-10 双脑 GREEN): for ``is_coordinator=True`` the
    singlepane artifacts are an ENGINE INVARIANT (中枢=独占红顶单栏窗), not UX polish — a
    write failure must FAIL CLOSED (abort before the ``.uri`` publish) and never fall back
    to the warm reuse-a-window path that produced the fourth red-top gap. ``main()``
    catches this and exits non-zero with a remedy."""


def maybe_write_singlepane_sidecar(
    cfg: _config.Config,
    project: str,
    task: str,
    workspace: Path,
    queue_dir: Path,
    *,
    worktree_active: bool,
    role: str,
    close_policy: str,
    spawn_nonce: str,
    predecessor_nonce: str | None = None,
    is_coordinator: bool = False,
) -> None:
    """Single-pane (non-worktree) spawn: if ``project`` opts in via ``singlepane_projects``
    and this is NOT a worktree spawn, generate an OUT-OF-TREE ``.handoff.code-workspace``
    (``folders`` → the real ``workspace``; ``window.title`` binds ``project·task·role·spawn_nonce``)
    under ``$HANDOFF_HOME/<project>/singlepane/`` and drop a ``queue/<task>.singlepane`` JSON
    sidecar. The watchdog opens the workspace file (cold-style ``code -n``) so the handoff-helper
    extension collapses both side bars on load (single editor pane) — guarded by the
    ``.handoff.code-workspace`` suffix — while the agent still works in the real repo (no isolation,
    today's concurrency). It is written OUT-OF-TREE (not in the repo) so it never dirties the tree.

    Phase 2 (spawn-window-unify R2 M1/M4): the ``window.title`` carries the unguessable
    ``spawn_nonce`` (via ``spawn_nonce.title_for``) so the watchdog can ATOMICALLY prove the front
    window is the exact one we launched (kills focus-drift TOCTOU) — substring ``contains`` in
    osascript. The task token is KEPT in the title for backward-compat with the existing task-match
    submit guard. The sidecar is now **JSON** (breaking migration from the old plain-path text):
    ``{workspace, role, close_policy, spawn_nonce, predecessor_nonce}`` — the watchdog reads
    ``workspace`` (open target) + ``spawn_nonce`` (atomic title gate), and ``role``/``close_policy``/
    ``predecessor_nonce`` drive role-gated autoclose downstream. The write + watchdog read + tests
    migrate together (the read side cannot ``cat`` a path out of JSON).

    §五·2 red-top (owner-caught gap 2026-06-10): ``is_coordinator`` (``handoff dump
    --coordinator``) red-tops THIS singlepane window too — wilde-hexe/sdgf/fb are singlepane
    projects, so before this their 中枢 dumps could never go red (the flag only reached the
    worktree path via ``create_worktree``), violating "EVERY coordinator window must be
    red-topped regardless of spawn path". Same 🧭中枢· prefix + red titleBar as
    ``worktree.inject_vscode_workspace``/``spawn._singlepane_workspace_json`` (shared
    ``worktree._COORDINATOR_*`` constants). The prefix WRAPS the nonce-bound title so the
    watchdog's substring nonce/task gates are untouched; the red-top VISUAL keys are not the
    coordinator inject-config block the THIN rule bans (gating stays in the repo's own
    ``.vscode``). A non-coordinator dump stays byte-identical (zero regression, golden-locked).

    warmgap-B MUST-1 (owner 批 B / 2026-06-10): ``is_coordinator=True`` on a NON-worktree
    spawn FORCES singlepane production regardless of ``singlepane_projects`` — the warm
    reuse-a-window path is structurally unable to honour "凡监管中枢窗口必须红顶 🧭 + 单栏"
    (no workspace file to bind a title/red-top/nonce to), so for a coordinator the opt-in
    config becomes irrelevant: 中枢=独占红顶单栏窗 is an engine invariant, not a config
    courtesy. The watchdog is sidecar-driven (not config-driven), so the existing singlepane
    consume chain (``code -n`` + nonce gate + bounded Enter retry) picks this up unchanged.

    Cleanup/no-op otherwise: a project that does NOT opt in, or a worktree spawn (which has
    its own ``.handoff.code-workspace`` and wins), gets the sidecar REMOVED so a stale opt-in
    can't linger across a config flip-off. Best-effort for a NON-coordinator — an OSError
    never bricks the dump (single-pane is UX polish; the warm submit still works without it).
    For a coordinator a write failure raises :class:`CoordinatorSinglepaneError` instead
    (MUST-2 fail-closed: never fall back warm; the caller aborts before the .uri publish).

    warmgap-B SHOULD: a coordinator sidecar additionally carries ``"is_coordinator": true``
    (observable semantics for the watchdog/兜底/cleanup). The key is added ONLY when true so
    every non-coordinator sidecar stays byte-identical (golden-locked)."""
    sidecar = queue_dir / f"{task}.singlepane"
    forced = is_coordinator and not worktree_active  # MUST-1: config cannot opt a 中枢 out
    # Step 6 config unification: the opt-in test now routes through the unified EFFECTIVE
    # isolation accessor. With the current live config (no ``worker_isolation``) it falls
    # through to legacy ``singlepane_projects`` membership → byte-identical; an explicit
    # ``multiwindow``/``worktree`` resolution → not singlepane → no sidecar (correct).
    if worktree_active or (not forced and cfg.resolve_isolation(project) != "singlepane"):
        sidecar.unlink(missing_ok=True)
        return
    try:
        sp_dir = cfg.home / project / "singlepane"
        sp_dir.mkdir(parents=True, exist_ok=True)
        ws_file = sp_dir / f"{task}.handoff.code-workspace"
        # project·task·role·nonce via the Phase-1 title_for so the watchdog can match
        # the front window by the unguessable spawn_nonce (osascript substring `contains`,
        # kills focus-drift TOCTOU). KEEP the task token (backward-compat task-match) + the
        # [singlepane] marker + the VS Code ${activeEditorShort} display variable (literal
        # here; VS Code expands ${...} at runtime).
        title = (
            _spawn_nonce.title_for(project=project, task_id=task, role=role, nonce=spawn_nonce)
            + " [singlepane]${separator}${activeEditorShort}"
        )
        settings: dict[str, object] = {
            "window.title": title,
            # Same declarative single-pane settings the worktree workspace uses (see
            # worktree.write_workspace_file): hide the activity bar (removes the empty
            # Claude sidebar focus competitor) + no Welcome tab. P0 THIN workspace — these
            # UX keys only, never a coordinator/inject config block.
            "workbench.activityBar.location": "hidden",
            "workbench.startupEditor": "none",
            "claudeCode.preferredLocation": "panel",
        }
        if is_coordinator:
            # §五·2: prefix WRAPS the nonce-bound title (substring gates intact) + the shared
            # red-titleBar spec (worktree._COORDINATOR_* — same constants as the worktree /
            # spawn.py succession paths, so every 中枢 window renders identically).
            settings["window.title"] = _worktree._COORDINATOR_TITLE_PREFIX + title
            settings["workbench.colorCustomizations"] = dict(_worktree._COORDINATOR_RED_TITLEBAR)
        # Step2 B 轨二: session-identity env signal — LAST key (byte-precise golden diff).
        # A coordinator dump keeps role="worker" in the SIDECAR (watchdog contract,
        # asserted below) but its SESSION role is supervisor_succession (see
        # worktree.session_env_osx).
        settings[_worktree.SESSION_ENV_SETTINGS_KEY] = _worktree.session_env_osx(
            role=role, task=task, is_coordinator=is_coordinator
        )
        ws_file.write_text(
            json.dumps(
                {
                    "folders": [{"path": str(workspace)}],
                    "settings": settings,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        # JSON sidecar (breaking migration from the old plain-path text): watchdog reads `workspace`
        # (open target) + `spawn_nonce` (atomic title gate); role/close_policy/predecessor_nonce feed
        # role-gated autoclose (worker → keep / supervisor_succession → close_predecessor).
        #
        # CONTRACT — MUST stay COMPACT SINGLE-LINE JSON (no ``indent=``). The watchdog reads this
        # sidecar with a line-oriented ``json_get`` (awk) in ``install/auto-continue.sh`` (bash, no
        # jq), AND the autoclose role/predecessor_nonce extraction relies on the flat one-line shape.
        # Pretty-printing this (``indent=2``) would risk silently breaking those reads → autoclose
        # fail-closes. The ``.handoff.code-workspace`` ABOVE is read by VS Code itself, so it may be
        # indented; THIS sidecar may not. Keep ``json.dumps(...)`` here without an ``indent`` kwarg.
        sidecar_payload: dict[str, object] = {
            "workspace": str(ws_file),
            "role": role,
            "close_policy": close_policy,
            "spawn_nonce": spawn_nonce,
            "predecessor_nonce": predecessor_nonce,
        }
        if is_coordinator:
            # warmgap-B SHOULD: observable coordinator marker (watchdog/兜底/cleanup 语义).
            # Added only when true → non-coordinator sidecars stay byte-identical.
            sidecar_payload["is_coordinator"] = True
        sidecar.write_text(
            # ← no indent= on purpose: single-line contract for the bash json_get reader
            json.dumps(sidecar_payload),
            encoding="utf-8",
        )
    except OSError as e:
        sidecar.unlink(missing_ok=True)
        if is_coordinator:
            # MUST-2 fail-closed: a 中枢 window without its singlepane artifacts would fall
            # to the warm path = the exact fourth-red-top-gap accident. Abort the dump (the
            # .uri publish never happens), never degrade silently.
            raise CoordinatorSinglepaneError(
                f"coordinator relay {project}/{task}: singlepane workspace/sidecar write "
                f"failed ({e}) — fail-closed, NOT falling back to a warm window. Fix the "
                f"filesystem issue (or use dx-spawn-session.sh --coordinator) and re-dump."
            ) from e
        print(f"[dump] (non-fatal) could not write singlepane workspace: {e}")


# ─── singlepane worker concurrency hard-REJECT (design §5.4 / R2 M5) ─────────


class SinglepaneBusy(Exception):
    """A ``singlepane``-isolation project already has an active worker / a concurrent
    spawn holds its project lock → a SECOND worker dispatch is REJECTED.

    Fail-closed by design (§5.4): a singlepane project shares ONE editor pane in the
    main tree, so two workers would clobber each other's files. The engine NEVER
    silently degrades to a concurrent main-dir spawn — it raises this instead, and the
    guard writes an owner-readable ``ack/<task>.singlepane_busy.txt`` before raising so a
    non-technical owner can see WHY the dispatch was refused (not just a stack trace).
    """

    def __init__(
        self, *, project: str, task: str, reason: str, holder_task: str | None = None
    ) -> None:
        self.project = project
        self.task = task
        self.reason = reason
        self.holder_task = holder_task
        msg = f"singlepane project {project!r}: worker spawn for {task!r} rejected — {reason}"
        if holder_task:
            msg += f" (pane held by active worker {holder_task!r})"
        super().__init__(msg)


def _active_singlepane_worker(cfg: _config.Config, project: str, *, exclude_task: str) -> str | None:
    """task_id of an existing ACTIVE singlepane worker for ``project`` other than
    ``exclude_task``, else ``None``.

    Active = a ``queue/<task>.singlepane`` sidecar whose ``<task>.uri`` is present and
    NON-terminal (no ``.done`` / ``.BLOCKED.md``) — the same file-based "active tab"
    signal as ``count_global_active_tabs`` (deterministic; no flaky pid probe). A
    terminal task's ``.uri`` is unlinked by ``write_active_dump``, so its pane frees up
    and a successor may spawn. ``exclude_task`` is the task being dumped now, so a worker
    re-publishing its OWN active dump never rejects itself.
    """
    queue = cfg.queue_dir(project)
    if not queue.exists():
        return None
    for sidecar in sorted(queue.glob("*.singlepane")):
        other = sidecar.stem
        if other == exclude_task:
            continue
        if not (queue / f"{other}.uri").exists():
            continue  # spawn not pending / its .uri was unlinked → pane not held by it
        if (queue / f"{other}.done").exists() or (queue / f"{other}.BLOCKED.md").exists():
            continue  # terminal → pane free
        return other
    return None


def _reject_singlepane(
    cfg: _config.Config, project: str, task: str, reason: str, holder: str | None = None
) -> SinglepaneBusy:
    """Write the owner-readable busy ack (best-effort), then RETURN the exception to raise.

    The raise is the HARD guarantee (caller fails closed); the ack is the soft, human-
    facing breadcrumb. A failed ack write must not mask the rejection, so it is swallowed.
    """
    try:
        ack_dir = cfg.ack_dir(project)
        ack_dir.mkdir(parents=True, exist_ok=True)
        (ack_dir / f"{task}.singlepane_busy.txt").write_text(
            f"task_id: {task}\n"
            f"project: {project}\n"
            f"reason: {reason}\n"
            + (f"held_by: {holder}\n" if holder else "")
            + f"time: {now_iso()}\n"
            "action: REJECTED — a singlepane project may have only ONE active worker. "
            "The existing worker window must finish (its task → done/blocked) before a "
            "new worker can spawn here; the engine refuses to spawn concurrently to avoid "
            "clobbering the live worker's files.\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    return SinglepaneBusy(project=project, task=task, reason=reason, holder_task=holder)


@contextlib.contextmanager
def singlepane_worker_guard(
    cfg: _config.Config, *, project: str, task: str, role: str = "worker"
):
    """Concurrency hard-gate for a ``singlepane``-isolation WORKER spawn (design §5.4).

    NO-OP unless the project's EXPLICIT ``worker_isolation`` is ``"singlepane"`` AND this
    is a ``worker`` dispatch (the central / ``supervisor_succession`` path is exempt — it
    replaces the predecessor, design §6). Otherwise it holds the project ``.spawn.lock``
    across the WHOLE active critical section (the body run inside the ``with``), so a
    concurrent worker #2 cannot slip its sidecar / ``.uri`` in between our check and
    publish. It REJECTS — fail-closed + owner-readable ack — when EITHER:
      * the project spawn lock is already held (a concurrent spawn in flight), OR
      * an active singlepane worker for a DIFFERENT task already occupies the pane.
    The same lock dir + TTL backs the autoclose critical section (§7: one lock, no
    sub-lock races). It NEVER degrades to a concurrent main-dir spawn.
    """
    if role != "worker" or cfg.worker_isolation_for(project) != "singlepane":
        yield
        return
    try:
        with project_spawn_lock(project, root=cfg.home):
            holder = _active_singlepane_worker(cfg, project, exclude_task=task)
            if holder is not None:
                raise _reject_singlepane(
                    cfg, project, task, "active singlepane worker present", holder
                )
            yield  # the active dump body runs here, still under the lock
    except LockHeld as e:
        raise _reject_singlepane(cfg, project, task, "concurrent spawn lock held") from e


# ─── role.env writing (used by sub-task / fan-in handoffs) ──────────────────


def write_role_env(
    env_path: Path,
    role: str,
    batch_id: str,
    workspace: Path,
    sub_task_id: str | None = None,
) -> None:
    """Write the role-env file that sub-task / fan-in tabs source on every Bash call."""
    guard_dir = git_guard_dir()
    lines = [
        "# handoff-fanout role env (sub-task / fan-in must source before any git op)",
        f"export HANDOFF_ROLE={role}",
        f"export HANDOFF_BATCH_ID={batch_id}",
    ]
    if sub_task_id:
        lines.append(f"export HANDOFF_SUB_TASK_ID={sub_task_id}")
    lines.append(f'export PATH="{guard_dir}:$PATH"')
    atomic.write_with_fsync(env_path, "\n".join(lines) + "\n")


# ─── URI encoding ───────────────────────────────────────────────────────────


def encode_short_prompt(project: str, task: str) -> str:
    root = handoff_root()
    short = (
        f"自动接续 / project=`{project}` / task=`{task}` — "
        f"open `{root}/{project}/queue/{task}.md` "
        f"and continue per the baseline + reading list."
    )
    return urllib.parse.quote(short)


def build_uri(cfg: _config.Config, project: str, task: str) -> str:
    encoded = encode_short_prompt(project, task)
    return cfg.uri_template.format(prompt=encoded)


_EXIT_FAIL_CLOSED = 2  # spawn.py parity — a fail-closed dump writes nothing publishable.


def _dump_anchor_decision(
    cfg: _config.Config,
    project: str,
    *,
    self_task: str | None = None,
    origin: str = _spawner_focus.ORIGIN_COORDINATOR,
    callsite: str = "dump",
) -> _spawner_focus.AnchorDecision:
    """Resolve the spawning coordinator's anchor EXACTLY ONCE (``$HANDOFF_WINDOW_FOCUS_PATH`` →
    env-independent self-id) and build the Step 4 :class:`~handoff_fanout.spawner_focus.AnchorDecision`
    (design §2.4). The dump command entry computes this once and threads the SAME object to every
    writer; a writer reached directly (the watchdog's fan-in / unit tests) computes its own via this
    helper — still single-parse for that path. NEVER raises."""
    return _spawner_focus.resolve_anchor_decision(
        os.getcwd(),
        cfg=cfg,
        home=cfg.home,
        project=project,
        self_task=self_task,
        origin=origin,
        env_focus_path=os.environ.get("HANDOFF_WINDOW_FOCUS_PATH"),
        callsite=callsite,
    )


def _anchor_gate(
    decision: _spawner_focus.AnchorDecision,
    *,
    cfg: _config.Config,
    project: str,
    task: str | None,
) -> int | None:
    """Step 4 fail-closed gate (design §2.4). Returns an exit code the caller must ``return`` BEFORE
    writing ANY artifact (so a blocked dispatch leaves no half-product ``.uri``), or ``None`` to
    proceed:

      * required coordinator dispatch + anchor MISS + enforce(block) → ``EXIT_FAIL_CLOSED`` + a clear
        指引 to stderr;
      * same but enforce(``dry_run``) → record the would-block (``LOG_BLOCK_INTENT``) + proceed (the
        ≥24-48h shadow buffer; behavior unchanged);
      * warn / a resolved anchor / a legitimate non-coordinator origin → ``None`` (existing fail-open).
    """
    if not (decision.required and decision.focus_line is None):
        return None
    if decision.enforcement == _spawner_focus.ENFORCE_BLOCK:
        print(
            "❌ [dump] cannot resolve the coordinator workspace dispatching this worker (anchor "
            f"{decision.miss_reason}). A singlepane coordinator must pass --self-task <its own task "
            "id>; a worktree coordinator must dump from its own cwd; if there is genuinely no "
            "coordinator desktop (manual / owner / cron / bootstrap) pass an explicit "
            "--origin {interactive|system}. No spawn artifacts were written (fail-closed).",
            file=sys.stderr,
        )
        return _EXIT_FAIL_CLOSED
    if decision.enforcement == _spawner_focus.ENFORCE_DRY_RUN:
        _spawner_focus.log_block_intent(
            home=cfg.home,
            project=project,
            task=task,
            cwd=os.getcwd(),
            origin=decision.origin,
            enforcement=decision.enforcement,
            reason="dump:anchor-unresolved",
        )
    return None


def _retrieval_pull_enforce_enabled(cfg: _config.Config, project: str) -> bool:
    """B1 (learning-loop component 6 / L1): is the retrieval-pull ENFORCE gate ON for
    ``project``? DEFAULT-OFF + fail-SAFE-OFF.

    ON iff the project (or ``"*"``) is in ``cfg.retrieval_pull_enforce_projects`` AND no
    kill-switch sentinel is present. The kill-switch —
    ``$HANDOFF_HOME/<project>/.retrieval-pull-enforce-off`` (per-project) or
    ``$HANDOFF_HOME/.retrieval-pull-enforce-off`` (fleet-wide) — is the one-key rollback: a
    fleet-wide misfire is disabled WITHOUT a config edit. Any filesystem error → OFF (never
    block a handoff because a sentinel ``stat`` raised)."""
    projects = getattr(cfg, "retrieval_pull_enforce_projects", [])
    if project not in projects and "*" not in projects:
        return False
    try:
        if (cfg.home / project / ".retrieval-pull-enforce-off").exists():
            return False
        if (cfg.home / ".retrieval-pull-enforce-off").exists():
            return False
    except OSError:
        return False
    return True


def _run_retrieval_pull_gate(
    args: argparse.Namespace, project: str, cfg: _config.Config
) -> int | None:
    """B1 retrieval-pull ENFORCE gate (learning-loop component 6 / L1). DEFAULT-OFF.

    A COORDINATOR ACTIVE handoff whose retro evidence carries NO
    ``predecessor_lesson_backref`` AND NO ``no_novel_lesson_attested`` disposition is
    REFUSED (``EXIT_RETRY``): the closing coordinator must read its predecessor's lesson and
    record a back-reference, or honestly attest there was nothing novel to learn. Returns an
    exit code the caller MUST ``return`` BEFORE any artifact is written (so a blocked handoff
    leaves no half-product), or ``None`` to proceed.

    Returns ``None`` (no-op, byte-identical to the pre-B1 path) when: the gate is disabled
    (default — empty enforce list / kill-switch present), the dump is not a coordinator
    ACTIVE handoff, there is no evidence to inspect, the requirement is already met, or the
    evidence is unreadable (fail-SAFE-OFF — never block on an I/O error)."""
    # Only a coordinator ACTIVE handoff spawns a successor that should pull the predecessor
    # lesson — a worker spawn / a terminal done|blocked close is out of scope.
    if args.status != "active" or not getattr(args, "coordinator", False):
        return None
    evidence_path = (
        Path(args.retro_evidence) if getattr(args, "retro_evidence", None) else None
    )
    if evidence_path is None:
        return None
    if not _retrieval_pull_enforce_enabled(cfg, project):
        return None
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # fail-SAFE-OFF: an unreadable evidence never blocks a handoff
    if not isinstance(payload, dict):
        return None
    backref = payload.get("predecessor_lesson_backref")
    has_backref = isinstance(backref, list) and bool(backref)
    disp = payload.get("lesson_disposition")
    attested = (
        isinstance(disp, dict) and disp.get("disposition") == "no_novel_lesson_attested"
    )
    if has_backref or attested:
        return None  # requirement met — predecessor lesson read+recorded, or honestly attested
    print(
        "❌ [dump] ERR-RETRY retrieval-pull-no-backref: this coordinator handoff did not read "
        "its predecessor's lesson. Read it, then re-dump with --predecessor-lesson-backref "
        "<lesson>=applied|superseded:<new>|not_relevant:<why> (one per predecessor lesson), "
        "OR — if there was genuinely no novel lesson to carry — --lesson-disposition "
        "no_novel_lesson_attested:<reason>. No artifacts were written (fail-closed). One-key "
        f"rollback: touch {cfg.home}/{project}/.retrieval-pull-enforce-off",
        file=sys.stderr,
    )
    return retro_gate.EXIT_RETRY


def _closeout_obligations_warn_enabled(cfg: _config.Config, project: str) -> bool:
    """Is the ``closeout_obligations`` WARN-mode advisory ON for ``project``? DEFAULT-OFF +
    fail-SAFE-OFF. (Byte-for-byte the same enable shape as
    :func:`_retrieval_pull_enforce_enabled`, only the config field + sentinel name differ — and
    "ON" here means "print an advisory", never "block".)

    ON iff the project (or ``"*"``) is in ``cfg.closeout_obligations_warn_projects`` AND no
    off-switch sentinel is present. The off-switch —
    ``$HANDOFF_HOME/<project>/.closeout-obligations-warn-off`` (per-project) or
    ``$HANDOFF_HOME/.closeout-obligations-warn-off`` (fleet-wide) — is the one-key rollback: a
    fleet-wide noisy advisory is silenced WITHOUT a config edit. Any filesystem error → OFF."""
    projects = getattr(cfg, "closeout_obligations_warn_projects", [])
    if project not in projects and "*" not in projects:
        return False
    try:
        if (cfg.home / project / ".closeout-obligations-warn-off").exists():
            return False
        if (cfg.home / ".closeout-obligations-warn-off").exists():
            return False
    except OSError:
        return False
    return True


def _closure_attestation_mandate_enabled(cfg: _config.Config, project: str) -> bool:
    """Is the ship-live closure_attestation gate ON for ``project``? DEFAULT-**ON** (ship-live is
    owner law, NOT opt-in — the opposite default from the warn/anchor lists above) + fail-SAFE.

    OFF iff ANY of (so a fleet-wide misfire has multiple one-key rollbacks):
      * env ``HANDOFF_CLOSURE_OFF=1``                              (fleet kill, no config edit)
      * ``config.json: closure_attestation_mandate: false``       (durable owner kill)
      * sentinel ``$HANDOFF_HOME/<project>/.closure-gate-off``     (per-project rollback)
      * sentinel ``$HANDOFF_HOME/.closure-gate-off``               (fleet-wide rollback)
      * the config was present-but-untrustworthy (``config_trusted=False``) — a BLOCKING gate must
        never run off an unparseable config (禁止 a corrupt config silently blocking handoffs).

    The gate itself is additive + narrow (rides the existing evidence path; fires only on a
    structured release=✅), so even ON it is a no-op for coordination / no-evidence dumps. Any
    unexpected error → OFF (fail-open: a blocking gate must never brick a handoff on its own bug).
    """
    try:
        if os.environ.get("HANDOFF_CLOSURE_OFF") == "1":
            return False
        if getattr(cfg, "closure_attestation_mandate", True) is False:
            return False
        if getattr(cfg, "config_trusted", True) is False:
            return False
        if (cfg.home / project / ".closure-gate-off").exists():
            return False
        if (cfg.home / ".closure-gate-off").exists():
            return False
    except OSError:
        return False
    except Exception:
        return False
    return True


def _run_closeout_obligations_gate(
    args: argparse.Namespace, project: str, cfg: _config.Config
) -> None:
    """closeout_obligations WARN-mode advisory (the third status-vector). WARN-ONLY: this
    function ALWAYS returns ``None`` and NEVER returns a blocking exit code — it only prints a
    non-blocking stderr advisory. The caller therefore does NOT check its return value (unlike
    the B1 retrieval-pull gate, which can block).

    A coordinator ACTIVE handoff whose retro evidence is MISSING the closeout vector gets an
    advisory to add ``--closeout-status``; one whose ``sedimentation_always`` is not ✅ gets an
    advisory that sedimentation should be done on every hop. Everything else (worker spawn /
    non-coordinator / no evidence / vector present + sedimentation ✅ / gate disabled) is silent.

    🔴 Q3 — "who verifies the honesty of an N/A (skip+reason)?" — is a CHOSEN design decision
    (owner-ratified, written here + in PROTOCOL §13.5): warn-mode v1 does NOT verify N/A
    honesty. An independent consumer that scrutinizes a suspicious ``release:skip`` is DEFERRED
    to enforce-mode (where it will mirror retrieval-pull: the next coordinator's §0 audit reads
    the predecessor's closeout vector surfaced into ``old_ready`` and can challenge it). The
    warn-mode v1 signal is simply that the vector becomes a VISIBLE artifact (folded into the
    hashed evidence + surfaced into old_ready + an advisory when absent). This is the
    intentional "right size" (freeze Case B + owner-chosen warn-first + simplicity-first: do
    NOT build an independent consumer in v1).

    DEFAULT-OFF (empty ``closeout_obligations_warn_projects``) → silent no-op, byte-identical to
    the pre-closeout path. Fail-SAFE-OFF: an unreadable / malformed evidence, a non-dict
    payload, or ANY unexpected error during the advisory logic → return ``None`` silently
    (a warn-only gate must NEVER crash the dump and thereby block a handoff)."""
    # Only a coordinator ACTIVE handoff has a closeout to advise on — a worker spawn / a
    # terminal done|blocked close is out of scope (mirrors the retrieval-pull gate's trigger).
    if args.status != "active" or not getattr(args, "coordinator", False):
        return None
    evidence_path = (
        Path(args.retro_evidence) if getattr(args, "retro_evidence", None) else None
    )
    if evidence_path is None:
        return None
    if not _closeout_obligations_warn_enabled(cfg, project):
        return None
    # Everything below is best-effort advisory: wrap it so NOTHING (a read error, a surprising
    # payload shape, a broken stderr) can raise out of a warn-only gate and block the handoff.
    try:
        try:
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None  # fail-SAFE-OFF: an unreadable evidence is never a blocker (or noisy)
        if not isinstance(payload, dict):
            return None
        closeout = payload.get("closeout_obligations")
        if not isinstance(closeout, dict) or not closeout:
            print(
                "⚠️ [dump] closeout-obligations-advisory (warn-mode advisory, non-blocking): "
                "this coordinator handoff's retro evidence carries no closeout_obligations "
                "vector. Consider recording one with --closeout-status "
                "<key>=<✅|skip:reason> (keys: sedimentation_always / audit / doc_mapping / "
                "release / sync_pipeline / postmortem; ✅ = done, skip:reason = N/A). "
                f"Silence: touch {cfg.home}/{project}/.closeout-obligations-warn-off",
                file=sys.stderr,
            )
            return None
        sed = closeout.get("sedimentation_always")
        sed_status = sed.get("status") if isinstance(sed, dict) else None
        if sed_status != "✅":
            print(
                "⚠️ [dump] closeout-obligations-advisory (warn-mode advisory, non-blocking): "
                f"sedimentation_always is {sed_status!r}, not ✅ — lesson + retro-evidence "
                "should be sedimented on EVERY coordinator handoff. "
                f"Silence: touch {cfg.home}/{project}/.closeout-obligations-warn-off",
                file=sys.stderr,
            )
        return None
    except Exception:  # noqa: BLE001 — a warn-only gate must never block a handoff by raising
        return None


def _spawner_focus_line(
    decision: _spawner_focus.AnchorDecision,
    *,
    home: Path,
    project: str,
    worker_task: str | None = None,
    isolation: str | None = None,
) -> str:
    """Emit the additive ``SPAWNER_FOCUS=<path>`` line from the ONCE-resolved ``decision`` — the
    SPAWNING coordinator's OWN ``.handoff.code-workspace`` so the watchdog/code-router runs the
    EXISTING one-step ``focus-jump`` and the worker is born on the coordinator's desktop.

    spawn-unification Step 4: this CONSUMES the pre-resolved :class:`AnchorDecision` and NEVER re-reads
    cwd/env/cfg (design §2.4/§2.5 — single resolution, no TOCTOU). The block/dry_run决策 already ran at
    the dump command entry (:func:`_anchor_gate`) BEFORE any artifact, so this helper only renders the
    line. FAIL-OPEN: no anchor → ``""`` (the ``.uri`` stays byte-identical to the pre-feature form,
    向后兼容) + record the Step 1 miss telemetry. Never raises — a UX hint must not block dump."""
    if decision.focus_line is None:
        # spawn-unification Step 1 (2026-06-22): no anchor → the .uri omits SPAWNER_FOCUS and
        # code-router.sh falls back to the static desktop map (wrong-desktop root cause). Behavior
        # UNCHANGED (byte-identical fail-open), but record the miss instead of staying silent.
        _spawner_focus.log_anchor_miss(
            home=home,
            project=project,
            task=worker_task,
            cwd=os.getcwd(),
            isolation=isolation or "dump",
            reason="dump:anchor-unresolved",
        )
        return ""
    return decision.focus_line


# ─── single-task dump (default mode) ────────────────────────────────────────


def write_active_dump(
    *,
    cfg: _config.Config,
    project: str,
    task: str,
    workspace: Path,
    next_brief: str,
    status: str,
    tests: str | None,
    baseline: dict,
    queue_dir: Path,
    osascript_subtitle: str | None = None,
    retro_evidence_path: Path | None = None,
    source_workspace: Path | None = None,
    old_head: str | None = None,
    worktree_info: dict | None = None,
    is_coordinator: bool = False,
    suppress_spawn_artifacts: bool = False,
    self_task: str | None = None,
    anchor_decision: _spawner_focus.AnchorDecision | None = None,
) -> int:
    # warmgap-C §1a: ``suppress_spawn_artifacts=True`` (Python-keyword-only, NEVER a CLI
    # flag — a public flag would be a legal bypass of the spawn-side G4 contract) keeps
    # the LEDGER half of an active dump (queue/<task>.md / BLOCKED supersede / .queued /
    # old_ready / pbcopy) and SKIPS the WINDOW-INTENT half (worktree resolution — see
    # main(), fix1 MUST-1 — / singlepane sidecar+workspace / coordinator memory
    # baseline / .uri publish / notification) —
    # the retro-gated ``audit-close --coordinator --status active`` succession route
    # publishes those via ``spawn --role supervisor_succession`` instead (codex_audit
    # ``_succession_relay``; the spawn writes the baseline itself, audit-close sends the
    # single notification — never a double 响). Default False = byte-identical v0
    # behavior for every caller (CLI / batch / fan-in / skill / tests).
    #
    # spawn-unification Step 4 (codex-RED #1 / sw-s4-fix): SELF-GATE at the writer boundary.
    # main() resolves + gates the AnchorDecision at the command entry and threads it in
    # (anchor_decision not None) — but the writer is the CONTRACT boundary (design §2.4: "4 writers
    # ... block 在任何产物前 return EXIT_FAIL_CLOSED"), so a writer reached DIRECTLY (the watchdog
    # fan-in / unit tests / any future caller bypassing main's entry) must gate ITSELF before the
    # FIRST artifact (.md / ack / sidecar / .uri) — otherwise an enforce+miss coordinator spawn writes
    # a half-product. Only the SPAWNING path needs it, matching main()'s exact gate predicate
    # (status=="active" AND not suppressed — a terminal done/blocked close unlinks the .uri = no spawn;
    # the suppressed succession ledger gates on the spawn side). When main already threaded a decision
    # in, we DON'T re-gate (it's already gated; re-running would double-log a dry_run block-intent).
    # Warn-mode (default, empty enforce list) → gate returns None → byte-identical (the disable-fix
    # guard test asserts this).
    if anchor_decision is None and status == "active" and not suppress_spawn_artifacts:
        anchor_decision = _dump_anchor_decision(cfg, project, self_task=self_task)
        gate_rc = _anchor_gate(anchor_decision, cfg=cfg, project=project, task=task)
        if gate_rc is not None:
            return gate_rc
    roadmap_excerpt = get_roadmap_excerpt(cfg, project)
    # ``workspace`` is the successor's tree (a worktree under isolation, else the
    # source tree); ``source_workspace`` is the closing session's tree used only for
    # the old_ready predecessor anchor (R1-C1). Default to identity for legacy callers.
    if source_workspace is None:
        source_workspace = workspace

    md_path = queue_dir / f"{task}.md"
    # singlepane self-continuation must carry --self-task <this-session's-task> so the
    # successor's engine can resolve the spawner anchor (Tier-2). The value is THIS
    # session's own task. Non-singlepane (worktree/default/unconfigured) → "" →
    # byte-identical handoff.md (worktree golden-locked path stays untouched).
    self_task_args = (
        f" --self-task {task}" if cfg.resolve_isolation(project) == "singlepane" else ""
    )
    handoff_content = templates.build_handoff_md(
        task=task,
        project=project,
        workspace=workspace,
        next_brief=next_brief,
        status=status,
        tests=tests,
        baseline=baseline,
        roadmap_excerpt=roadmap_excerpt,
        inject_blocks=cfg.inject_blocks_for(project),
        handoff_home=cfg.home,
        handoff_md_path=md_path,
        worktree_info=worktree_info,
        self_task_args=self_task_args,
    )
    # Crash-/kill-atomic single-task write (temp+os.replace). A supervisor kill
    # mid-dump must never leave a partial .md the launcher then misreads. The
    # atomic_replace temp name (`.{name}.tmp.<pid>.<ns>`) never matches the
    # launcher's `*.uri`/`*.md` globs (so an early WatchPaths wake on the temp is
    # a harmless no-op). NOT write_with_fsync (in-place O_TRUNC = durable but NOT
    # crash-atomic-replace). See docs/design-unlock-pivot-and-autoclose-removal §3.7.
    atomic.atomic_replace(md_path, handoff_content)
    print(f"[dump] wrote {md_path} ({len(handoff_content)} bytes)")

    if status == "done":
        (queue_dir / f"{task}.done").touch()
        (queue_dir / f"{task}.uri").unlink(missing_ok=True)
        # Terminal state: stop the heartbeat from outliving the task. A leaked
        # heartbeat keeps ticking stale and watchdog mode 6 mis-flags the done
        # task as 529-suspected.
        (queue_dir / f"{task}.heartbeat").unlink(missing_ok=True)
        print(f"[dump] ✅ {project}/{task} marked done")
        return 0

    if status == "blocked":
        blocked_file = queue_dir / f"{task}.BLOCKED.md"
        blocked_file.write_text(
            templates.build_blocked_md(
                project=project,
                task=task,
                head=baseline.get("git_head", "(unknown)"),
                reason=osascript_subtitle or "",
            ),
            encoding="utf-8",
        )
        (queue_dir / f"{task}.uri").unlink(missing_ok=True)
        # Terminal state — same heartbeat cleanup as the done path.
        (queue_dir / f"{task}.heartbeat").unlink(missing_ok=True)
        print(f"[dump] ⛔ BLOCKED written to {blocked_file}")
        _notify(osascript_subtitle or task, f"自动接续 / {project}", task, sound="Basso")
        return 0

    # A successful active dump SUPERSEDES any prior terminal BLOCKED.md for this task
    # (dual-brain P0): the worktree merge-back gate / a same-task collision writes
    # <task>.BLOCKED.md + returns 2; once the session publishes + re-dumps active, a
    # stale BLOCKED.md would make the launcher (auto-continue.sh: `[ -f
    # <task>.BLOCKED.md ] && continue`) skip this valid .uri → the relay STALLS.
    (queue_dir / f"{task}.BLOCKED.md").unlink(missing_ok=True)

    # active: prepare all sidecars FIRST, then publish the .uri trigger LAST.
    # CRITICAL ORDERING (codex+Gemini R2): the .uri sidecar is the launchd WatchPaths
    # trigger — the instant it lands, the launcher spawns the next session, whose §0
    # audit immediately reads ack/<task>.old_ready. So .uri MUST be written AFTER
    # .queued AND .old_ready (the latter runs a slow `git rev-parse`); otherwise the
    # new session can race ahead of old_ready and false-BLOCK. Everything below the
    # "publish" line is the trigger; everything above is preparation.
    uri = build_uri(cfg, project, task)
    uri_path = queue_dir / f"{task}.uri"

    # ack/<task>.queued — early "dump ran, awaiting spawn" breadcrumb (parity with
    # the pre-A4 standalone global). The launcher writes .spawned/.submitted/.failed
    # afterward; the spawn-new-session skill's Step 5 poll uses .queued to tell
    # "dump ran, launchd is slow" apart from "dump never ran". Best-effort.
    try:
        ack_dir = cfg.ack_dir(project)
        ack_dir.mkdir(parents=True, exist_ok=True)
        (ack_dir / f"{task}.queued").write_text(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"workspace={workspace}\nstatus={status}\n",
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[dump] (non-fatal) could not write queued ack: {e}")

    # §7.6 — write ack/<task>.old_ready when retro evidence drove the dump. MUST be
    # before the .uri publish so the next session's §0 audit sees it (see ordering note).
    # ``source_workspace`` + ``old_head`` keep the predecessor anchor on the CLOSING
    # session's tree even when ``workspace`` is the successor's worktree (R1-C1).
    if retro_evidence_path is not None:
        old_ready_path = _write_old_ready(
            project=project,
            task=task,
            workspace=source_workspace,
            evidence_path=retro_evidence_path,
            ack_dir=cfg.ack_dir(project),
            home=cfg.home,
            commit_hash=old_head,
        )
        if old_ready_path is None:
            # The gate passed with an evidence file, yet old_ready couldn't be
            # written (evidence vanished / unreadable between the gate and here).
            # Don't fail the already-published dump, but make it loud: without
            # this artifact the §0 new-session audit can't verify this session.
            print(
                "[dump] ⚠️  retro evidence supplied but old_ready was NOT written "
                "(evidence vanished/unreadable); §0 new-session audit can't verify "
                f"{project}/{task}"
            )

    # ack/<task>.worktree — record the worktree (path/branch/base/integration) so
    # prune/gc can find + reclaim it, and the §0/fan-in steps can trace it. Written
    # BEFORE the .uri publish (same ordering rule as old_ready). Only for a CREATED
    # worktree; degrade/report leave no sidecar.
    if worktree_info and worktree_info.get("status") == _worktree.ST_CREATED:
        try:
            ack_dir = cfg.ack_dir(project)
            ack_dir.mkdir(parents=True, exist_ok=True)
            # R2 P2-H (+ R5): crash-atomic via temp + os.replace (atomic_replace), NOT
            # write_with_fsync — the latter is in-place O_TRUNC (durable but a kill mid-
            # write leaves partial JSON the GC would mis-parse). atomic_replace's temp
            # name never matches a launcher glob, so the pre-.uri ordering is safe.
            atomic.atomic_replace(
                ack_dir / f"{task}.worktree",
                json.dumps(
                    {**worktree_info, "source_workspace": str(source_workspace)},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
        except OSError as e:
            print(f"[dump] (non-fatal) could not write .worktree sidecar: {e}")

    # Single-pane (non-worktree) spawn workspace + sidecar (before the .uri publish, same
    # ordering rule as the other sidecars). Skipped when this is a CREATED worktree (the
    # worktree has its own .handoff.code-workspace and wins — and already carries the §五·2
    # red-top via create_worktree(is_coordinator=...) on that path).
    if not suppress_spawn_artifacts:
        maybe_write_singlepane_sidecar(
            cfg,
            project,
            task,
            workspace,
            queue_dir,
            worktree_active=bool(
                worktree_info and worktree_info.get("status") == _worktree.ST_CREATED
            ),
            # E3 role taxonomy: the single-task active relay is the SOLO chain, not a true
            # worker (batch sub-task / fan-in keep role="worker" at their own call sites).
            # A coordinator dump keeps role="worker" in the sidecar — the watchdog contract
            # asserted by the red-top tests — its coordinator semantics ride in the env
            # signal (supervisor_succession) + the is_coordinator sidecar marker.
            role=("worker" if is_coordinator else ROLE_SOLO),
            close_policy="keep",
            spawn_nonce=_spawn_nonce.new_nonce(),
            is_coordinator=is_coordinator,
        )

    # Step2 契约 A (G3): a coordinator dispatch — BOTH the worktree and the singlepane
    # relay land here — records the dispatch-time memory snapshot baseline before the
    # .uri publish (same ordering rule as the other sidecars). The successor's own
    # relay (`audit-close --coordinator --self-task <this task>`) compares against it.
    # ``source_workspace`` (the real project tree, not a successor worktree) locates
    # the project memory dir. Best-effort by contract: never bricks the dump.
    if is_coordinator and not suppress_spawn_artifacts:
        _memory_baseline.write_baseline(
            home=cfg.home,
            project=project,
            coordinator_task=task,
            workspace=source_workspace,
        )

    _maybe_pbcopy(handoff_content)

    if suppress_spawn_artifacts:
        # warmgap-C §1a: ledger-only close — NO window intent was published (no sidecar /
        # baseline / .uri / notification). The caller (audit-close succession route) now
        # owns publishing it via `spawn --role supervisor_succession`, or failing CLOSED.
        print(
            f"[dump] ✅ ledger dump complete for {project}/{task} "
            "(spawn artifacts suppressed — succession spawn publishes the window intent)"
        )
        return 0

    # ── PUBLISH: write the .uri trigger LAST (all sidecars now exist) ────────────
    # §3.7 — atomic .uri write (see the .md note above). direct-jump-spawn: append the
    # SPAWNER_FOCUS line when this dump runs in a coordinator terminal (fail-open → "").
    # Step 4: consume the SINGLE pre-resolved decision — main() threaded it in, else the top-of-
    # function self-gate resolved it (so it is never None on this spawning publish path); no re-read.
    # place-role-explicit-contract (2026-06-29): stamp the explicit ROLE= the launcher tiles by.
    # ``is_coordinator`` is the cold-start coordinator case (DEFECT#1): the singlepane sidecar above
    # records role="worker" for it (the watchdog red-top contract), so the ROLE= line — derived
    # straight from is_coordinator — is the ONLY place that carries the true coord identity to the
    # launcher's placement (→ ROLE=coord → right-half). A non-coordinator dump → ROLE=worker.
    atomic.atomic_replace(
        uri_path,
        f"WORKSPACE={workspace}\nURI={uri}\nROLE={'coord' if is_coordinator else 'worker'}\n"
        f"{_spawner_focus_line(anchor_decision, home=cfg.home, project=project, worker_task=task, isolation='worktree' if worktree_info else 'singlepane')}",
    )
    print(f"[dump] wrote {uri_path}")

    _notify(next_brief, f"自动接续 / {project}", task)
    print(f"[dump] ✅ active dump complete for {project}/{task}")
    return 0


def _write_old_ready(
    *,
    project: str,
    task: str,
    workspace: Path,
    evidence_path: Path,
    ack_dir: Path,
    home: Path,
    commit_hash: str | None = None,
) -> Path | None:
    """Write ``ack/<task>.old_ready`` per spec §7.6.

    Only invoked when the retro gate ran with an evidence file and passed. The
    artifact is **audit metadata** read by the §0 new-session predecessor audit
    and the Phase C/D codex-audit gate (it carries ``retro_evidence_hash`` +
    ``codex_audit_hash`` / ``codex_audit_mode`` / ``next_session_forced_task``),
    so a new session can verify the prior session actually closed + audited.
    (Historically it also drove the v4 tab-autoclose watcher, now removed — the
    artifact stays because the audit/retro-mandate chain depends on it.) Returns
    the written path (or ``None`` if the evidence file vanished between the gate
    check and this call).
    """
    if not evidence_path.exists():
        return None
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    sid, sid_kind = resolve_session_id()
    # Prefer the explicitly captured closing-session HEAD (R1-C1): under worktree
    # isolation ``workspace`` is the source tree, but capturing the SHA before any
    # substitution removes any doubt the anchor reflects the predecessor.
    commit_hash = commit_hash or run(["git", "rev-parse", "HEAD"], workspace) or "(unknown)"

    project_root = (home / project).resolve()
    try:
        rel_path = str(evidence_path.resolve().relative_to(project_root))
    except ValueError:
        # Evidence file lives outside the project's handoff home (unusual but
        # legal for tests or hand-curated paths). Fall back to the absolute
        # path so the watcher can still resolve it.
        rel_path = str(evidence_path.resolve())

    phase0 = payload.get("phase0", {}) if isinstance(payload.get("phase0"), dict) else {}
    tests_entry = phase0.get("tests", {}) if isinstance(phase0.get("tests"), dict) else {}
    memory_entry = phase0.get("memory", {}) if isinstance(phase0.get("memory"), dict) else {}

    old_ready = {
        "schema_version": OLD_READY_SCHEMA_VERSION,
        "task_id": task,
        "nonce": payload.get("nonce", "") if isinstance(payload.get("nonce", ""), str) else "",
        "session_id": sid,
        "session_id_kind": sid_kind,
        "commit_hash": commit_hash,
        "push_completed_at": now_iso(),
        "tests_passed": tests_entry.get("status") == "✅",
        "memory_updated": memory_entry.get("status") == "✅",
        "dump_success": True,
        "retro_evidence_hash": compute_retro_evidence_hash(evidence_path),
        "retro_evidence_path": rel_path,
        "retro_evidence_path_absolute": str(evidence_path.resolve()),
    }

    # Phase C — surface the codex audit block so the next session's §0 audit can
    # verify it. ``codex_audit_hash`` lets it detect a
    # tampered block; ``next_session_forced_task`` is set ONLY for a bypass
    # (the next session owes the skipped audit, spec §1.3). Non-bypass modes
    # impose no forced task. Lazy import: codex_audit imports dump (in
    # main_audit_close) so a top-level import here would risk a cycle.
    codex_audit = payload.get("codex_audit")
    if isinstance(codex_audit, dict):
        from handoff_fanout import codex_audit as _ca

        old_ready["codex_audit_hash"] = _ca.compute_codex_audit_hash(codex_audit)
        mode = codex_audit.get("audit_mode")
        if isinstance(mode, str):
            old_ready["codex_audit_mode"] = mode
        forced = _ca.forced_follow_up_task(codex_audit)
        if forced is not None:
            old_ready["next_session_forced_task"] = forced
        # Cross-repo anchor (gap 3): for a dual-repo task (audited code lives in a
        # repo distinct from this workspace, declared via audit-close --code-repo),
        # the codex_audit block carries the already-validated code_repo (abs path)
        # + code_repo_head (sha). commit_hash above only anchors the WORKSPACE HEAD,
        # so without this the §0 new-session audit can't trace the engine-side
        # commit. Surface it as optional additive metadata — absent for same-repo
        # tasks (the common case), so old_ready stays byte-stable there. No schema
        # bump: old_ready is unhashed, and the value is copied from the already
        # schema-versioned codex_audit block, not recomputed.
        code_repo = codex_audit.get("code_repo")
        code_repo_head = codex_audit.get("code_repo_head")
        if (
            isinstance(code_repo, str)
            and code_repo
            and isinstance(code_repo_head, str)
            and code_repo_head
        ):
            old_ready["code_repo"] = code_repo
            old_ready["code_repo_head"] = code_repo_head

    # retrieval-pull (L1): surface the closing session's predecessor_lesson_backref
    # so the §0 new-session audit + the fleet learning canary can read it without
    # re-parsing the evidence file. Additive-when-present: a non-list / absent value
    # is ignored → old_ready stays byte-stable for the common case. old_ready is
    # unhashed; the value is copied from the already-hashed evidence, not recomputed.
    backref = payload.get("predecessor_lesson_backref")
    if isinstance(backref, list) and backref:
        old_ready["predecessor_lesson_backref"] = backref

    # component 5 (L2): surface the closing session's lesson_disposition so the fleet
    # learning canary can read it without re-parsing the evidence file. Additive-when-
    # present: a non-dict / absent value is ignored → old_ready stays byte-stable for
    # the common case. old_ready is unhashed; the value is copied from the already-
    # hashed evidence, not recomputed.
    lesson_disposition = payload.get("lesson_disposition")
    if isinstance(lesson_disposition, dict) and lesson_disposition:
        old_ready["lesson_disposition"] = lesson_disposition

    # closeout_obligations (third vector): surface the closing session's closeout vector so the
    # §0 new-session audit (and a future enforce-mode independent consumer — Q3, PROTOCOL §13.5)
    # can read it without re-parsing the evidence. Additive-when-present: a non-dict / absent /
    # empty value is ignored → old_ready stays byte-stable for the common case. old_ready is
    # unhashed; the value is copied from the already-hashed evidence, not recomputed.
    closeout_obligations = payload.get("closeout_obligations")
    if isinstance(closeout_obligations, dict) and closeout_obligations:
        old_ready["closeout_obligations"] = closeout_obligations

    # closure_attestation (the「闭环证书」): surface the closing session's binding(s) so the next
    # §0 audit can challenge a suspicious release=skip (the same successor-challenge posture
    # closeout has — 同信任域 visibility, PROTOCOL §13.6). Additive-when-present: a non-list /
    # absent / empty value is ignored → old_ready stays byte-stable for the common case.
    closure_attestation = payload.get("closure_attestation")
    if isinstance(closure_attestation, list) and closure_attestation:
        old_ready["closure_attestation"] = closure_attestation

    ack_dir.mkdir(parents=True, exist_ok=True)
    out = ack_dir / f"{task}.old_ready"
    atomic.write_with_fsync(
        out,
        json.dumps(old_ready, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )
    print(f"[dump] wrote {out}")
    return out


def _maybe_pbcopy(content: str) -> None:
    """Copy handoff content to the macOS clipboard, with two safety guards.

    Skips the real ``pbcopy`` call when either env var is *present* in
    ``os.environ`` (presence is the contract — empty string still skips):

      * ``PYTEST_CURRENT_TEST`` — auto-set by pytest for each running test
        (including pytest-xdist worker subprocesses); prevents
        fixture-tmpdir handoff content (e.g. ``project=demo``) from
        silently hijacking the user's clipboard during a test run.
      * ``HANDOFF_NO_PBCOPY`` — manual opt-out for CI, headless sessions,
        or any caller that wants the side effect suppressed.

    Key-presence (``in os.environ``) rather than truthiness so callers
    can use ``HANDOFF_NO_PBCOPY=`` (empty) and still suppress, matching
    the documented "set the env var to skip" contract.

    The original ``FileNotFoundError`` / ``OSError`` swallow is preserved
    so non-macOS hosts (no ``pbcopy`` binary) keep working.
    """
    if "PYTEST_CURRENT_TEST" in os.environ or "HANDOFF_NO_PBCOPY" in os.environ:
        return
    try:
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        p.communicate(input=content.encode("utf-8"))
    except (FileNotFoundError, OSError):
        pass


def _notify(message: str, title: str, subtitle: str, sound: str | None = None) -> None:
    """Best-effort macOS notification (no-op on other platforms)."""
    osa = f'display notification "{message}" with title "{title}" subtitle "{subtitle}"'
    if sound:
        osa += f' sound name "{sound}"'
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        subprocess.run(["osascript", "-e", osa], check=False, timeout=5)


# ─── batch open (fan-out) ───────────────────────────────────────────────────


def handle_open_batch(
    args,
    cfg: _config.Config,
    workspace: Path,
    project: str,
    queue_dir: Path,
    anchor_decision: _spawner_focus.AnchorDecision | None = None,
) -> int:
    manifest_input = Path(args.open_batch)
    if not manifest_input.exists():
        raise SystemExit(f"❌ --open-batch file not found: {manifest_input}")
    try:
        manifest = json.loads(manifest_input.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"❌ manifest JSON parse failed: {e}") from e

    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit(f"❌ schema_version must = {SCHEMA_VERSION}")
    batch_id = manifest["batch_id"]
    if not TASK_ID_RE.match(batch_id):
        raise SystemExit(f"❌ batch_id must be kebab-case: {batch_id}")
    sub_tasks = manifest.get("sub_tasks", [])
    if not sub_tasks or len(sub_tasks) < 2:
        raise SystemExit(f"❌ need ≥2 sub_tasks (got {len(sub_tasks)})")
    if len(sub_tasks) > SUB_TASK_N_MAX:
        raise SystemExit(
            f"❌ sub_tasks N={len(sub_tasks)} > N_max={SUB_TASK_N_MAX} (spawn-storm guard)"
        )

    active = count_global_active_tabs()
    if active + len(sub_tasks) > GLOBAL_ACTIVE_LIMIT:
        raise SystemExit(
            f"❌ active tabs {active} + batch {len(sub_tasks)} > "
            f"GLOBAL_ACTIVE_LIMIT={GLOBAL_ACTIVE_LIMIT}"
        )

    for st in sub_tasks:
        if st.get("depends_on"):
            raise SystemExit(f"❌ depends_on must be [] in v5 (violator: {st['id']})")

    try:
        validate_ownership_no_overlap(sub_tasks, workspace)
    except ValueError as e:
        raise SystemExit(f"❌ Gate A failed: {e}") from e

    # spawn-unification Step 4 (codex-RED #1 / sw-s4-fix): SELF-GATE at the writer boundary. main()
    # resolves + gates the decision and threads it in (anchor_decision not None); a DIRECT caller
    # (unit tests / any future caller bypassing main's entry) must gate ITSELF BEFORE creating
    # batch_dir / manifest.json / any sub-task .md/.uri, so an enforce+miss fan-out leaves NO
    # half-product (this whole path always spawns sub-task .uris — no terminal variant — so the gate
    # is unconditional when self-resolving). Warn-mode (default) → gate returns None → byte-identical.
    if anchor_decision is None:
        anchor_decision = _dump_anchor_decision(cfg, project, self_task=getattr(args, "self_task", None))
        gate_rc = _anchor_gate(anchor_decision, cfg=cfg, project=project, task=batch_id)
        if gate_rc is not None:
            return gate_rc

    batch_dir = handoff_root() / project / "batches" / batch_id
    if batch_dir.exists():
        raise SystemExit(f"❌ batch_dir already exists: {batch_dir}")
    batch_dir.mkdir(parents=True, exist_ok=False)

    manifest["created_at"] = now_iso()
    manifest.setdefault("timeout_hours", 3)
    atomic.write_with_fsync(
        batch_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    print(f"[open-batch] {batch_dir}/manifest.json written")

    baseline = detect_baseline(workspace, cfg=cfg, project=project)
    roadmap_excerpt = get_roadmap_excerpt(cfg, project)
    # mp-locate-return: resolve the coordinator's own focus PATH ONCE and reuse for every sub-task .uri
    # (it's the same coordinator window for all sub-tasks).
    # spawn-unification Step 1 (sw-spawn-unify-s1fix / codex #2): because the focus resolves ONCE for
    # the whole batch, this records AT MOST ONE anchor-miss per batch (keyed on ``batch_id``), not one
    # per sub-task .uri — intentional, since the miss is a property of the SHARED coordinator anchor,
    # not of each sub-task. The AUTHORITATIVE per-worker fallback canary is the ENGINE's per-produce
    # log (``spawn.run_spawn`` calls ``log_anchor_miss`` once per actual ``handoff spawn``), so this
    # coarse batch-level record is a convenience signal, not the canary count — no per-uri undercount.
    # Step 4: consume the SINGLE pre-resolved decision — main() threaded it in, else the self-gate
    # above resolved it (never None here); no re-read (design §2.4/§2.5 single-parse, TOCTOU-free).
    _focus_line = _spawner_focus_line(
        anchor_decision,
        home=cfg.home,
        project=project,
        worker_task=batch_id,
        isolation="singlepane",
    )

    for idx, st in enumerate(sub_tasks):
        sub_id = st["id"]
        if not TASK_ID_RE.match(sub_id):
            raise SystemExit(f"❌ sub-task id must be kebab-case: {sub_id}")

        assert_batch_alive(batch_dir, stage=f"pre-env[{sub_id}]")
        env_path = batch_dir / f"{sub_id}.env"
        write_role_env(env_path, HANDOFF_ROLE_SUB_TASK, batch_id, workspace, sub_id)

        content = templates.build_sub_task_handoff_md(
            task=sub_id,
            project=project,
            workspace=workspace,
            next_brief=st["brief"],
            batch_id=batch_id,
            sub_task_id=sub_id,
            file_ownership=st["file_ownership"],
            baseline=baseline,
            roadmap_excerpt=roadmap_excerpt,
            inject_blocks=cfg.inject_blocks_for(project),
            handoff_home=cfg.home,
            git_guard_path=git_guard_dir(),
        )
        # Launcher-visible: written with atomic_replace (temp + os.replace), NOT
        # write_with_fsync — the launchd WatchPaths watcher tails queue/*.uri and
        # the spawned session reads queue/*.md, so an in-place O_TRUNC window
        # would expose a torn read. Same rationale as the single-task path
        # (write_active_dump §3.7).
        atomic.atomic_replace(queue_dir / f"{sub_id}.md", content)

        if idx > 0:
            print(f"[open-batch]   stagger sleep {STAGGER_SPAWN_SECONDS}s ...")
            time.sleep(STAGGER_SPAWN_SECONDS)

        assert_batch_alive(batch_dir, stage=f"pre-uri[{sub_id}]")
        if not env_path.exists():
            raise SystemExit(f"❌ env vanished mid-spawn ({sub_id}): {env_path}")

        # Single-pane (non-worktree): open-batch sub-tasks run on the main repo (codex R2),
        # so they qualify for the single-pane window like the single-task path.
        maybe_write_singlepane_sidecar(
            cfg,
            project,
            sub_id,
            workspace,
            queue_dir,
            worktree_active=False,
            role="worker",
            close_policy="keep",
            spawn_nonce=_spawn_nonce.new_nonce(),
        )
        uri = build_uri(cfg, project, sub_id)
        # Launcher-visible trigger — atomic_replace (see the .md note above). direct-jump-spawn:
        # append the SPAWNER_FOCUS line when dumped from a coordinator terminal (fail-open → "").
        # place-role-explicit-contract: a fan-out sub-task is always a worker → ROLE=worker.
        atomic.atomic_replace(
            queue_dir / f"{sub_id}.uri",
            f"WORKSPACE={workspace}\nURI={uri}\nROLE=worker\n{_focus_line}",
        )
        print(f"[open-batch]   sub-task {sub_id} (#{idx + 1}/{len(sub_tasks)}) written")

    print(f"[open-batch] ✅ batch {batch_id} opened with {len(sub_tasks)} sub-tasks")
    _notify(
        f"batch {batch_id}: {len(sub_tasks)} sub-tasks launching",
        f"v5 fan-out / {project}",
        batch_id,
    )
    return 0


def trigger_fan_in_if_ready(
    project: str,
    workspace: Path,
    batch_id: str,
    queue_dir: Path,
    cfg: _config.Config | None = None,
    self_task: str | None = None,
    decision: _spawner_focus.AnchorDecision | None = None,
) -> bool:
    """If all sub-tasks have ``.done``/``.blocked``, atomic-create the trigger and dump fan-in."""
    if cfg is None:
        cfg = _config.load()
    batch_dir = handoff_root() / project / "batches" / batch_id
    manifest = load_manifest(batch_dir)
    if not manifest:
        print(f"[trigger-fan-in] manifest missing/corrupt: {batch_dir}")
        return False

    expected = {st["id"] for st in manifest["sub_tasks"]}
    done_set = {f.stem for f in batch_dir.glob("*.done")} & expected
    blocked_set = {f.stem for f in batch_dir.glob("*.blocked")} & expected

    raw_done = {f.stem for f in batch_dir.glob("*.done")}
    unknown = raw_done - expected - SPECIAL_MARKERS
    if unknown:
        print(f"⚠️  unknown .done files (not in expected_ids): {unknown}", file=sys.stderr)

    finished = done_set | blocked_set
    if finished < expected:
        print(
            f"[trigger-fan-in] incomplete: done={done_set} blocked={blocked_set} "
            f"missing={expected - finished}"
        )
        return False

    # spawn-unification Step 4: the fan-in .uri triggers a NEW window (design §2.4 "fan-in 不例外"),
    # so a coordinator anchor-miss under enforce must fail-closed HERE — BEFORE _fanin_triggered and
    # any fan-in artifact — so the sub-tasks' .done markers are preserved and the fan-in stays
    # re-dispatchable once the anchor resolves. Direct callers (watchdog) compute their own decision
    # (single-parse for that path). Warn-mode (default) → gate is a no-op → byte-identical.
    fan_in_task = manifest["fan_in_task"]
    if decision is None:
        decision = _dump_anchor_decision(cfg, project, self_task=self_task, callsite="fan-in")
    if _anchor_gate(decision, cfg=cfg, project=project, task=fan_in_task) is not None:
        # Step 4 (codex #3 / sw-s4-fix): a fail-closed fan-in refusal must be MACHINE-DISTINGUISHABLE
        # from the other False returns (incomplete / sibling-already-triggered). We keep the bool
        # contract (return False — existing callers treat all "no fan-in this round" alike + re-poll)
        # and the loud stderr, and ALSO drop a ``_fanin_blocked`` sentinel so the watchdog / an
        # operator can tell "refused under enforce, anchor unresolved" apart from "still waiting".
        # The sub-task .done markers and the absence of _fanin_triggered keep it RE-DISPATCHABLE; the
        # sentinel is cleared the moment a later attempt clears the gate (below). Best-effort, never
        # load-bearing (same fail-open contract as the telemetry logs).
        try:
            (batch_dir / "_fanin_blocked").write_text(
                f"{now_iso()} anchor unresolved under enforce ({decision.miss_reason})\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        print(
            f"❌ [trigger-fan-in] anchor unresolved under enforce — fan-in for batch {batch_id} "
            "refused (fail-closed); sub-task results preserved, re-dispatchable once the "
            "coordinator anchor resolves (signal: _fanin_blocked).",
            file=sys.stderr,
        )
        return False

    # Past the gate (anchor resolved, or warn / dry_run / non-coordinator origin): clear any stale
    # refusal sentinel from an earlier blocked attempt so it never lingers (Step 4 codex #3).
    (batch_dir / "_fanin_blocked").unlink(missing_ok=True)

    if not atomic.atomic_create(batch_dir / "_fanin_triggered"):
        print("[trigger-fan-in] sibling already triggered, exiting")
        return False

    print(f"[trigger-fan-in] ✅ batch {batch_id} complete, dumping fan-in")
    baseline = detect_baseline(workspace, cfg=cfg, project=project)
    # fan_in_task already read above (before the Step 4 anchor gate).

    write_role_env(batch_dir / "fan-in.env", HANDOFF_ROLE_FAN_IN, batch_id, workspace)
    content = templates.build_fan_in_handoff_md(
        project=project,
        workspace=workspace,
        batch_id=batch_id,
        manifest=manifest,
        done_files=done_set,
        blocked_files=blocked_set,
        baseline=baseline,
        inject_blocks=cfg.inject_blocks_for(project),
        handoff_home=cfg.home,
    )
    # Launcher-visible fan-in description + trigger: atomic_replace, not
    # write_with_fsync (same torn-read rationale as the single-task path).
    atomic.atomic_replace(queue_dir / f"{fan_in_task}.md", content)

    maybe_write_singlepane_sidecar(
        cfg,
        project,
        fan_in_task,
        workspace,
        queue_dir,
        worktree_active=False,
        role="worker",
        close_policy="keep",
        spawn_nonce=_spawn_nonce.new_nonce(),
    )
    uri = build_uri(cfg, project, fan_in_task)
    # direct-jump-spawn: append the SPAWNER_FOCUS line when dumped from a coordinator
    # terminal (fail-open → ""; the fan-in tab lands on the spawner's desktop too).
    # place-role-explicit-contract: the fan-in window is a worker → ROLE=worker.
    atomic.atomic_replace(
        queue_dir / f"{fan_in_task}.uri",
        f"WORKSPACE={workspace}\nURI={uri}\nROLE=worker\n"
        f"{_spawner_focus_line(decision, home=cfg.home, project=project, worker_task=fan_in_task, isolation='singlepane')}",
    )
    print(f"[trigger-fan-in] wrote queue/{fan_in_task}.{{md,uri}} + fan-in.env")

    _notify(
        f"batch {batch_id} complete → fan-in tab starting",
        f"v5 fan-in / {project}",
        fan_in_task,
    )
    return True


def handle_batch_done(
    args,
    cfg: _config.Config,
    workspace: Path,
    project: str,
    queue_dir: Path,
    anchor_decision: _spawner_focus.AnchorDecision | None = None,
) -> int:
    if not args.batch_id:
        raise SystemExit("❌ --batch-done requires --batch-id")
    batch_dir = handoff_root() / project / "batches" / args.batch_id
    if not batch_dir.exists():
        blocked_file = queue_dir / f"{args.task}.BLOCKED.md"
        atomic.write_with_fsync(
            blocked_file,
            (
                f"# BLOCKED — sub-task `{args.task}`\n\n"
                f"Reason: batch_dir vanished ({batch_dir})\n"
                f"Time: {datetime.now()}\n"
            ),
        )
        print(f"[batch-done] batch_dir missing, BLOCKED written to {blocked_file}")
        return 1
    sub_task_id = args.task.removesuffix("-done")
    summary_path = batch_dir / f"{sub_task_id}.done"
    atomic.write_with_fsync(
        summary_path,
        (f"sub_task_id: {sub_task_id}\ncompleted_at: {now_iso()}\nsummary: {args.next_brief}\n"),
    )
    print(f"[batch-done] {summary_path} written")
    # Terminal state: drop the sub-task heartbeat so the watchdog's mode-4/6
    # stale sweep doesn't mis-flag a completed sub-task.
    (batch_dir / f"{sub_task_id}.heartbeat").unlink(missing_ok=True)
    trigger_fan_in_if_ready(project, workspace, args.batch_id, queue_dir, cfg=cfg,
                            self_task=getattr(args, "self_task", None),
                            decision=anchor_decision)
    return 0


def handle_batch_blocked(
    args,
    cfg: _config.Config,
    workspace: Path,
    project: str,
    queue_dir: Path,
    anchor_decision: _spawner_focus.AnchorDecision | None = None,
) -> int:
    if not args.batch_id:
        raise SystemExit("❌ --batch-blocked requires --batch-id")
    batch_dir = handoff_root() / project / "batches" / args.batch_id
    if not batch_dir.exists():
        blocked_file = queue_dir / f"{args.task}.BLOCKED.md"
        atomic.write_with_fsync(
            blocked_file,
            (
                f"# BLOCKED — sub-task `{args.task}`\n\n"
                f"Reason: batch_dir vanished ({batch_dir})\n"
                f"Original reason: {args.blocked_reason}\n"
            ),
        )
        return 1
    sub_task_id = args.task.removesuffix("-blocked")
    blocked_path = batch_dir / f"{sub_task_id}.blocked"
    atomic.write_with_fsync(
        blocked_path,
        (
            f"sub_task_id: {sub_task_id}\n"
            f"blocked_at: {now_iso()}\n"
            f"reason: {args.blocked_reason or '(unspecified)'}\n"
        ),
    )
    print(f"[batch-blocked] {blocked_path} written")
    # Terminal state — same heartbeat cleanup as the batch-done path.
    (batch_dir / f"{sub_task_id}.heartbeat").unlink(missing_ok=True)
    trigger_fan_in_if_ready(project, workspace, args.batch_id, queue_dir, cfg=cfg,
                            self_task=getattr(args, "self_task", None),
                            decision=anchor_decision)
    return 0


# ─── orphan detection + cleanup (v5.2) ──────────────────────────────────────


def find_orphans(project_filter: str | None = None) -> list[dict]:
    """Scan all projects' ``ack/*.spawned`` for orphan tabs.

    An orphan is a ``.spawned`` marker whose corresponding queue ``.md`` is
    gone (and which has no ``.done`` either) — the IDE tab is still running
    but has no task description to load.
    """
    out: list[dict] = []
    root = handoff_root()
    if not root.exists():
        return out
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        if proj_dir.name in {"locks", "_recovery"}:
            continue
        if project_filter and proj_dir.name != project_filter:
            continue
        ack_dir = proj_dir / "ack"
        if not ack_dir.is_dir():
            continue
        queue_dir = proj_dir / "queue"
        launched_dir = proj_dir / "launched"
        for spawned in ack_dir.glob("*.spawned"):
            task_id = spawned.stem
            if (queue_dir / f"{task_id}.md").exists():
                continue
            if (queue_dir / f"{task_id}.done").exists():
                continue
            launched_paths = []
            if launched_dir.is_dir():
                launched_paths = sorted(launched_dir.glob(f"{task_id}-*.txt"))
            out.append(
                {
                    "project": proj_dir.name,
                    "task": task_id,
                    "spawned_path": spawned,
                    "submitted_path": ack_dir / f"{task_id}.submitted",
                    "queued_path": ack_dir / f"{task_id}.queued",
                    "blocked_md_path": queue_dir / f"{task_id}.BLOCKED.md",
                    # Single-task orphan residue the engine also produces per task:
                    # old_ready (retro audit metadata, ack/) + heartbeat (queue/,
                    # left ticking → watchdog mode 6 mis-flags it 529-suspected).
                    # Batch sub-task heartbeats live in batches/<batch>/ and are
                    # owned by the batch_dir lifecycle (handle_batch_done/blocked),
                    # so they are intentionally out of this single-task cleanup.
                    "old_ready_path": ack_dir / f"{task_id}.old_ready",
                    "heartbeat_path": queue_dir / f"{task_id}.heartbeat",
                    "launched_paths": launched_paths,
                    "age_seconds": time.time() - spawned.stat().st_mtime,
                }
            )
    return out


def handle_cleanup_orphan(args) -> int:
    """Dry-run lists orphans; with ``--apply`` removes ack/launched/BLOCKED residue."""
    project_filter = args.project if getattr(args, "project", None) else None
    orphans = find_orphans(project_filter)
    if not orphans:
        print("✅ 无孤儿残留 / no orphans found")
        return 0

    print(f"\nfound {len(orphans)} 个孤儿:")
    for o in orphans:
        bm = "✓" if o["blocked_md_path"].exists() else " "
        launched_n = len(o["launched_paths"])
        print(
            f"  [{bm}BLOCKED] {o['project']}/{o['task']}  "
            f"age={o['age_seconds']:.0f}s  launched={launched_n}"
        )
    print()

    if not args.apply:
        print("(dry-run / pass --apply to delete. --kill-spawned also pings the user.)")
        return 0

    cleaned = 0
    for o in orphans:
        for p in [
            o["spawned_path"],
            o["submitted_path"],
            o["queued_path"],
            o["blocked_md_path"],
            o["old_ready_path"],
            o["heartbeat_path"],
        ]:
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                print(f"  ⚠️ unlink failed {p}: {e}")
        for ln in o["launched_paths"]:
            try:
                ln.unlink(missing_ok=True)
            except OSError as e:
                print(f"  ⚠️ unlink failed {ln}: {e}")
        cleaned += 1
        print(f"  🗑 cleaned: {o['project']}/{o['task']}")
    print(f"\n✅ cleanup complete: {cleaned} orphan(s)")

    recovery_dir = handoff_root() / "_recovery"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    record = recovery_dir / f"orphans-{ts}.json"
    record.write_text(
        json.dumps(
            [
                {"project": o["project"], "task": o["task"], "age_seconds": o["age_seconds"]}
                for o in orphans
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"📝 留档: {record}")

    if getattr(args, "kill_spawned", False):
        tasks_md = "\n".join(f"- `{o['project']}/{o['task']}`" for o in orphans)
        print("\n⚠️  --kill-spawned: IDE tab title doesn't carry task_id; manual close needed.")
        print("   Please close Claude tabs for:")
        print(tasks_md)
        _notify(
            f"{len(orphans)} tabs need manual close (see terminal)",
            "v5.2 cleanup-orphan",
            "kill-spawned",
            sound="Basso",
        )
    return 0


# ─── per-session worktree isolation (opt-in) ────────────────────────────────


def _worktree_info_dict(result: _worktree.WorktreeResult, mode: str) -> dict:
    """Serialize a WorktreeResult into the dict threaded to the handoff template
    + the ``.worktree`` sidecar."""
    return {
        "mode": mode,
        "status": result.status,
        "path": str(result.spawn_workspace),
        "branch": result.branch,
        "base_sha": result.base_sha,
        "integration_branch": result.integration_branch,
        "linked": result.linked,
        "degraded": result.status == _worktree.ST_DEGRADED,
        "reason": result.reason,
        "warnings": result.warnings,
        "planned_cmd": result.planned_cmd,
    }


def _write_worktree_block(queue_dir: Path, project: str, task: str, head: str, reason: str) -> None:
    """Write a BLOCKED.md for an unsafe worktree state (unpublished HEAD / dirty
    collision) so the closing session sees why + how to unblock, and drop the .uri
    trigger so the launcher does not spawn a successor on stale/unsafe state."""
    blocked_file = queue_dir / f"{task}.BLOCKED.md"
    blocked_file.write_text(
        templates.build_blocked_md(
            project=project,
            task=task,
            head=head,
            reason=f"worktree isolation gate: {reason}",
        ),
        encoding="utf-8",
    )
    (queue_dir / f"{task}.uri").unlink(missing_ok=True)
    print(f"[dump] ⛔ worktree gate BLOCKED → {blocked_file}\n[dump]    {reason}")
    _notify(reason, f"worktree gate / {project}", task, sound="Basso")


def resolve_spawn_workspace(
    *,
    args,
    cfg: _config.Config,
    source_workspace: Path,
    project: str,
    queue_dir: Path,
) -> tuple[Path, dict | None, int | None]:
    """Resolve where the successor session works (the shared tree or a new worktree).

    Returns ``(spawn_workspace, worktree_info, block_exit_code)``. When
    ``block_exit_code`` is non-None the caller must return it immediately (an unsafe
    state was detected + a BLOCKED.md written) — never spawn a successor. Worktree
    isolation applies ONLY to the single-task ``active`` path (not batch / done /
    blocked / dry-run); every other path returns the source tree unchanged
    (byte-identical legacy behavior).
    """
    mode = _worktree.resolve_mode(cfg, project)
    if mode == _worktree.MODE_OFF:
        return source_workspace, None, None

    is_coordinator = getattr(args, "coordinator", False)
    # sw-coord-p21 symmetry fix: serialize create_worktree under the project spawn lock,
    # exactly as the SPAWN path does (spawn.py:391). Concurrent same-project dumps each run
    # ``git worktree add -b``, which writes branch upstream config into the SHARED source
    # repo's ``.git/config``; git's own lock turns that race into spurious "could not lock
    # config file" fail-closes. An N=8 reproduction created only 5/8 worktrees UNLOCKED (3
    # spurious degrades, no corruption) vs 8/8 LOCKED — the lock is the fix. Parallel
    # worktree workers are LEGITIMATE (design §2.2), so this WAITS (queues) rather than
    # rejecting, mirroring spawn.py's ``_WORKTREE_LOCK_WAIT``. The lock is held ONLY across
    # create_worktree (the sole ``.git/config`` writer here — the later sidecar/.uri publish
    # touches only the queue dir) and released before main() reaches singlepane_worker_guard,
    # which acquires the SAME non-reentrant lock but only on the singlepane path. The two are
    # SEQUENTIAL in main(), never nested — so this is deadlock-safe even for a project
    # mis-configured as both worktree-ON and singlepane (the MODE_OFF early-return above
    # already keeps every non-worktree dump path out of the lock = byte-identical).
    try:
        with project_spawn_lock(project, root=cfg.home, wait=_WORKTREE_LOCK_WAIT):
            result = _worktree.create_worktree(
                source_workspace=source_workspace,
                project=project,
                task=args.task,
                cfg=cfg,
                mode=mode,
                # E3 role taxonomy: the dump single-task active relay is the SOLO chain on this
                # path too — its workspace env signal (HANDOFF_SESSION_ROLE) must say "solo", not
                # "worker". Title is unaffected (dump passes no spawn_nonce → legacy title). A
                # coordinator's env is overridden to supervisor_succession inside session_env_osx
                # regardless of this value (kept "worker" for symmetry with the sidecar contract).
                role=("worker" if is_coordinator else ROLE_SOLO),
                # §五·2 (2026-06-09 owner立法): a 中枢 dump red-tops its worktree window. getattr keeps
                # batch / fan-in / legacy callers (whose args lack --coordinator) working unchanged.
                is_coordinator=is_coordinator,
            )
    except LockHeld as e:
        # Held past the bounded wait → fail closed (禁止静默降级: never silently fall back to a
        # shared-tree spawn). Mirror spawn.py:406-412 with the dump-side owner-readable
        # artifact: a BLOCKED.md (so the closing session sees why) + drop the .uri (so the
        # launcher never spawns a successor on this contended state).
        head = _worktree.head_sha(source_workspace) or "(unknown)"
        _write_worktree_block(
            queue_dir,
            project,
            args.task,
            head,
            f"project spawn lock held past {_WORKTREE_LOCK_WAIT:.0f}s ({e}); refusing to "
            "create the worktree concurrently — retry after the other spawn/dump settles",
        )
        return source_workspace, None, 2  # EXIT_FAIL_CLOSED (spawn.py parity)

    if result.is_blocked:
        head = _worktree.head_sha(source_workspace) or "(unknown)"
        _write_worktree_block(queue_dir, project, args.task, head, result.reason or "unsafe")
        return source_workspace, _worktree_info_dict(result, mode), 2  # ERR-BLOCKED-ish

    if result.status == _worktree.ST_REPORT:
        print(f"[dump] [worktree:report] would run: {result.planned_cmd}")
        if result.reason:
            print(f"[dump] [worktree:report] note: {result.reason}")
        return source_workspace, _worktree_info_dict(result, mode), None

    if result.status == _worktree.ST_DEGRADED:
        print(
            f"[dump] ⚠️  worktree isolation requested but unavailable "
            f"({result.reason}); falling back to shared tree"
        )
        return source_workspace, _worktree_info_dict(result, mode), None

    # created
    print(
        f"[dump] [worktree] {result.spawn_workspace} on {result.branch} "
        f"(base {(result.base_sha or '?')[:8]} / linked {result.linked})"
    )
    for w in result.warnings:
        print(f"[dump] [worktree] ⚠️  {w}")
    return result.spawn_workspace, _worktree_info_dict(result, mode), None


# ─── CLI ────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="handoff-dump",
        description="Generate handoff queue files for the next task or batch.",
    )
    ap.add_argument(
        "--task", default=None, help="kebab-case task ID (optional under --cleanup-orphan)"
    )
    ap.add_argument(
        "--next", dest="next_brief", default=None, help="one-line brief of the next task"
    )
    ap.add_argument("--project", default=None, help="project slug; defaults to basename(cwd)")
    ap.add_argument(
        "--workspace", default=None, help="absolute path to project root; defaults to cwd"
    )
    ap.add_argument("--status", default="active", choices=["active", "done", "blocked"])
    ap.add_argument(
        "--self-task", dest="self_task", default=None,
        help="mp-locate-return §1: the SPAWNING coordinator's OWN task id (self-reported, "
             "env-independent) → singlepane Tier-2 derives its workspace via derive_singlepane_focus "
             "so the worker focus-jumps to the coordinator's desktop. Omit for worktree coordinators "
             "(Tier-1 uses cwd) or when no self-identification is wanted (fail-open).",
    )
    ap.add_argument(
        "--origin", default=_spawner_focus.ORIGIN_COORDINATOR,
        choices=list(_spawner_focus.ORIGINS),
        help="spawn-unification Step 4: who is dispatching (default coordinator — an automated "
             "中枢→worker dispatch that MUST resolve an anchor). PER-INVOCATION, never inherited: "
             "leniency (allow-no-anchor) comes only from non-inheritable signals — 'system' ⟺ project "
             "∈ config spawner_anchor_system_allow, 'interactive' ⟺ a real front TTY without "
             "HANDOFF_UNATTENDED, 'test' ⟺ in-process pytest. Under an enforce-phase project a "
             "coordinator anchor-miss is fail-closed; default = warn (config lists empty → zero "
             "behavior change).",
    )
    ap.add_argument("--blocked-reason", default="")
    ap.add_argument("--tests", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--batch-id", default=None, help="v5 batch ID (current task is sub-task or fan-in)"
    )
    ap.add_argument(
        "--batch-done", action="store_true", help="mark sub-task done + try fan-in trigger"
    )
    ap.add_argument("--batch-blocked", action="store_true", help="mark sub-task blocked")
    ap.add_argument(
        "--batch-fan-in", action="store_true", help="(internal) mark this as the fan-in dump"
    )
    ap.add_argument(
        "--open-batch", default=None, help="path to a manifest.json: opens a fan-out batch"
    )
    ap.add_argument(
        "--file-ownership", default=None, help="(internal) sub-task file_ownership JSON"
    )
    ap.add_argument(
        "--cleanup-orphan",
        action="store_true",
        help="list / delete orphan ack residue (default dry-run)",
    )
    ap.add_argument(
        "--apply", action="store_true", help="with --cleanup-orphan: actually delete residue"
    )
    ap.add_argument(
        "--kill-spawned",
        action="store_true",
        help="with --cleanup-orphan --apply: notify user to close tabs",
    )
    # v5.4 retro-evidence gate (§7.1 / §7.2 / §7.7).
    # The gate is skipped entirely when no flag and no env is set so legacy
    # callers (ERP shim's pre-v5.4 invocations) continue working unchanged.
    # Phase 4b will flip on ``HANDOFF_RETRO_MANDATE`` via CLAUDE.md.
    ap.add_argument(
        "--retro-evidence",
        default=None,
        help="path to precheck/<task>.retro.evidence.json (activates v5.4 gate)",
    )
    ap.add_argument(
        "--nonce",
        default=None,
        help="optional per-task nonce; must match payload.nonce when present (§7.2)",
    )
    # Deprecated no-op (backward-compat). The pre-v5.4 standalone global dump
    # auto-appended a ``-YYYYMMDD-HHMMSS`` suffix to active task IDs unless
    # ``--no-dedupe`` was passed. v5.4 binds the task ID to its evidence file
    # (``precheck/<task>.retro.evidence.json``) + ``old_ready`` lookups, so task
    # IDs MUST be exact — no auto-suffix. The flag is accepted and ignored so old
    # callers / scripts that still pass it don't crash with "unrecognized arguments".
    ap.add_argument(
        "--no-dedupe",
        action="store_true",
        help="(deprecated, ignored) task IDs are exact under v5.4; no timestamp suffix",
    )
    # §五·2 (2026-06-09 owner立法 / handoff-fanout 派窗路径红顶普适化): mark the spawned session as a
    # supervisor center (中枢). Its isolated worktree's .handoff.code-workspace gets a red title bar +
    # 🧭中枢· prefix (byte-parity with dx-spawn-session.sh --coordinator) so the owner can't misclose
    # the 中枢 among many windows. Effective on the worktree-isolation active path AND the singlepane
    # active path (owner-caught gap 2026-06-10 — wilde-hexe/sdgf/fb are singlepane projects); a no-op
    # for non-中枢 dumps (byte-identical legacy behavior).
    ap.add_argument(
        "--coordinator",
        action="store_true",
        help="red-top the spawned window — worktree AND singlepane paths (supervisor center / 中枢; "
        "§五 防误关). A 中枢 relay is a SINGLE-TASK dispatch: combining with any batch/fan-in flag "
        "is machine-REJECTED (warmgap-B MUST-3), and a non-worktree coordinator FORCES the "
        "singlepane window form regardless of singlepane_projects (MUST-1 engine invariant)",
    )
    return ap


def main(argv: list[str] | None = None, *, suppress_spawn_artifacts: bool = False) -> int:
    # warmgap-C §1a: ``suppress_spawn_artifacts`` is a Python-keyword-only seam for the
    # audit-close succession route (codex MUST#2: it must NEVER enter argparse — a public
    # CLI flag would let any caller strip the window intent off a dump and bypass the
    # spawn-side G4 contract). See ``write_active_dump`` for the kept/skipped split.
    args = _build_parser().parse_args(argv)
    cfg = _config.load()

    # warmgap-B MUST-3 (machine gate, was help-text only): --coordinator × batch/fan-in is
    # structurally invalid — a 中枢 relay is a single-task dispatch; the batch paths thread
    # neither the red-top nor the forced-singlepane invariant, so silently accepting the
    # combo would reopen the warm-gap (and fan-in to a coordinator risks session bleeding).
    if getattr(args, "coordinator", False) and (
        args.open_batch
        or args.batch_id
        or args.batch_done
        or args.batch_blocked
        or args.batch_fan_in
    ):
        raise SystemExit(
            "❌ --coordinator cannot be combined with batch/fan-in flags "
            "(--open-batch/--batch-id/--batch-done/--batch-blocked/--batch-fan-in): "
            "a 中枢 relay is a single-task dispatch (warmgap-B MUST-3)"
        )

    if args.cleanup_orphan:
        return handle_cleanup_orphan(args)

    # --open-batch opens a fan-out batch from a manifest.json; the sub-task IDs come
    # from the manifest (handle_open_batch never reads args.task), so it does NOT
    # require --task/--next. codex R2-P1: the documented global invocation
    # ``dump-handoff.py --open-batch <manifest>`` was wrongly blocked by this guard.
    if not args.open_batch and (not args.task or not args.next_brief):
        raise SystemExit(
            "❌ --task and --next are required (except under --cleanup-orphan / --open-batch)"
        )
    if args.task:
        validate_task_id(args.task)

    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    if not workspace.exists():
        raise SystemExit(f"❌ workspace not found: {workspace}")
    project = args.project or workspace.name
    validate_project_slug(project)

    queue_dir = cfg.queue_dir(project)
    queue_dir.mkdir(parents=True, exist_ok=True)

    stop_path = any_stop_auto(project, args.batch_id)
    if stop_path:
        print(f"[dump] STOP detected at {stop_path}, exit 0 (no write)")
        return 0

    gate_result = _run_retro_gate(args, workspace, project, cfg)
    if gate_result is not None and not gate_result.is_ok:
        gate_result.emit()
        return gate_result.exit_code

    # B1 (learning-loop component 6 / L1): retrieval-pull ENFORCE gate (DEFAULT-OFF). A
    # coordinator ACTIVE handoff that didn't read its predecessor's lesson (no
    # predecessor_lesson_backref) and didn't honestly attest no_novel_lesson_attested is
    # REFUSED here — BEFORE any artifact (.md / sidecar / old_ready / .uri) — so a blocked
    # handoff leaves no half-product. Disabled by default (empty
    # retrieval_pull_enforce_projects) → returns None → byte-identical to the pre-B1 path.
    rp_rc = _run_retrieval_pull_gate(args, project, cfg)
    if rp_rc is not None:
        return rp_rc

    # closeout_obligations (third vector / WARN-mode): emit a non-blocking advisory if a
    # coordinator handoff is missing the closeout vector (or sedimentation_always != ✅). It
    # ALWAYS returns None and NEVER blocks — so we call it for its side effect and intentionally
    # do NOT check a return value (contrast the rp gate above). DEFAULT-OFF → silent no-op,
    # byte-identical to the pre-closeout path.
    _run_closeout_obligations_gate(args, project, cfg)

    if args.open_batch:
        # spawn-unification Step 4: the fan-out sub-task .uris carry the coordinator anchor → resolve
        # the decision ONCE (design §2.4 唯一解析点 for this path) and fail-closed BEFORE writing any
        # batch artifact, then thread it to the writer (no re-resolve). Warn-mode (default) → no-op.
        anchor_decision = _dump_anchor_decision(
            cfg, project, self_task=getattr(args, "self_task", None),
            origin=getattr(args, "origin", _spawner_focus.ORIGIN_COORDINATOR),
        )
        gate_rc = _anchor_gate(anchor_decision, cfg=cfg, project=project, task=args.batch_id)
        if gate_rc is not None:
            return gate_rc
        return handle_open_batch(args, cfg, workspace, project, queue_dir, anchor_decision)
    # batch_done / batch_blocked record the sub-task result then MAYBE fan in: the anchor decision is
    # resolved lazily inside ``trigger_fan_in_if_ready`` only when a fan-in actually fires (so a
    # non-spawning terminal sub-task close adds no resolve — surgical, matches pre-Step-4 timing).
    if args.batch_done:
        return handle_batch_done(args, cfg, workspace, project, queue_dir)
    if args.batch_blocked:
        return handle_batch_blocked(args, cfg, workspace, project, queue_dir)
    if args.batch_id and not args.batch_fan_in:
        raise SystemExit(
            "❌ --batch-id must be paired with --batch-done / --batch-blocked / --batch-fan-in"
        )

    # Project-scoped preflight gates (2C / generic): a fail-closed pre-req run
    # before producing the closure artifact. Skipped for --dry-run previews and
    # for projects without dump_preflight_commands config.
    if not args.dry_run:
        preflight_rc = run_preflight_gates(
            cfg, workspace=workspace, project=project, status=args.status
        )
        if preflight_rc:
            return preflight_rc

    print(f"[dump] project={project} task={args.task} status={args.status}")
    # ``workspace`` is the CLOSING session's tree (source); retro/preflight gates
    # already ran against it above. Per-session worktree isolation (opt-in) may
    # redirect the SUCCESSOR to its own worktree — keep the two roles distinct so
    # old_ready stays anchored to the source and the successor's baseline/handoff/
    # .uri point at the worktree (design §8.1 R1-C1).
    source_workspace = workspace

    spawn_workspace = source_workspace
    worktree_info: dict | None = None
    # old_ready's commit anchor is the CLOSING session's HEAD. It is read from
    # ``source_workspace`` inside ``_write_old_ready`` (lazily, only when retro
    # evidence drives a dump), so the default-OFF path adds ZERO git subprocesses
    # (R2 codex P2-I byte-identical). When a worktree IS created we capture it
    # explicitly here — the source tree is never moved by worktree creation, so this
    # is the same SHA, but the explicit anchor documents R1-C1.
    old_head: str | None = None
    # spawn-unification Step 4: an ACTIVE worker spawn carries the coordinator anchor → resolve the
    # decision ONCE here (the SINGLE解析点 for this path, design §2.4) and fail-closed BEFORE the
    # worktree-resolution step below (which would CREATE a worktree) and before write_active_dump
    # touches ANY artifact (.md / sidecar / old_ready / .uri) — so a blocked dispatch leaves NO
    # half-product, not even an orphan worktree (原子无半产物). The decision is then threaded to
    # write_active_dump (no re-resolve). Only the spawning, non-dry-run, non-suppressed active path
    # needs it (a terminal close unlinks the .uri = no spawn → no resolve; the suppressed succession
    # ledger publishes its window intent later via the spawn side, which gates itself). Warn-mode
    # (default) → gate is a no-op → byte-identical.
    anchor_decision: _spawner_focus.AnchorDecision | None = None
    if args.status == "active" and not args.dry_run and not suppress_spawn_artifacts:
        anchor_decision = _dump_anchor_decision(
            cfg, project, self_task=getattr(args, "self_task", None),
            origin=getattr(args, "origin", _spawner_focus.ORIGIN_COORDINATOR),
        )
        gate_rc = _anchor_gate(anchor_decision, cfg=cfg, project=project, task=args.task)
        if gate_rc is not None:
            return gate_rc
    # warmgap-C fix1 MUST-1: worktree resolution is a WINDOW-INTENT step, so the
    # suppressed (succession-route) dump must skip it entirely — the succession spawn
    # always opens a singlepane window on the SOURCE tree (coordinator invariant,
    # warmgap design Q3), and resolving a worktree here would split the ledger
    # (.md / .worktree ack pointing at a worktree) from the actual window (source
    # tree) and orphan the worktree. spawn_workspace stays the source tree,
    # worktree_info stays None, old_head stays None (old_ready lazily reads the
    # source-tree HEAD — same value either way).
    if args.status == "active" and not args.dry_run and not suppress_spawn_artifacts:
        spawn_workspace, worktree_info, block_rc = resolve_spawn_workspace(
            args=args,
            cfg=cfg,
            source_workspace=source_workspace,
            project=project,
            queue_dir=queue_dir,
        )
        if block_rc is not None:
            return block_rc
        if worktree_info and worktree_info.get("status") == _worktree.ST_CREATED:
            old_head = _worktree.head_sha(source_workspace)

    baseline = detect_baseline(spawn_workspace, cfg=cfg, project=project)
    print(f"[dump] HEAD={baseline['git_head']}")

    if args.dry_run:
        roadmap_excerpt = get_roadmap_excerpt(cfg, project)
        md_path = queue_dir / f"{args.task}.md"
        # singlepane self-continuation carries --self-task <this-session's-task> (the
        # spawner anchor for Tier-2 resolution); non-singlepane → "" → byte-identical.
        self_task_args = (
            f" --self-task {args.task}"
            if cfg.resolve_isolation(project) == "singlepane"
            else ""
        )
        content = templates.build_handoff_md(
            task=args.task,
            project=project,
            workspace=spawn_workspace,
            next_brief=args.next_brief,
            status=args.status,
            tests=args.tests or None,
            baseline=baseline,
            roadmap_excerpt=roadmap_excerpt,
            inject_blocks=cfg.inject_blocks_for(project),
            handoff_home=cfg.home,
            handoff_md_path=md_path,
            worktree_info=worktree_info,
            self_task_args=self_task_args,
        )
        print("=" * 60)
        print(f"DRY-RUN: target paths\n  {md_path}\n  {queue_dir / f'{args.task}.uri'}")
        print("=" * 60)
        print(content[:2000])
        print("...")
        return 0

    # Pass the evidence path down so write_active_dump can persist
    # ack/<task>.old_ready alongside the .uri sidecar (§7.6). The gate
    # already validated the file's existence + hash, so plumbing it
    # through is safe.
    retro_evidence_path: Path | None = None
    if (
        args.retro_evidence
        and gate_result is not None
        and gate_result.is_ok
        and args.status == "active"
    ):
        retro_evidence_path = Path(args.retro_evidence).resolve()

    # Singlepane concurrency hard-gate (design §5.4): for an ACTIVE worker spawn into a
    # ``worker_isolation == "singlepane"`` project, hold the project spawn lock across the
    # WHOLE write (sidecar + .uri publish) and REJECT a concurrent / over-spawned second
    # worker rather than clobber the live one. NO-OP for non-singlepane projects and for
    # the terminal (done/blocked) paths — those never spawn a worker.
    spawn_guard: contextlib.AbstractContextManager = (
        singlepane_worker_guard(cfg, project=project, task=args.task)
        if args.status == "active"
        else contextlib.nullcontext()
    )
    try:
        with spawn_guard:
            return write_active_dump(
                cfg=cfg,
                project=project,
                task=args.task,
                workspace=spawn_workspace,
                next_brief=args.next_brief,
                status=args.status,
                tests=args.tests or None,
                baseline=baseline,
                queue_dir=queue_dir,
                osascript_subtitle=args.blocked_reason or None,
                retro_evidence_path=retro_evidence_path,
                source_workspace=source_workspace,
                old_head=old_head,
                worktree_info=worktree_info,
                # §五·2 singlepane red-top (owner-caught gap 2026-06-10): the same getattr
                # convention as resolve_spawn_workspace keeps legacy/batch callers (whose
                # args lack --coordinator) working unchanged.
                is_coordinator=getattr(args, "coordinator", False),
                suppress_spawn_artifacts=suppress_spawn_artifacts,
                self_task=getattr(args, "self_task", None),
                anchor_decision=anchor_decision,
            )
    except SinglepaneBusy as e:
        print(
            f"❌ {e}\n   (singlepane project already busy; see "
            f"ack/{args.task}.singlepane_busy.txt — the existing worker must finish first)",
            file=sys.stderr,
        )
        return 2
    except CoordinatorSinglepaneError as e:
        # warmgap-B MUST-2: the forced-singlepane artifacts failed to write — the .uri was
        # never published (no spawn happened), and falling back warm is forbidden.
        print(f"❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
