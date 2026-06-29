#!/usr/bin/env python3
"""autoclose-audited-workers.py — req3 driver/sweep (cross-project).

🔴🔴 SAFETY-CRITICAL. Auto-closes ONLY worker windows that the ``handoff_fanout``
``autoclose_gate`` has cleared against ALL 5 fail-safe predicates (worker-not-coordinator,
non-forgeable audited-to-terminal git corroboration, not-in-flight, not-dirty, fail-safe).
Supreme rule: 宁可漏关，绝不误关 — any uncertainty ⇒ DON'T close.

DIVISION OF RESPONSIBILITY (do not blur):
  • The dangerous JUDGEMENT ("is THIS worker audited-to-terminal + idle + clean?") lives
    entirely in ``handoff_fanout.autoclose_gate`` (pure, unit-tested, no GUI). This driver
    NEVER re-implements a predicate.
  • This driver is thin GLUE: discover candidate tasks → run the gate per task → bind each
    cleared task to its STABLE Quartz window-id via a same-round winlist detection (intact
    nonce title only — a fully AI-retitled window with no recoverable identity is left for
    MANUAL close, never guessed) → hand the vetted WID allow-list to the audited
    ``coord-close-windows.py --close-wid`` tool (which does the mechanical safe close).

SAFETY GATING (this layer, on top of the predicate gate):
  • DRY-RUN by default. ``--execute`` only ever takes effect when the dedicated opt-in
    switch ``worker-autoclose.enabled`` is ON (DEFAULT-OFF; SEPARATE from the v4
    coordinator-autoclose switch, which stays OFF). Otherwise the run is forced to dry-run.
  • KILL-SWITCH sentinel (``.worker-autoclose-off``, fleet or per-project): present ⇒ the
    driver does nothing (not even dry-run) and exits 0.
  • Every decision (close / refuse + reason / unbindable) is appended to a durable JSONL log.
  • Reversible by construction: dry-run default, opt-in switch, kill-switch, durable log,
    and a closed window's session is /resume-recoverable.

Modes:
  --task <task>   going-forward: gate + close ONE just-discharged worker (signal path).
  --sweep         janitor: gate EVERY ``ack/<task>.audit_discharged`` signal in the project.
  --reconcile     git-terminal reconciler: gate EVERY OPEN worker window of the project from
                  its OWN already-merged delivery (NO discharge signal needed). Solves the
                  signal path's inertness (the ``audit_discharged`` signal is only ever
                  written by a coordinator hand-running ``handoff audit-discharge``, which
                  almost never happens). Gated on a SEPARATE, DEFAULT-OFF switch
                  ``worker-autoclose-reconcile.enabled`` (decoupled from ``worker-autoclose
                  .enabled``); ``--execute`` still additionally requires ``worker-autoclose
                  .enabled`` (defense in depth). All judgement is in
                  ``autoclose_gate.reconcile_open_worker_windows`` / ``gate_task_git_terminal``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    from handoff_fanout import autoclose_gate as _gate
    from handoff_fanout import config as _config
except ImportError as e:  # fail-safe: the engine must be importable (editable install)
    sys.stderr.write(
        f"ERR: cannot import handoff_fanout ({e}). This driver needs the handoff-fanout "
        "engine on the Python path (editable install). Aborting (fail-safe).\n"
    )
    sys.exit(2)

CCW_PATH = Path(__file__).resolve().parent / "coord-close-windows.py"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def log(msg: str) -> None:
    print(msg, flush=True)


# ─── opt-in switch + kill-switch (DEFAULT-OFF; SEPARATE from v4 coordinator-autoclose) ──


def opt_in_enabled(cfg: _config.Config, project: str) -> bool:
    """``worker-autoclose.enabled`` — DEFAULT-OFF. Any one of: env
    ``HANDOFF_WORKER_AUTOCLOSE_ENABLED=1`` / fleet ``$HANDOFF_HOME/worker-autoclose.enabled``
    / per-project ``$HANDOFF_HOME/<project>/worker-autoclose.enabled``. Deliberately
    DISTINCT from the v4 coordinator-autoclose ``autoclose.enabled`` (which stays OFF)."""
    if os.environ.get("HANDOFF_WORKER_AUTOCLOSE_ENABLED") == "1":
        return True
    if (cfg.home / "worker-autoclose.enabled").exists():
        return True
    if (cfg.home / project / "worker-autoclose.enabled").exists():
        return True
    return False


def kill_switch_active(cfg: _config.Config, project: str) -> bool:
    """Emergency one-touch disable (no config edit): ``.worker-autoclose-off`` fleet-wide
    or per-project. Present ⇒ the driver does nothing at all."""
    return (cfg.home / ".worker-autoclose-off").exists() or (
        cfg.home / project / ".worker-autoclose-off"
    ).exists()


# ─── window detection (reuse the audited close tool's winlist + parser) ──────


_CCW = None


def _close_tool():
    """Lazily importlib-load coord-close-windows.py (sibling, non-git) to reuse its winlist
    probe + title parser, so detection + close share ONE identity implementation."""
    global _CCW
    if _CCW is None:
        spec = importlib.util.spec_from_file_location("coord_close_windows", CCW_PATH)
        if not spec or not spec.loader:
            raise RuntimeError(f"cannot load {CCW_PATH}")
        _CCW = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_CCW)
    return _CCW


def detect_windows() -> list[dict]:
    return _close_tool().probe_windows()


def resolve_wid(windows: list[dict], project: str, task: str, nonce: str | None) -> int | None:
    """Bind a cleared task to its STABLE Quartz window-id via the SAME-ROUND winlist.

    Safe binding requires an intact STRUCTURED title: project field == ``project`` AND
    task-id field == ``task`` AND nonce field == the worker's spawn ``nonce`` AND that nonce
    UNIQUE across ALL current windows. Exactly one match → its window_number; otherwise
    ``None`` (a fully AI-retitled window has no recoverable task identity from winlist → it
    is NOT auto-closed, it is left for manual close — fail-safe, never guessed)."""
    if not nonce:
        return None
    parse_title = _close_tool().parse_title
    nonce_count = sum(1 for w in windows if nonce in w.get("title", ""))
    if nonce_count != 1:
        return None  # nonce not uniquely identifying → unsafe → don't bind
    matches = []
    for w in windows:
        proj, tid, is_coord, wnonce = parse_title(w.get("title", ""))
        if is_coord:
            continue  # never bind a coordinator window
        if proj == project and tid == task and wnonce == nonce:
            matches.append(w)
    if len(matches) != 1:
        return None
    return matches[0].get("window_number")


# ─── close-tool invocation (the audited mechanical close) ────────────────────


def invoke_close_tool(project: str, wids: list[int], execute: bool) -> int:
    """Invoke coord-close-windows.py --close-wid for the vetted WID allow-list. dry-run
    unless ``execute``. Returns the tool's rc; its stdout is echoed (and logged by caller)."""
    cmd = [sys.executable, str(CCW_PATH), "--project", project, "--close-wid",
           ",".join(str(w) for w in wids)]
    if execute:
        cmd.append("--execute")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.stdout:
        log(r.stdout.rstrip())
    if r.returncode != 0 and r.stderr:
        log(f"  ⚠️ coord-close-windows rc={r.returncode}: {r.stderr.strip()[:300]}")
    return r.returncode


