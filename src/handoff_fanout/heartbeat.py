"""Fan-in tab companion: heartbeat / completion / metrics / calibration.

Four subcommands:

  - ``heartbeat`` runs as a background process inside the fan-in tab;
    every ~60 s it ``touch``-es ``_fan_in_heartbeat`` so the watchdog can
    tell the tab is alive. Exits on ``_fan_in_done`` / ``_aborted`` /
    SIGTERM / SIGINT / hard 3 h ceiling / any STOP_AUTO trigger.

  - ``complete`` atomic-creates ``_fan_in_done`` and appends a record
    to ``metrics.jsonl`` (n_sub_tasks, estimated vs actual minutes, the
    Amdahl numbers, a one-line summary).

  - ``calibration`` is intended for the main session's Gate C path. It
    reads up to ``CALIBRATION_WINDOW`` recent metrics records, computes
    the mean ``actual/estimated`` ratio, clamps it to [0.5, 3.0], and
    prints it on a single line — pipe-friendly.

  - ``status`` dumps the batch's state-machine markers for debugging.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from handoff_fanout import atomic
from handoff_fanout import config as _config

HEARTBEAT_INTERVAL = 60
HEARTBEAT_MAX_LIFETIME = 3 * 60 * 60  # 3 hours
DEFAULT_CALIBRATION = 1.0
CALIBRATION_WINDOW = 10
CALIBRATION_MIN = 0.5
CALIBRATION_MAX = 3.0


def handoff_root() -> Path:
    return _config.home_dir()


# ─── helpers ────────────────────────────────────────────────────────────────


def any_stop_auto(project: str, batch_id: str | None = None) -> str | None:
    root = handoff_root()
    paths = [root / "done", root / "STOP_AUTO"]
    if project:
        paths.append(root / project / "STOP_AUTO")
    if batch_id and project:
        paths.append(root / project / "batches" / batch_id / "STOP")
    for p in paths:
        if p.exists():
            return str(p)
    return None


def load_manifest(batch_dir: Path) -> dict:
    f = batch_dir / "manifest.json"
    if not f.exists():
        raise SystemExit(f"❌ manifest.json missing: {f}")
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"❌ manifest.json corrupt: {e}") from e


def resolve_project(batch_dir: Path) -> str:
    abs_dir = batch_dir.resolve()
    if abs_dir.parent.name != "batches":
        raise SystemExit(f"❌ batch_dir not under batches/: {abs_dir}")
    return abs_dir.parent.parent.name


# ─── heartbeat daemon ───────────────────────────────────────────────────────


def cmd_heartbeat(batch_dir: Path) -> int:
    if not batch_dir.is_dir():
        print(f"❌ batch_dir missing: {batch_dir}", file=sys.stderr)
        return 2

    project = resolve_project(batch_dir)
    batch_id = batch_dir.name
    started = batch_dir / "_fan_in_started"
    heartbeat = batch_dir / "_fan_in_heartbeat"
    done = batch_dir / "_fan_in_done"
    aborted = batch_dir / "_aborted"

    if not started.exists():
        atomic.atomic_create(started)
    if done.exists():
        print("✅ _fan_in_done already exists — heartbeat not needed", file=sys.stderr)
        return 0

    heartbeat.touch()
    print(
        f"💓 heartbeat daemon pid={os.getpid()} batch={batch_id} interval={HEARTBEAT_INTERVAL}s",
        file=sys.stderr,
    )

    interrupted = {"flag": False, "reason": ""}

    def _handler(signum, _frame):
        interrupted["flag"] = True
        interrupted["reason"] = f"signal {signum}"

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

    start = time.monotonic()
    while True:
        slept = 0.0
        while slept < HEARTBEAT_INTERVAL and not interrupted["flag"]:
            time.sleep(1.0)
            slept += 1.0
        if interrupted["flag"]:
            print(f"⚠️ {interrupted['reason']} — exit", file=sys.stderr)
            return 0
        if done.exists():
            print("✅ _fan_in_done detected — exit", file=sys.stderr)
            return 0
        if aborted.exists():
            print("⚠️ _aborted detected — exit", file=sys.stderr)
            return 0
        if not batch_dir.is_dir():
            print("⚠️ batch_dir vanished — exit", file=sys.stderr)
            return 0
        stop = any_stop_auto(project, batch_id)
        if stop:
            print(f"⚠️ STOP triggered ({stop}) — exit", file=sys.stderr)
            return 0
        if time.monotonic() - start > HEARTBEAT_MAX_LIFETIME:
            print("⚠️ heartbeat 3h ceiling — exit (watchdog takes over)", file=sys.stderr)
            return 0
        try:
            heartbeat.touch()
        except OSError as e:
            print(f"❌ heartbeat touch failed: {e} — exit", file=sys.stderr)
            return 3


# ─── completion + metrics ───────────────────────────────────────────────────


def cmd_complete(
    batch_dir: Path,
    actual_minutes: float | None,
    amdahl_actual: float | None,
    summary: str,
) -> int:
    if not batch_dir.is_dir():
        print(f"❌ batch_dir missing: {batch_dir}", file=sys.stderr)
        return 2

    manifest = load_manifest(batch_dir)
    project = resolve_project(batch_dir)
    batch_id = manifest.get("batch_id", batch_dir.name)
    done = batch_dir / "_fan_in_done"

    if done.exists():
        print("✅ _fan_in_done already exists — idempotent skip", file=sys.stderr)
    else:
        atomic.atomic_create(done)

    estimated = sum(s.get("estimated_minutes", 0) for s in manifest.get("sub_tasks", []))
    amdahl_est = manifest.get("amdahl_estimate", {}).get("estimated_speedup", 0.0)
    actual = actual_minutes if actual_minutes is not None else 0.0
    if amdahl_actual is not None:
        amdahl_act = amdahl_actual
    elif estimated > 0 and actual > 0:
        amdahl_act = estimated / actual
    else:
        amdahl_act = 0.0

    metrics_file = handoff_root() / project / "metrics.jsonl"
    metrics_file.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "batch_id": batch_id,
        "n_sub_tasks": len(manifest.get("sub_tasks", [])),
        "estimated_minutes_sum": estimated,
        "actual_minutes_sum": round(actual, 2),
        "amdahl_estimated": round(amdahl_est, 3),
        "amdahl_actual": round(amdahl_act, 3),
        "summary": (summary or "")[:200],
        "completed_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"

    fd = os.open(str(metrics_file), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(str(metrics_file.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    print(f"✅ fan-in complete; metrics appended to {metrics_file}")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def cmd_calibration(project: str) -> int:
    metrics = handoff_root() / project / "metrics.jsonl"
    if not metrics.exists():
        print(f"{DEFAULT_CALIBRATION:.3f}")
        return 0
    try:
        lines = metrics.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        print(f"{DEFAULT_CALIBRATION:.3f}")
        return 0

    ratios: list[float] = []
    for raw in lines[-CALIBRATION_WINDOW:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        est = float(rec.get("estimated_minutes_sum", 0) or 0)
        act = float(rec.get("actual_minutes_sum", 0) or 0)
        if est <= 0 or act <= 0:
            continue
        ratios.append(act / est)

    if not ratios:
        print(f"{DEFAULT_CALIBRATION:.3f}")
        return 0
    factor = sum(ratios) / len(ratios)
    factor = max(CALIBRATION_MIN, min(CALIBRATION_MAX, factor))
    print(f"{factor:.3f}")
    return 0


def cmd_status(batch_dir: Path) -> int:
    if not batch_dir.is_dir():
        print(f"❌ batch_dir missing: {batch_dir}", file=sys.stderr)
        return 2

    markers = [
        "_fanin_triggered",
        "_fan_in_started",
        "_fan_in_heartbeat",
        "_fan_in_done",
        "_watchdog_triggered",
        "_aborted",
        "_corrupted",
    ]
    print(f"# Batch {batch_dir.name}")
    print(f"# Path  {batch_dir}")
    for m in markers:
        p = batch_dir / m
        if p.exists():
            age = time.time() - p.stat().st_mtime
            print(f"  ✅ {m:<25} age={age:>6.0f}s")
        else:
            print(f"  ⬜ {m}")

    dones = sorted(p.name for p in batch_dir.glob("*.done"))
    blockeds = sorted(p.name for p in batch_dir.glob("*.blocked"))
    if dones:
        print(f"\n  *.done    : {dones}")
    if blockeds:
        print(f"  *.blocked : {blockeds}")

    try:
        manifest = load_manifest(batch_dir)
        expected = {s["id"] for s in manifest.get("sub_tasks", [])}
        done_set = {p.stem for p in batch_dir.glob("*.done")} & expected
        block_set = {p.stem for p in batch_dir.glob("*.blocked")} & expected
        print(f"\n  expected  : {sorted(expected)}")
        print(f"  done ∩    : {sorted(done_set)}")
        print(f"  blocked ∩ : {sorted(block_set)}")
        missing = expected - done_set - block_set
        print(f"  missing   : {sorted(missing)}")
    except SystemExit as e:
        print(f"  manifest  : ⚠️ {e}", file=sys.stderr)
    return 0


# ─── CLI ────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="handoff-heartbeat",
        description="Fan-in tab companion (heartbeat / complete / metrics / calibration).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_hb = sub.add_parser("heartbeat", help="run heartbeat daemon")
    p_hb.add_argument("batch_dir", type=Path)

    p_co = sub.add_parser("complete", help="mark fan-in done + write metrics")
    p_co.add_argument("batch_dir", type=Path)
    p_co.add_argument("--actual-minutes", type=float, default=None)
    p_co.add_argument("--amdahl-actual", type=float, default=None)
    p_co.add_argument("--summary", type=str, default="")

    p_cal = sub.add_parser("calibration", help="print calibration factor (0.5-3.0 float)")
    p_cal.add_argument("project", type=str)

    p_st = sub.add_parser("status", help="print batch state-machine markers")
    p_st.add_argument("batch_dir", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "heartbeat":
        return cmd_heartbeat(args.batch_dir)
    if args.cmd == "complete":
        return cmd_complete(args.batch_dir, args.actual_minutes, args.amdahl_actual, args.summary)
    if args.cmd == "calibration":
        return cmd_calibration(args.project)
    if args.cmd == "status":
        return cmd_status(args.batch_dir)
    return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
