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
         grow_transcript: Path | None = None, grow_on_attempt: int | None = None) -> dict:
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
        "#!/bin/bash\nargs=\"$*\"\n"
        'printf "%s\\n" "$args" >> "$_OSA_SINK"\n'
        "case \"$args\" in\n"
        '  *"UI elements enabled"*) echo true ;;\n'
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
            "HANDOFF_WIN_FRONT_SECS": "2",
            "HANDOFF_WIN_FRONT_SECS_WARM": "1",
            # cold transcript-gated retry (fast): 2 attempts × 1s wait; transcript root in tmp
            "HANDOFF_TRANSCRIPT_ROOT": str(tmp_path / "transcripts"),
            "HANDOFF_COLD_SUBMIT_ATTEMPTS": "2",
            "HANDOFF_COLD_SUBMIT_WAIT_SECS": "1",
            "_FRONT_WIN": front_window,
            "_KEY_SINK": str(key_sink),
            "_CODE_SINK": str(code_sink),
            "_OPEN_SINK": str(open_sink),
            "_OSA_SINK": str(tmp_path / "osa.log"),
            "_SUBMIT_COUNT": str(tmp_path / "submit_count.txt"),
            "_GROW_TRANSCRIPT": str(grow_transcript) if grow_transcript else "",
            "_GROW_ON_ATTEMPT": str(grow_on_attempt) if grow_on_attempt else "",
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
    live); only the removed focus chord broke it."""
    task = "cold-wrongwin"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)  # dir exists, never grows (no Enter is ever sent — front window mismatches)
    env = _env(home, tmp_path, front_window="some-other-project — z.py")
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


def test_cold_submit_retries_when_first_enter_swallowed(home, tmp_path):
    """1st Enter swallowed (no growth) → 2nd Enter lands (growth) → submitted (transcript-gated retry)."""
    task = "cold-retry"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py",
               grow_transcript=tr, grow_on_attempt=2)  # only the 2nd Enter grows the transcript
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "kk", "two Enters — first swallowed, retry submitted"
    assert _ack(home, task, "submitted")


def test_cold_submit_exhausts_honestly_when_never_grows(home, tmp_path):
    """All attempts swallowed (transcript never grows) → honest `failed` (manual Enter needed)."""
    task = "cold-exhaust"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)  # dir exists, never grows → every Enter is swallowed
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py")  # grow_on_attempt=None
    assert _run(env).returncode == 0
    assert _read(tmp_path / "key.log") == "kk", "both attempts sent Enter (window matched) but were swallowed"
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
    """COLD submit must NOT run any focus chord before the Enter (2026-06-05 owner-diagnosed simplification,
    verified 3/3 live). The URI paste already leaves keyboard focus ON the editor Claude input; raising the
    window (AXRaise reset focus to the left sidebar/Explorer) + the claude-vscode.focus chord (grabbed the
    empty sidebar CC) were superfluous actions that MOVED focus off the editor → the Enter submitted the
    empty sidebar → ABORT. "Paste, then bare Enter" submits the editor input. So the focus chord (the
    cmd+ctrl+option modifier keystroke) must NOT appear in the osascript log."""
    task = "cold-bare"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=f"demo · {task} [worktree] — x.py",
               grow_transcript=tr, grow_on_attempt=1)
    assert _run(env).returncode == 0
    osa = _read(tmp_path / "osa.log")
    assert "command down, control down, option down" not in osa, "cold submit must NOT send a focus chord (bare Enter)"
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


def test_cold_submit_logs_per_attempt_when_enter_sent_but_swallowed(home, tmp_path):
    """An Enter that is SENT (window matched) but never grows the transcript (paste not settled / input
    not focused) must be logged distinctly per attempt — rc=0 + the frontmost window name — so a live
    spawn can tell this apart from a window MISMATCH. (Per-attempt diagnostics, 2026-06-05.)"""
    task = "cold-diag-swallow"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)  # never grows → every Enter swallowed
    fw = f"demo · {task} [worktree] — x.py"
    env = _env(home, tmp_path, front_window=fw)  # window matches token → rc=0 (sent)
    assert _run(env).returncode == 0
    log = _log(home)
    assert "COLD-SUBMIT-START:" in log, "must log the cold-submit start with base line count"
    assert "COLD-SUBMIT-ATTEMPT 1/2: rc=0" in log, "a SENT Enter must be logged with rc=0 per attempt"
    assert f"front_window='{fw}'" in log, "the frontmost window name must be captured for diagnosis"
    assert "swallowed (paste not settled yet?)" in log, "a sent-but-no-growth must say so"
    assert _ack(home, task, "failed")


def test_cold_submit_logs_mismatch_distinct_from_sent(home, tmp_path):
    """A window MISMATCH (focus drift to a wrong window, NO Enter sent) must log rc=1 — distinct from the
    rc=0 sent-but-swallowed case — so the two failure modes are never conflated in the log."""
    task = "cold-diag-mismatch"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window="some-other-project — z.py")  # title lacks token → rc=1
    assert _run(env).returncode == 0
    log = _log(home)
    assert "COLD-SUBMIT-ATTEMPT 1/2: rc=1" in log, "a withheld Enter (wrong window) must log rc=1 (mismatch)"
    assert "rc=0" not in log, "a pure mismatch must never be logged as a sent (rc=0) attempt"
    assert _read(tmp_path / "key.log") == "", "no Enter is sent on a mismatch"
    assert _ack(home, task, "failed")


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