# ─── durable decision log ────────────────────────────────────────────────────


def log_path(cfg: _config.Config, project: str) -> Path:
    return cfg.home / project / "autoclose-audited.log"


def _append_log(cfg: _config.Config, project: str, record: dict) -> None:
    p = log_path(cfg, project)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now_iso(), **record}, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as e:
        log(f"  ⚠️ could not write decision log {p}: {e}")


# ─── the run ─────────────────────────────────────────────────────────────────


def run(
    cfg: _config.Config,
    project: str,
    tasks: list[str],
    *,
    execute: bool,
    idle_threshold: float,
    now_epoch: float | None = None,
    projects_root: Path | None = None,
    windows: list[dict] | None = None,
) -> dict:
    """Gate every task, bind the cleared ones to WIDs, and (on execute) close them via the
    audited tool. Returns a summary dict. Pure orchestration — ALL judgement is ``gate_task``.
    """
    if windows is None:
        windows = detect_windows()
    vetted: list[tuple[str, int]] = []
    refused: list[tuple[str, str]] = []
    unbindable: list[str] = []
    for task in tasks:
        d = _gate.gate_task(
            cfg, project, task, idle_threshold_sec=idle_threshold,
            now_epoch=now_epoch, projects_root=projects_root,
        )
        if not d.close_ok:
            refused.append((task, d.reason))
            log(f"  ⏭️  {task}: REFUSE [{d.reason}] {d.detail}")
            _append_log(cfg, project, {
                "task": task, "decision": "refuse", "reason": d.reason, "detail": d.detail,
            })
            continue
        wid = resolve_wid(windows, project, task, d.nonce)
        if wid is None:
            unbindable.append(task)
            log(f"  🔎 {task}: audited-to-terminal but NO unique structured window found "
                f"(AI-titled / no unique nonce) → left for MANUAL close (fail-safe)")
            _append_log(cfg, project, {
                "task": task, "decision": "unbindable", "reason": "no-unique-window",
                "nonce": d.nonce, "evidence": d.evidence,
            })
            continue
        vetted.append((task, wid))
        log(f"  ✅ {task}: CLEARED → wid {wid} (merge {str(d.evidence.get('merge_sha',''))[:8]})")
        _append_log(cfg, project, {
            "task": task, "decision": "cleared", "wid": wid, "evidence": d.evidence,
            "will_execute": execute,
        })

    summary = {
        "project": project, "execute": execute,
        "cleared": [t for t, _ in vetted], "wids": [w for _, w in vetted],
        "refused": refused, "unbindable": unbindable,
    }
    if not vetted:
        log("No windows cleared for close this run.")
        return summary
    wids = [w for _, w in vetted]
    log(f"{'EXECUTING' if execute else 'DRY-RUN'} close for wids {wids} via coord-close-windows.py")
    rc = invoke_close_tool(project, wids, execute)
    summary["close_tool_rc"] = rc
    _append_log(cfg, project, {
        "decision": "invoke-close-tool", "wids": wids, "execute": execute, "rc": rc,
    })
    return summary


