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
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from handoff_fanout import atomic, config as _config, templates
from handoff_fanout.git_guard import git_guard_dir

# v5 protocol constants
SCHEMA_VERSION = 2
SPECIAL_MARKERS = {
    "_fanin_triggered", "_fan_in_started", "_fan_in_heartbeat",
    "_fan_in_done", "_watchdog_triggered", "_aborted", "_corrupted",
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
        raise SystemExit(
            f"❌ task-id must be kebab-case (a-z 0-9 -). got: {task_id!r}"
        )
    if len(task_id) > 60:
        raise SystemExit(f"❌ task-id too long ({len(task_id)} > 60): {task_id}")


def validate_project_slug(slug: str) -> None:
    if not TASK_ID_RE.match(slug):
        raise SystemExit(f"❌ project-slug must be kebab-case. got: {slug!r}")


# ─── small helpers ──────────────────────────────────────────────────────────


def run(cmd: list[str], cwd: Path, timeout: float = 10.0) -> str:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(cwd),
        )
        return (r.stdout or "").strip()
    except Exception as e:
        return f"<error: {e}>"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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
        return {
            str(p.relative_to(ws_resolved))
            for p in target_dir.rglob("*") if p.is_file()
        }
    if typ == "glob":
        return {
            str(p.relative_to(ws_resolved))
            for p in workspace.glob(raw_path) if p.is_file()
        }
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


def detect_baseline(workspace: Path, cfg: _config.Config | None = None) -> dict:
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
        raw = run(hook.command, workspace)
        if hook.regex:
            m = re.search(hook.regex, raw)
            baseline[hook.name] = m.group(1) if m else "(N/A)"
        else:
            baseline[hook.name] = raw
    return baseline


def get_roadmap_excerpt(cfg: _config.Config) -> str:
    rm = cfg.roadmap
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
        slice_ = matches[-rm.max_sections:]
        return "\n\n".join(m.group(0)[: rm.max_chars_per_section] for m in slice_)
    return content[-rm.fallback_tail_chars:]


# ─── role.env writing (used by sub-task / fan-in handoffs) ──────────────────


def write_role_env(
    env_path: Path, role: str, batch_id: str, workspace: Path,
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
) -> int:
    roadmap_excerpt = get_roadmap_excerpt(cfg)

    md_path = queue_dir / f"{task}.md"
    handoff_content = templates.build_handoff_md(
        task=task, project=project, workspace=workspace,
        next_brief=next_brief, status=status, tests=tests,
        baseline=baseline, roadmap_excerpt=roadmap_excerpt,
        inject_blocks=cfg.inject_blocks, handoff_home=cfg.home,
        handoff_md_path=md_path,
    )
    md_path.write_text(handoff_content, encoding="utf-8")
    print(f"[dump] wrote {md_path} ({len(handoff_content)} bytes)")

    if status == "done":
        (queue_dir / f"{task}.done").touch()
        (queue_dir / f"{task}.uri").unlink(missing_ok=True)
        print(f"[dump] ✅ {project}/{task} marked done")
        return 0

    if status == "blocked":
        blocked_file = queue_dir / f"{task}.BLOCKED.md"
        blocked_file.write_text(
            templates.build_blocked_md(
                project=project, task=task,
                head=baseline.get("git_head", "(unknown)"),
                reason=osascript_subtitle or "",
            ),
            encoding="utf-8",
        )
        (queue_dir / f"{task}.uri").unlink(missing_ok=True)
        print(f"[dump] ⛔ BLOCKED written to {blocked_file}")
        _notify(osascript_subtitle or task, f"自动接续 / {project}", task, sound="Basso")
        return 0

    # active: write .uri sidecar + clipboard + notification
    uri = build_uri(cfg, project, task)
    uri_path = queue_dir / f"{task}.uri"
    uri_path.write_text(f"WORKSPACE={workspace}\nURI={uri}\n", encoding="utf-8")
    print(f"[dump] wrote {uri_path}")

    try:
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        p.communicate(input=handoff_content.encode("utf-8"))
    except (FileNotFoundError, OSError):
        pass

    _notify(next_brief, f"自动接续 / {project}", task)
    print(f"[dump] ✅ active dump complete for {project}/{task}")
    return 0


