"""``handoff prune`` — remove leftover sidecar files for terminal tasks.

A task is *terminal* once it has a ``.done`` or ``.BLOCKED.md`` marker. After
that, its ``.heartbeat`` / ``.529-suspected`` / ``.uri`` sidecars are dead
weight: the heartbeat reads stale forever, so watchdog mode 4/6 can mis-flag a
finished task as ``529-suspected`` (and, with enforcement on, hunt PIDs for a
task that completed cleanly). ``dump`` now drops the heartbeat on the terminal
transition, but queues built before that fix accumulated leftovers, and a
crash between the ``.done`` write and the cleanup can still leak one.

prune is the janitor. For each *terminal* task it removes ONLY the sidecars —
the dead ``queue/`` weight above plus the inert ``ack/<task>.worker_reported``
completion breadcrumb — never the ``.md`` / ``.done`` / ``.BLOCKED.md`` history,
never an ``ack/`` file the watchdog / autoclose / gate / reclaim machinery reads
(see ``ACK_SIDECAR_EXTS``), and never a non-terminal (active or unknown) task's
files. Default is a dry-run; pass ``--execute`` to actually unlink.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from handoff_fanout import config as _config

# queue/ sidecars that are safe to drop once a task is terminal. Ordered for
# stable output. Deliberately excludes .md / .done / .BLOCKED.md (history) and
# manifest/evidence artifacts (live elsewhere).
SIDECAR_EXTS = ("heartbeat", "529-suspected", "uri")

# ack/ sidecars safe to drop once a task is terminal. DELIBERATELY narrow: only
# pure post-completion breadcrumbs that no live mechanism reads. Everything the
# watchdog / autoclose / gate / reclaim machinery touches is EXCLUDED — even for a
# terminal task — because pruning it would break a live mechanism:
#   .spawned / .submitted / .queued / .failed — the autoclose 4-sentinel scan +
#       watchdog terminal checks iterate/read these (install/auto-continue.sh);
#   .old_ready — the successor session's §0 retro-audit trail;
#   .host_pid.json / .reclaim_* / .head.json — the §6c reclaim protocol;
#   .audit.override.json / .audit_overdue.txt / .owner_ack.*.json — codex-audit gate;
#   .retro.* / .mandate_drift.json — retro-gate artifacts.
# .worker_reported is the lone safe ext: a worker touches it on completion and
# only the coordinator's patrol of the *live* task + the status board read it, so
# a terminal task's copy is inert dead weight. Grow this list only after proving
# the new ext has no live reader.
ACK_SIDECAR_EXTS = ("worker_reported",)

# Top-level dirs under HANDOFF_HOME that are not projects.
SPECIAL_DIRS = {"locks", "_recovery"}

_BLOCKED_SUFFIX = ".BLOCKED.md"


def _iter_queue_dirs(root: Path, project: str | None):
    """Yield ``(project_name, queue_dir)`` for every project queue."""
    if not root.exists():
        return
    for proj_dir in sorted(root.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name in SPECIAL_DIRS:
            continue
        if project is not None and proj_dir.name != project:
            continue
        queue_dir = proj_dir / "queue"
        if queue_dir.is_dir():
            yield proj_dir.name, queue_dir


def _terminal_task_ids(queue_dir: Path) -> set[str]:
    done = {f.stem for f in queue_dir.glob("*.done")}
    blocked = {f.name[: -len(_BLOCKED_SUFFIX)] for f in queue_dir.glob(f"*{_BLOCKED_SUFFIX}")}
    return done | blocked


def find_prunable(root: Path, project: str | None = None) -> list[dict]:
    """One record per terminal task that still has at least one sidecar.

    Record shape: ``{"project": str, "task": str, "files": list[Path]}``.
    """
    out: list[dict] = []
    for proj_name, queue_dir in _iter_queue_dirs(root, project):
        ack_dir = queue_dir.parent / "ack"
        for task_id in sorted(_terminal_task_ids(queue_dir)):
            files = [
                queue_dir / f"{task_id}.{ext}"
                for ext in SIDECAR_EXTS
                if (queue_dir / f"{task_id}.{ext}").exists()
            ]
            files += [
                ack_dir / f"{task_id}.{ext}"
                for ext in ACK_SIDECAR_EXTS
                if (ack_dir / f"{task_id}.{ext}").exists()
            ]
            if files:
                out.append({"project": proj_name, "task": task_id, "files": files})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="handoff prune",
        description=(
            "Remove leftover queue (heartbeat/529/uri) + ack (worker_reported) "
            "sidecars for terminal tasks."
        ),
    )
    ap.add_argument("--project", default=None, help="Limit to one project (default: all).")
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Actually unlink the sidecars (default: dry-run, removes nothing).",
    )
    args = ap.parse_args(argv)

    root = _config.home_dir()
    records = find_prunable(root, args.project)
    if not records:
        print("[prune] nothing to prune — no terminal tasks with leftover sidecars.")
        return 0

    total = 0
    for rec in records:
        for f in rec["files"]:
            print(f"  {'rm' if args.execute else 'would rm'} {rec['project']}/{f.parent.name}/{f.name}")
            if args.execute:
                f.unlink(missing_ok=True)
            total += 1

    verb = "removed" if args.execute else "would remove"
    print(f"[prune] {verb} {total} sidecar(s) across {len(records)} terminal task(s).")
    if not args.execute:
        print("[prune] dry-run — re-run with --execute to apply.")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
