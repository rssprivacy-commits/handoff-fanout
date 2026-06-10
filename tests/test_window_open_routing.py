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

import json
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


def _seed(home: Path, ws: Path, task: str, *, heartbeat: bool = False) -> None:
    q = home / PROJECT / "queue"
    (q / f"{task}.uri").write_text(
        f"WORKSPACE={ws}\nURI=vscode://anthropic.claude-code/open?prompt=x\n", encoding="utf-8"
    )
    (q / f"{task}.md").write_text("# prompt\n", encoding="utf-8")
    if heartbeat:
        # makes verify_session_started() return immediately on the cold success path
        (q / f"{task}.heartbeat").write_text("", encoding="utf-8")


def _env(home: Path, tmp_path: Path, *, front_window: str,
         grow_transcript: Path | None = None, grow_on_attempt: int | None = None,
         grow_after_open: str | None = None, ready_after: str | None = None,
         wrong_ready: str | None = None, osa_sleep: str | None = None,
         code_wins: list[str] | None = None) -> dict:
    stub = tmp_path / "stubs"
    stub.mkdir(exist_ok=True)
    code_sink = tmp_path / "code.log"
    open_sink = tmp_path / "open.log"
    key_sink = tmp_path / "key.log"

    # lock probe: always unlocked (the GUI path needs a confirmed-unlocked screen)
    _w(stub / "lockprobe", "#!/bin/bash\necho unlocked\n")
    # code: record the FULL argv (so tests assert the flag: -n / -r / none) then exit 0
    _w(stub / "code", f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{code_sink}"\nexit 0\n')
    # open: record + succeed (spawns the Claude tab). If _GROW_AFTER_OPEN is set, spawn a DELAYED background
    # writer that grows the transcript that many seconds AFTER open (simulating a manual/early Enter landing
    # DURING the settle, i.e. AFTER the pre-settle baseline is captured) → exercises the already-grew rc=3 path.
    _w(stub / "open",
       f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{open_sink}"\n'
       'if [ -n "$_GROW_AFTER_OPEN" ] && [ -n "$_GROW_TRANSCRIPT" ]; then '
       '( sleep "$_GROW_AFTER_OPEN"; echo x >> "$_GROW_TRANSCRIPT" ) & fi\nexit 0\n')
    # osascript stub. Order matters — earlier cases win on scripts that contain several substrings:
    #   - focus-drift v2 scripts FIRST (each carries a unique handoff-window-* marker; they also
    #     contain generic substrings like "name of front window"/"on run argv" that would otherwise
    #     mis-route to the legacy cases): probe → FRONT_APP:/FRONT_WIN:/WIN: lines (front app is
    #     always Code here, matching the legacy `frontmost is true` answer; window list from
    #     $_CODE_WINS); enum → hit iff a $_CODE_WINS line contains the token (LAST argv);
    #     raise → "raised" (its script text carries activate+AXRaise → visible in the sink).
    #   - atomic submit (`on run argv` … `keystroke return`): token is the LAST argv; emulate
    #     "front window contains token" → echo sent + record `k`, else echo mismatch (NO keystroke).
    #   - legacy warm escape-hatch keystroke (`… to keystroke return`, no argv): record `k` + echo ok.
    #   - frontmost_code_window_name (`name of front window`): echo $_FRONT_WIN.
    #   - is_frontmost_code (`frontmost is true`): echo Code.
    _w(
        stub / "osascript",
        "#!/bin/bash\nargs=\"$*\"\n"
        'printf "%s\\n" "$args" >> "$_OSA_SINK"\n'
        # optional per-call delay — simulates the render-contention slowdown where a System Events query
        # blocks for seconds while a freshly-opened VS Code window renders (the 2026-06-07 spawn-speedup
        # root cause). Used to prove wait_target_window_frontmost honours a WALL-CLOCK budget (not step-count).
        '[ -n "$_OSA_SLEEP" ] && sleep "$_OSA_SLEEP"\n'
        "case \"$args\" in\n"
        '  *"handoff-window-probe"*)\n'
        '      echo "PROBE:OK"\n'
        '      echo "FRONT_APP:Code"\n'
        '      echo "FRONT_WIN:$_FRONT_WIN"\n'
        '      if [ -n "$_CODE_WINS" ]; then printf "%s\\n" "$_CODE_WINS" | while IFS= read -r w; do echo "WIN:$w"; done; fi ;;\n'
        '  *"handoff-window-enum"*)\n'
        '      tok="${@: -1}"\n'
        '      if [ -n "$_CODE_WINS" ] && printf "%s\\n" "$_CODE_WINS" | grep -Fq -- "$tok"; then echo hit; else echo nohit; fi ;;\n'
        '  *"handoff-window-raise"*) echo raised ;;\n'
        '  *"UI elements enabled"*) echo true ;;\n'
        # readiness-gated cold submit (`on run argv` … `AXFocusedUIElement` … `Message input`): simulate the
        # center Claude tab grabbing focus only after _READY_AFTER polls. token is the LAST argv.
        '  *"AXFocusedUIElement"*)\n'
        '      tok="${@: -1}"\n'
        '      case "$_FRONT_WIN" in\n'
        '        *"$tok"*)\n'
        # _WRONG_READY: focused input is non-empty but its value lacks the task token (stale sidebar draft) →
        # the real osascript returns "wronginput"; the gate must keep waiting / withhold, NEVER submit (codex P1).
        '          if [ -n "$_WRONG_READY" ]; then echo wronginput;\n'
        '          else\n'
        '            p=$(cat "$_POLL_COUNT" 2>/dev/null || echo 0); p=$((p+1)); echo "$p" > "$_POLL_COUNT"\n'
        '            if [ "$p" -ge "${_READY_AFTER:-1}" ]; then\n'
        '              printf k >> "$_KEY_SINK"\n'
        '              n=$(cat "$_SUBMIT_COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$_SUBMIT_COUNT"\n'
        '              if [ -n "$_GROW_ON_ATTEMPT" ] && [ "$n" -ge "$_GROW_ON_ATTEMPT" ]; then echo x >> "$_GROW_TRANSCRIPT"; fi\n'
        '              echo sent\n'
        '            else echo emptyinput; fi\n'
        '          fi ;;\n'
        '        *) echo mismatch ;;\n'
        "      esac ;;\n"
        '  *"on run argv"*)\n'
        '      tok="${@: -1}"\n'
        '      case "$_FRONT_WIN" in\n'
        '        *"$tok"*)\n'
        '          printf k >> "$_KEY_SINK"\n'
        # count submit attempts; grow the worktree transcript on/after attempt N (simulate the cold
        # session finally STARTING — transcript appears → cold_submit_with_retry detects + stops).
        '          n=$(cat "$_SUBMIT_COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$_SUBMIT_COUNT"\n'
        '          if [ -n "$_GROW_ON_ATTEMPT" ] && [ "$n" -ge "$_GROW_ON_ATTEMPT" ]; then echo x >> "$_GROW_TRANSCRIPT"; fi\n'
        '          echo sent ;;\n'
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
            "HANDOFF_SIDEBAR_SETTLE_SECS": "0",  # no post-close layout-settle sleep in tests
            "HANDOFF_WIN_FRONT_SECS": "2",
            "HANDOFF_WIN_FRONT_SECS_WARM": "1",
            # cold single-Enter verify (fast): transcript-growth verify timeout 1s; transcript root in tmp
            "HANDOFF_TRANSCRIPT_ROOT": str(tmp_path / "transcripts"),
            "HANDOFF_COLD_VERIFY_SECS": "1",
            "HANDOFF_COLD_READY_SECS": "2",  # readiness-gate poll timeout (8 × 0.25s) — keep tests fast
            "_FRONT_WIN": front_window,
            "_CODE_WINS": "\n".join(code_wins) if code_wins else "",
            "_OSA_SLEEP": str(osa_sleep) if osa_sleep else "",
            "_KEY_SINK": str(key_sink),
            "_CODE_SINK": str(code_sink),
            "_OPEN_SINK": str(open_sink),
            "_OSA_SINK": str(tmp_path / "osa.log"),
            "_SUBMIT_COUNT": str(tmp_path / "submit_count.txt"),
            "_POLL_COUNT": str(tmp_path / "poll_count.txt"),
            "_GROW_TRANSCRIPT": str(grow_transcript) if grow_transcript else "",
            "_GROW_ON_ATTEMPT": str(grow_on_attempt) if grow_on_attempt else "",
            "_GROW_AFTER_OPEN": str(grow_after_open) if grow_after_open else "",
            "_READY_AFTER": str(ready_after) if ready_after else "",
            "_WRONG_READY": str(wrong_ready) if wrong_ready else "",
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


# --------------------------------------------------------------------------- single-pane path
# Phase 2 spawn-window-unify (R2 M1 TOCTOU): the dump now writes a JSON `.singlepane` sidecar
# {workspace, role, close_policy, spawn_nonce, predecessor_nonce} and binds the spawn_nonce into the
# generated workspace window.title. The watchdog must (a) JSON-parse the sidecar (not `cat` a path
# out of it) and (b) gate the Enter on the unguessable spawn_nonce — not merely the task token.


def _singlepane_sidecar(home: Path, tmp_path: Path, task: str, nonce: str) -> Path:
    """Seed a JSON singlepane sidecar (Task 2.1 format) + the workspace file it points to, so the
    watchdog routes to the SINGLEPANE_WINDOW path (`code -n` the generated .handoff.code-workspace)
    and gates the submit on the spawn_nonce read from the JSON."""
    ws_file = tmp_path / "sp" / f"{task}.handoff.code-workspace"
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(
        json.dumps(
            {
                "folders": [{"path": str(tmp_path / "repo")}],
                "settings": {"window.title": f"{PROJECT} · {task} · worker · {nonce} [singlepane]"},
            }
        ),
        encoding="utf-8",
    )
    sidecar = home / PROJECT / "queue" / f"{task}.singlepane"
    sidecar.write_text(
        json.dumps(
            {
                "workspace": str(ws_file),
                "role": "worker",
                "close_policy": "keep",
                "spawn_nonce": nonce,
                "predecessor_nonce": None,
            }
        ),
        encoding="utf-8",
    )
    return sidecar


def test_singlepane_submit_gates_on_spawn_nonce(home, tmp_path):
    """SINGLEPANE: JSON-sidecar parse routes to a dedicated `-n` window, and the Enter fires only
    because the front window title carries the unguessable spawn_nonce (the atomic title gate)."""
    task = "wh-sp"
    nonce = "deadbeefcafef00d"
    ws = tmp_path / "repo"
    ws.mkdir()
    _seed(home, ws, task, heartbeat=True)
    _singlepane_sidecar(home, tmp_path, task, nonce)
    # front window title carries the spawn_nonce → the atomic gate matches → submit fires
    env = _env(home, tmp_path, front_window=f"{PROJECT} · {task} · worker · {nonce} [singlepane] - x.py")
    assert _run(env).returncode == 0
    assert " -n " in f" {_code_log(tmp_path)} ", "singlepane routes to a dedicated new window with -n"
    assert _read(tmp_path / "key.log") == "k", "Enter fires when the front window carries the spawn_nonce"
    assert _ack(home, task, "submitted")


def test_singlepane_withholds_enter_when_title_lacks_nonce(home, tmp_path):
    """SINGLEPANE: a front window whose title carries the TASK token but the WRONG nonce (stale /
    guessed / sibling window) must NOT receive the Enter — the spawn_nonce is the gate, not the task.
    The window still opens (`-n`), only the synthetic Enter is withheld."""
    task = "wh-sp"
    nonce = "deadbeefcafef00d"
    ws = tmp_path / "repo"
    ws.mkdir()
    _seed(home, ws, task, heartbeat=True)
    _singlepane_sidecar(home, tmp_path, task, nonce)
    # title carries the task token but a DIFFERENT nonce → the nonce gate withholds the Enter
    env = _env(home, tmp_path, front_window=f"{PROJECT} · {task} · worker · 0000000000000000 [singlepane]")
    assert _run(env).returncode == 0
    assert " -n " in f" {_code_log(tmp_path)} ", "singlepane still opens the dedicated window"
    assert _read(tmp_path / "key.log") == "", "Enter withheld when the front window lacks the spawn_nonce"
    assert _ack(home, task, "failed")
    assert not _ack(home, task, "submitted")


# --------------------------------------------------------------------------- cold path


def _cold_ws(tmp_path: Path, task: str) -> Path:
    ws = tmp_path / "worktrees" / task
    ws.mkdir(parents=True)
    # the engine injects this; auto-continue picks it as OPEN_TARGET (identifiable title)
    (ws / ".handoff.code-workspace").write_text("{}", encoding="utf-8")
    return ws


def _cold_transcript(tmp_path: Path, ws: Path) -> Path:
    """The worktree session transcript path the script derives from WORKSPACE (slug = the path with
    every '/' and '.' → '-'). Create the (empty) dir so the osascript stub can grow a .jsonl into it
    on submit — `cold_submit_with_retry` reads its line count as the 'session started' signal."""
    slug = re.sub(r"[/.]", "-", str(ws))
    tdir = tmp_path / "transcripts" / slug
    tdir.mkdir(parents=True, exist_ok=True)
    return tdir / "sess.jsonl"


def test_cold_worktree_spawn_forces_new_window_with_dash_n(home, tmp_path):
    task = "cold-task"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)
    # front window title carries the task → focus assert + frontmost-wait pass; transcript grows on 1st Enter
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py",
               grow_transcript=tr, grow_on_attempt=1)
    assert _run(env).returncode == 0
    code_log = _code_log(tmp_path)
    assert " -n " in f" {code_log} ", "cold worktree spawn must force a NEW dedicated window (-n)"
    assert " -r " not in f" {code_log} ", "cold spawn must never use the clobbering -r flag"