def run_reconcile(
    cfg: _config.Config,
    project: str,
    *,
    execute: bool,
    idle_threshold: float,
    now_epoch: float | None = None,
    projects_root: Path | None = None,
    windows: list[dict] | None = None,
) -> dict:
    """Git-terminal reconciler glue: gate EVERY OPEN worker window of this project from its OWN
    already-merged delivery (no discharge signal), bind each cleared window to its WID, and
    (on execute) close them via the audited tool. PURE orchestration — ALL judgement is in
    ``autoclose_gate.reconcile_open_worker_windows`` (which runs ``gate_task_git_terminal`` per
    window and returns ONLY the close_ok decisions, each annotated with its ``task``)."""
    if windows is None:
        windows = detect_windows()
    parse_title = _close_tool().parse_title
    decisions = _gate.reconcile_open_worker_windows(
        cfg, project, windows, parse_title,
        idle_threshold_sec=idle_threshold, now_epoch=now_epoch, projects_root=projects_root,
    )
    vetted: list[tuple[str, int]] = []
    unbindable: list[str] = []
    for d in decisions:
        if not d.task:  # defensive: a close decision must carry its task identity to WID-bind
            continue
        wid = resolve_wid(windows, project, d.task, d.nonce)
        if wid is None:
            unbindable.append(d.task)
            log(f"  🔎 {d.task}: git-terminal CLEARED but NO unique structured window found "
                f"(AI-titled / no unique nonce) → left for MANUAL close (fail-safe)")
            _append_log(cfg, project, {
                "task": d.task, "decision": "unbindable", "reason": "no-unique-window",
                "mode": "reconcile", "nonce": d.nonce, "evidence": d.evidence,
            })
            continue
        vetted.append((d.task, wid))
        log(f"  ✅ {d.task}: git-terminal CLEARED → wid {wid} "
            f"(merge {str(d.evidence.get('merge_sha', ''))[:8]})")
        _append_log(cfg, project, {
            "task": d.task, "decision": "cleared", "mode": "reconcile", "wid": wid,
            "evidence": d.evidence, "will_execute": execute,
        })

    summary = {
        "project": project, "execute": execute, "mode": "reconcile",
        "cleared": [t for t, _ in vetted], "wids": [w for _, w in vetted],
        "unbindable": unbindable,
    }
    if not vetted:
        log("No windows cleared for close this reconcile run.")
        return summary
    wids = [w for _, w in vetted]
    log(f"{'EXECUTING' if execute else 'DRY-RUN'} close for wids {wids} via coord-close-windows.py")
    rc = invoke_close_tool(project, wids, execute)
    summary["close_tool_rc"] = rc
    _append_log(cfg, project, {
        "decision": "invoke-close-tool", "mode": "reconcile", "wids": wids,
        "execute": execute, "rc": rc,
    })
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="autoclose-audited-workers.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--project", required=True, help="project slug")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--task", help="going-forward: gate + close ONE just-discharged worker")
    g.add_argument("--sweep", action="store_true",
                   help="janitor: gate EVERY ack/<task>.audit_discharged in the project")
    g.add_argument("--reconcile", action="store_true",
                   help="git-terminal reconciler: gate EVERY OPEN worker window from its own "
                        "merged delivery (no discharge signal); gated on the SEPARATE "
                        "DEFAULT-OFF worker-autoclose-reconcile.enabled switch")
    ap.add_argument("--execute", action="store_true",
                    help="actually close (only honored when worker-autoclose.enabled is ON)")
    ap.add_argument("--idle-threshold", type=float, default=_gate.DEFAULT_IDLE_THRESHOLD_SEC,
                    help=f"idle floor seconds (default {_gate.DEFAULT_IDLE_THRESHOLD_SEC})")
    args = ap.parse_args(argv)

    cfg = _config.load()

    if kill_switch_active(cfg, args.project):
        log(f"🛑 kill-switch active (.worker-autoclose-off) — doing nothing for {args.project}.")
        return 0

    requested_execute = args.execute
    enabled = opt_in_enabled(cfg, args.project)
    execute = requested_execute and enabled
    if requested_execute and not enabled:
        log("⚠️ --execute requested but worker-autoclose.enabled is OFF (DEFAULT-OFF) → "
            "forcing DRY-RUN. Enable via: touch $HANDOFF_HOME/<project>/worker-autoclose.enabled")

    if args.reconcile:
        # SEPARATE, DEFAULT-OFF switch — decoupled from worker-autoclose.enabled. OFF ⇒ no-op
        # (not even dry-run), so deploying the hourly --reconcile daemon is inert until the
        # owner flips this switch. ``execute`` (computed above) still additionally requires
        # worker-autoclose.enabled — defense in depth.
        if not _gate.reconcile_enabled(cfg, args.project):
            log(f"⏭️  --reconcile but worker-autoclose-reconcile.enabled is OFF (DEFAULT-OFF) "
                f"for {args.project} → no-op. Enable via: "
                f"touch $HANDOFF_HOME/<project>/worker-autoclose-reconcile.enabled")
            return 0
        log(f"=== autoclose reconcile | project={args.project} | execute={execute} ===")
        summary = run_reconcile(cfg, args.project, execute=execute, idle_threshold=args.idle_threshold)
        log(f"=== SUMMARY: cleared {len(summary['cleared'])} | "
            f"unbindable {len(summary['unbindable'])} | execute={execute} ===")
        return 0

    if args.sweep:
        tasks = _gate.discharged_tasks(cfg, args.project)
        log(f"=== autoclose sweep | project={args.project} | {len(tasks)} discharged signal(s) "
            f"| execute={execute} ===")
    else:
        tasks = [args.task]
        log(f"=== autoclose going-forward | project={args.project} | task={args.task} "
            f"| execute={execute} ===")
    if not tasks:
        log("No discharged signals to consider.")
        return 0

    summary = run(cfg, args.project, tasks, execute=execute, idle_threshold=args.idle_threshold)
    log(f"=== SUMMARY: cleared {len(summary['cleared'])} | refused {len(summary['refused'])} "
        f"| unbindable {len(summary['unbindable'])} | execute={execute} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
