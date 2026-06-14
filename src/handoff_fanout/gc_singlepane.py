"""``handoff gc-singlepane`` — janitor for STALE singlepane *coordinator* sidecars
(focusjump-fix 2026-06-15 / S4 — hygiene, NON-correctness path).

WHY (the L2 backlog): singlepane succession never cleaned its predecessors, so a project's
``queue/`` accumulates ``<task>.singlepane`` sidecars (p10–p26 all still ``active``, all pointing
at the SAME shared repo cwd) plus their out-of-tree ``singlepane/<task>.handoff.code-workspace``
files. That backlog is what makes the SHARED identity resolver
(``dx_session_role._scan_singlepane_supervisor``) ambiguous. The CORRECTNESS fix is the resolver's
own「唯一化或失败」(S1) + succession writing the predecessor ``.done`` going forward (S2); this GC
is the one-shot + ongoing *hygiene* that drains the backlog so the resolver can resolve UNIQUELY
again and ``dx-spawn`` auto-derive (S3) starts working.

🔴 SAFETY (codex/deepseek — highest-risk item = never delete a LIVE coordinator's focus file):
  * A candidate must be NOT-LIVE — proven by a cross-Space VS Code window probe (Quartz
    ``CGWindowListCopyWindowInfo`` over ALL Spaces, owner ``Code``; the established ``winlist``
    binary is preferred when present). Liveness is matched by the task token bounded in the window
    title (so ``sw-coord-p1`` never matches ``…sw-coord-p12…``).
  * FAIL-SAFE: if the liveness probe is unavailable / fails (returns ``None``) NOTHING is eligible
    and ``--execute`` aborts — when we can't prove deadness we never delete (so a running
    coordinator, e.g. sw-coord-p26, is protected even if it isn't passed via ``--protect``).
  * NOT mtime-alone (deepseek: a slow-updating live coordinator would be mis-judged) — liveness is
    the binding gate; ``--retention-days`` is only a conservative buffer ON TOP of not-live.
  * REVERSIBLE: ``--execute`` QUARANTINES (moves files under ``<home>/_gc_quarantine/<stamp>/``
    preserving the relative path), never ``unlink`` — even a mistake is restorable.
  * dry-run by DEFAULT; ``--protect <task>`` (repeatable) for belt-and-suspenders.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

from handoff_fanout import config as _config

# VS Code's Quartz ``kCGWindowOwnerName`` is "Code"; include common variants defensively.
_CODE_OWNER_NAMES = {"Code", "Code - Insiders", "Visual Studio Code", "Electron"}
# Only coordinator (succession) sidecars feed the resolver ambiguity → scope GC to them.
_SUPERVISOR_ROLE = "supervisor_succession"
_DEFAULT_RETENTION_DAYS = 1.0


def _live_code_window_titles() -> list[str] | None:
    """All VS Code window titles across ALL Spaces, or ``None`` when the probe is unavailable.

    Preference order: the established ``winlist`` binary (the system's trusted cross-Space liveness
    primitive — same one the focus-jump return-anchor uses) → Quartz directly. ``None`` on any total
    failure so the caller FAILS SAFE (refuses to GC when liveness can't be proven). Module-level so
    tests monkeypatch it deterministically (no real windows needed)."""
    titles = _winlist_titles()
    if titles is not None:
        return titles
    return _quartz_titles()


def _winlist_titles() -> list[str] | None:
    import os
    import subprocess

    binp = os.environ.get("HANDOFF_WINLIST_BIN") or os.path.expanduser(
        "~/Projects/dharmaxis/scripts/vscode-spaces/winlist"
    )
    if not (os.path.isfile(binp) and os.access(binp, os.X_OK)):
        return None
    try:
        out = subprocess.run(
            [binp, "Code", "--all"], capture_output=True, text=True, timeout=10
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout or "[]")
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    return [str((w or {}).get("title") or "") for w in data]


def _quartz_titles() -> list[str] | None:
    try:
        import Quartz  # type: ignore
    except Exception:
        return None
    try:
        opts = Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements
        infos = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
        if infos is None:
            return None
        out: list[str] = []
        for w in infos:
            if (w.get("kCGWindowOwnerName") or "") in _CODE_OWNER_NAMES:
                out.append(str(w.get("kCGWindowName") or ""))
        return out
    except Exception:
        return None


def _task_is_live(task: str, titles: list[str]) -> bool:
    """A window title carries the task as a ``·``-delimited token (``<proj>·<task>·<role>·<nonce>``
    or ``🧭中枢·<task>``). Match the task bounded by non-``[A-Za-z0-9-]`` so ``sw-coord-p1`` does NOT
    match ``…sw-coord-p12…`` (the substring trap)."""
    pat = re.compile(r"(?<![A-Za-z0-9-])" + re.escape(task) + r"(?![A-Za-z0-9-])")
    return any(pat.search(t) for t in titles)


def _iter_supervisor_sidecars(root: Path, project: str | None):
    """Yield ``(project_name, queue_dir, sidecar_path, data)`` for every active (no ``.done``)
    ``supervisor_succession`` singlepane sidecar."""
    if not root.exists():
        return
    for proj_dir in sorted(root.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name in {"locks", "_recovery", "_gc_quarantine"}:
            continue
        if project is not None and proj_dir.name != project:
            continue
        queue_dir = proj_dir / "queue"
        if not queue_dir.is_dir():
            continue
        for sp in sorted(queue_dir.glob("*.singlepane")):
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("role") != _SUPERVISOR_ROLE:
                continue
            yield proj_dir.name, queue_dir, sp, data


def find_gc_candidates(
    root: Path,
    *,
    project: str | None = None,
    retention_days: float = _DEFAULT_RETENTION_DAYS,
    protect: set[str] | None = None,
    live_titles: list[str] | None,
    now: float | None = None,
) -> list[dict]:
    """Pure candidate finder (no I/O beyond reads; ``live_titles``/``now`` injected → fully testable).

    A stale supervisor sidecar is eligible IFF ALL hold:
      * its task is NOT in ``protect``;
      * liveness is KNOWN (``live_titles is not None``) AND no live window carries the task
        (``live_titles is None`` → liveness UNKNOWN → NOT eligible, fail-safe);
      * terminal-or-aged: a ``<task>.done`` exists OR the sidecar mtime age ≥ ``retention_days``.

    Record: ``{project, task, reason, sidecar: Path, workspace: Path | None}``.
    """
    protect = protect or set()
    now = time.time() if now is None else now
    out: list[dict] = []
    for proj_name, queue_dir, sp, data in _iter_supervisor_sidecars(root, project):
        task = sp.name[: -len(".singlepane")]
        if task in protect:
            continue
        if live_titles is None or _task_is_live(task, live_titles):
            continue  # live OR liveness-unknown → never a candidate (fail-safe)
        has_done = (queue_dir / f"{task}.done").exists()
        try:
            age_days = (now - sp.stat().st_mtime) / 86400.0
        except OSError:
            age_days = 0.0
        if not (has_done or age_days >= retention_days):
            continue  # young + not explicitly done → keep (conservative buffer)
        ws_raw = data.get("workspace") or ""
        ws_path = Path(ws_raw) if ws_raw and Path(ws_raw).is_file() else None
        out.append(
            {
                "project": proj_name,
                "task": task,
                "reason": "done" if has_done else f"aged {age_days:.1f}d≥{retention_days}d, not-live",
                "sidecar": sp,
                "workspace": ws_path,
            }
        )
    return out


def _quarantine(root: Path, path: Path, stamp: str) -> Path:
    """Move ``path`` under ``<home>/_gc_quarantine/<stamp>/`` preserving its path relative to
    ``root`` (so a mistaken GC is restorable by reversing the move). Returns the new location."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = Path(path.name)
    dest = root / "_gc_quarantine" / stamp / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="handoff gc-singlepane",
        description="Quarantine STALE singlepane coordinator sidecars + workspace files so the "
        "shared identity resolver resolves uniquely again. dry-run by default; liveness-gated; "
        "reversible (quarantine, never unlink).",
    )
    ap.add_argument("--project", default=None, help="Limit to one project (default: all).")
    ap.add_argument(
        "--retention-days",
        type=float,
        default=_DEFAULT_RETENTION_DAYS,
        help="Conservative buffer: a not-done sidecar younger than this is kept (default 1.0). "
        "Liveness is the binding gate; this only guards probe blind spots for recent sidecars.",
    )
    ap.add_argument(
        "--protect",
        action="append",
        default=[],
        metavar="TASK",
        help="Task id to NEVER quarantine (repeatable). Belt-and-suspenders on top of the "
        "live-window gate (e.g. --protect sw-coord-p26).",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Actually quarantine (move) the files (default: dry-run, moves nothing).",
    )
    args = ap.parse_args(argv)

    root = _config.home_dir()
    live = _live_code_window_titles()
    if live is None:
        print(
            "[gc-singlepane] ⚠️ liveness probe UNAVAILABLE (no winlist binary / Quartz) — "
            "FAIL-SAFE: nothing is eligible (cannot prove any sidecar is dead).",
            file=sys.stderr,
        )
        if args.execute:
            print(
                "[gc-singlepane] --execute ABORTED: refusing to quarantine when liveness is "
                "unknown (a running coordinator could be wrongly cleaned).",
                file=sys.stderr,
            )
            return 1
    else:
        print(f"[gc-singlepane] liveness probe: {len(live)} VS Code window(s) across all Spaces.")

    candidates = find_gc_candidates(
        root,
        project=args.project,
        retention_days=args.retention_days,
        protect=set(args.protect),
        live_titles=live,
    )
    if not candidates:
        print("[gc-singlepane] nothing to GC — no stale, not-live supervisor sidecars.")
        return 0

    stamp = time.strftime("gc-%Y%m%dT%H%M%S", time.gmtime())
    moved = 0
    for rec in candidates:
        files = [rec["sidecar"]] + ([rec["workspace"]] if rec["workspace"] else [])
        for f in files:
            verb = "quarantine" if args.execute else "would quarantine"
            print(f"  {verb} {rec['project']}/{rec['task']} ({rec['reason']}): {f}")
            if args.execute:
                _quarantine(root, f, stamp)
            moved += 1

    verb = "quarantined" if args.execute else "would quarantine"
    print(f"[gc-singlepane] {verb} {moved} file(s) across {len(candidates)} stale coordinator(s).")
    if args.execute:
        print(f"[gc-singlepane] moved under {root}/_gc_quarantine/{stamp}/ (reversible).")
    else:
        print("[gc-singlepane] dry-run — re-run with --execute to apply (review the list first).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