def test_cold_focus_assert_blocks_enter_when_task_window_not_frontmost(home, tmp_path):
    """The synthetic Enter must NOT fire if the frontmost window isn't THE task window — and on that
    mismatch the launcher must AXRaise the task window back to front (owner: "if it's not on top, let it
    be on top, then Enter") so a later attempt can submit. AXRaise preserves the editor focus (proven
    live); only the removed focus chord broke it.

    focus-drift v2 note: the hardened raise only AXRaises a window that EXISTS (enumerate-first),
    so the task window is modeled in ``code_wins``; the frontmost stranger window is NOT in the
    pre-open snapshot (it appeared post-snapshot) → the discriminator still dispatches the URI and
    the Enter is withheld by the readiness gate — the pre-v2 contract of this test, unchanged."""
    task = "cold-wrongwin"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)  # dir exists, never grows (no Enter is ever sent — front window mismatches)
    env = _env(home, tmp_path, front_window="some-other-project — z.py",
               code_wins=[f"demo · {task} [worktree] — x.py"])
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "", "Enter must NOT be pressed onto a wrong window"
    assert "AXRaise" in _read(tmp_path / "osa.log"), "on mismatch the task window must be AXRaised back to front"
    assert _ack(home, task, "failed"), "abort must record a truthful `failed` ack (manual Enter needed)"
    assert not _ack(home, task, "submitted"), "must not claim submitted when Enter was withheld"


