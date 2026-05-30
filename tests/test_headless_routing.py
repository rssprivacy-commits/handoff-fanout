"""Lock-aware routing tests for ``install/auto-continue.sh`` (headless fallback).

Exercises the design-spec §3.1 routing table by shelling out to the launcher
with the main spawn loop ACTIVE (``HANDOFF_SKIP_SPAWN=0``) and the lock probe
stubbed via ``HANDOFF_LOCK_CHECK_CMD``:

| lock state | headless opt-in | expected |
|---|---|---|
| unlocked   | —   | GUI: ``open`` the URI, no ``.req`` |
| locked     | yes | ``headless-req/<task>.req`` written, ``.uri`` claimed, no ``open`` |
| locked     | no  | defer: ``.uri`` kept, ``<task>.deferred`` marker, no ``.req`` |
| unknown    | —   | fail-closed defer (same as locked+no-opt-in) |

External commands (open / osascript / code / the lock probe) are stubbed to
record-or-print so the test is hermetic on Linux CI too.
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


def _write_stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _record_stub(path: Path, sink: Path, *, exit_code: int = 0) -> None:
    _write_stub(path, f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{sink}"\nexit {exit_code}\n')


def _lock_stub(path: Path, state: str) -> None:
    """Stub the lock probe: prints locked|unlocked|<unknown>."""
    _write_stub(path, f'#!/bin/bash\necho "{state}"\n')


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = tmp_path / "claude-handoff"
    (root / PROJECT / "queue").mkdir(parents=True)
    (root / PROJECT / "ack").mkdir(parents=True)
    return root


def _seed_uri(home: Path, workspace: Path, task: str = TASK) -> None:
    q = home / PROJECT / "queue"
    (q / f"{task}.uri").write_text(
        f"WORKSPACE={workspace}\nURI=vscode://anthropic.claude-code/open?prompt=x\n",
        encoding="utf-8",
    )
    (q / f"{task}.md").write_text("# prompt\nrun this task\n", encoding="utf-8")


def _base_env(home: Path, tmp_path: Path, *, lock: str, headless: bool) -> dict[str, str]:
    stub = tmp_path / "stubs"
    stub.mkdir(exist_ok=True)
    open_sink = tmp_path / "open.log"
    osa_sink = tmp_path / "osascript.log"
    code_sink = tmp_path / "code.log"
    open_stub = stub / "open"
    osa_stub = stub / "osascript"
    code_stub = stub / "code"
    lock_stub = stub / "lockprobe"
    _record_stub(open_stub, open_sink)
    _record_stub(osa_stub, osa_sink)
    _record_stub(code_stub, code_sink)
    _lock_stub(lock_stub, lock)
    env = dict(os.environ)
    env.update(
        {
            "HANDOFF_ROOT": str(home),
            "HANDOFF_OPEN_CMD": str(open_stub),
            "HANDOFF_OSASCRIPT_CMD": str(osa_stub),
            "HANDOFF_CODE_BIN": str(code_stub),
            "HANDOFF_LOCK_CHECK_CMD": str(lock_stub),
            "HANDOFF_SKIP_SPAWN": "0",
            "HANDOFF_VSCODE_CHECK": "0",
            "HANDOFF_HEADLESS_SWEEP": "0",  # don't shell out to `handoff` here
            "HANDOFF_HEADLESS_ENABLED": "1" if headless else "0",
            "HANDOFF_AUTOCLOSE_ENABLED": "0",
        }
    )
    env.pop("HANDOFF_HOME", None)
    env["_OPEN_SINK"] = str(open_sink)
    return env


def _sink(env: dict[str, str]) -> str:
    """open-stub sink content, or '' if open was never invoked (no file)."""
    p = Path(env["_OPEN_SINK"])
    return p.read_text().strip() if p.exists() else ""


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=25,
    )


def test_unlocked_routes_to_gui(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_uri(home, ws)
    env = _base_env(home, tmp_path, lock="unlocked", headless=False)
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    # GUI path consumed the .uri and called open.
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()
    assert _sink(env), "open should have been invoked"
    assert not (home / PROJECT / "headless-req" / f"{TASK}.req").exists()


def test_locked_opted_in_routes_to_headless(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_uri(home, ws)
    env = _base_env(home, tmp_path, lock="locked", headless=True)
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    req = home / PROJECT / "headless-req" / f"{TASK}.req"
    assert req.exists(), "headless-req/<task>.req must be written when locked + opted-in"
    body = req.read_text()
    assert f"WORKSPACE={ws}" in body
    assert f"task={TASK}" in body
    # .uri claimed (moved to launched/); GUI open NOT called.
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()
    assert _sink(env) == ""
    # prompt md stays for the runner to read.
    assert (home / PROJECT / "queue" / f"{TASK}.md").exists()
    assert (home / PROJECT / "ack" / f"{TASK}.headless-dispatched").exists()


def test_locked_not_opted_in_defers(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_uri(home, ws)
    env = _base_env(home, tmp_path, lock="locked", headless=False)
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    # .uri kept (no dead tab, no risky agent), defer marker written, no .req.
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists()
    assert "reason=locked-not-opted-in" in marker.read_text()
    assert not (home / PROJECT / "headless-req" / f"{TASK}.req").exists()
    assert _sink(env) == ""


def test_unknown_lock_fails_closed_to_defer(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_uri(home, ws)
    # headless opted-in, but lock state UNKNOWN ⇒ must still defer (fail-closed).
    env = _base_env(home, tmp_path, lock="dunno", headless=True)
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists()
    assert "reason=lock-unknown" in marker.read_text()
    assert not (home / PROJECT / "headless-req" / f"{TASK}.req").exists()


def test_headless_dispatch_works_with_vscode_not_running(home, tmp_path):
    """Guard conditionalization (P0 #5): a locked headless dispatch must not be
    blocked by the VS Code-running guard."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_uri(home, ws)
    env = _base_env(home, tmp_path, lock="locked", headless=True)
    env["HANDOFF_VSCODE_CHECK"] = "1"  # enforce the VS Code guard
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert (home / PROJECT / "headless-req" / f"{TASK}.req").exists()


def _ioreg_env(home, tmp_path, ioreg_body, *, headless):
    """Env that drives screen_is_locked through the REAL ioreg-parsing branch (no
    lock-check override), with ioreg itself stubbed via the ioreg cmd override."""
    env = _base_env(home, tmp_path, lock="unused", headless=headless)
    env.pop("HANDOFF_LOCK_CHECK_CMD", None)  # force the ioreg path
    ioreg_stub = tmp_path / "stubs" / "ioreg"
    _write_stub(ioreg_stub, ioreg_body)
    env["HANDOFF_IOREG_CMD"] = str(ioreg_stub)
    return env


def test_ioreg_key_absent_is_unlocked_not_defer(home, tmp_path):
    """REGRESSION: unlocked macs have the key ABSENT. key-absent MUST route GUI,
    never defer (else the relay stalls 100% on every unlocked machine)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_uri(home, ws)
    env = _ioreg_env(home, tmp_path, '#!/bin/bash\necho "  | {someotherkey = 1}"\n', headless=False)
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists(), "unlocked -> GUI consumes .uri"
    assert _sink(env), "unlocked (key absent) must GUI-spawn, not defer"
    assert not (home / PROJECT / "queue" / f"{TASK}.deferred").exists()


def test_ioreg_key_yes_is_locked(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_uri(home, ws)
    env = _ioreg_env(
        home,
        tmp_path,
        '#!/bin/bash\necho "    \\"CGSSessionScreenIsLocked\\" = Yes"\n',
        headless=True,
    )
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert (home / PROJECT / "headless-req" / f"{TASK}.req").exists(), "= Yes -> locked -> headless"


def test_ioreg_command_failure_is_unknown_defer(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_uri(home, ws)
    env = _ioreg_env(home, tmp_path, "#!/bin/bash\nexit 1\n", headless=True)
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert (home / PROJECT / "queue" / f"{TASK}.uri").exists()
    marker = home / PROJECT / "queue" / f"{TASK}.deferred"
    assert marker.exists()
    assert "reason=lock-unknown" in marker.read_text()


def test_defer_marker_cleared_when_uri_consumed(home, tmp_path):
    """A stale .deferred from an earlier locked tick is removed once the .uri is
    finally consumed (here: unlock → GUI)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_uri(home, ws)
    # Tick 1: locked + not opted-in → defer marker appears.
    env_locked = _base_env(home, tmp_path, lock="locked", headless=False)
    _run(env_locked)
    assert (home / PROJECT / "queue" / f"{TASK}.deferred").exists()
    # Tick 2: unlocked → GUI consumes the .uri and clears the marker.
    env_unlocked = _base_env(home, tmp_path, lock="unlocked", headless=False)
    _run(env_unlocked)
    assert not (home / PROJECT / "queue" / f"{TASK}.deferred").exists()
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()
