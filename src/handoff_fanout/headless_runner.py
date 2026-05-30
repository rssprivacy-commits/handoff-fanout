"""Lock-screen / display-off resilient headless runner (方案 2 — 显示器可关，改无头).

Background
----------
The GUI auto-continue path submits a freshly-spawned Claude tab with a synthetic
``osascript … keystroke return``. When the display sleeps the Mac locks
(``screenLock=immediate``), the foreground becomes ``loginwindow``, and the
synthetic keystroke is silently rejected — the tab opens but never submits, so
the relay dead-stalls (see ``docs/design-headless-fallback-display-off.md`` §1).

This module is the launchd-OWNED replacement for that path while locked: it runs
the next session **headless** (``claude -p`` reading the prompt from stdin, under
``caffeinate -i``), waits for it, records the real outcome, and parks the chain
**visibly** (a ``BLOCKED.md``) on failure rather than dying silently.

Why Python (R3 structural pivot — the bash design was macOS-broken)
-------------------------------------------------------------------
* ``os.setsid`` is a **syscall** — no missing ``setsid`` binary on Darwin.
* ``subprocess.Popen`` takes an **argv list** — no multi-word-command quoting bug.
* ``proc.communicate(timeout=)`` gives wall-clock — no ``/usr/bin/timeout`` (absent
  on macOS) or ``gtimeout`` dependency, and the timeout is classified by the
  ``TimeoutExpired`` branch, never a guessed ``rc 124``.
* ``os.killpg`` does a **whole-tree** kill (caffeinate + claude + tool-call
  grandchildren).

Process ownership (resolves the R3 setsid/AbandonProcessGroup contradiction)
----------------------------------------------------------------------------
The child runs in its **own session** (``start_new_session=True`` ⇒ ``os.setsid``
in the child ⇒ ``pgid == pid``) and **this runner owns its lifecycle** — it always
``communicate``s/kills/reaps it. ``AbandonProcessGroup`` is therefore moot for the
child. The only orphan window is launchd SIGKILLing the *runner* itself before
cleanup; that is covered by (a) the SIGTERM handler killing the child group during
launchd's graceful-stop grace (``ExitTimeOut`` ≥ 60) and (b) the janitor sweep at
every runner start.

This module is intentionally dependency-free (stdlib only) and every external
binary is env-overridable so the test suite can stub them on Linux CI.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from handoff_fanout import config

# ─── env-overridable knobs (defaults match the design spec §6) ──────────────

# The headless claude entrypoint. `claude` on the developer box is a zsh
# *function* wrapping the python rc script — NOT on PATH for launchd bash — so we
# never invoke a bare `claude`. Default to the python entrypoint; tests stub it.
DEFAULT_CLAUDE_CMD = f"python3 {Path('~/.openclaw/scripts/claude-rc.py').expanduser()}"


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val is not None else default


def _argv(name: str, default: str) -> list[str]:
    """shlex-split an env var into an argv list (empty string ⇒ no tokens)."""
    raw = os.environ.get(name)
    src = raw if raw is not None else default
    return shlex.split(src)


def claude_argv() -> list[str]:
    return _argv("HANDOFF_CLAUDE_HEADLESS_CMD", DEFAULT_CLAUDE_CMD)


def claude_flags() -> list[str]:
    """Flags appended after the base command.

    Default = ``--permission-mode bypassPermissions --model <model> -p`` (prompt
    via stdin). Tests set this to an empty string to drive a plain stub
    (``/bin/cat``) that just echoes stdin.
    """
    raw = os.environ.get("HANDOFF_CLAUDE_HEADLESS_FLAGS")
    if raw is not None:
        return shlex.split(raw)
    model = _env("HANDOFF_HEADLESS_MODEL", "opus")
    return ["--permission-mode", "bypassPermissions", "--model", model, "-p"]


def caffeinate_argv() -> list[str]:
    """``caffeinate -i`` keeps the *system* awake while a child runs, letting the
    display sleep (exactly what 方案 2 wants). Empty ⇒ no wrapper (tests/Linux)."""
    return _argv("HANDOFF_CAFFEINATE_CMD", "caffeinate -i")


def headless_timeout() -> int:
    with contextlib.suppress(ValueError):
        return int(_env("HANDOFF_HEADLESS_TIMEOUT", "2700"))
    return 2700


def max_headless() -> int:
    with contextlib.suppress(ValueError):
        return max(1, int(_env("HANDOFF_MAX_HEADLESS", "1")))
    return 1


def protected_branches() -> set[str]:
    """Branches a headless run refuses to touch.

    Default EMPTY: the whole handoff workflow commits-and-pushes on ``main`` (the
    5/14 autonomous-commit law), so blocking ``main`` by default would disable the
    relay entirely. The MECHANISM is mandatory before enable (§3.4-4); the owner
    populates the list (e.g. ``HANDOFF_PROTECTED_BRANCHES="main master"``) as part
    of the §2.2 enable decision if they want headless to refuse a protected branch.
    """
    return {b for b in _env("HANDOFF_PROTECTED_BRANCHES", "").split() if b}


PS_CMD = _env("HANDOFF_PS_CMD", "/bin/ps")
GIT_CMD = _env("HANDOFF_GIT_CMD", "git")

# Backoff (seconds) when over the global cap, so launchd's QueueDirectories does
# not hot-loop the runner (R4 P1). Bounded so STOP/done are still honored promptly.
CAP_BACKOFF_SECONDS = 5.0
CAP_BACKOFF_MAX_TICKS = 24  # ≈ 2 min resident wait before giving up this invocation


# ─── small process helpers ──────────────────────────────────────────────────


def _ps_field(pid: int, fmt: str) -> str | None:
    """Return ``ps -o <fmt>= -p <pid>`` trimmed, or None if the pid is gone."""
    try:
        out = subprocess.run(
            [PS_CMD, "-o", f"{fmt}=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    val = out.stdout.strip()
    return val or None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another uid
    return True


def _killpg(pgid: int, sig: int = signal.SIGTERM) -> None:
    """Signal the whole process group (caffeinate + claude + grandchildren)."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, sig)


