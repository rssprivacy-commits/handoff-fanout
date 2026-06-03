"""Window-open routing tests for ``install/auto-continue.sh`` (2026-06-03 code-r-clobber fix).

The pre-existing ``code -r`` ("reuse window") FORCE-replaced the last-active VS Code window when the
spawn target was not already open — so a background spawn for project B silently clobbered the owner's
focused window belonging to a *different* running project A (observed: a warm ``code -r /Private/ledger``
replaced a focused erp worktree window, freezing that session the same second). The fix (dual-brain
codex+Gemini / owner ruling 分治) drops ``-r`` and splits by window kind:

  cold (worktree, WORKSPACE under ``*/worktrees/*``) → ``code -n``   (new dedicated window; clobber-proof)
  warm (main repo)                                   → ``code <tgt>`` (no flag: reuse-if-open-else-new)

plus a cold-path focus assertion: the synthetic Enter only fires when THE task window is frontmost
(else it could land on a wrong window — a terminal mid-command / finance UI).

Verified by shelling out to the launcher with stubs that RECORD the ``code`` argv + the ``keystroke``
calls, and that drive ``is_frontmost_code`` / front-window-name deterministically.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "install" / "auto-continue.sh"
PROJECT = "demo"


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


def _seed(home: Path, ws: Path, task: str, *, heartbeat: bool = False) -> None:
    q = home / PROJECT / "queue"
    (q / f"{task}.uri").write_text(
        f"WORKSPACE={ws}\nURI=vscode://anthropic.claude-code/open?prompt=x\n", encoding="utf-8"
    )
    (q / f"{task}.md").write_text("# prompt\n", encoding="utf-8")
    if heartbeat:
        # makes verify_session_started() return immediately on the cold success path
        (q / f"{task}.heartbeat").write_text("", encoding="utf-8")


def _env(home: Path, tmp_path: Path, *, front_window: str) -> dict:
    stub = tmp_path / "stubs"
    stub.mkdir(exist_ok=True)
    code_sink = tmp_path / "code.log"
    open_sink = tmp_path / "open.log"
    key_sink = tmp_path / "key.log"

    # lock probe: always unlocked (the GUI path needs a confirmed-unlocked screen)
    _w(stub / "lockprobe", "#!/bin/bash\necho unlocked\n")
    # code: record the FULL argv (so tests assert the flag: -n / -r / none) then exit 0
    _w(stub / "code", f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{code_sink}"\nexit 0\n')
    # open: record + succeed (spawns the Claude tab)
    _w(stub / "open", f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{open_sink}"\nexit 0\n')
    # osascript stub. Order matters — earlier cases win on scripts that contain several substrings:
    #   - atomic submit (`on run argv` … `keystroke return`): token is the LAST argv; emulate
    #     "front window contains token" → echo sent + record `k`, else echo mismatch (NO keystroke).
    #   - legacy warm escape-hatch keystroke (`… to keystroke return`, no argv): record `k` + echo ok.
    #   - frontmost_code_window_name (`name of front window`): echo $_FRONT_WIN.
    #   - is_frontmost_code (`frontmost is true`): echo Code.
    _w(
        stub / "osascript",
        "#!/bin/bash\nargs=\"$*\"\ncase \"$args\" in\n"
        '  *"UI elements enabled"*) echo true ;;\n'
        '  *"on run argv"*)\n'
        '      tok="${@: -1}"\n'
        '      case "$_FRONT_WIN" in\n'
        '        *"$tok"*) printf k >> "$_KEY_SINK"; echo sent ;;\n'
        '        *) echo mismatch ;;\n'
        "      esac ;;\n"
        '  *"keystroke return"*) printf k >> "$_KEY_SINK"; echo ok ;;\n'
        '  *"name of front window"*) echo "$_FRONT_WIN" ;;\n'
        '  *"frontmost is true"*) echo Code ;;\n'
        "  *) exit 0 ;;\n"
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
            "HANDOFF_CAFFEINATE_CMD": "",
            "HANDOFF_SKIP_SPAWN": "0",
            "HANDOFF_VSCODE_CHECK": "0",
            "HANDOFF_UNLOCK_ENABLED": "0",
            # keep the test fast: no lock probe (unlocked default), short waits, no cold render sleep
            "HANDOFF_COLD_RENDER_SECS": "0",
            "HANDOFF_WIN_FRONT_SECS": "2",
            "HANDOFF_WIN_FRONT_SECS_WARM": "1",
            "HANDOFF_SUBMIT_VERIFY_SECS": "2",
            "_FRONT_WIN": front_window,
            "_KEY_SINK": str(key_sink),
            "_CODE_SINK": str(code_sink),
            "_OPEN_SINK": str(open_sink),
        }
    )
    env.pop("HANDOFF_HOME", None)
    return env


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=40, check=False
    )


def _read(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def _code_log(tmp_path: Path) -> str:
    return _read(tmp_path / "code.log")


def _ack(home: Path, task: str, status: str) -> bool:
    return (home / PROJECT / "ack" / f"{task}.{status}").exists()


# --------------------------------------------------------------------------- warm path


def test_warm_spawn_drops_dash_r_and_uses_no_flag(home, tmp_path):
    """A main-repo (non-worktree) spawn must NOT use `-r` (the clobber bug) — and not `-n` either."""
    task = "warm-task"
    ws = tmp_path / "repo"
    ws.mkdir()
    _seed(home, ws, task, heartbeat=True)
    # warm window title carries the workspace rootName ("repo") → window guard matches → submits
    env = _env(home, tmp_path, front_window=f"main.py — {ws.name}")
    assert _run(env).returncode == 0
    code_log = _code_log(tmp_path)
    assert str(ws) in code_log, "warm spawn must invoke `code <workspace>`"
    assert " -r " not in f" {code_log} ", "warm spawn must NOT use the clobbering -r flag"
    assert " -n " not in f" {code_log} ", "warm spawn must reuse-if-open (no forced new window)"
    assert _ack(home, task, "submitted"), "warm window guard matched the project window → submit"


def test_warm_window_guard_withholds_enter_on_wrong_project(home, tmp_path):
    """Warm submit must NOT fire when a DIFFERENT project's window is frontmost (P1 window guard)."""
    task = "warm-wrong"
    ws = tmp_path / "ledger"
    ws.mkdir()
    _seed(home, ws, task, heartbeat=True)
    # frontmost is another project's window — its title lacks the workspace name "ledger"
    env = _env(home, tmp_path, front_window="erp-system — service.py")
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "", "Enter withheld when a wrong project window is frontmost"
    assert _ack(home, task, "failed")
    assert not _ack(home, task, "submitted")


