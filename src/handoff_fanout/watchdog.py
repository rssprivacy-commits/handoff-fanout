"""Periodic watchdog that backstops the fan-in trigger and detects stuck tabs.

The dump module's last-one-out path is the happy case: when the final
sub-task writes its ``.done`` it also dumps the fan-in handoff. The
watchdog covers the failure modes:

  1. **Mode 1 — complete but not triggered.** All sub-tasks have
     ``.done``/``.blocked``, but no ``_fanin_triggered`` marker exists.
     The last writer crashed between writing its ``.done`` and calling
     ``trigger_fan_in_if_ready``. We invoke it ourselves.
  2. **Mode 2 — fan-in heartbeat stale.** ``_fan_in_started`` exists but
     ``_fan_in_heartbeat`` hasn't been touched for >3 min, and there's
     no ``_fan_in_done``. The fan-in tab died; re-trigger so a fresh tab
     restarts the workflow (idempotent on its end).
  3. **Mode 3 — timeout degradation.** Created-at age exceeds
     ``manifest.timeout_hours``. Force-trigger fan-in in DEGRADED mode.
  4. **Mode 4 — sub-task heartbeat stale (529 detection).** A sub-task
     hasn't touched its heartbeat for >5 min. Mark it ``.529-suspected``
     and notify the user; this used to fire constantly during Anthropic
     overload incidents.
  5. **Mode 5 — orphan spawn scan.** Cross-project sweep for
     ``ack/*.spawned`` files whose corresponding queue ``.md`` is gone.
     Writes a ``BLOCKED.md`` so the user can locate and close the tab.
  6. **Mode 6 — single-task heartbeat stale (v4.1 / 529 detection).**
     Cross-project sweep for ``queue/<task>.heartbeat`` files older than
     ``SUB_TASK_HEARTBEAT_STALE_SECONDS``. Mirror of Mode 4 for the v4.1
     single-task path: writes a ``queue/<task>.529-suspected`` marker so
     the user can recover. Symmetry with ``build_sub_task_handoff_md``
     Step 2: now ``build_handoff_md`` Step 1 also touches a heartbeat,
     and this mode notices when it stops. **Enforcement** (added
     2026-05-29 after 04:05 incident): after marking, the watchdog also
     looks up the stuck task's heartbeat-shell process (scoped via the
     literal heartbeat path so a same-named task in another project
     can't be matched) and sends SIGTERM → 5s grace → SIGKILL so the
     operator doesn't have to manually hunt the wedged PID. STOP_AUTO
     suppresses enforcement.

Intended to run via ``launchd`` / cron, every ~10 min.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from handoff_fanout import atomic, dump, templates
from handoff_fanout import config as _config

LOCK_STALE_SECONDS = 1800  # 30 min — a prior watchdog still running past this is wedged
HEARTBEAT_STALE_SECONDS = 180  # 3 min — fan-in heartbeat decay
SUB_TASK_HEARTBEAT_STALE_SECONDS = 300  # 5 min — sub-task heartbeat decay (529 detection)
ORPHAN_GRACE_SECONDS = 300  # 5 min — orphan candidate must be older than this

# Mode 6 enforcement (kill stuck task processes after marking 529-suspected)
ENFORCE_TERM_WAIT_SECONDS = 5.0  # SIGTERM grace before SIGKILL escalation
ENFORCE_POLL_INTERVAL = 0.1  # poll cadence while waiting for SIGTERM to land
ENFORCE_PGREP_TIMEOUT = 10  # `pgrep -fa` subprocess timeout

# Mode 6 enforce status strings — also referenced by tests to keep wording stable.
ENFORCE_STATUS_STOP_AUTO = "stop_auto"  # STOP_AUTO marker active; deliberately skipped
ENFORCE_STATUS_PGREP_UNAVAILABLE = "pgrep_unavailable"  # missing binary / timeout / rc>1
ENFORCE_STATUS_NO_MATCH = "no_match"  # pgrep ran but found nothing
ENFORCE_STATUS_KILLED = "killed"  # all matched PIDs confirmed gone
ENFORCE_STATUS_PARTIAL = "partial"  # some killed, others permission/still-alive
ENFORCE_STATUS_FAILED = "failed"  # nothing successfully killed despite matches
ENFORCE_STATUS_RACED_GONE = "raced_gone"  # all matches exited before our SIGTERM


def handoff_root() -> Path:
    return _config.home_dir()


def lock_path() -> Path:
    return handoff_root() / "watchdog.lock"


# ─── lock ───────────────────────────────────────────────────────────────────


def acquire_lock() -> int | None:
    """O_EXCL lock; stale (>30min mtime) auto-clears once before retry."""
    root = handoff_root()
    root.mkdir(parents=True, exist_ok=True)
    lp = lock_path()
    try:
        return os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        if (time.time() - lp.stat().st_mtime) > LOCK_STALE_SECONDS:
            print(f"[watchdog] lock stale (>{LOCK_STALE_SECONDS}s), forcing clear")
            lp.unlink()
            return acquire_lock()
        print(f"[watchdog] lock held (mtime={lp.stat().st_mtime}), exiting")
        return None


def release_lock(fd: int) -> None:
    os.close(fd)
    with contextlib.suppress(FileNotFoundError):
        lock_path().unlink()


# ─── helpers ────────────────────────────────────────────────────────────────


def parse_iso_utc(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(UTC)


def _infer_workspace(cfg: _config.Config, project: str) -> Path | None:
    """Resolve workspace from ``<workspace_root>/<project>`` if it exists."""
    p = cfg.workspace_root / project
    return p if p.exists() else None


# ─── batch scanner ──────────────────────────────────────────────────────────


def scan_batch(batch_dir: Path, cfg: _config.Config) -> None:
    project = batch_dir.parent.parent.name
    batch_id = batch_dir.name

    if dump.any_stop_auto(project, batch_id):
        print(f"[watchdog] {project}/{batch_id} STOP, skip")
        return

    for marker in ("_fan_in_done", "_aborted", "_corrupted"):
        if (batch_dir / marker).exists():
            return

    manifest = dump.load_manifest(batch_dir)
    if not manifest:
        print(f"[watchdog] {project}/{batch_id} manifest missing/corrupt, skip")
        return

    expected = {st["id"] for st in manifest["sub_tasks"]}
    done = {f.stem for f in batch_dir.glob("*.done")} & expected
    blocked = {f.stem for f in batch_dir.glob("*.blocked")} & expected
    finished = done | blocked

    workspace = Path(manifest["workspace"]) if manifest.get("workspace") else None
    if not workspace or not workspace.exists():
        workspace = _infer_workspace(cfg, project)

    # Mode 1: complete but not triggered
    if finished == expected and not (batch_dir / "_fanin_triggered").exists():
        print(f"[watchdog] mode 1 ({project}/{batch_id}): complete but not triggered")
        if not workspace:
            print("  ❌ cannot locate workspace, skip")
            return
        queue_dir = cfg.queue_dir(project)
        dump.trigger_fan_in_if_ready(project, workspace, batch_id, queue_dir, cfg=cfg)
        return

    # Mode 2: fan-in heartbeat stale
    if (batch_dir / "_fan_in_started").exists() and not (batch_dir / "_fan_in_done").exists():
        heartbeat = batch_dir / "_fan_in_heartbeat"
        if heartbeat.exists():
            stale = time.time() - heartbeat.stat().st_mtime
        else:
            stale = time.time() - (batch_dir / "_fan_in_started").stat().st_mtime
        if stale > HEARTBEAT_STALE_SECONDS:
            print(
                f"[watchdog] mode 2 ({project}/{batch_id}): fan-in heartbeat stale ({stale:.0f}s)"
            )
            for marker in ("_fan_in_started", "_fanin_triggered"):
                with contextlib.suppress(FileNotFoundError):
                    (batch_dir / marker).unlink()
            if workspace:
                queue_dir = cfg.queue_dir(project)
                dump.trigger_fan_in_if_ready(project, workspace, batch_id, queue_dir, cfg=cfg)
            return

    # Mode 4: sub-task heartbeat stale
    for st in manifest["sub_tasks"]:
        sub_id = st["id"]
        if sub_id in finished:
            continue
        if (batch_dir / f"{sub_id}.529-suspected").exists():
            continue
        heartbeat = batch_dir / f"{sub_id}.heartbeat"
        if not heartbeat.exists():
            env = batch_dir / f"{sub_id}.env"
            if (
                env.exists()
                and (time.time() - env.stat().st_mtime) > SUB_TASK_HEARTBEAT_STALE_SECONDS
            ):
                _mark_529_suspected(
                    batch_dir, sub_id, project, batch_id, reason="no heartbeat and env >5min old"
                )
            continue
        stale = time.time() - heartbeat.stat().st_mtime
        if stale > SUB_TASK_HEARTBEAT_STALE_SECONDS:
            _mark_529_suspected(
                batch_dir, sub_id, project, batch_id, reason=f"heartbeat stale {stale:.0f}s"
            )

    # Mode 3: timeout degradation
    created_at = parse_iso_utc(manifest.get("created_at", ""))
    timeout = timedelta(hours=manifest.get("timeout_hours", 3))
    now = datetime.now(UTC).astimezone()
    if now - created_at > timeout:
        if (batch_dir / "_watchdog_triggered").exists():
            return
        print(f"[watchdog] mode 3 ({project}/{batch_id}): timeout {timeout} (created {created_at})")
        atomic.atomic_create(batch_dir / "_watchdog_triggered")
        atomic.atomic_create(batch_dir / "_fanin_triggered")
        if not workspace:
            print("  ❌ cannot locate workspace, skip")
            return
        _dump_degraded_fan_in(
            cfg, project, workspace, batch_id, manifest, done, blocked, expected - finished
        )


def _mark_529_suspected(
    batch_dir: Path,
    sub_id: str,
    project: str,
    batch_id: str,
    reason: str,
) -> None:
    marker = batch_dir / f"{sub_id}.529-suspected"
    if not atomic.atomic_create(marker):
        return
    atomic.write_with_fsync(
        marker,
        (
            f"sub_task_id: {sub_id}\n"
            f"detected_at: {dump.now_iso()}\n"
            f"reason: {reason}\n"
            f"batch_dir: {batch_dir}\n\n"
            f"## Possible cause\n"
            f"Provider 529 (overloaded) — sub-task tab is stuck in a retry loop or\n"
            f"an unhandled exception path.\n\n"
            f"## Manual recovery\n"
            f"1. Open the sub-task's Claude tab and read the error.\n"
            f"2. If confirmed: `touch {batch_dir}/{sub_id}.retry` to re-dump it.\n"
            f"3. To give up: `touch {batch_dir}/{sub_id}.blocked` (triggers degraded fan-in).\n"
        ),
    )
    print(f"  [watchdog mode 4] 529-suspected: {project}/{batch_id}/{sub_id} ({reason})")
    _notify(
        f"{sub_id}: {reason}",
        "v5.1 watchdog / 529-suspected",
        f"{project}/{batch_id}",
        sound="Basso",
    )


def _dump_degraded_fan_in(
    cfg: _config.Config,
    project: str,
    workspace: Path,
    batch_id: str,
    manifest: dict,
    done: set[str],
    blocked: set[str],
    missing: set[str],
) -> None:
    queue_dir = cfg.queue_dir(project)
    queue_dir.mkdir(parents=True, exist_ok=True)
    batch_dir = handoff_root() / project / "batches" / batch_id
    baseline = dump.detect_baseline(workspace, cfg=cfg, project=project)

    dump.write_role_env(
        batch_dir / "fan-in.env",
        dump.HANDOFF_ROLE_FAN_IN,
        batch_id,
        workspace,
    )
    content = templates.build_fan_in_handoff_md(
        project=project,
        workspace=workspace,
        batch_id=batch_id,
        manifest=manifest,
        done_files=done,
        blocked_files=blocked,
        baseline=baseline,
        inject_blocks=cfg.inject_blocks_for(project),
        handoff_home=cfg.home,
        degraded=True,
        missing=missing,
    )
    fan_in_task = manifest["fan_in_task"] + "-watchdog"
    # Launcher-visible degraded fan-in description + trigger: atomic_replace, not
    # write_with_fsync. This is the watchdog's batch fan-in producer and shares
    # the same launchd WatchPaths surface as dump.trigger_fan_in_if_ready — an
    # in-place O_TRUNC window would expose a torn read to the launcher / spawned
    # session (same rationale as the single-task path, dump.write_active_dump §3.7).
    atomic.atomic_replace(queue_dir / f"{fan_in_task}.md", content)

    uri = dump.build_uri(cfg, project, fan_in_task)
    atomic.atomic_replace(
        queue_dir / f"{fan_in_task}.uri",
        f"WORKSPACE={workspace}\nURI={uri}\n",
    )
    print(f"  [watchdog] degraded fan-in dumped: queue/{fan_in_task}.{{md,uri}}")
    _notify(
        f"batch {batch_id} timed out / missing {len(missing)}",
        f"v5 watchdog / {project}",
        batch_id,
        sound="Basso",
    )


# ─── mode 6: single-task heartbeat stale (v4.1) ─────────────────────────────


def scan_single_task_heartbeats() -> int:
    """Find ``queue/<task>.heartbeat`` files whose mtime is stale past the threshold.

    The v4.1 single-task spawn-prompt (``build_handoff_md`` Step 1) instructs
    the new session to touch its own heartbeat every 60s. If the session
    is wedged in a 529 retry loop or hit ``API Error``, the heartbeat goes
    silent. We mirror Mode 4's behaviour: write ``<task>.529-suspected``
    and notify, only when the task is still active (``.md`` present, no
    ``.done`` / ``.BLOCKED.md`` / existing ``.529-suspected`` marker).
    """
    suspected = 0
    root = handoff_root()
    if not root.exists():
        return 0
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        if proj_dir.name in {"locks", "_recovery"}:
            continue
        queue_dir = proj_dir / "queue"
        if not queue_dir.is_dir():
            continue
        for heartbeat in queue_dir.glob("*.heartbeat"):
            task_id = heartbeat.stem
            if not (queue_dir / f"{task_id}.md").exists():
                continue
            if (queue_dir / f"{task_id}.done").exists():
                continue
            if (queue_dir / f"{task_id}.BLOCKED.md").exists():
                continue
            marker = queue_dir / f"{task_id}.529-suspected"
            if marker.exists():
                continue
            stale = time.time() - heartbeat.stat().st_mtime
            if stale <= SUB_TASK_HEARTBEAT_STALE_SECONDS:
                continue
            _mark_single_task_529(
                queue_dir,
                task_id,
                proj_dir.name,
                reason=f"heartbeat stale {stale:.0f}s",
            )
            suspected += 1
    return suspected


def _mark_single_task_529(
    queue_dir: Path,
    task_id: str,
    project: str,
    reason: str,
) -> None:
    marker = queue_dir / f"{task_id}.529-suspected"
    if not atomic.atomic_create(marker):
        return
    result = _enforce_kill_stuck_task(task_id, project, queue_dir)
    enforcement_block = _format_enforcement_block(result, project, task_id, queue_dir)
    atomic.write_with_fsync(
        marker,
        (
            f"task_id: {task_id}\n"
            f"detected_at: {dump.now_iso()}\n"
            f"reason: {reason}\n"
            f"queue_dir: {queue_dir}\n\n"
            f"## Possible cause\n"
            f"v4.1 single-task tab is wedged — Provider 529 (overloaded), an\n"
            f"unhandled exception path, or API Error 会话裸跑.\n\n"
            f"{enforcement_block}\n"
            f"## Manual recovery\n"
            f"1. Open the Claude tab for `{task_id}` and read the error.\n"
            f"2. To re-spawn: `rm {shlex.quote(str(marker))}` and\n"
            f"   `touch {shlex.quote(str(queue_dir / f'{task_id}.uri'))}` (launchd will re-fire).\n"
            f"3. To give up: `touch {shlex.quote(str(queue_dir / f'{task_id}.BLOCKED.md'))}`.\n"
        ),
    )
    summary = _summarize_enforce_result(result)
    print(f"  [watchdog mode 6] 529-suspected: {project}/{task_id} ({reason}) — {summary}")
    _notify(
        f"{task_id}: {reason} — {summary}",
        "v5.4 watchdog / 529-suspected (v4.1)",
        project,
        sound="Basso",
    )


# ─── mode 6 enforcement: find + kill stuck task processes ───────────────────


@dataclasses.dataclass(frozen=True)
class EnforceResult:
    """Structured outcome of one ``_enforce_kill_stuck_task`` call.

    Distinct outcomes are kept separate (instead of collapsing to "killed
    vs not") because the marker block and the operator notification need
    to tell the difference between "we can't kill it" (permission) and
    "we tried but the process won't die" (still_alive after SIGKILL).
    """

    status: str
    killed: tuple[int, ...] = ()
    still_alive: tuple[int, ...] = ()
    permission_denied: tuple[int, ...] = ()
    raced_gone: tuple[int, ...] = ()


def _enforce_kill_stuck_task(
    task_id: str,
    project: str,
    queue_dir: Path,
) -> EnforceResult:
    """Find processes owning ``<queue_dir>/<task_id>.heartbeat`` and kill them.

    The match criterion is the LITERAL heartbeat path (regex-escaped) so
    a task id with regex metacharacters (``.``, ``+``, ``[``…) can't
    accidentally widen the kill target, and a same-named task in a
    different project's queue can't be matched either.

    STOP_AUTO suppresses the kill (mark + notify still happen). Test
    runners (pytest, ``python -m pytest``, ``uv run pytest``, tox, nox)
    are filtered out so a ``pytest -k <task>`` invocation can't kill its
    own watcher when running the regression suite.

    Returns a structured ``EnforceResult`` instead of a bool — the
    marker text and notification rendering depend on the precise mix of
    killed / still_alive / permission_denied / raced_gone PIDs.
    """
    if dump.any_stop_auto(project):
        return EnforceResult(status=ENFORCE_STATUS_STOP_AUTO)

    found = _find_stuck_pids(task_id, queue_dir)
    if found is None:
        return EnforceResult(status=ENFORCE_STATUS_PGREP_UNAVAILABLE)
    if not found:
        return EnforceResult(status=ENFORCE_STATUS_NO_MATCH)

    killed: list[int] = []
    still_alive: list[int] = []
    permission_denied: list[int] = []
    raced_gone: list[int] = []
    for pid in found:
        outcome = _kill_pid(pid)
        if outcome == "killed":
            killed.append(pid)
        elif outcome == "still_alive":
            still_alive.append(pid)
        elif outcome == "permission_denied":
            permission_denied.append(pid)
        elif outcome == "raced_gone":
            raced_gone.append(pid)

    if killed and not still_alive and not permission_denied:
        status = ENFORCE_STATUS_KILLED
    elif killed:
        status = ENFORCE_STATUS_PARTIAL
    elif still_alive or permission_denied:
        status = ENFORCE_STATUS_FAILED
    else:
        status = ENFORCE_STATUS_RACED_GONE
    return EnforceResult(
        status=status,
        killed=tuple(killed),
        still_alive=tuple(still_alive),
        permission_denied=tuple(permission_denied),
        raced_gone=tuple(raced_gone),
    )


def _find_stuck_pids(task_id: str, queue_dir: Path) -> list[int] | None:
    """Return PIDs whose cmdline contains the literal heartbeat path.

    Uses ``pgrep -fa`` (full cmdline + leading PID). The pattern is the
    regex-escaped full path of ``<queue_dir>/<task_id>.heartbeat`` —
    that path is unique to this task in this project and is part of
    every heartbeat subshell's cmdline (the ``touch <path>`` step).

    Returns:
      * ``None`` — pgrep missing / timed out / returned an error rc (>1)
      * ``[]`` — pgrep ran cleanly but found nothing
      * ``[pid, …]`` — matching PIDs, with self/test-runner/pgrep filtered
    """
    heartbeat_path = str(queue_dir / f"{task_id}.heartbeat")
    pattern = re.escape(heartbeat_path)
    try:
        result = subprocess.run(
            ["pgrep", "-fa", pattern],
            capture_output=True,
            text=True,
            timeout=ENFORCE_PGREP_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode not in (0, 1):
        return None

    my_pid = os.getpid()
    my_ppid = os.getppid()
    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmd = parts[1]
        if pid in (my_pid, my_ppid):
            continue
        if _is_pgrep_invocation(cmd):
            continue
        if _is_test_runner(cmd):
            continue
        pids.append(pid)
    return pids


def _is_pgrep_invocation(cmd: str) -> bool:
    """Match the very ``pgrep -fa <pattern>`` call we just issued."""
    tokens = cmd.split()
    if not tokens:
        return False
    return Path(tokens[0]).name == "pgrep"


def _is_test_runner(cmd: str) -> bool:
    """Match Python test-runner invocations broadly so they're never killed.

    Covers: ``pytest``, ``/.venv/bin/pytest``, ``python -m pytest``,
    ``python3 -m pytest``, ``uv run pytest``, ``uvx pytest``, ``tox``,
    ``nox``. We err on the side of false positives — accidentally
    skipping a doomed shell is far cheaper than killing the user's
    regression suite during the watchdog's own tests.
    """
    tokens = cmd.split()
    if not tokens:
        return False
    runner_basenames = {"pytest", "tox", "nox"}
    head_basename = Path(tokens[0]).name
    if head_basename in runner_basenames:
        return True
    if (head_basename == "python" or head_basename.startswith("python")) and "-m" in tokens:
        m_idx = tokens.index("-m")
        if m_idx + 1 < len(tokens) and tokens[m_idx + 1] in runner_basenames:
            return True
    if head_basename in {"uv", "uvx"}:
        for tok in tokens[1:]:
            if Path(tok).name in runner_basenames:
                return True
    return any(Path(tok).name in runner_basenames for tok in tokens[1:])


def _kill_pid(pid: int) -> str:
    """Send SIGTERM → grace → SIGKILL, verify after each step.

    Returns one of:
      * ``"killed"`` — process is confirmed gone
      * ``"still_alive"`` — SIGKILL sent but a probe still finds the PID
        (zombie or D-state; operator must investigate)
      * ``"raced_gone"`` — process was already gone at first SIGTERM
      * ``"permission_denied"`` — kernel refused our signal at some point

    Verifying after SIGKILL closes the R1 finding: previously the helper
    returned True unconditionally and the marker recorded false "killed"
    claims for processes that didn't actually die.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "raced_gone"
    except PermissionError:
        return "permission_denied"

    deadline = time.monotonic() + ENFORCE_TERM_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(ENFORCE_POLL_INTERVAL)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "killed"
        except PermissionError:
            continue

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "killed"
    except PermissionError:
        return "permission_denied"

    time.sleep(ENFORCE_POLL_INTERVAL)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "killed"
    except PermissionError:
        return "permission_denied"
    return "still_alive"


def _summarize_enforce_result(result: EnforceResult) -> str:
    """One-line summary used in stdout log + osascript notification."""
    if result.status == ENFORCE_STATUS_STOP_AUTO:
        return "STOP_AUTO active — 未 kill"
    if result.status == ENFORCE_STATUS_PGREP_UNAVAILABLE:
        return "pgrep 不可用 — 手动 kill"
    if result.status == ENFORCE_STATUS_NO_MATCH:
        return "无匹配进程"
    if result.status == ENFORCE_STATUS_KILLED:
        return f"已 kill {len(result.killed)} 进程"
    if result.status == ENFORCE_STATUS_PARTIAL:
        remaining = len(result.still_alive) + len(result.permission_denied)
        return f"已 kill {len(result.killed)} / 仍剩 {remaining}"
    if result.status == ENFORCE_STATUS_FAILED:
        return "kill 失败 — 须手动介入"
    if result.status == ENFORCE_STATUS_RACED_GONE:
        return "进程已自然退出"
    return f"status={result.status}"


def _format_enforcement_block(
    result: EnforceResult,
    project: str,
    task_id: str,
    queue_dir: Path,
) -> str:
    """Human-readable enforcement summary embedded in the .529-suspected marker.

    Every interpolation of ``task_id`` / paths goes through
    ``shlex.quote`` so a hostile or whitespace-bearing task id can't
    break the shell snippets the operator might copy-paste.
    """
    quoted_task = shlex.quote(task_id)
    heartbeat_path = shlex.quote(str(queue_dir / f"{task_id}.heartbeat"))
    marker_path = shlex.quote(str(queue_dir / f"{task_id}.529-suspected"))

    if result.status == ENFORCE_STATUS_STOP_AUTO:
        return (
            "## Enforcement (v5.4 / 2026-05-29)\n"
            "STOP_AUTO 已置位 — watchdog **未** kill 任何进程 (operator pause).\n"
            f"放行后须**手动** `rm {marker_path}` (+ 让 heartbeat 重新 stale) 才能让\n"
            "下次扫描重跑 enforce，否则 marker 永久 short-circuit 后续 mode 6 扫描.\n"
            "或手动 kill:\n"
            f"  pgrep -fa -- {heartbeat_path} | awk '{{print $1}}' | xargs -r kill\n"
        )
    if result.status == ENFORCE_STATUS_PGREP_UNAVAILABLE:
        return (
            "## Enforcement (v5.4 / 2026-05-29)\n"
            "pgrep 不可用（缺失 / 超时 / rc>1）— watchdog 未能扫描进程.\n"
            "手动定位:\n"
            f"  ps -eo pid,command | grep -F {heartbeat_path}\n"
        )
    if result.status == ENFORCE_STATUS_NO_MATCH:
        return (
            "## Enforcement (v5.4 / 2026-05-29)\n"
            "watchdog 未找到匹配 heartbeat 路径的进程 — Claude tab 可能已退出,\n"
            "或 heartbeat subshell 早已被 SIGHUP. 若 codex / Bash wrapper 仍 wedged:\n"
            f"  pgrep -fa -- codex      # 查看所有 codex 进程\n"
            f"  pgrep -fa -- {quoted_task}    # 模糊匹配 task id\n"
        )

    lines = ["## Enforcement (v5.4 / 2026-05-29)"]
    if result.killed:
        pid_list = ", ".join(str(p) for p in result.killed)
        lines.append(
            f"已 kill {len(result.killed)} 进程: PIDs {pid_list}"
            " (SIGTERM → 5s grace → SIGKILL escalation)"
        )
    if result.still_alive:
        pid_list = ", ".join(str(p) for p in result.still_alive)
        lines.append(
            f"⚠️ SIGKILL 后仍存活: PIDs {pid_list} — 可能是 zombie 或 D-state."
            " 手动检查: `ps -p <pid> -o stat,command`"
        )
    if result.permission_denied:
        pid_list = ", ".join(str(p) for p in result.permission_denied)
        lines.append(
            f"⚠️ 权限拒绝: PIDs {pid_list} — watchdog 无权 signal"
            "（root 进程或 sandbox），需主人介入."
        )
    if result.raced_gone:
        pid_list = ", ".join(str(p) for p in result.raced_gone)
        lines.append(f"已自然退出 (signal 前已 gone): PIDs {pid_list}")
    return "\n".join(lines) + "\n"


# ─── mode 5: cross-project orphan scan ──────────────────────────────────────


def scan_orphan_spawns() -> int:
    """Find ``ack/*.spawned`` files whose queue ``.md`` is missing past the grace window."""
    orphans = 0
    root = handoff_root()
    if not root.exists():
        return 0
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        if proj_dir.name in {"locks", "_recovery"}:
            continue
        ack_dir = proj_dir / "ack"
        if not ack_dir.is_dir():
            continue
        queue_dir = proj_dir / "queue"
        for spawned in ack_dir.glob("*.spawned"):
            task_id = spawned.stem
            blocked_md = queue_dir / f"{task_id}.BLOCKED.md"
            if blocked_md.exists():
                continue
            if (queue_dir / f"{task_id}.md").exists():
                continue
            if (queue_dir / f"{task_id}.done").exists():
                continue
            age = time.time() - spawned.stat().st_mtime
            if age < ORPHAN_GRACE_SECONDS:
                continue
            _mark_orphan(proj_dir, task_id, age)
            orphans += 1
    return orphans


def _mark_orphan(proj_dir: Path, task_id: str, age_seconds: float) -> None:
    project = proj_dir.name
    queue_dir = proj_dir / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    cfg = _config.load()
    blocked_md = queue_dir / f"{task_id}.BLOCKED.md"
    content = templates.build_orphan_blocked_md(
        project=project,
        task_id=task_id,
        age_seconds=age_seconds,
        grace_seconds=ORPHAN_GRACE_SECONDS,
        handoff_home=cfg.home,
        workspace_root=cfg.workspace_root,
        now_iso=dump.now_iso(),
    )
    atomic.write_with_fsync(blocked_md, content)
    print(f"  [watchdog mode 5] orphan: {project}/{task_id} (age={age_seconds:.0f}s)")
    _notify(
        f"{task_id} orphan ({age_seconds:.0f}s)",
        "v5.2 watchdog / orphan",
        project,
        sound="Basso",
    )


# ─── notification ───────────────────────────────────────────────────────────


def _notify(message: str, title: str, subtitle: str, sound: str | None = None) -> None:
    osa = f'display notification "{message}" with title "{title}" subtitle "{subtitle}"'
    if sound:
        osa += f' sound name "{sound}"'
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        subprocess.run(["osascript", "-e", osa], check=False, timeout=5)


# ─── entry point ────────────────────────────────────────────────────────────


def main() -> int:
    if dump.any_stop_auto(""):
        stop = dump.any_stop_auto("")
        print(f"[watchdog] global STOP at {stop}, exit 0")
        return 0

    fd = acquire_lock()
    if fd is None:
        return 0

    cfg = _config.load()
    try:
        scanned = 0
        for batch_dir in handoff_root().glob("*/batches/*/"):
            if not batch_dir.is_dir():
                continue
            try:
                scan_batch(batch_dir, cfg)
                scanned += 1
            except Exception as e:
                print(f"[watchdog] {batch_dir} error: {e}", file=sys.stderr)
        try:
            orphans = scan_orphan_spawns()
        except Exception as e:
            print(f"[watchdog] orphan scan error: {e}", file=sys.stderr)
            orphans = 0
        try:
            stale_v41 = scan_single_task_heartbeats()
        except Exception as e:
            print(f"[watchdog] v4.1 heartbeat scan error: {e}", file=sys.stderr)
            stale_v41 = 0
        print(
            f"[watchdog] scanned {scanned} batches / {orphans} orphans / "
            f"{stale_v41} stale v4.1 heartbeats"
        )
    finally:
        release_lock(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