def _now() -> str:
    # Local wall-clock for human-facing ack/log lines (mirrors auto-continue.sh).
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ─── opt-in + halt gates ─────────────────────────────────────────────────────


def headless_enabled(root: Path, project: str) -> bool:
    """Per-project opt-in (default OFF), mirroring the autoclose sentinels."""
    if os.environ.get("HANDOFF_HEADLESS_ENABLED") == "1":
        return True
    if (root / "headless.enabled").exists():
        return True
    return (root / project / "headless.enabled").exists()


def is_halted(root: Path, project: str) -> bool:
    """Global / project STOP_AUTO or done ⇒ stop taking new work."""
    return any(
        (
            (root / "STOP_AUTO").exists(),
            (root / "done").exists(),
            (root / project / "STOP_AUTO").exists(),
            (root / project / "done").exists(),
        )
    )


# ─── pidfile (PID-reuse defence via process start-time) ──────────────────────


def _pidfile_path(root: Path, project: str, task: str) -> Path:
    return root / project / "headless" / f"{task}.pid"


def write_pidfile(
    path: Path, pid: int, pgid: int, start_epoch: float, start_lstart: str, task: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"pid={pid}\npgid={pgid}\nstart_epoch={start_epoch:.0f}\n"
        f"start_lstart={start_lstart}\ntask={task}\n",
        encoding="utf-8",
    )


def read_pidfile(path: Path) -> dict[str, str] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def pidfile_owns_live_child(meta: dict[str, str]) -> bool:
    """Is the pid in ``meta`` STILL our headless child (not a recycled PID)?

    PID-reuse defence = process **start-time** (the canonical check; a recycled
    PID has a different start time, and start-time is reliably queryable unlike an
    env/argv marker ``claude -p`` does not expose). All of:
      ``kill -0 pid`` AND ``ps -o lstart= -p pid`` == recorded ``start_lstart``
      AND ``ps -o pgid= -p pid`` == recorded ``pgid``.
    """
    try:
        pid = int(meta.get("pid", ""))
        pgid = int(meta.get("pgid", ""))
    except (ValueError, TypeError):
        return False
    rec_lstart = meta.get("start_lstart", "")
    if not rec_lstart:
        return False
    if not _pid_alive(pid):
        return False
    if _ps_field(pid, "lstart") != rec_lstart:
        return False
    cur_pgid = _ps_field(pid, "pgid")
    return cur_pgid is not None and cur_pgid.strip() == str(pgid)


