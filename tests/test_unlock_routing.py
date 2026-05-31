"""Unlock-pivot routing tests for ``install/auto-continue.sh``.

The VS Code GUI exit needs an UNLOCKED screen (synthetic keystrokes are forbidden
against the macOS lock screen). When locked + the project opted in, the launcher
auto-unlocks first (via ``HANDOFF_UNLOCK_CMD``), runs the visible GUI path, then
re-locks (``HANDOFF_RELOCK_CMD``). Locked + not-opted-in / unlock-failed / unknown
⇒ defer (keep .uri, no GUI). Verified here by shelling out to the launcher with
stateful lock/unlock/relock stubs (a shared state file the stubs read/write).

| lock | opt-in | unlock | expected |
|---|---|---|---|
| unlocked | —   | —       | GUI (open invoked), unlock NOT called |
| locked   | yes | success | unlock called → GUI (open) → relock called |
| locked   | yes | fail    | defer(unlock-failed) + cooldown, no GUI, no relock |
| locked   | no  | —       | defer(locked-unlock-not-enabled), unlock NOT called |
| unknown  | —   | —       | defer(lock-unknown) |
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "install" / "auto-continue.sh"
PROJECT = "demo"
TASK = "demo-task"


def _w(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = tmp_path / "claude-handoff"
    (root / PROJECT / "queue").mkdir(parents=True)
    (root / PROJECT / "ack").mkdir(parents=True)
    return root


def _seed(home: Path, ws: Path) -> None:
    q = home / PROJECT / "queue"
    (q / f"{TASK}.uri").write_text(
        f"WORKSPACE={ws}\nURI=vscode://anthropic.claude-code/open?prompt=x\n", encoding="utf-8"
    )
    (q / f"{TASK}.md").write_text("# prompt\n", encoding="utf-8")


def _env(
    home: Path,
    tmp_path: Path,
    *,
    initial: str,
    opt_in: bool,
    unlock_ok: bool,
    fail_rc: int = 1,
    relock: bool = True,
) -> dict:
    stub = tmp_path / "stubs"
    stub.mkdir(exist_ok=True)
    state = tmp_path / "lockstate"
    state.write_text(initial + "\n", encoding="utf-8")  # locked | unlocked | dunno
    open_sink = tmp_path / "open.log"
    unlock_sink = tmp_path / "unlock.log"
    relock_sink = tmp_path / "relock.log"

    # R2 P0-1: opt-in is the per-project sentinel ONLY (no global env in tests).
    if opt_in:
        (home / PROJECT / "unlock.enabled").write_text("", encoding="utf-8")

    # lock probe: prints current state file content
    _w(stub / "lockprobe", '#!/bin/bash\ncat "$_LOCKSTATE" 2>/dev/null || echo unlocked\n')
    # unlock: success flips state→unlocked (records call); fail leaves it (exit fail_rc)
    if unlock_ok:
        _w(
            stub / "unlock",
            '#!/bin/bash\nprintf u >> "$_UNLOCK_SINK"\necho unlocked > "$_LOCKSTATE"\nexit 0\n',
        )
    else:
        _w(stub / "unlock", f'#!/bin/bash\nprintf u >> "$_UNLOCK_SINK"\nexit {fail_rc}\n')
    # relock: state→locked (records call)
    _w(
        stub / "relock",
        '#!/bin/bash\nprintf r >> "$_RELOCK_SINK"\necho locked > "$_LOCKSTATE"\nexit 0\n',
    )
    # open / code / osascript (smart: answers accessibility + frontmost)
    _w(stub / "open", f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{open_sink}"\nexit 0\n')
    _w(stub / "code", "#!/bin/bash\nexit 0\n")
    _w(
        stub / "osascript",
        '#!/bin/bash\nargs="$*"\ncase "$args" in\n'
        '  *"UI elements enabled"*) echo true ;;\n'
        '  *"frontmost is true"*) echo Code ;;\n'
        "esac\nexit 0\n",
    )
    env = dict(os.environ)
    env.update(
        {
            "HANDOFF_ROOT": str(home),
            "HANDOFF_OPEN_CMD": str(stub / "open"),
            "HANDOFF_OSASCRIPT_CMD": str(stub / "osascript"),
            "HANDOFF_CODE_BIN": str(stub / "code"),
            "HANDOFF_LOCK_CHECK_CMD": str(stub / "lockprobe"),
            "HANDOFF_UNLOCK_CMD": str(stub / "unlock"),
            "HANDOFF_RELOCK_CMD": str(stub / "relock") if relock else "",
            "HANDOFF_CAFFEINATE_CMD": "",  # no caffeinate in tests
            "HANDOFF_SKIP_SPAWN": "0",
            "HANDOFF_VSCODE_CHECK": "0",
            "HANDOFF_UNLOCK_ENABLED": "0",  # opt-in via per-project sentinel only
            "_LOCKSTATE": str(state),
            "_UNLOCK_SINK": str(unlock_sink),
            "_RELOCK_SINK": str(relock_sink),
            "_OPEN_SINK": str(open_sink),
        }
    )
    env.pop("HANDOFF_HOME", None)
    return env


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=40, check=False
    )


def _read(env: dict, key: str) -> str:
    p = Path(env[key])
    return p.read_text() if p.exists() else ""


def test_unlocked_goes_straight_to_gui(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    env = _env(home, tmp_path, initial="unlocked", opt_in=False, unlock_ok=True)
    assert _run(env).returncode == 0
    assert _read(env, "_OPEN_SINK").strip(), "unlocked → GUI open invoked"
    assert _read(env, "_UNLOCK_SINK") == "", "unlocked → unlock must NOT be called"
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_locked_opted_in_unlock_success_runs_gui_then_relocks(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    env = _env(home, tmp_path, initial="locked", opt_in=True, unlock_ok=True)
    assert _run(env).returncode == 0
    assert _read(env, "_UNLOCK_SINK") == "u", "locked+opted-in → unlock attempted once"
    assert _read(env, "_OPEN_SINK").strip(), "after unlock → GUI open invoked"
    assert _read(env, "_RELOCK_SINK") == "r", "we unlocked → must re-lock after"
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_locked_unlock_failure_defers_and_cools_down(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    env = _env(home, tmp_path, initial="locked", opt_in=True, unlock_ok=False)
    assert _run(env).returncode == 0
    assert _read(env, "_UNLOCK_SINK") == "u", "unlock attempted"
    assert _read(env, "_OPEN_SINK") == "", "unlock failed → no GUI"
    assert _read(env, "_RELOCK_SINK") == "", "we never unlocked → no relock"
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists(), ".uri kept on defer"
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists() and "unlock-failed" in marker.read_text()
    assert (home / PROJECT / ".unlock-cooldown").exists(), "cooldown marker written"


def test_locked_not_opted_in_defers_without_unlocking(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    env = _env(home, tmp_path, initial="locked", opt_in=False, unlock_ok=True)
    assert _run(env).returncode == 0
    assert _read(env, "_UNLOCK_SINK") == "", "not opted in → never attempt unlock"
    assert _read(env, "_OPEN_SINK") == ""
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists() and "locked-unlock-not-enabled" in marker.read_text()
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_unknown_lock_state_fails_closed_to_defer(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    env = _env(home, tmp_path, initial="dunno", opt_in=True, unlock_ok=True)
    assert _run(env).returncode == 0
    assert _read(env, "_UNLOCK_SINK") == "", "unknown → never attempt unlock"
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists() and "lock-unknown" in marker.read_text()
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_relock_cmd_unset_defers_without_unlocking(home, tmp_path):
    """R2 P0-3: never unlock without a way to re-lock. With no relock cmd (and an
    unlock cmd that can't derive --lock), the locked task defers + never unlocks."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    env = _env(home, tmp_path, initial="locked", opt_in=True, unlock_ok=True, relock=False)
    assert _run(env).returncode == 0
    assert _read(env, "_UNLOCK_SINK") == "", "no relock path → must NOT unlock"
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists() and "relock-cmd-unset" in marker.read_text()
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_rc2_config_error_is_permanent_cooldown(home, tmp_path):
    """R2 P0: unlock rc=2 (no password / config error) ⇒ manual-only — a far-future
    next_retry, not a 30-min auto-retry loop."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    env = _env(home, tmp_path, initial="locked", opt_in=True, unlock_ok=False, fail_rc=2)
    assert _run(env).returncode == 0
    cd = home / PROJECT / ".unlock-cooldown"
    assert cd.exists(), "cooldown marker written"
    body = cd.read_text()
    assert "last_rc=2" in body
    import time as _t

    nr = int(
        [ln for ln in body.splitlines() if ln.startswith("next_retry_epoch=")][0].split("=")[1]
    )
    assert nr > _t.time() + 365 * 24 * 3600, "rc=2 ⇒ effectively permanent pause (manual clear)"


def test_cooldown_blocks_unlock(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    env = _env(home, tmp_path, initial="locked", opt_in=True, unlock_ok=True)
    # Pre-seed a cooldown that expires far in the future.
    cd = home / PROJECT / ".unlock-cooldown"
    cd.write_text(
        "count=5\nlast_epoch=0\nnext_retry_epoch=9999999999\nlast_rc=1\n", encoding="utf-8"
    )
    assert _run(env).returncode == 0
    assert _read(env, "_UNLOCK_SINK") == "", "in cooldown → never attempt unlock"
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists() and "unlock-cooldown" in marker.read_text()


def test_global_env_does_not_enable_unlock(home, tmp_path):
    """Full-sweep A1: the REMOVED global ``HANDOFF_UNLOCK_ENABLED=1`` env must NOT
    enable auto-unlock — only the per-project ``unlock.enabled`` sentinel does. A
    stray export (launchd / shell rc) must not arm password injection for a project
    that never opted in (red-line ③)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    # locked + NO per-project sentinel, but the old global backdoor env set to 1.
    env = _env(home, tmp_path, initial="locked", opt_in=False, unlock_ok=True)
    env["HANDOFF_UNLOCK_ENABLED"] = "1"  # must be ignored now
    assert _run(env).returncode == 0
    assert _read(env, "_UNLOCK_SINK") == "", "global env must NOT trigger unlock without sentinel"
    assert _read(env, "_OPEN_SINK") == "", "no unlock → no GUI"
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists() and "locked-unlock-not-enabled" in marker.read_text()
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_relock_failed_marker_halts_spawns_durably(home, tmp_path):
    """Full-sweep A3: a durable ``$HANDOFF_ROOT/.relock-failed`` marker (a prior run
    could not re-lock the Mac) must halt ALL spawns on subsequent runs — even on an
    already-unlocked screen — until the owner clears it. Without this the relay
    resumes spawning on an unattended unlocked Mac (red-line ②)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    (home / ".relock-failed").write_text("", encoding="utf-8")
    env = _env(home, tmp_path, initial="unlocked", opt_in=False, unlock_ok=True)
    assert _run(env).returncode == 0
    assert _read(env, "_OPEN_SINK") == "", ".relock-failed present → spawn skipped (no GUI)"
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists(), (
        ".uri kept until owner clears the halt"
    )


def test_corrupt_cooldown_marker_fails_closed(home, tmp_path):
    """Full-sweep A4: a PRESENT-but-corrupt cooldown marker (no numeric
    ``next_retry_epoch`` — e.g. a kill mid-write) must fail CLOSED — pause
    auto-unlock — not fall through and re-inject the login password."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    cd = home / PROJECT / ".unlock-cooldown"
    cd.write_text("count=oops\nlast_epoch=\n", encoding="utf-8")  # no valid next_retry_epoch
    env = _env(home, tmp_path, initial="locked", opt_in=True, unlock_ok=True)
    assert _run(env).returncode == 0
    assert _read(env, "_UNLOCK_SINK") == "", "corrupt cooldown → fail closed, no unlock attempt"
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists() and "unlock-cooldown" in marker.read_text()
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_cooldown_marker_nonnumeric_next_retry_fails_closed(home, tmp_path):
    """Gate0b A4 variant: a NON-numeric next_retry_epoch (not just a missing one)
    also fails closed — exercises the other half of the corrupt-marker branch."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    cd = home / PROJECT / ".unlock-cooldown"
    cd.write_text("count=2\nlast_epoch=0\nnext_retry_epoch=abc\nlast_rc=1\n", encoding="utf-8")
    env = _env(home, tmp_path, initial="locked", opt_in=True, unlock_ok=True)
    assert _run(env).returncode == 0
    assert _read(env, "_UNLOCK_SINK") == "", "non-numeric next_retry → fail closed, no unlock"
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists() and "unlock-cooldown" in marker.read_text()


def test_sigterm_after_unlock_before_flag_relocks(home, tmp_path):
    """Gate0b P1 (A2 race): a TERM that lands AFTER the unlock CLI already unlocked
    the screen but BEFORE ``UNLOCKED_BY_US=1`` is set must STILL re-lock — via the
    ``MAY_NEED_RELOCK`` guard set before the unlock CLI runs — or the Mac is stranded
    unlocked (red-line ②). We force that exact window by making the unlock stub flip
    state→unlocked immediately, then block (sleep) so the launcher is still inside
    run_with_timeout (UNLOCKED_BY_US not yet set) when we deliver SIGTERM."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    env = _env(home, tmp_path, initial="locked", opt_in=True, unlock_ok=True)
    stub = tmp_path / "stubs"
    # flip state→unlocked NOW, then block so the launcher is mid-run_with_timeout.
    _w(
        stub / "unlock",
        '#!/bin/bash\nprintf u >> "$_UNLOCK_SINK"\necho unlocked > "$_LOCKSTATE"\nsleep 6\nexit 0\n',
    )
    proc = subprocess.Popen(
        ["/bin/bash", str(SCRIPT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    state = Path(env["_LOCKSTATE"])
    unlock_sink = Path(env["_UNLOCK_SINK"])
    deadline = time.time() + 8
    in_window = False
    while time.time() < deadline:
        if (
            unlock_sink.exists()
            and unlock_sink.read_text() == "u"
            and state.read_text().strip() == "unlocked"
        ):
            in_window = True
            break
        time.sleep(0.1)
    if not in_window:
        proc.kill()
        proc.wait(timeout=10)
        raise AssertionError("unlock stub did not reach the race window in time")
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=15)
    assert Path(env["_RELOCK_SINK"]).read_text() == "r", "TERM in race window must still re-lock"
    assert state.read_text().strip() == "locked", "screen must end re-locked, not stranded unlocked"


def test_lockprobe_derives_quartz_status_when_no_explicit_check(home, tmp_path):
    """P0 lock-probe fix: with NO explicit HANDOFF_LOCK_CHECK_CMD, the launcher must
    derive a Quartz lock probe from HANDOFF_UNLOCK_CMD (--unlock→--status, exit-code
    0=unlocked/1=locked) instead of the ioreg fallback (which on modern macOS reports
    "unlocked" for a LOCKED screen — the on-box 2c P0). Drives locked→unlock→relock
    entirely through the derived --status exit-code probe (no stdout stub, no ioreg)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    stub = tmp_path / "stubs"
    stub.mkdir(exist_ok=True)
    state = tmp_path / "lockstate"
    state.write_text("locked\n", encoding="utf-8")
    unlock_sink = tmp_path / "u.log"
    relock_sink = tmp_path / "r.log"
    open_sink = tmp_path / "o.log"
    # one mp-unlock stub multiplexing --status (exit-code), --unlock, --lock
    _w(
        stub / "mp",
        '#!/bin/bash\ncase "$1" in\n'
        '  --status) [ "$(cat "$_LOCKSTATE")" = locked ] && exit 1 || exit 0 ;;\n'
        '  --unlock) printf u >> "$_UNLOCK_SINK"; echo unlocked > "$_LOCKSTATE"; exit 0 ;;\n'
        '  --lock)   printf r >> "$_RELOCK_SINK"; echo locked > "$_LOCKSTATE"; exit 0 ;;\n'
        "esac\nexit 2\n",
    )
    _w(stub / "open", f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{open_sink}"\nexit 0\n')
    _w(stub / "code", "#!/bin/bash\nexit 0\n")
    _w(
        stub / "osascript",
        '#!/bin/bash\ncase "$*" in\n'
        '  *"UI elements enabled"*) echo true ;;\n'
        '  *"frontmost is true"*) echo Code ;;\n'
        "esac\nexit 0\n",
    )
    (home / PROJECT / "unlock.enabled").write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "HANDOFF_ROOT": str(home),
            "HANDOFF_OPEN_CMD": str(stub / "open"),
            "HANDOFF_OSASCRIPT_CMD": str(stub / "osascript"),
            "HANDOFF_CODE_BIN": str(stub / "code"),
            "HANDOFF_LOCK_CHECK_CMD": "",  # FORCE the derived Quartz --status path
            "HANDOFF_UNLOCK_CMD": f"{stub / 'mp'} --unlock",
            "HANDOFF_RELOCK_CMD": f"{stub / 'mp'} --lock",
            "HANDOFF_CAFFEINATE_CMD": "",
            "HANDOFF_SKIP_SPAWN": "0",
            "HANDOFF_VSCODE_CHECK": "0",
            "_LOCKSTATE": str(state),
            "_UNLOCK_SINK": str(unlock_sink),
            "_RELOCK_SINK": str(relock_sink),
            "_OPEN_SINK": str(open_sink),
        }
    )
    env.pop("HANDOFF_HOME", None)
    assert _run(env).returncode == 0
    assert unlock_sink.read_text() == "u", "derived --status detected locked → unlock attempted"
    assert open_sink.read_text().strip(), "after unlock → GUI open invoked"
    assert relock_sink.read_text() == "r", "we unlocked → relock"
    assert state.read_text().strip() == "locked", "ended re-locked via derived probe"


def test_unlock_cmd_without_unlock_flag_fails_closed_unknown(home, tmp_path):
    """lock-probe P0-2: unlock CONFIGURED but no Quartz --status derivable (the cmd has
    no `--unlock` token to swap) AND no explicit HANDOFF_LOCK_CHECK_CMD ⇒ screen_is_locked
    returns UNKNOWN → fail-closed defer; it must NEVER fall back to the unreliable ioreg
    (which on macOS 26 reads a locked screen as 'unlocked' → blind spawn behind the lock)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    stub = tmp_path / "stubs"
    stub.mkdir(exist_ok=True)
    open_sink = tmp_path / "o.log"
    ioreg_called = tmp_path / "ioreg_called"
    _w(stub / "open", '#!/bin/bash\nprintf "%s\\n" "$*" >> "$_OPEN_SINK"\nexit 0\n')
    _w(stub / "code", "#!/bin/bash\nexit 0\n")
    _w(stub / "noop", "#!/bin/bash\nexit 0\n")
    _w(stub / "mpwrap", "#!/bin/bash\nexit 0\n")  # an unlock cmd with NO --unlock token
    # ioreg stub that records being called AND emits an "unlocked-looking" payload
    # (no CGSSessionScreenIsLocked = Yes). If the code WRONGLY fell back to ioreg it
    # would read "unlocked" → spawn; the P0-2 return-2 must short-circuit BEFORE this.
    _w(stub / "ioreg", f'#!/bin/bash\ntouch "{ioreg_called}"\necho "  | nothing = here"\nexit 0\n')
    (home / PROJECT / "unlock.enabled").write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "HANDOFF_ROOT": str(home),
            "HANDOFF_OPEN_CMD": str(stub / "open"),
            "HANDOFF_OSASCRIPT_CMD": str(stub / "noop"),
            "HANDOFF_CODE_BIN": str(stub / "code"),
            "HANDOFF_IOREG_CMD": str(stub / "ioreg"),  # would say "unlocked" if reached
            "HANDOFF_LOCK_CHECK_CMD": "",  # no explicit probe
            "HANDOFF_UNLOCK_CMD": str(stub / "mpwrap"),  # non-empty, NO --unlock → underivable
            "HANDOFF_RELOCK_CMD": str(stub / "mpwrap"),
            "HANDOFF_CAFFEINATE_CMD": "",
            "HANDOFF_SKIP_SPAWN": "0",
            "HANDOFF_VSCODE_CHECK": "0",
            "_OPEN_SINK": str(open_sink),
        }
    )
    env.pop("HANDOFF_HOME", None)
    assert _run(env).returncode == 0
    assert not ioreg_called.exists(), (
        "P0-2: must NOT fall back to ioreg when unlock configured but underivable"
    )
    txt = open_sink.read_text() if open_sink.exists() else ""
    assert txt == "", "underivable probe + unlock configured → UNKNOWN → NO blind spawn"
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists() and "lock-unknown" in marker.read_text()
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_unknown_after_unlock_fails_closed(home, tmp_path):
    """lock-probe P0-1: if the POST-UNLOCK verify probe returns UNKNOWN (rc=2 — e.g. a
    Quartz --status timeout/error right after the unlock CLI ran), the launcher must fail
    CLOSED (defer + cooldown), NOT treat UNKNOWN as 'unlocked' and spawn into the void."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(home, ws)
    stub = tmp_path / "stubs"
    stub.mkdir(exist_ok=True)
    state = tmp_path / "lockstate"
    state.write_text("locked\n", encoding="utf-8")
    open_sink = tmp_path / "o.log"
    unlock_sink = tmp_path / "u.log"
    # --status: locked→1, unlocked→0, anything else→2; --unlock leaves state UNKNOWN
    _w(
        stub / "mp",
        '#!/bin/bash\ncase "$1" in\n'
        '  --status) case "$(cat "$_LOCKSTATE")" in locked) exit 1 ;; unlocked) exit 0 ;; *) exit 2 ;; esac ;;\n'
        '  --unlock) printf u >> "$_UNLOCK_SINK"; echo unknown > "$_LOCKSTATE"; exit 0 ;;\n'
        '  --lock)   echo locked > "$_LOCKSTATE"; exit 0 ;;\n'
        "esac\nexit 2\n",
    )
    _w(stub / "open", '#!/bin/bash\nprintf "%s\\n" "$*" >> "$_OPEN_SINK"\nexit 0\n')
    _w(stub / "code", "#!/bin/bash\nexit 0\n")
    _w(stub / "noop", "#!/bin/bash\nexit 0\n")
    (home / PROJECT / "unlock.enabled").write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "HANDOFF_ROOT": str(home),
            "HANDOFF_OPEN_CMD": str(stub / "open"),
            "HANDOFF_OSASCRIPT_CMD": str(stub / "noop"),
            "HANDOFF_CODE_BIN": str(stub / "code"),
            "HANDOFF_LOCK_CHECK_CMD": "",
            "HANDOFF_UNLOCK_CMD": f"{stub / 'mp'} --unlock",
            "HANDOFF_RELOCK_CMD": f"{stub / 'mp'} --lock",
            "HANDOFF_CAFFEINATE_CMD": "",
            "HANDOFF_SKIP_SPAWN": "0",
            "HANDOFF_VSCODE_CHECK": "0",
            "_LOCKSTATE": str(state),
            "_OPEN_SINK": str(open_sink),
            "_UNLOCK_SINK": str(unlock_sink),
        }
    )
    env.pop("HANDOFF_HOME", None)
    assert _run(env).returncode == 0
    assert unlock_sink.read_text() == "u", "initial locked → unlock attempted"
    txt = open_sink.read_text() if open_sink.exists() else ""
    assert txt == "", "post-unlock UNKNOWN → fail closed, NO GUI spawn"
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists() and "verify2" in marker.read_text()
    assert (home / PROJECT / ".unlock-cooldown").exists(), "verify-failure ⇒ cooldown bumped"