def test_warm_escape_hatch_falls_back_to_app_level(home, tmp_path):
    """HANDOFF_WARM_WINDOW_GUARD=0 → legacy app-level Enter (fires even if title lacks the rootName)."""
    task = "warm-escape"
    ws = tmp_path / "ledger"
    ws.mkdir()
    _seed(home, ws, task, heartbeat=True)
    env = _env(home, tmp_path, front_window="erp-system — service.py")  # would mismatch under the guard
    env["HANDOFF_WARM_WINDOW_GUARD"] = "0"
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "k", "escape hatch → app-level Enter still fires"
    assert _ack(home, task, "submitted")


# --------------------------------------------------------------------------- cold path


def _cold_ws(tmp_path: Path, task: str) -> Path:
    ws = tmp_path / "worktrees" / task
    ws.mkdir(parents=True)
    # the engine injects this; auto-continue picks it as OPEN_TARGET (identifiable title)
    (ws / ".handoff.code-workspace").write_text("{}", encoding="utf-8")
    return ws


def test_cold_worktree_spawn_forces_new_window_with_dash_n(home, tmp_path):
    task = "cold-task"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, heartbeat=True)
    # front window title carries the task → focus assert + frontmost-wait both pass immediately
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py")
    assert _run(env).returncode == 0
    code_log = _code_log(tmp_path)
    assert " -n " in f" {code_log} ", "cold worktree spawn must force a NEW dedicated window (-n)"
    assert " -r " not in f" {code_log} ", "cold spawn must never use the clobbering -r flag"


def test_cold_focus_assert_blocks_enter_when_task_window_not_frontmost(home, tmp_path):
    """The synthetic Enter must NOT fire if the frontmost window isn't THE task window."""
    task = "cold-wrongwin"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, heartbeat=True)
    # frontmost window belongs to a DIFFERENT project → focus assert must abort the keystroke
    env = _env(home, tmp_path, front_window="some-other-project — z.py")
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "", "Enter must NOT be pressed onto a wrong window"
    assert _ack(home, task, "failed"), "abort must record a truthful `failed` ack (manual Enter needed)"
    assert not _ack(home, task, "submitted"), "must not claim submitted when Enter was withheld"


def test_cold_focus_assert_allows_enter_when_task_window_frontmost(home, tmp_path):
    """When THE task window is frontmost, the Enter fires and the heartbeat verifies the submit."""
    task = "cold-rightwin"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, heartbeat=True)  # heartbeat present → verify_session_started passes
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py")
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "k", "Enter must fire when the task window is frontmost"
    assert _ack(home, task, "submitted"), "heartbeat present → submit verified"
