"""Return-leg WIRING tests for ``install/auto-continue.sh`` (djs-jump-return, 2026-06-14).

The return-to-origin leg (MP-style locate-act-return) was MOVED out of dharmaxis code-router into
auto-continue.sh by the djs-jump-fixret Fixer: the placement bug was that code-router fired the
goto-back-to-B at the ``$CODE_BIN -n`` step — BEFORE the wait_frontmost → AXRaise → open URI → Enter
sequence (which needs the worker window frontmost on desktop A). These tests pin the NEW orchestration:

  * precapture runs BEFORE ``code -n`` (the outbound jump), spawn-return runs AFTER URI+submit;
  * spawn-return carries the precaptured --origin / --before;
  * disarmed (no calls, byte-for-byte legacy) when: feature OFF (env or file), no SPAWNER_FOCUS,
    or CODE_BIN is not the router (no sibling vscode-spaces.py);
  * the screen-relock DEFER branch (after open URI) suppresses spawn-return (nothing truly dispatched).

The PRIMITIVE logic (race①/origin==target/fail-open) is covered by dharmaxis
scripts/vscode-spaces/test_return_leg.py; here we only assert the bash WIRING + ordering. Verified by
shelling out to the launcher with stubs that all append to one ordered EVENTS sink.

Run (hf suite): ``python -m pytest tests/test_return_leg_wiring.py -q`` (shim python).
"""

from __future__ import annotations

import os
import re
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


def _cold_ws(tmp_path: Path, task: str) -> Path:
    ws = tmp_path / "worktrees" / task
    ws.mkdir(parents=True)
    (ws / ".handoff.code-workspace").write_text("{}", encoding="utf-8")
    return ws


def _cold_transcript(tmp_path: Path, ws: Path) -> Path:
    slug = re.sub(r"[/.]", "-", str(ws))
    tdir = tmp_path / "transcripts" / slug
    tdir.mkdir(parents=True, exist_ok=True)
    return tdir / "sess.jsonl"


def _seed(home: Path, ws: Path, task: str, *, spawner_focus: str | None) -> None:
    q = home / PROJECT / "queue"
    uri = f"WORKSPACE={ws}\nURI=vscode://anthropic.claude-code/open?prompt=x\n"
    if spawner_focus is not None:
        uri += f"SPAWNER_FOCUS={spawner_focus}\n"
    (q / f"{task}.uri").write_text(uri, encoding="utf-8")
    (q / f"{task}.md").write_text("# prompt\n", encoding="utf-8")