def test_cold_submit_succeeds_first_try_no_double(home, tmp_path):
    """First Enter lands → worktree transcript grows → submitted; exactly ONE Enter (no double-submit)."""
    task = "cold-ok"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py",
               grow_transcript=tr, grow_on_attempt=1)  # transcript grows on the 1st Enter
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "k", "exactly ONE Enter — transcript grew → no retry/double-submit"
    assert _ack(home, task, "submitted")


def test_cold_submit_single_enter_no_retry(home, tmp_path):
    """主人立法 2026-06-06: cold submit sends EXACTLY ONE bare Enter — NO blind retry. The stub would only grow
    the transcript on a (hypothetical) 2nd Enter, but only ONE Enter is ever sent → no growth → honest `failed`
    (manual Enter needed); never a 2nd Enter, never a false `submitted`. (The old 4× retry produced FALSE-POSITIVE
    acks: when an Enter hit the empty sidebar input the owner pressed Enter manually and the script credited its
    own retry. Single Enter + transcript-verify removes that lie.)"""
    task = "cold-noretry"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py",
               grow_transcript=tr, grow_on_attempt=2)  # would only grow on a 2nd Enter — which never fires
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "k", "EXACTLY one Enter — no retry (主人立法: 单次 bare Enter)"
    assert _ack(home, task, "failed"), "single Enter swallowed → honest failed (no false submitted)"
    assert not _ack(home, task, "submitted")