def janitor_sweep(root: Path) -> int:
    """Kill orphaned headless children left by a SIGKILLed runner, then clear
    their pidfiles. NEVER ``killpg`` on liveness alone — verify start-time first
    (a recycled PID with a different ``lstart`` is left untouched, only its stale
    pidfile is cleared)."""
    killed = 0
    for pidfile in sorted(root.glob("*/headless/*.pid")):
        meta = read_pidfile(pidfile)
        if meta is None:
            continue
        if pidfile_owns_live_child(meta):
            # Still genuinely ours and alive. If the chain is halted, the halt
            # supervisor (below) handles it; the janitor only reaps true orphans
            # whose owning runner is gone. A live, validated child whose runner is
            # also alive is normal — leave it.
            continue
        # Either dead, or a recycled PID. If a *validated-dead* group leader still
        # has survivors sharing the stored pgid we'd want them gone, but we can
        # only safely killpg when we proved identity — which we just failed. So:
        # clear the stale pidfile; do not killpg an unverified pgid.
        with contextlib.suppress(OSError):
            pidfile.unlink()
    return killed


def halt_supervisor_sweep(root: Path) -> int:
    """At runner start: any live headless child whose chain is now halted
    (STOP_AUTO/done) → SIGTERM the whole group, then SIGKILL after grace; clear
    the pidfile. This is what makes ``暂停``/``永久停`` actually stop overnight
    agents (P0 #4)."""
    stopped = 0
    for pidfile in sorted(root.glob("*/headless/*.pid")):
        project = pidfile.parent.parent.name
        meta = read_pidfile(pidfile)
        if meta is None:
            continue
        if not pidfile_owns_live_child(meta):
            continue
        if not is_halted(root, project):
            continue
        pgid = int(meta["pgid"])
        _killpg(pgid, signal.SIGTERM)
        for _ in range(30):
            if not _pid_alive(int(meta["pid"])):
                break
            time.sleep(1)
        _killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            pidfile.unlink()
        stopped += 1
    return stopped


def count_live_headless(root: Path) -> int:
    n = 0
    for pidfile in sorted(root.glob("*/headless/*.pid")):
        meta = read_pidfile(pidfile)
        if meta and pidfile_owns_live_child(meta):
            n += 1
    return n


# ─── per-task lock (mkdir is atomic; macOS has no flock for dirs) ────────────