def _build_stubs(tmp_path: Path, *, router: bool, lock_seq: str = "unlocked") -> Path:
    """Lay down the stub dir. The `code` stub IS HANDOFF_CODE_BIN; when `router=True` a sibling
    `vscode-spaces.py` exists beside it so _return_spaces_py resolves it (router-only contract)."""
    stub = tmp_path / "stubs"
    stub.mkdir(exist_ok=True)
    events = tmp_path / "events.log"

    # lock probe: stateful sequence (comma-separated). 1st call → 1st token, later calls → last token.
    # Lets the screen-relock test return "unlocked" at the initial gate then "locked" at submit time.
    _w(stub / "lockprobe",
       '#!/bin/bash\n'
       f'seq="{lock_seq}"; cf="{tmp_path}/lock_calls.txt"\n'
       'n=$(cat "$cf" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$cf"\n'
       'tok=$(printf "%s" "$seq" | cut -d, -f"$n"); [ -z "$tok" ] && tok=$(printf "%s" "$seq" | rev | cut -d, -f1 | rev)\n'
       'echo "$tok"\n')

    # code: record argv to the ordered EVENTS sink (+ a dedicated code sink for flag asserts).
    _w(stub / "code",
       f'#!/bin/bash\nprintf "CODE %s\\n" "$*" >> "{events}"\nprintf "%s\\n" "$*" >> "{tmp_path}/code.log"\nexit 0\n')

    # open: record the URI dispatch.
    _w(stub / "open",
       f'#!/bin/bash\nprintf "OPEN %s\\n" "$*" >> "{events}"\nexit 0\n')

    # vscode-spaces.py: run via `/usr/bin/python3 <this> spawn-precapture|spawn-return`. Records to
    # EVENTS; precapture prints ORIGIN=/BEFORE= so the wiring threads them into spawn-return's argv.
    if router:
        (stub / "vscode-spaces.py").write_text(
            "import os, sys\n"
            "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
            f"ev = {str(events)!r}\n"
            "with open(ev, 'a') as f:\n"
            "    if cmd == 'spawn-precapture':\n"
            "        f.write('PRECAPTURE\\n')\n"
            "    elif cmd == 'spawn-return':\n"
            "        f.write('SPAWN-RETURN ' + ' '.join(sys.argv[2:]) + '\\n')\n"
            "if cmd == 'spawn-precapture':\n"
            "    print('ORIGIN=' + os.environ.get('_PRE_ORIGIN', '8'))\n"
            "    print('BEFORE=' + os.environ.get('_PRE_BEFORE', '111,222'))\n"
            "    _aws = os.environ.get('_PRE_ANCHOR_WS', '')\n"
            "    if _aws:\n"
            "        print('RETURN_ANCHOR_WS=' + _aws)\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )

    # osascript: the proven cold-path model (probe → FRONT_APP/FRONT_WIN/WIN; readiness-gated cold
    # submit presses Enter + grows the transcript; raise/enum/frontmost). Key presses also append
    # ENTER to EVENTS so ordering vs OPEN/SPAWN-RETURN is observable.
    _w(stub / "osascript",
       "#!/bin/bash\nargs=\"$*\"\n"
       'printf "%s\\n" "$args" >> "$_OSA_SINK"\n'
       "case \"$args\" in\n"
       '  *"handoff-window-probe"*)\n'
       '      echo "PROBE:OK"; echo "FRONT_APP:Code"; echo "FRONT_WIN:$_FRONT_WIN"\n'
       '      if [ -n "$_CODE_WINS" ]; then printf "%s\\n" "$_CODE_WINS" | while IFS= read -r w; do echo "WIN:$w"; done; fi ;;\n'
       '  *"handoff-window-enum"*)\n'
       '      tok="${@: -1}"\n'
       '      if [ -n "$_CODE_WINS" ] && printf "%s\\n" "$_CODE_WINS" | grep -Fq -- "$tok"; then echo hit; else echo nohit; fi ;;\n'
       '  *"handoff-window-raise"*) echo raised ;;\n'
       '  *"UI elements enabled"*) echo "${_ACCESSIBILITY:-true}" ;;\n'
       '  *"AXFocusedUIElement"*)\n'
       '      tok="${@: -1}"\n'
       '      case "$_FRONT_WIN" in\n'
       '        *"$tok"*)\n'
       '          p=$(cat "$_POLL_COUNT" 2>/dev/null || echo 0); p=$((p+1)); echo "$p" > "$_POLL_COUNT"\n'
       '          if [ "$p" -ge "${_READY_AFTER:-1}" ]; then\n'
       '            printf k >> "$_KEY_SINK"; printf "ENTER\\n" >> "$_EVENTS"\n'
       '            [ -n "$_GROW_TRANSCRIPT" ] && echo x >> "$_GROW_TRANSCRIPT"\n'
       '            echo sent\n'
       '          else echo emptyinput; fi ;;\n'
       '        *) echo mismatch ;;\n'
       "      esac ;;\n"
       '  *"on run argv"*)\n'
       '      tok="${@: -1}"\n'
       '      case "$_FRONT_WIN" in\n'
       '        *"$tok"*) printf k >> "$_KEY_SINK"; printf "ENTER\\n" >> "$_EVENTS"; [ -n "$_GROW_TRANSCRIPT" ] && echo x >> "$_GROW_TRANSCRIPT"; echo sent ;;\n'
       '        *) echo mismatch ;;\n'
       "      esac ;;\n"
       '  *"keystroke return"*) printf k >> "$_KEY_SINK"; printf "ENTER\\n" >> "$_EVENTS"; echo ok ;;\n'
       '  *"name of front window"*) echo "$_FRONT_WIN" ;;\n'
       '  *"frontmost is true"*) echo Code ;;\n'
       "  *) exit 0 ;;\n"
       "esac\nexit 0\n")

    return stub


def _env(home: Path, tmp_path: Path, *, front_window: str, grow_transcript: Path | None,
         router: bool = True, lock_seq: str = "unlocked", code_bin_dir: str | None = None,
         extra: dict | None = None) -> dict:
    stub = _build_stubs(tmp_path, router=router, lock_seq=lock_seq)
    events = tmp_path / "events.log"
    code_bin = (Path(code_bin_dir) / "code") if code_bin_dir else (stub / "code")
    env = dict(os.environ)
    env.update({
        "HANDOFF_ROOT": str(home),
        "HANDOFF_OPEN_CMD": str(stub / "open"),
        "HANDOFF_OSASCRIPT_CMD": str(stub / "osascript"),
        "HANDOFF_CODE_BIN": str(code_bin),
        "HANDOFF_LOCK_CHECK_CMD": str(stub / "lockprobe"),
        "HANDOFF_CAFFEINATE_CMD": "",
        "HANDOFF_SKIP_SPAWN": "0",
        "HANDOFF_VSCODE_CHECK": "0",
        "HANDOFF_UNLOCK_ENABLED": "0",
        "HANDOFF_COLD_RENDER_SECS": "0",
        "HANDOFF_SIDEBAR_SETTLE_SECS": "0",
        "HANDOFF_WIN_FRONT_SECS": "2",
        "HANDOFF_WIN_FRONT_SECS_WARM": "1",
        "HANDOFF_TRANSCRIPT_ROOT": str(tmp_path / "transcripts"),
        "HANDOFF_COLD_VERIFY_SECS": "1",
        "HANDOFF_COLD_READY_SECS": "2",
        "_EVENTS": str(events),
        "_FRONT_WIN": front_window,
        "_CODE_WINS": "",
        "_KEY_SINK": str(tmp_path / "key.log"),
        "_OSA_SINK": str(tmp_path / "osa.log"),
        "_POLL_COUNT": str(tmp_path / "poll_count.txt"),
        "_GROW_TRANSCRIPT": str(grow_transcript) if grow_transcript else "",
        "_READY_AFTER": "1",
    })
    env.pop("HANDOFF_HOME", None)
    if extra:
        env.update(extra)
    return env


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True,
                          timeout=40, check=False)