def test_cold_submit_honest_failed_when_enter_swallowed(home, tmp_path):
    """The single Enter is sent (window matched) but the transcript never grows (focus off the prompt input)
    → honest `failed` (manual Enter needed), and exactly ONE Enter (no retry)."""
    task = "cold-exhaust"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)  # dir exists, never grows → the Enter is swallowed
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py")  # grow_on_attempt=None
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "k", "exactly ONE Enter sent (window matched) but swallowed — no retry"
    assert _ack(home, task, "failed")
    assert not _ack(home, task, "submitted")


def test_cold_submit_monotonic_across_old_transcript_no_double(home, tmp_path):
    """A REUSED worktree with an OLD high-line transcript must NOT double-submit: the monotonic SUM
    signal sees the new (low-line) session file as growth on the 1st Enter. (With the buggy newest-file
    count, base=100 and the fresh 1-line file reads 1 < 100 → growth missed → 2nd Enter. codex+Gemini R2 P0.)"""
    task = "cold-reused"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)             # the NEW session file the stub grows
    old = tr.parent / "old-session.jsonl"           # a prior session left a 100-line transcript here
    old.write_text("{}\n" * 100, encoding="utf-8")
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py",
               grow_transcript=tr, grow_on_attempt=1)
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "k", "exactly ONE Enter — SUM(old 100 + new 1) > base 100 → growth"
    assert _ack(home, task, "submitted")


