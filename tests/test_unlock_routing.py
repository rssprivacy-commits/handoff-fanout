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
import subprocess
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