def _events(tmp_path: Path) -> list[str]:
    p = tmp_path / "events.log"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def _tags(events: list[str]) -> list[str]:
    return [ln.split(" ", 1)[0] for ln in events]


# ─── happy path: ordering + arg threading ───────────────────────────────────────────────────────


def test_precapture_before_code_n_and_return_after_open(home, tmp_path):
    """A cold SPAWNER_FOCUS spawn: PRECAPTURE precedes CODE(-n); SPAWN-RETURN follows OPEN + ENTER."""
    task = "ret-cold"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, spawner_focus=str(tmp_path / "coordinator.handoff.code-workspace"))
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py", grow_transcript=tr)
    assert _run(env).returncode == 0
    tags = _tags(_events(tmp_path))
    assert "PRECAPTURE" in tags and "CODE" in tags and "OPEN" in tags and "SPAWN-RETURN" in tags, tags
    assert tags.index("PRECAPTURE") < tags.index("CODE"), f"precapture must precede code -n: {tags}"
    assert tags.index("OPEN") < tags.index("SPAWN-RETURN"), f"return must follow open URI: {tags}"
    # the worker window opened (code -n) before the prompt was injected (open URI)
    assert tags.index("CODE") < tags.index("OPEN"), tags


def test_code_n_flag_still_present(home, tmp_path):
    """Regression: adding precapture must not disturb the `-n` new-window flag on the cold path."""
    task = "ret-coldn"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, spawner_focus=str(tmp_path / "c.handoff.code-workspace"))
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py", grow_transcript=tr)
    assert _run(env).returncode == 0
    code_log = (tmp_path / "code.log").read_text(encoding="utf-8")
    assert " -n " in f" {code_log} ", "cold spawn must still force a NEW window (-n)"