def test_cold_submit_uses_bare_enter_no_focus_chord(home, tmp_path):
    """COLD submit must be a BARE Enter — NO modifier chord of ANY kind before it. The tombstoned
    claude-vscode.focus chord (+ AXRaise) moved focus off the editor onto the empty sidebar → the Enter
    submitted nothing → ABORT. Single-pane is now owned by the handoff-helper VS Code extension, which closes
    the side bars on window load (onStartupFinished) — NOT by a launcher keystroke (see
    extension/test/handoffClose.test.ts). So the launcher's cold path sends NO cmd/ctrl/alt chord at all; only
    the bare Enter. This is the regression guard against any focus/layout chord creeping back into the launcher."""
    task = "cold-bare"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py",
               grow_transcript=tr, grow_on_attempt=1)
    assert _run(env).returncode == 0
    osa = _read(tmp_path / "osa.log")
    assert "control down, option down" not in osa, \
        "the cold submit must send NO modifier chord (no focus chord, no launcher close chord — single-pane is the extension's job)"
    assert _read(tmp_path / "key.log") == "k", "exactly ONE bare Enter — transcript grew → submitted, no retry"
    assert _ack(home, task, "submitted")


def test_warm_submit_does_not_run_focus_command(home, tmp_path):
    """WARM submit must NOT send the focus chord — the proven project-window path is unchanged."""
    task = "warm-nofocus"
    ws = tmp_path / "repo"
    ws.mkdir()
    _seed(home, ws, task)
    env = _env(home, tmp_path, front_window=f"main.py — {ws.name}")
    assert _run(env).returncode == 0
    osa = _read(tmp_path / "osa.log")
    assert "control down, option down" not in osa, "warm submit must NOT send the focus chord"


# ------------------------------------------------------ cold per-attempt diagnostics (2026-06-05 / E-1)


def _log(home: Path) -> str:
    return _read(home / "auto-continue.log")


def test_cold_submit_logs_readiness_verified_before_enter(home, tmp_path):
    """The readiness gate logs that focus was VERIFIED on the prompt input BEFORE the Enter — so a live spawn can
    see the Enter only ever fires on the prompt-bearing input (never a blind Enter). Here the input is ready
    immediately but the (stub) transcript never grows → honest rc=1 'verified input but no growth' (an unexpected
    edge), never a false 'submitted'."""
    task = "cold-diag-swallow"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)  # never grows
    fw = f"demo · {task} [worktree] — x.py"
    env = _env(home, tmp_path, front_window=fw)  # window matches token; input ready (default _READY_AFTER=1)
    assert _run(env).returncode == 0
    log = _log(home)
    assert "COLD-SUBMIT-START:" in log, "must log the cold-submit start with base line count"
    assert "focus VERIFIED on the prompt input" in log, "the Enter must only fire AFTER focus is verified on the prompt input"
    assert "transcript NOT grown" in log, "a verified-but-no-growth must be logged honestly (rc=1)"
    assert _ack(home, task, "failed")


def test_cold_submit_withholds_enter_until_ready_then_times_out(home, tmp_path):
    """When focus NEVER settles on the prompt input (here: the task window is never frontmost → the readiness gate
    only ever sees a mismatch), NO Enter is ever sent and the gate honestly times out (rc=5, 'focus never settled')
    — never a blind Enter onto the empty sidebar / a wrong window (the trust-preserving WITHHOLD)."""
    task = "cold-diag-mismatch"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window="some-other-project — z.py")  # title lacks token → gate sees mismatch
    assert _run(env).returncode == 0
    log = _log(home)
    assert "focus never settled on the prompt input" in log, "a never-ready submit must log rc=5 (Enter withheld)"
    assert _read(tmp_path / "key.log") == "", "NO Enter is ever sent when readiness never arrives"
    assert _ack(home, task, "failed")