def _notify(message: str, title: str, subtitle: str, sound: str | None = None) -> None:
    """Best-effort macOS notification (no-op on other platforms)."""
    osa = (
        f'display notification "{message}" '
        f'with title "{title}" subtitle "{subtitle}"'
    )
    if sound:
        osa += f' sound name "{sound}"'
    try:
        subprocess.run(["osascript", "-e", osa], check=False, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


# ─── batch open (fan-out) ───────────────────────────────────────────────────


def handle_open_batch(
    args, cfg: _config.Config, workspace: Path, project: str, queue_dir: Path,
) -> int:
    manifest_input = Path(args.open_batch)
    if not manifest_input.exists():
        raise SystemExit(f"❌ --open-batch file not found: {manifest_input}")
    try:
        manifest = json.loads(manifest_input.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"❌ manifest JSON parse failed: {e}")

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
            raise SystemExit(
                f"❌ depends_on must be [] in v5 (violator: {st['id']})"
            )

    try:
        validate_ownership_no_overlap(sub_tasks, workspace)
    except ValueError as e:
        raise SystemExit(f"❌ Gate A failed: {e}")

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

    baseline = detect_baseline(workspace, cfg=cfg)
    roadmap_excerpt = get_roadmap_excerpt(cfg)

    for idx, st in enumerate(sub_tasks):
        sub_id = st["id"]
        if not TASK_ID_RE.match(sub_id):
            raise SystemExit(f"❌ sub-task id must be kebab-case: {sub_id}")

        assert_batch_alive(batch_dir, stage=f"pre-env[{sub_id}]")
        env_path = batch_dir / f"{sub_id}.env"
        write_role_env(env_path, HANDOFF_ROLE_SUB_TASK, batch_id, workspace, sub_id)

        content = templates.build_sub_task_handoff_md(
            task=sub_id, project=project, workspace=workspace,
            next_brief=st["brief"], batch_id=batch_id, sub_task_id=sub_id,
            file_ownership=st["file_ownership"], baseline=baseline,
            roadmap_excerpt=roadmap_excerpt,
            inject_blocks=cfg.inject_blocks, handoff_home=cfg.home,
            git_guard_path=git_guard_dir(),
        )
        atomic.write_with_fsync(queue_dir / f"{sub_id}.md", content)

        if idx > 0:
            print(f"[open-batch]   stagger sleep {STAGGER_SPAWN_SECONDS}s ...")
            time.sleep(STAGGER_SPAWN_SECONDS)

        assert_batch_alive(batch_dir, stage=f"pre-uri[{sub_id}]")
        if not env_path.exists():
            raise SystemExit(f"❌ env vanished mid-spawn ({sub_id}): {env_path}")

        uri = build_uri(cfg, project, sub_id)
        atomic.write_with_fsync(
            queue_dir / f"{sub_id}.uri",
            f"WORKSPACE={workspace}\nURI={uri}\n",
        )
        print(f"[open-batch]   sub-task {sub_id} (#{idx+1}/{len(sub_tasks)}) written")

    print(f"[open-batch] ✅ batch {batch_id} opened with {len(sub_tasks)} sub-tasks")
    _notify(
        f"batch {batch_id}: {len(sub_tasks)} sub-tasks launching",
        f"v5 fan-out / {project}", batch_id,
    )
    return 0


def trigger_fan_in_if_ready(
    project: str, workspace: Path, batch_id: str, queue_dir: Path,
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
    baseline = detect_baseline(workspace, cfg=cfg)
    fan_in_task = manifest["fan_in_task"]

    write_role_env(batch_dir / "fan-in.env", HANDOFF_ROLE_FAN_IN, batch_id, workspace)
    content = templates.build_fan_in_handoff_md(
        project=project, workspace=workspace, batch_id=batch_id,
        manifest=manifest, done_files=done_set, blocked_files=blocked_set,
        baseline=baseline, inject_blocks=cfg.inject_blocks, handoff_home=cfg.home,
    )
    atomic.write_with_fsync(queue_dir / f"{fan_in_task}.md", content)

    uri = build_uri(cfg, project, fan_in_task)
    atomic.write_with_fsync(
        queue_dir / f"{fan_in_task}.uri",
        f"WORKSPACE={workspace}\nURI={uri}\n",
    )
    print(f"[trigger-fan-in] wrote queue/{fan_in_task}.{{md,uri}} + fan-in.env")

    _notify(
        f"batch {batch_id} complete → fan-in tab starting",
        f"v5 fan-in / {project}", fan_in_task,
    )
    return True


def handle_batch_done(
    args, cfg: _config.Config, workspace: Path, project: str, queue_dir: Path,
) -> int:
    if not args.batch_id:
        raise SystemExit("❌ --batch-done requires --batch-id")
    batch_dir = handoff_root() / project / "batches" / args.batch_id
    if not batch_dir.exists():
        blocked_file = queue_dir / f"{args.task}.BLOCKED.md"
        atomic.write_with_fsync(blocked_file, (
            f"# BLOCKED — sub-task `{args.task}`\n\n"
            f"Reason: batch_dir vanished ({batch_dir})\n"
            f"Time: {datetime.now()}\n"
        ))
        print(f"[batch-done] batch_dir missing, BLOCKED written to {blocked_file}")
        return 1
    sub_task_id = args.task.removesuffix("-done")
    summary_path = batch_dir / f"{sub_task_id}.done"
    atomic.write_with_fsync(summary_path, (
        f"sub_task_id: {sub_task_id}\n"
        f"completed_at: {now_iso()}\n"
        f"summary: {args.next_brief}\n"
    ))
    print(f"[batch-done] {summary_path} written")
    trigger_fan_in_if_ready(project, workspace, args.batch_id, queue_dir, cfg=cfg)
    return 0


def handle_batch_blocked(
    args, cfg: _config.Config, workspace: Path, project: str, queue_dir: Path,
) -> int:
    if not args.batch_id:
        raise SystemExit("❌ --batch-blocked requires --batch-id")
    batch_dir = handoff_root() / project / "batches" / args.batch_id
    if not batch_dir.exists():
        blocked_file = queue_dir / f"{args.task}.BLOCKED.md"
        atomic.write_with_fsync(blocked_file, (
            f"# BLOCKED — sub-task `{args.task}`\n\n"
            f"Reason: batch_dir vanished ({batch_dir})\n"
            f"Original reason: {args.blocked_reason}\n"
        ))
        return 1
    sub_task_id = args.task.removesuffix("-blocked")
    blocked_path = batch_dir / f"{sub_task_id}.blocked"
    atomic.write_with_fsync(blocked_path, (
        f"sub_task_id: {sub_task_id}\n"
        f"blocked_at: {now_iso()}\n"
        f"reason: {args.blocked_reason or '(unspecified)'}\n"
    ))
    print(f"[batch-blocked] {blocked_path} written")
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
            out.append({
                "project": proj_dir.name,
                "task": task_id,
                "spawned_path": spawned,
                "submitted_path": ack_dir / f"{task_id}.submitted",
                "queued_path": ack_dir / f"{task_id}.queued",
                "blocked_md_path": queue_dir / f"{task_id}.BLOCKED.md",
                "launched_paths": launched_paths,
                "age_seconds": time.time() - spawned.stat().st_mtime,
            })
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
        for p in [o["spawned_path"], o["submitted_path"], o["queued_path"],
                  o["blocked_md_path"]]:
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
    record.write_text(json.dumps([
        {"project": o["project"], "task": o["task"], "age_seconds": o["age_seconds"]}
        for o in orphans
    ], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📝 留档: {record}")

    if getattr(args, "kill_spawned", False):
        tasks_md = "\n".join(f"- `{o['project']}/{o['task']}`" for o in orphans)
        print("\n⚠️  --kill-spawned: IDE tab title doesn't carry task_id; manual close needed.")
        print("   Please close Claude tabs for:")
        print(tasks_md)
        _notify(
            f"{len(orphans)} tabs need manual close (see terminal)",
            "v5.2 cleanup-orphan", "kill-spawned", sound="Basso",
        )
    return 0


# ─── CLI ────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="handoff-dump",
        description="Generate handoff queue files for the next task or batch.",
    )
    ap.add_argument("--task", default=None,
                    help="kebab-case task ID (optional under --cleanup-orphan)")
    ap.add_argument("--next", dest="next_brief", default=None,
                    help="one-line brief of the next task")
    ap.add_argument("--project", default=None,
                    help="project slug; defaults to basename(cwd)")
    ap.add_argument("--workspace", default=None,
                    help="absolute path to project root; defaults to cwd")
    ap.add_argument("--status", default="active",
                    choices=["active", "done", "blocked"])
    ap.add_argument("--blocked-reason", default="")
    ap.add_argument("--tests", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch-id", default=None,
                    help="v5 batch ID (current task is sub-task or fan-in)")
    ap.add_argument("--batch-done", action="store_true",
                    help="mark sub-task done + try fan-in trigger")
    ap.add_argument("--batch-blocked", action="store_true",
                    help="mark sub-task blocked")
    ap.add_argument("--batch-fan-in", action="store_true",
                    help="(internal) mark this as the fan-in dump")
    ap.add_argument("--open-batch", default=None,
                    help="path to a manifest.json: opens a fan-out batch")
    ap.add_argument("--file-ownership", default=None,
                    help="(internal) sub-task file_ownership JSON")
    ap.add_argument("--cleanup-orphan", action="store_true",
                    help="list / delete orphan ack residue (default dry-run)")
    ap.add_argument("--apply", action="store_true",
                    help="with --cleanup-orphan: actually delete residue")
    ap.add_argument("--kill-spawned", action="store_true",
                    help="with --cleanup-orphan --apply: notify user to close tabs")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = _config.load()

    if args.cleanup_orphan:
        return handle_cleanup_orphan(args)

    if not args.task or not args.next_brief:
        raise SystemExit("❌ --task and --next are required (except under --cleanup-orphan)")
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

    print(f"[dump] project={project} task={args.task} status={args.status}")
    print(f"[dump] workspace={workspace}")

    baseline = detect_baseline(workspace, cfg=cfg)
    print(f"[dump] HEAD={baseline['git_head']}")

    if args.dry_run:
        roadmap_excerpt = get_roadmap_excerpt(cfg)
        md_path = queue_dir / f"{args.task}.md"
        content = templates.build_handoff_md(
            task=args.task, project=project, workspace=workspace,
            next_brief=args.next_brief, status=args.status,
            tests=args.tests or None, baseline=baseline,
            roadmap_excerpt=roadmap_excerpt, inject_blocks=cfg.inject_blocks,
            handoff_home=cfg.home, handoff_md_path=md_path,
        )
        print("=" * 60)
        print(f"DRY-RUN: target paths\n  {md_path}\n  {queue_dir / f'{args.task}.uri'}")
        print("=" * 60)
        print(content[:2000])
        print("...")
        return 0

    return write_active_dump(
        cfg=cfg, project=project, task=args.task, workspace=workspace,
        next_brief=args.next_brief, status=args.status,
        tests=args.tests or None, baseline=baseline, queue_dir=queue_dir,
        osascript_subtitle=args.blocked_reason or None,
    )


if __name__ == "__main__":
    sys.exit(main() or 0)