def test_spawn_return_carries_precaptured_origin_before_and_anchor(home, tmp_path):
    """spawn-return's argv threads precapture ORIGIN/BEFORE + the §2.1 RETURN_ANCHOR_WS (so the
    primitive can one-step re-activate the owner's anchor) + the identity --anchor-token."""
    task = "ret-args"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, spawner_focus=str(tmp_path / "c.handoff.code-workspace"))
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py", grow_transcript=tr,
               extra={"_PRE_ORIGIN": "8", "_PRE_BEFORE": "111,222",
                      "_PRE_ANCHOR_WS": "/o/owner.code-workspace"})
    assert _run(env).returncode == 0
    line = next((e for e in _events(tmp_path) if e.startswith("SPAWN-RETURN")), "")
    assert "--origin=8" in line, line
    assert "--before=111,222" in line, line
    # mp-locate-return §2: the precaptured anchor is threaded; --max-wait is GONE (no poll)
    assert "--anchor-ws=/o/owner.code-workspace" in line, line
    assert "--anchor-app=" in line, line
    assert "--max-wait=" not in line, line
    # the cold submit token is the task id → threaded as the focus-steal identity guard token
    assert f"--anchor-token={task}" in line, line


# ─── disarmed paths (byte-for-byte legacy: no precapture, no return) ─────────────────────────────


def test_feature_off_env_no_return_calls(home, tmp_path):
    task = "ret-off-env"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, spawner_focus=str(tmp_path / "c.handoff.code-workspace"))
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py", grow_transcript=tr,
               extra={"HANDOFF_RETURN_AFTER_SPAWN": "off"})
    assert _run(env).returncode == 0
    tags = _tags(_events(tmp_path))
    assert "PRECAPTURE" not in tags and "SPAWN-RETURN" not in tags, tags
    assert "OPEN" in tags, "the spawn itself still happens with the feature off (byte-compat)"


def test_feature_off_file_no_return_calls(home, tmp_path):
    task = "ret-off-file"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, spawner_focus=str(tmp_path / "c.handoff.code-workspace"))
    tr = _cold_transcript(tmp_path, ws)
    fake_home = tmp_path / "fakehome"
    (fake_home / ".vscode-spaces").mkdir(parents=True)
    (fake_home / ".vscode-spaces" / "return-after-spawn.off").write_text("", encoding="utf-8")
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py", grow_transcript=tr,
               extra={"HOME": str(fake_home)})
    assert _run(env).returncode == 0
    tags = _tags(_events(tmp_path))
    assert "PRECAPTURE" not in tags and "SPAWN-RETURN" not in tags, tags


def test_no_spawner_focus_no_return_calls(home, tmp_path):
    """A cold spawn WITHOUT SPAWNER_FOCUS (no outbound jump) → disarmed → legacy byte-for-byte."""
    task = "ret-nofocus"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, spawner_focus=None)
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py", grow_transcript=tr)
    assert _run(env).returncode == 0
    tags = _tags(_events(tmp_path))
    assert "PRECAPTURE" not in tags and "SPAWN-RETURN" not in tags, tags
    assert "OPEN" in tags


def test_non_router_code_bin_no_return_calls(home, tmp_path):
    """CODE_BIN whose dir has NO sibling vscode-spaces.py (a plain `code`) → _return_spaces_py fails
    → fail-open, no return leg (even with SPAWNER_FOCUS + feature on)."""
    task = "ret-norouter"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, spawner_focus=str(tmp_path / "c.handoff.code-workspace"))
    tr = _cold_transcript(tmp_path, ws)
    # point CODE_BIN at a separate dir that has a `code` but NO vscode-spaces.py
    plain = tmp_path / "plainbin"
    _w(plain / "code", f'#!/bin/bash\nprintf "CODE %s\\n" "$*" >> "{tmp_path}/events.log"\nexit 0\n')
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py", grow_transcript=tr,
               code_bin_dir=str(plain))
    assert _run(env).returncode == 0
    tags = _tags(_events(tmp_path))
    assert "PRECAPTURE" not in tags and "SPAWN-RETURN" not in tags, tags
    assert "OPEN" in tags