def test_cold_submit_withholds_when_focused_input_lacks_task_token(home, tmp_path):
    """codex P1 2026-06-06: a focused Claude 'Message input' with a NON-EMPTY value that does NOT contain the task
    token (e.g. a stale left-sidebar draft) must NOT be treated as ready — `value is not ""` alone is too weak. The
    real osascript returns 'wronginput'; the gate keeps waiting and honestly times out (rc=5, Enter WITHHELD), never
    submitting into the wrong input."""
    task = "cold-wronginput"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py", wrong_ready="1")
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "", "NO Enter is sent when the focused input lacks the task token"
    assert _ack(home, task, "failed")
    log = _log(home)
    assert "focus never settled on the prompt input" in log and "wronginput" in log, "must log the wronginput withhold (rc=5)"


# --------------------------------------------------------------------------- spawn-speedup (2026-06-07)


def test_wait_target_window_frontmost_respects_wall_clock_budget(home, tmp_path):
    """wait_target_window_frontmost must honour a /bin/date WALL-CLOCK budget, NOT step-count
    (attempts = secs×5, sleep 0.2) which IGNORED the per-iter osascript cost and overshot ~3-5× under
    render contention (a nominal 3s ran ~16s — observed: the sp-deploy2 spawn 2026-06-07). Drive each
    osascript ~0.5s + a front window that NEVER matches the task → the wait times out, AXRaises, and
    re-waits. Assert the launcher's OWN measured `wait-frontmost` PERF segment stays within the budget
    (primary ≈2s + fallback ≈2s + one in-flight osascript ≈ ~5s), well below the old step-count overshoot
    (~14s at 0.5s/call). This is the regression guard against silently reverting to step-counting."""
    task = "cold-wallclock"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)  # never grows (no Enter ever lands — window never matches)
    env = _env(home, tmp_path, front_window="some-other-project — z.py", osa_sleep="0.5")
    assert _run(env).returncode == 0
    log = _log(home)
    m = re.search(r"PERF\[" + re.escape(task) + r"\]: wait-frontmost (\d+)ms", log)
    assert m, "wait-frontmost PERF segment must be emitted (instrumentation present)"
    waited_ms = int(m.group(1))
    assert waited_ms < 10000, (
        f"wait-frontmost took {waited_ms}ms — the /bin/date wall-clock budget (≈2s+2s+1 in-flight) was "
        "breached; the step-counting overshoot (~14s at 0.5s/call) has regressed"
    )
    assert "TIMED OUT" in log, "the never-match path must hit the timeout + AXRaise fallback"


def test_cold_path_skips_redundant_is_frontmost_code(home, tmp_path):
    """+20s fix: the COLD path must NOT call is_frontmost_code — it is redundant with cold_submit_with_retry's
    STRONGER atomic gate (front-window title + focused-input value both carry the task token) and BLOCKED ~10s
    under cold render. The PERF line for it must be ABSENT on a cold spawn, and the cold submit still proceeds
    via the readiness gate (one Enter, transcript grows → submitted)."""
    task = "cold-skipfront"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py",
               grow_transcript=tr, grow_on_attempt=1)
    assert _run(env).returncode == 0
    log = _log(home)
    assert f"PERF[{task}]: is-frontmost-code" not in log, (
        "cold path must SKIP the redundant is_frontmost_code osascript (short-circuited by COLD_WINDOW=1)"
    )
    assert _read(tmp_path / "key.log") == "k", "cold submit still proceeds via the readiness gate (one Enter)"
    assert _ack(home, task, "submitted")


def test_warm_path_still_runs_is_frontmost_code(home, tmp_path):
    """The +20s short-circuit is COLD-only: the warm path must STILL evaluate is_frontmost_code (its `else`
    not-frontmost abort depends on it). Assert the warm spawn emits the is-frontmost-code PERF line + submits."""
    task = "warm-front"
    ws = tmp_path / "repo"
    ws.mkdir()
    _seed(home, ws, task)
    env = _env(home, tmp_path, front_window=ws.name)  # warm token = basename(WORKSPACE) = "repo"
    assert _run(env).returncode == 0
    log = _log(home)
    assert f"PERF[{task}]: is-frontmost-code" in log, "warm path must still run (and time) is_frontmost_code"
    assert _read(tmp_path / "key.log") == "k", "warm submit fires its single window-guarded Enter"