def acquire_task_lock(root: Path, project: str, task: str) -> bool:
    lock = root / project / "locks" / f"{task}.headless.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        return False
    (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_task_lock(root: Path, project: str, task: str) -> None:
    lock = root / project / "locks" / f"{task}.headless.lock"
    with contextlib.suppress(OSError):
        (lock / "pid").unlink()
    with contextlib.suppress(OSError):
        lock.rmdir()


# ─── safety precheck (unattended bypassPermissions containment, §3.4) ────────


def safety_precheck(root: Path, workspace: Path) -> tuple[bool, str]:
    """Return (ok, reason). Refuse a headless bypassPermissions run when the
    workspace is in a state where an autonomous agent could lose work."""
    if not workspace.is_dir():
        return False, f"workspace not a directory: {workspace}"
    # Tracked-file changes (modified/staged) risk loss under an autonomous agent.
    # Untracked-only ('?? …') is allowed (junk files never block the relay).
    try:
        st = subprocess.run(
            [GIT_CMD, "-C", str(workspace), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git status failed: {exc}"
    if st.returncode != 0:
        return False, "git status nonzero (not a git repo?)"
    dirty = [ln for ln in st.stdout.splitlines() if ln.strip() and not ln.startswith("??")]
    if dirty:
        return False, f"dirty worktree ({len(dirty)} tracked change(s)) — risk of loss"
    prot = protected_branches()
    if prot:
        try:
            br = subprocess.run(
                [GIT_CMD, "-C", str(workspace), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            branch = br.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            branch = ""
        if branch in prot:
            return False, f"protected branch '{branch}' (HANDOFF_PROTECTED_BRANCHES)"
    return True, ""


def _git_head(workspace: Path) -> str:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        out = subprocess.run(
            [GIT_CMD, "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if out.returncode == 0:
            return out.stdout.strip()[:12] or "(unknown)"
    return "(unknown)"


# ─── ack + BLOCKED writers ────────────────────────────────────────────────────


def _write_ack(root: Path, project: str, task: str, state: str, detail: str) -> None:
    ack_dir = root / project / "ack"
    ack_dir.mkdir(parents=True, exist_ok=True)
    (ack_dir / f"{task}.{state}").write_text(f"{_now()}\n{detail}\n", encoding="utf-8")


def _write_blocked(root: Path, project: str, task: str, workspace: Path, reason: str) -> None:
    # Lazy import: templates pulls in the heavier dump-side helpers.
    from handoff_fanout import templates

    queue = root / project / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    head = _git_head(workspace)
    blocked = queue / f"{task}.BLOCKED.md"
    with contextlib.suppress(Exception):
        blocked.write_text(
            templates.build_blocked_md(project=project, task=task, head=head, reason=reason),
            encoding="utf-8",
        )
    if not blocked.exists():
        # Never let a template error swallow the visible park.
        blocked.write_text(
            f"# BLOCKED — {project}/{task}\n\nreason: {reason}\nhead: {head}\n",
            encoding="utf-8",
        )
    # Terminal — drop the .uri so the GUI launcher won't also try to re-spawn it.
    with contextlib.suppress(OSError):
        (queue / f"{task}.uri").unlink()


# ─── finalize (name-baselined; §3.2) ─────────────────────────────────────────


def finalize(
    root: Path,
    project: str,
    task: str,
    workspace: Path,
    rc: int | None,
    reason: str | None,
    base_uris: set[str],
) -> str:
    """Classify the headless outcome into a visible terminal state."""
    queue = root / project / "queue"
    if reason == "timeout":
        _write_blocked(root, project, task, workspace, "headless timeout")
        _write_ack(root, project, task, "failed", "headless timeout")
        return "blocked-timeout"
    if reason == "halted":
        # STOP_AUTO/done killed the run mid-flight (P0 #4). Park it visibly so a
        # returning owner sees the in-flight task was stopped, not silently gone.
        _write_blocked(root, project, task, workspace, "halted by STOP_AUTO/done")
        _write_ack(root, project, task, "failed", "halted by STOP_AUTO/done")
        return "blocked-halted"
    if rc is None or rc != 0:
        _write_blocked(root, project, task, workspace, f"headless exit rc={rc}")
        _write_ack(root, project, task, "failed", f"headless exit rc={rc}")
        return "blocked-rc"
    # rc == 0 — look for the produced handoff artifact.
    new_uris = {p.name for p in queue.glob("*.uri")} - base_uris
    if new_uris:
        _write_ack(
            root,
            project,
            task,
            "submitted-headless",
            f"next leg dispatched: {sorted(new_uris)}",
        )
        return "submitted-headless"
    if (queue / f"{task}.done").exists():
        _write_ack(root, project, task, "submitted-headless", "terminal: status=done")
        return "done"
    if (queue / f"{task}.BLOCKED.md").exists():
        # The task itself dumped status=blocked — already a visible park.
        _write_ack(root, project, task, "failed", "task self-reported blocked")
        return "self-blocked"
    _write_blocked(root, project, task, workspace, "completed without handoff artifact")
    _write_ack(root, project, task, "failed", "completed without handoff artifact")
    return "blocked-no-artifact"


# ─── spawn one request ────────────────────────────────────────────────────────


def parse_req(req: Path) -> tuple[str, str]:
    """Parse a ``<task>.req`` (KV: WORKSPACE= / task=). The task id falls back to
    the filename stem; the workspace must be present (R3 P0)."""
    task = req.stem
    workspace = ""
    with contextlib.suppress(OSError):
        for line in req.read_text(encoding="utf-8").splitlines():
            if line.startswith("WORKSPACE="):
                workspace = line.split("=", 1)[1].strip()
            elif line.startswith("task="):
                task = line.split("=", 1)[1].strip() or task
    return task, workspace


def _log(root: Path, msg: str) -> None:
    with contextlib.suppress(OSError):
        (root / "headless-runner.log").open("a", encoding="utf-8").write(f"[{_now()}] {msg}\n")


def run_one(root: Path, project: str, task: str, workspace: str) -> str:
    """Spawn + supervise one headless child. Returns the finalize outcome label."""
    queue = root / project / "queue"
    prompt_md = queue / f"{task}.md"
    ws = Path(workspace).expanduser()

    if not prompt_md.exists():
        _write_blocked(root, project, task, ws, "missing prompt .md")
        _write_ack(root, project, task, "failed", "missing prompt .md")
        return "blocked-no-prompt"

    ok, reason = safety_precheck(root, ws)
    if not ok:
        _write_blocked(root, project, task, ws, f"unsafe worktree: {reason}")
        _write_ack(root, project, task, "failed", f"unsafe worktree: {reason}")
        _log(root, f"BLOCK {project}/{task}: {reason}")
        return "blocked-unsafe"

    base_uris = {p.name for p in queue.glob("*.uri")}  # NAME baseline (no mtime race)
    argv = [*caffeinate_argv(), *claude_argv(), *claude_flags()]

    log_dir = root / project / "headless"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task}.log"
    pidfile = _pidfile_path(root, project, task)

    child_env = {k: v for k, v in os.environ.items()}
    child_env["HANDOFF_TASK"] = task  # log/debug only (NOT the reuse check)

    _log(root, f"SPAWN {project}/{task} argv={argv} cwd={ws}")
    try:
        log_fh = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — closed in finally
        prompt_fh = open(prompt_md, "rb")  # noqa: SIM115 — closed in finally
    except OSError as exc:
        _write_blocked(root, project, task, ws, f"cannot open io: {exc}")
        _write_ack(root, project, task, "failed", f"cannot open io: {exc}")
        return "blocked-io"

    proc: subprocess.Popen | None = None
    halt_watcher: _HaltWatcher | None = None
    rc: int | None = None
    fin_reason: str | None = None
    try:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(ws),
                stdin=prompt_fh,
                stdout=log_fh,
                stderr=log_fh,
                env=child_env,
                start_new_session=True,  # os.setsid in child ⇒ pgid == pid
            )
        except OSError as exc:
            # Failed start (ENOENT etc.) — no pidfile, visible failure.
            _write_blocked(root, project, task, ws, f"spawn failed: {exc}")
            _write_ack(root, project, task, "failed", f"spawn failed: {exc}")
            _log(root, f"SPAWN-FAIL {project}/{task}: {exc}")
            return "blocked-spawn"

        pgid = os.getpgid(proc.pid)  # syscall; child is its own group leader
        start_epoch = time.time()
        start_lstart = _ps_field(proc.pid, "lstart") or ""
        write_pidfile(pidfile, proc.pid, pgid, start_epoch, start_lstart, task)

        halt_watcher = _HaltWatcher(root, project, proc, pgid)
        halt_watcher.start()
        try:
            proc.communicate(timeout=headless_timeout())
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            _killpg(pgid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(30)  # never let a hung wait skip the SIGKILL
            _killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(10)
            rc, fin_reason = proc.returncode, "timeout"
    finally:
        if halt_watcher is not None:
            halt_watcher.stop()
        with contextlib.suppress(OSError):
            log_fh.close()
        with contextlib.suppress(OSError):
            prompt_fh.close()
        with contextlib.suppress(OSError):
            pidfile.unlink()

    # A halt mid-run (STOP/done) makes the child exit nonzero with reason=None;
    # tag it so finalize parks it as an explicit "halted" rather than a cryptic rc.
    if fin_reason is None and halt_watcher is not None and halt_watcher.fired:
        fin_reason = "halted"
    # Post-run git verify (auditable record of what a bypass run did, §3.4-7).
    _log(
        root,
        f"DONE {project}/{task} rc={rc} reason={fin_reason} halted={halt_watcher and halt_watcher.fired}",
    )
    outcome = finalize(root, project, task, ws, rc, fin_reason, base_uris)
    _log(root, f"FINALIZE {project}/{task} → {outcome}")
    return outcome


class _HaltWatcher(threading.Thread):
    """Poll for STOP_AUTO/done mid-run; on halt, killpg the whole child group."""

    def __init__(self, root: Path, project: str, proc: subprocess.Popen, pgid: int) -> None:
        super().__init__(daemon=True)
        self._root = root
        self._project = project
        self._proc = proc
        self._pgid = pgid
        self._stop = threading.Event()
        self.fired = False

    def run(self) -> None:
        while not self._stop.wait(2.0):
            if self._proc.poll() is not None:
                return
            if is_halted(self._root, self._project):
                self.fired = True
                _killpg(self._pgid, signal.SIGTERM)
                # give it a grace period, then SIGKILL the group
                for _ in range(15):
                    if self._proc.poll() is not None:
                        return
                    if self._stop.wait(1.0):
                        return
                _killpg(self._pgid, signal.SIGKILL)
                return

    def stop(self) -> None:
        self._stop.set()


# ─── main drain loop (invoked by launchd QueueDirectories) ───────────────────


def _resolve_root() -> Path:
    """Headless root = where auto-continue.sh writes ``<project>/headless-req/``.

    Defaults to ``config.home_dir()`` (``$HANDOFF_HOME``) so it tracks the same
    tree dump/queue use. In the dharmaxis deployment ``HANDOFF_HOME`` and
    auto-continue.sh's ``HANDOFF_ROOT`` are the same dir (``~/.claude-handoff``);
    ``HANDOFF_HEADLESS_ROOT`` can override for tests / non-standard layouts.
    """
    override = os.environ.get("HANDOFF_HEADLESS_ROOT")
    if override:
        return Path(override).expanduser()
    return config.home_dir()


def drain_once(root: Path) -> int:
    """Process every pending ``*.req`` across all projects. Returns count run."""
    ran = 0
    for req in sorted(root.glob("*/headless-req/*.req")):
        project = req.parent.parent.name
        task, workspace = parse_req(req)

        if is_halted(root, project):
            _log(root, f"HALTED {project}/{task} — STOP_AUTO/done; leaving .req")
            break  # stop taking new work; leave .req for after resume

        if not headless_enabled(root, project):
            # opt-in revoked between .req write and spawn (R4 P0)
            _write_ack(root, project, task, "deferred", "headless opt-in revoked")
            with contextlib.suppress(OSError):
                req.unlink()
            _log(root, f"DEFER {project}/{task} — opt-in revoked")
            continue

        # Global cap + per-task lock. Over cap ⇒ bounded resident backoff so
        # launchd's QueueDirectories does not hot-loop (R4 P1).
        backoff_ticks = 0
        while count_live_headless(root) >= max_headless():
            if is_halted(root, project):
                break
            backoff_ticks += 1
            if backoff_ticks > CAP_BACKOFF_MAX_TICKS:
                _log(root, f"CAP-BUSY {project}/{task} — leaving .req for next invocation")
                return ran  # leave .req; next QueueDirectories tick retries
            time.sleep(CAP_BACKOFF_SECONDS)
        if is_halted(root, project):
            break

        if not acquire_task_lock(root, project, task):
            _log(root, f"LOCK-HELD {project}/{task} — skip")
            continue
        try:
            run_one(root, project, task, workspace)
            ran += 1
        finally:
            release_task_lock(root, project, task)
            with contextlib.suppress(OSError):
                req.unlink()  # drained — launchd re-launches while any remain
    return ran


def _install_term_handler(root: Path) -> None:
    """On launchd SIGTERM (graceful stop within ExitTimeOut), kill any live child
    groups we own before launchd SIGKILLs us — the runner-orphan window guard."""

    def _handler(signum, frame):  # noqa: ANN001, ARG001
        for pidfile in sorted(root.glob("*/headless/*.pid")):
            meta = read_pidfile(pidfile)
            if meta and pidfile_owns_live_child(meta):
                with contextlib.suppress(ValueError, KeyError):
                    _killpg(int(meta["pgid"]), signal.SIGTERM)
        # Let the default behaviour exit the process.
        raise SystemExit(143)

    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, _handler)


def main(argv: list[str] | None = None) -> int:
    sweep_only = bool(argv) and "--sweep-only" in argv
    root = _resolve_root()
    if not root.is_dir():
        return 0
    _install_term_handler(root)
    # Reconcile any orphans from a previously SIGKILLed runner, and stop any live
    # children whose chain is now halted (P0 #4 — 暂停/永久停 must bite).
    janitor_sweep(root)
    halt_supervisor_sweep(root)
    if sweep_only:
        # Launcher-start supervisor sweep (§3.4-2): reconcile + halt only, never
        # run a task synchronously (the GUI launcher must not block on claude).
        return 0
    drain_once(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