# ─── defer exclusion: screen re-locked after open URI → no return ────────────────────────────────


def test_screen_relock_defer_suppresses_return(home, tmp_path):
    """Screen unlocked at the initial gate but re-locked by submit time → the abort/defer branch runs
    (after open URI). precapture armed + OPEN happened, but spawn-return must be SUPPRESSED
    (_RETURN_DEFERRED=1): nothing was truly dispatched, the .uri is restored for retry."""
    task = "ret-relock"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, spawner_focus=str(tmp_path / "c.handoff.code-workspace"))
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py", grow_transcript=tr,
               lock_seq="unlocked,locked")
    assert _run(env).returncode == 0
    tags = _tags(_events(tmp_path))
    assert "PRECAPTURE" in tags, "precapture still arms before code -n"
    assert "OPEN" in tags, "open URI happened before the relock check"
    assert "SPAWN-RETURN" not in tags, f"defer branch must suppress the return jump: {tags}"


# ─── P2-live-1: return arms ONLY on a SUCCESSFUL dispatch (ack `submitted`) ───────────────────────
# The gate is `_RETURN_DISPATCHED` (set only where a submit succeeds), NOT `!_RETURN_DEFERRED`. Any
# path that opens the worker tab on A but does NOT confirm the Enter (accessibility missing, Enter
# withheld / no transcript growth, frontmost-not-Code, …) must SUPPRESS the return — else the owner
# is snapped back to B, HIDING a worker that still needs a manual Enter (the LIVE P2 this closes).


def test_accessibility_missing_suppresses_return(home, tmp_path):
    """Accessibility not trusted → Enter never pressed (tab open, unsubmitted) → return SUPPRESSED."""
    task = "ret-noax"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, spawner_focus=str(tmp_path / "c.handoff.code-workspace"))
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py", grow_transcript=tr,
               extra={"_ACCESSIBILITY": "false"})
    assert _run(env).returncode == 0
    tags = _tags(_events(tmp_path))
    assert "PRECAPTURE" in tags, "precapture still arms before code -n"
    assert "OPEN" in tags, "the tab still opened (worker born on A, just unsubmitted)"
    assert "SPAWN-RETURN" not in tags, f"unsubmitted (no accessibility) must NOT return: {tags}"


def test_cold_no_transcript_growth_suppresses_return(home, tmp_path):
    """Cold submit: Enter sent on the verified input but the transcript NEVER grows (the session did
    not actually start) → ack `failed`, not `submitted` → return SUPPRESSED (worker needs manual Enter)."""
    task = "ret-nogrow"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, spawner_focus=str(tmp_path / "c.handoff.code-workspace"))
    # grow_transcript=None ⇒ the cold-submit stub presses Enter but the transcript file never grows
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py", grow_transcript=None)
    assert _run(env).returncode == 0
    tags = _tags(_events(tmp_path))
    assert "PRECAPTURE" in tags and "OPEN" in tags, tags
    assert "SPAWN-RETURN" not in tags, f"failed submit (no growth) must NOT return: {tags}"


def test_successful_cold_dispatch_still_returns(home, tmp_path):
    """Positive regression for the dispatch gate: a genuinely submitted cold spawn STILL returns."""
    task = "ret-ok"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task, spawner_focus=str(tmp_path / "c.handoff.code-workspace"))
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py", grow_transcript=tr)
    assert _run(env).returncode == 0
    tags = _tags(_events(tmp_path))
    assert "SPAWN-RETURN" in tags, f"a verified submit must still snap the owner back: {tags}"
