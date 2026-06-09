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
from handoff_fanout import spawn_nonce as _spawn_nonce
from handoff_fanout import worktree as _worktree
from handoff_fanout.git_guard import git_guard_dir
from handoff_fanout.handoff_precheck import (
    EVIDENCE_SCHEMA_VERSION,
    compute_retro_evidence_hash,
    resolve_session_id,
)

# v5.4 old_ready schema (§7.6). Bumped together with retro_evidence schema.
OLD_READY_SCHEMA_VERSION = EVIDENCE_SCHEMA_VERSION

# v5 protocol constants
SCHEMA_VERSION = 2
SPECIAL_MARKERS = {
    "_fanin_triggered",
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

# v5.1 spawn-storm defenders (carried over from the v5.1 / 5.2 audit).
SUB_TASK_N_MAX = 3
STAGGER_SPAWN_SECONDS = 30
GLOBAL_ACTIVE_LIMIT = 5

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

    Cleanup/no-op otherwise: a project that does NOT opt in, or a worktree spawn (which has
    its own ``.handoff.code-workspace`` and wins), gets the sidecar REMOVED so a stale opt-in
    can't linger across a config flip-off. Best-effort — an OSError never bricks the dump
    (single-pane is UX polish; the warm submit still works without it)."""
    sidecar = queue_dir / f"{task}.singlepane"
    if worktree_active or project not in cfg.singlepane_projects:
        sidecar.unlink(missing_ok=True)
        return
    try:
        sp_dir = cfg.home / project / "singlepane"
        sp_dir.mkdir(parents=True, exist_ok=True)
        ws_file = sp_dir / f"{task}.handoff.code-workspace"
        ws_file.write_text(
            json.dumps(
                {
                    "folders": [{"path": str(workspace)}],
                    "settings": {
                        # project·task·role·nonce via the Phase-1 title_for so the watchdog can match
                        # the front window by the unguessable spawn_nonce (osascript substring `contains`,
                        # kills focus-drift TOCTOU). KEEP the task token (backward-compat task-match) + the
                        # [singlepane] marker + the VS Code ${activeEditorShort} display variable (literal
                        # here; VS Code expands ${...} at runtime).
                        "window.title": (
                            _spawn_nonce.title_for(
                                project=project, task_id=task, role=role, nonce=spawn_nonce
                            )
                            + " [singlepane]${separator}${activeEditorShort}"
                        ),
                        # Same declarative single-pane settings the worktree workspace uses (see
                        # worktree.write_workspace_file): hide the activity bar (removes the empty
                        # Claude sidebar focus competitor) + no Welcome tab. P0 THIN workspace — these
                        # UX keys only, never a coordinator/inject config block.
                        "workbench.activityBar.location": "hidden",
                        "workbench.startupEditor": "none",
                        "claudeCode.preferredLocation": "panel",
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        # JSON sidecar (breaking migration from the old plain-path text): watchdog reads `workspace`
        # (open target) + `spawn_nonce` (atomic title gate); role/close_policy/predecessor_nonce feed
        # role-gated autoclose (worker → keep / supervisor_succession → close_predecessor).
        sidecar.write_text(
            json.dumps(
                {
                    "workspace": str(ws_file),
                    "role": role,
                    "close_policy": close_policy,
                    "spawn_nonce": spawn_nonce,
                    "predecessor_nonce": predecessor_nonce,
                }
            ),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[dump] (non-fatal) could not write singlepane workspace: {e}")
        sidecar.unlink(missing_ok=True)


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
) -> int:
    roadmap_excerpt = get_roadmap_excerpt(cfg, project)
    # ``workspace`` is the successor's tree (a worktree under isolation, else the
    # source tree); ``source_workspace`` is the closing session's tree used only for
    # the old_ready predecessor anchor (R1-C1). Default to identity for legacy callers.
    if source_workspace is None:
        source_workspace = workspace

    md_path = queue_dir / f"{task}.md"
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
    # worktree has its own .handoff.code-workspace and wins).
    maybe_write_singlepane_sidecar(
        cfg,
        project,
        task,
        workspace,
        queue_dir,
        worktree_active=bool(
            worktree_info and worktree_info.get("status") == _worktree.ST_CREATED
        ),
        role="worker",
        close_policy="keep",
        spawn_nonce=_spawn_nonce.new_nonce(),
    )

    _maybe_pbcopy(handoff_content)

    # ── PUBLISH: write the .uri trigger LAST (all sidecars now exist) ────────────
    # §3.7 — atomic .uri write (see the .md note above).
    atomic.atomic_replace(uri_path, f"WORKSPACE={workspace}\nURI={uri}\n")
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
        # Launcher-visible trigger — atomic_replace (see the .md note above).
        atomic.atomic_replace(
            queue_dir / f"{sub_id}.uri",
            f"WORKSPACE={workspace}\nURI={uri}\n",
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

    if not atomic.atomic_create(batch_dir / "_fanin_triggered"):
        print("[trigger-fan-in] sibling already triggered, exiting")
        return False

    print(f"[trigger-fan-in] ✅ batch {batch_id} complete, dumping fan-in")
    baseline = detect_baseline(workspace, cfg=cfg, project=project)
    fan_in_task = manifest["fan_in_task"]

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
    atomic.atomic_replace(
        queue_dir / f"{fan_in_task}.uri",
        f"WORKSPACE={workspace}\nURI={uri}\n",
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
    trigger_fan_in_if_ready(project, workspace, args.batch_id, queue_dir, cfg=cfg)
    return 0


def handle_batch_blocked(
    args,
    cfg: _config.Config,
    workspace: Path,
    project: str,
    queue_dir: Path,
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
    trigger_fan_in_if_ready(project, workspace, args.batch_id, queue_dir, cfg=cfg)
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

    result = _worktree.create_worktree(
        source_workspace=source_workspace,
        project=project,
        task=args.task,
        cfg=cfg,
        mode=mode,
    )
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
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = _config.load()

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

    if args.open_batch:
        return handle_open_batch(args, cfg, workspace, project, queue_dir)
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
    if args.status == "active" and not args.dry_run:
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
    )


if __name__ == "__main__":
    sys.exit(main() or 0)