def test_cold_submit_waits_for_slow_render_then_submits(home, tmp_path):
    """THE readiness gate's core value (replaces the flaky fixed-0.5s gamble that missed ~40% under load): the
    center Claude tab grabs focus LATE (here only on the 4th poll ≈ 1s) — a fixed-delay Enter would fire into the
    empty sidebar and MISS. The gate WAITS (read-only) until focus is verified on the prompt input, then sends
    EXACTLY ONE Enter → submitted. No miss, no lie."""
    task = "cold-slowrender"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py",
               grow_transcript=tr, grow_on_attempt=1, ready_after="4")  # input focused only on the 4th poll
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "k", "EXACTLY one Enter — sent only once focus was verified ready"
    assert _ack(home, task, "submitted"), "the gate waited out the slow render and submitted (no miss, no lie)"
    assert not _ack(home, task, "failed")


def test_cold_submit_already_grew_during_settle_acks_submitted_not_failed(home, tmp_path):
    """codex+Gemini dual-brain P0 (2026-06-06): if a manual/early Enter STARTS the session DURING the settle
    (transcript grows past the PRE-SETTLE baseline), the session IS running → ack `submitted` (so the control
    plane never re-triggers a DUPLICATE window) — NOT `failed`. We send NO Enter (rc=3) and report it HONESTLY
    as external (NOT script-verified). The baseline is captured BEFORE the settle, so the growth-during-settle
    is detected (a baseline taken AFTER the settle would already include it → the bug this guards)."""
    task = "cold-alreadygrew"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)  # transcript dir + session file
    # `open` spawns a delayed writer that grows the transcript 0.3s after open (AFTER the pre-settle baseline);
    # a 1s settle spans it → cold_submit's first already-grew check sees cur > base → rc=3. ready_after=99 keeps
    # the readiness gate from firing its OWN Enter, so the ONLY growth is the external (manual) one → genuine rc=3.
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py",
               grow_transcript=tr, grow_after_open="0.3", ready_after="99")
    env["HANDOFF_COLD_RENDER_SECS"] = "1"  # settle long enough to span the delayed (manual-Enter) growth
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "", "rc=3: NO Enter sent (session already started externally)"
    assert _ack(home, task, "submitted"), "an already-running session must be acked submitted (no duplicate re-trigger)"
    assert not _ack(home, task, "failed"), "must NOT mark a running session failed (the P0 state-mismatch this fixes)"
    log = _log(home)
    assert "rc=3" in log, "must log the already-grew (external) rc=3 detection"
    assert "NOT script-verified" in log, "must be HONEST it was external, not our auto-Enter"


def test_cold_submit_resolves_symlinked_workspace_for_transcript(home, tmp_path):
    """A symlinked workspace root (e.g. macOS /tmp → /private/tmp) must still detect transcript growth.
    Claude Code writes its transcript under the RESOLVED cwd, so the slug must be computed from the
    resolved path. Without resolution the slug is wrong → growth is never seen → false ABORT + blind
    retries that defeat the monotonic-SUM double-submit guard (2026-06-05 live-test finding). With the
    fix the growth is detected through the symlink → exactly ONE Enter (submitted, no blind retry)."""
    task = "cold-symlink"
    realroot = (tmp_path / "real").resolve()
    real_ws = realroot / "worktrees" / task          # canonical location (where the transcript is keyed)
    real_ws.mkdir(parents=True)
    (real_ws / ".handoff.code-workspace").write_text("{}", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(realroot)                        # WORKSPACE the launcher receives goes via the symlink
    ws = alias / "worktrees" / task                   # contains '/worktrees/' (cold path) AND is symlinked
    _seed(home, ws, task)
    # the transcript lives under the RESOLVED slug (that is where Claude Code actually writes it)
    resolved_slug = re.sub(r"[/.]", "-", str(real_ws))
    tdir = tmp_path / "transcripts" / resolved_slug
    tdir.mkdir(parents=True)
    tr = tdir / "sess.jsonl"
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py",
               grow_transcript=tr, grow_on_attempt=1)
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "k", "ONE Enter — growth detected through the symlink (no blind retry)"
    assert _ack(home, task, "submitted"), "the resolved-path slug must find the real transcript → submitted"


# ----------------------------------------- startup drift guard: runtime-vs-source (甲 / 2026-06-05 owner B+C)
#
# The launchd-run copy ~/.local/bin/auto-continue.sh is a DEPLOYED COPY of the canonical source
# install/auto-continue.sh, kept current by `install.sh --sync-launcher` (now auto-fired by the
# post-commit hook). The OLD guard only compared the running copy against the LAST-SYNCED sha file, so
# "source edited but runtime not yet synced" left both equal → no warning → stale launcher ran silently
# (owner pain: cold-submit failed, manual Enter). The new guard compares the running copy ($0) against
# the live canonical SOURCE and LOUDLY surfaces a mismatch — but NEVER skips a spawn (甲: stale-but-running
# beats a halted 接续 loop). Fully non-fatal.


def _other_source(tmp_path: Path) -> Path:
    """A canonical-source stand-in whose content differs from SCRIPT → forces a drift mismatch."""
    p = tmp_path / "canon-src.sh"
    p.write_text("#!/bin/bash\n# a DIFFERENT canonical source than the running copy\nexit 0\n", encoding="utf-8")
    return p


def _run_guard_only(home: Path, tmp_path: Path, *, canon_src: str) -> subprocess.CompletedProcess:
    """Run the launcher with spawning skipped so only the startup drift guard (+ overdue scan) executes."""
    env = _env(home, tmp_path, front_window="irrelevant")
    env["HANDOFF_SKIP_SPAWN"] = "1"
    env["HANDOFF_CANON_SRC"] = canon_src
    return _run(env)


def test_drift_guard_detects_source_ahead_loud_log_and_notify(home, tmp_path):
    """Running copy != canonical source → a LOUD drift line in the log + a desktop notification."""
    src = _other_source(tmp_path)
    r = _run_guard_only(home, tmp_path, canon_src=str(src))
    assert r.returncode == 0
    log = _log(home)
    assert "DRIFT" in log, "a source-ahead mismatch must be loudly logged (not the old silent blind spot)"
    assert "--sync-launcher" in log, "the log must tell the owner the exact remedy command"
    osa = _read(tmp_path / "osa.log")
    assert "display notification" in osa, "drift must fire a one-shot desktop notification"


def test_drift_guard_silent_when_runtime_matches_source(home, tmp_path):
    """Running copy == canonical source (point it at SCRIPT itself) → no drift warning at all."""
    r = _run_guard_only(home, tmp_path, canon_src=str(SCRIPT))
    assert r.returncode == 0
    assert "DRIFT" not in _log(home), "identical content must never warn (no false positives in steady state)"
    assert "display notification" not in _read(tmp_path / "osa.log")


def test_drift_guard_nonfatal_when_source_missing(home, tmp_path):
    """A missing canonical source must skip the check silently — never break the 接续 loop."""
    r = _run_guard_only(home, tmp_path, canon_src=str(tmp_path / "nonexistent-source.sh"))
    assert r.returncode == 0, "a missing source path is non-fatal"
    assert "DRIFT" not in _log(home), "no source to compare → no (false) drift"


def test_drift_guard_never_skips_spawn(home, tmp_path):
    """Even under drift, the launcher must STILL consume the spawn (甲: stale-but-running, never halt)."""
    task = "drift-warm"
    ws = tmp_path / "repo"
    ws.mkdir()
    _seed(home, ws, task, heartbeat=True)
    env = _env(home, tmp_path, front_window=f"main.py — {ws.name}")
    env["HANDOFF_CANON_SRC"] = str(_other_source(tmp_path))  # drift present
    assert _run(env).returncode == 0
    assert "DRIFT" in _log(home), "drift is surfaced…"
    assert _ack(home, task, "submitted"), "…but the spawn is NOT skipped (接续 continues on the current copy)"


def test_drift_guard_notification_throttled_once_per_sha(home, tmp_path):
    """Persistent drift must NAG via the log every run but only notify ONCE per distinct drift sha
    (so active editing of THIS file doesn't spam the desktop)."""
    src = _other_source(tmp_path)
    _run_guard_only(home, tmp_path, canon_src=str(src))
    _run_guard_only(home, tmp_path, canon_src=str(src))  # same drift sha, second run
    osa = _read(tmp_path / "osa.log")
    assert osa.count("display notification") == 1, "notification is throttled to one per drift sha"
    assert _log(home).count("DRIFT") >= 2, "the loud log line still fires every run (durable nag)"
