"""Focus-drift fail-closed v2 for ``install/auto-continue.sh`` (sw-focusdrift-v2 / 2026-06-10).

THE BUG (wh-coord-10, log L94919-94929): on a 交棒 the dispatch command is typed in the OLD
window's terminal, so the OLD window holds frontmost. The watchdog `code -n`s the new window,
`wait_target_window_frontmost` times out (the owner actively holds the old window front),
`raise_task_window` + a 2s re-wait also fail — and the pre-fix code DISPATCHED THE URI ANYWAY
(its design comment assumed "timeout ⇒ frontmost == the just-opened window"; a 交棒 is exactly
the opposite). The URI prompt pasted into the OLD window (pollution), the nonce Enter gate
withheld, ack failed "focus drift", manual 2-step recovery. Contrast wh-coord-11: no contention,
501ms fast path, fully automatic.

THE FIX (dual-brain audited: codex's RED closed by the snapshot discriminator; gemini's MUST
raise-ordering absorbed; owner-approved):

  2.1 ONE pre-``code -n`` osascript snapshots every Code window name (it doubles as the
      retry-tick probe);
  2.2 fast path unchanged;
  2.3 ``raise_task_window`` hardened: enumerate FIRST (no activate), only a title hit
      activates + AXRaises — a miss does NOTHING (the old app-level activate-before-match
      pulled the OLD window front on a miss);
  2.4 timeout discriminator: frontmost is Code AND its window name ∉ snapshot → the fresh
      window with a lagging title → dispatch; EVERYTHING else → fail-closed: restore the
      .uri + defer + a dedicated ``focus_contended`` counter marker + diagnostics;
  2.5 bounded retry: marker count ≥ HANDOFF_FOCUS_DEFER_MAX (default 5) → give up with an
      actionable failed ack; retry ticks minimize focus theft (skip ``code -n`` when the
      target window still exists); URI success clears the marker.

C′ sandbox: every external exit (``osascript``/``open``/``code``) is BOTH env-seam stubbed AND
PATH-shadowed, with a per-binary positive-control tripwire that runs BEFORE the tested action
(p7-fix1 lesson: a probe placed after the action destroys the evidence of the tested calls).
No live queue, no real windows, no real keystrokes.
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

OWNER_WIN = "wilde-hexe — coordinator.md"  # the owner's pre-existing window (in the snapshot)


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


def _seed(home: Path, ws: Path, task: str) -> None:
    q = home / PROJECT / "queue"
    (q / f"{task}.uri").write_text(
        f"WORKSPACE={ws}\nURI=vscode://anthropic.claude-code/open?prompt=x\n", encoding="utf-8"
    )
    (q / f"{task}.md").write_text("# prompt\n", encoding="utf-8")


def _cold_ws(tmp_path: Path, task: str) -> Path:
    ws = tmp_path / "worktrees" / task
    ws.mkdir(parents=True)
    (ws / ".handoff.code-workspace").write_text("{}", encoding="utf-8")
    return ws


def _cold_transcript(tmp_path: Path, ws: Path) -> Path:
    """Worktree transcript path the script derives from WORKSPACE (slug: '/'+'.' → '-')."""
    slug = re.sub(r"[/.]", "-", str(ws))
    tdir = tmp_path / "transcripts" / slug
    tdir.mkdir(parents=True, exist_ok=True)
    return tdir / "sess.jsonl"


def _marker(home: Path, task: str) -> Path:
    return home / PROJECT / "queue" / f"{task}.focus_contended"


def _seed_marker(home: Path, task: str, count: int) -> None:
    _marker(home, task).write_text(f"count={count}\nfirst_epoch=1770000000\n", encoding="utf-8")


def _env(
    home: Path,
    tmp_path: Path,
    *,
    front_window: str,
    code_wins: list[str] | None = None,
    grow_transcript: Path | None = None,
    grow_on_attempt: int | None = None,
) -> dict:
    """Stub harness: env seams + PATH shadow over ``osascript``/``open``/``code``.

    ``front_window`` drives every frontmost answer (wait poll, probe FRONT_WIN, submit gate);
    ``code_wins`` is the full Code window-name list the probe/enumerate stubs report —
    i.e. the PRE-``code -n`` snapshot AND the raise-enumerate universe.
    """
    stub = tmp_path / "stubs"
    stub.mkdir(exist_ok=True)
    code_sink = tmp_path / "code.log"
    open_sink = tmp_path / "open.log"
    key_sink = tmp_path / "key.log"

    _w(stub / "lockprobe", "#!/bin/bash\necho unlocked\n")
    _w(stub / "code", '#!/bin/bash\nprintf "%s\\n" "$*" >> "$_CODE_SINK"\nexit 0\n')
    _w(stub / "open", '#!/bin/bash\nprintf "%s\\n" "$*" >> "$_OPEN_SINK"\nexit 0\n')
    # osascript stub. Case order matters — the three NEW v2 scripts carry unique markers and
    # are answered FIRST (they also contain generic substrings like "name of front window" /
    # "on run argv" that would otherwise mis-route to the legacy cases):
    #   handoff-window-probe → FRONT_APP:/FRONT_WIN:/WIN: lines from $_FRONT_APP/$_FRONT_WIN/$_CODE_WINS
    #   handoff-window-enum  → "hit" iff any $_CODE_WINS line contains the token (last argv)
    #   handoff-window-raise → "raised" (its script text carries activate+AXRaise → visible in the sink)
    _w(
        stub / "osascript",
        '#!/bin/bash\nargs="$*"\n'
        'printf "%s\\n" "$args" >> "$_OSA_SINK"\n'
        'case "$args" in\n'
        '  *"handoff-window-probe"*)\n'
        '      fa="${_FRONT_APP:-Code}"\n'
        '      echo "FRONT_APP:$fa"\n'
        '      if [ "$fa" = "Code" ]; then echo "FRONT_WIN:$_FRONT_WIN"; else echo "FRONT_WIN:"; fi\n'
        '      if [ -n "$_CODE_WINS" ]; then printf "%s\\n" "$_CODE_WINS" | while IFS= read -r w; do echo "WIN:$w"; done; fi ;;\n'
        '  *"handoff-window-enum"*)\n'
        '      tok="${@: -1}"\n'
        '      if [ -n "$_CODE_WINS" ] && printf "%s\\n" "$_CODE_WINS" | grep -Fq -- "$tok"; then echo hit; else echo nohit; fi ;;\n'
        '  *"handoff-window-raise"*) echo raised ;;\n'
        '  *"UI elements enabled"*) echo true ;;\n'
        '  *"AXFocusedUIElement"*)\n'
        '      tok="${@: -1}"\n'
        '      case "$_FRONT_WIN" in\n'
        '        *"$tok"*)\n'
        '          printf k >> "$_KEY_SINK"\n'
        '          n=$(cat "$_SUBMIT_COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$_SUBMIT_COUNT"\n'
        '          if [ -n "$_GROW_ON_ATTEMPT" ] && [ "$n" -ge "$_GROW_ON_ATTEMPT" ]; then echo x >> "$_GROW_TRANSCRIPT"; fi\n'
        "          echo sent ;;\n"
        "        *) echo mismatch ;;\n"
        "      esac ;;\n"
        '  *"on run argv"*)\n'
        '      tok="${@: -1}"\n'
        '      case "$_FRONT_WIN" in\n'
        '        *"$tok"*) printf k >> "$_KEY_SINK"; echo sent ;;\n'
        "        *) echo mismatch ;;\n"
        "      esac ;;\n"
        '  *"keystroke return"*) printf k >> "$_KEY_SINK"; echo ok ;;\n'
        '  *"name of front window"*) echo "$_FRONT_WIN" ;;\n'
        '  *"frontmost is true"*) echo "${_FRONT_APP:-Code}" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\nexit 0\n",
    )

    env = dict(os.environ)
    env.update(
        {
            # PATH shadow FIRST: a bare `osascript`/`open`/`code` resolves to the stubs,
            # never /usr/bin (belt) — the env seams below are the braces.
            "PATH": f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HANDOFF_ROOT": str(home),
            "HANDOFF_OPEN_CMD": str(stub / "open"),
            "HANDOFF_OSASCRIPT_CMD": str(stub / "osascript"),
            "HANDOFF_CODE_BIN": str(stub / "code"),
            "HANDOFF_LOCK_CHECK_CMD": str(stub / "lockprobe"),
            "HANDOFF_CAFFEINATE_CMD": "",
            "HANDOFF_SKIP_SPAWN": "0",
            "HANDOFF_VSCODE_CHECK": "0",
            "HANDOFF_UNLOCK_ENABLED": "0",
            "HANDOFF_AUTOCLOSE_ENABLED": "0",
            # hermetic drift guard: the script-under-test is its own canonical source
            "HANDOFF_CANON_SRC": str(SCRIPT),
            # fast waits: primary 1s + hardcoded 2s fallback re-wait
            "HANDOFF_WIN_FRONT_SECS": "1",
            "HANDOFF_WIN_FRONT_SECS_WARM": "1",
            "HANDOFF_COLD_RENDER_SECS": "0",
            "HANDOFF_TRANSCRIPT_ROOT": str(tmp_path / "transcripts"),
            "HANDOFF_COLD_VERIFY_SECS": "1",
            "HANDOFF_COLD_READY_SECS": "2",
            "_FRONT_WIN": front_window,
            "_FRONT_APP": "Code",
            "_CODE_WINS": "\n".join(code_wins) if code_wins else "",
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
    env.pop("HANDOFF_FOCUS_DEFER_MAX", None)
    return env


def _assert_shadow_then_clean(env: dict, tmp_path: Path) -> None:
    """Positive-control tripwire BEFORE the tested action (p7-fix1 lesson): prove the PATH
    shadow really intercepts each bare binary, then wipe the sinks so that afterwards
    'sink absent/empty' == 'never invoked' is trustworthy evidence."""
    sinks = {
        "osascript": Path(env["_OSA_SINK"]),
        "open": Path(env["_OPEN_SINK"]),
        "code": Path(env["_CODE_SINK"]),
    }
    for name, sink in sinks.items():
        probe = subprocess.run(
            ["/bin/bash", "-c", f"{name} --handoff-shadow-probe"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert probe.returncode == 0 and sink.is_file(), f"PATH shadow inactive for {name}"
        assert "--handoff-shadow-probe" in sink.read_text(), f"probe not recorded for {name}"
        sink.unlink()


def _run(env: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    _assert_shadow_then_clean(env, tmp_path)
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=40, check=False
    )


def _read(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def _log(home: Path) -> str:
    return _read(home / "auto-continue.log")


def _ack(home: Path, task: str, status: str) -> Path:
    return home / PROJECT / "ack" / f"{task}.{status}"


def _launched(home: Path, task: str) -> list[Path]:
    d = home / PROJECT / "launched"
    return sorted(d.glob(f"{task}-*.txt")) if d.is_dir() else []


def _marker_count(home: Path, task: str) -> int | None:
    m = _marker(home, task)
    if not m.exists():
        return None
    got = re.search(r"^count=(\d+)$", m.read_text(), re.M)
    return int(got.group(1)) if got else -1


# ─── 1. 争夺: owner's old window holds front → fail-closed, URI withheld ─────────────


def test_contention_fail_closed_withholds_uri_and_defers(home, tmp_path):
    """交棒 contention (wh-coord-10): the frontmost Code window IS in the pre-open snapshot
    (= the owner's OLD window still holds front after wait+raise+re-wait all failed) → the URI
    must NOT be dispatched (the pre-fix code dispatched anyway → prompt pasted into the old
    window). Fail-closed: .uri restored, focus_contended=1, deferred, NO spawned ack, and the
    log carries the matched_by/front_app/front_title diagnostics (codex MUST)."""
    task = "fd-contend"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)
    env = _env(home, tmp_path, front_window=OWNER_WIN, code_wins=[OWNER_WIN])
    assert _run(env, tmp_path).returncode == 0
    assert _read(tmp_path / "open.log") == "", "URI must be WITHHELD (tripwire: open never called)"
    assert (home / PROJECT / "queue" / f"{task}.uri").exists(), (
        ".uri must be restored for a retry tick"
    )
    assert not _launched(home, task), "the claimed intent must be handed back (mv → .uri)"
    assert _marker_count(home, task) == 1, "dedicated focus_contended counter must record 1"
    assert (home / PROJECT / "queue" / f"{task}.deferred").exists(), "defer marker must exist"
    assert "focus-contended" in _read(home / PROJECT / "queue" / f"{task}.deferred")
    assert not _ack(home, task, "spawned").exists(), "no spawned ack — nothing was dispatched"
    assert not _ack(home, task, "submitted").exists()
    assert _read(tmp_path / "key.log") == "", "no Enter anywhere near a fail-closed dispatch"
    log = _log(home)
    assert "matched_by=" in log and "front_app=" in log and "front_title=" in log, (
        "fail-closed must log the discriminator diagnostics (matched_by/front_app/front_title)"
    )
    assert "matched_by=none" in log, "no Code window carries the task token here → matched_by=none"


# ─── 2. 标题滞后: fresh window, title not bound yet → URI still dispatched ───────────


def test_title_lag_front_window_not_in_snapshot_dispatches(home, tmp_path):
    """A cold boot / slow render: the frontmost Code window's name is NOT in the pre-open
    snapshot → it IS the window we just opened (its title merely lags) → the URI is dispatched
    exactly as before (zero regression on the wh-coord-11 class of spawn; the Enter stays
    nonce/readiness-gated downstream)."""
    task = "fd-lag"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)
    # front window = a fresh untitled window; snapshot only knows the owner's window
    env = _env(
        home,
        tmp_path,
        front_window="Untitled (Workspace)",
        code_wins=[OWNER_WIN],
        grow_transcript=tr,
    )
    assert _run(env, tmp_path).returncode == 0
    assert "vscode://anthropic.claude-code" in _read(tmp_path / "open.log"), (
        "URI must be dispatched"
    )
    assert _ack(home, task, "spawned").exists(), "spawned ack written on dispatch"
    assert not (home / PROJECT / "queue" / f"{task}.uri").exists(), ".uri consumed"
    assert _marker_count(home, task) is None, "no contention marker on a dispatched spawn"
    assert "FOCUS-DISCRIMINATOR" in _log(home), "the dispatch decision must be logged"


# ─── 3. 有界放弃: 5th consecutive contention → actionable failed ack ─────────────────


def test_bounded_retry_gives_up_at_max_with_actionable_ack(home, tmp_path):
    """marker pre-seeded at 4 → this run's fail-closed bump makes 5 ≥ HANDOFF_FOCUS_DEFER_MAX
    (default 5) → give up: the .uri is CONSUMED (not restored), a failed ack carries the manual
    recovery instructions, the marker is cleared, and the throttled notification fires."""
    task = "fd-giveup"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)
    _seed_marker(home, task, 4)
    env = _env(home, tmp_path, front_window=OWNER_WIN, code_wins=[OWNER_WIN])
    assert _run(env, tmp_path).returncode == 0
    assert _read(tmp_path / "open.log") == "", "URI must still be withheld on the give-up tick"
    failed = _ack(home, task, "failed")
    assert failed.exists(), "give-up must write a failed ack"
    body = failed.read_text()
    assert "focus-contended x5" in body, "ack must say how many ticks were burned"
    assert "手动恢复" in body and "launched/" in body, (
        "ack must carry actionable manual recovery steps"
    )
    assert not (home / PROJECT / "queue" / f"{task}.uri").exists(), (
        ".uri consumed — no infinite retry"
    )
    assert _launched(home, task), (
        "the consumed intent stays parked in launched/ for manual recovery"
    )
    assert _marker_count(home, task) is None, "marker cleared on give-up"
    assert "display notification" in _read(tmp_path / "osa.log"), (
        "owner must be notified (throttled)"
    )


# ─── 4. 成功清零 + retry tick (a): target already front → skip code -n, dispatch ─────


def test_uri_success_clears_marker_and_retry_skips_code_n_when_front(home, tmp_path):
    """A retry tick that finds the target window ALREADY frontmost (probe, not a blind code -n)
    must skip `code -n` entirely (no focus theft, no duplicate window) and dispatch directly;
    the URI success clears the focus_contended marker (the 'consecutive' in bounded retry)."""
    task = "fd-clear"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)
    _seed_marker(home, task, 2)
    title = f"demo · {task} [worktree] — x.py"
    env = _env(
        home,
        tmp_path,
        front_window=title,
        code_wins=[OWNER_WIN, title],
        grow_transcript=tr,
        grow_on_attempt=1,
    )
    assert _run(env, tmp_path).returncode == 0
    assert _read(tmp_path / "code.log") == "", "code -n must be SKIPPED (target window already up)"
    assert "vscode://anthropic.claude-code" in _read(tmp_path / "open.log"), "URI dispatched"
    assert _marker_count(home, task) is None, "URI success must clear the focus_contended marker"
    assert _ack(home, task, "spawned").exists()
    assert _ack(home, task, "submitted").exists(), "happy retry completes the submit"


# ─── 5. 重试探针 (b): target exists in background → raise, no code -n ────────────────


def test_retry_tick_raises_existing_background_window_without_code_n(home, tmp_path):
    """A retry tick that finds the target window EXISTING but in the BACKGROUND must NOT
    `code -n` again (`code -n` focusing an existing workspace is only a SHOULD-level
    assumption) — it goes straight to the hardened raise; the still-contended front then
    fail-closes again (count bumps)."""
    task = "fd-retry"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)
    _seed_marker(home, task, 1)
    title = f"demo · {task} [worktree] — x.py"
    env = _env(home, tmp_path, front_window=OWNER_WIN, code_wins=[OWNER_WIN, title])
    assert _run(env, tmp_path).returncode == 0
    assert _read(tmp_path / "code.log") == "", "code -n must NOT be re-run (tripwire)"
    assert "handoff-window-raise" in _read(tmp_path / "osa.log"), "the hardened raise must fire"
    assert _marker_count(home, task) == 2, "still contended → counter bumps 1→2"
    assert _read(tmp_path / "open.log") == "", "URI still withheld"
    assert (home / PROJECT / "queue" / f"{task}.uri").exists(), ".uri restored again"


# ─── 6. raise 硬化: no title hit → ZERO activate (the old reverse-effect) ────────────


def test_raise_does_not_activate_when_no_window_matches(home, tmp_path):
    """gemini MUST: when NO Code window's title carries the token, raise_task_window must do
    NOTHING — the old code ran the app-level `activate` BEFORE the title match, so a miss
    net-pulled the owner's OLD window to front (the secondary lesion). The osascript sink must
    show ZERO activate calls."""
    task = "fd-noact"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    _cold_transcript(tmp_path, ws)
    # owner window holds front and is in the snapshot; the target window does not exist at all
    env = _env(home, tmp_path, front_window=OWNER_WIN, code_wins=[OWNER_WIN])
    assert _run(env, tmp_path).returncode == 0
    osa = _read(tmp_path / "osa.log")
    assert "handoff-window-enum" in osa, "the raise must have probed (enumerate-first)"
    assert "activate" not in osa, "NO activate may reach osascript when nothing matched"
    assert "AXRaise" not in osa, "no raise script at all on a miss"


# ─── 7. happy path 回归: immediate front match → byte-equivalent fast path ───────────


def test_happy_path_immediate_match_unchanged(home, tmp_path):
    """wh-coord-11 class (501ms): the fresh window takes front immediately → fast path —
    code -n, URI dispatched, ONE Enter, submitted ack; no discriminator, no marker, no defer."""
    task = "fd-happy"
    ws = _cold_ws(tmp_path, task)
    _seed(home, ws, task)
    tr = _cold_transcript(tmp_path, ws)
    env = _env(
        home,
        tmp_path,
        front_window=f"demo · {task} [worktree] — x.py",
        grow_transcript=tr,
        grow_on_attempt=1,
    )
    assert _run(env, tmp_path).returncode == 0
    assert " -n " in f" {_read(tmp_path / 'code.log')} ", "cold spawn still forces a new window"
    assert "vscode://anthropic.claude-code" in _read(tmp_path / "open.log")
    assert _read(tmp_path / "key.log") == "k", "exactly ONE Enter"
    assert _ack(home, task, "submitted").exists()
    assert _marker_count(home, task) is None, "no marker on the fast path"
    assert not (home / PROJECT / "queue" / f"{task}.deferred").exists()
    log = _log(home)
    assert "FOCUS-CONTENDED" not in log and "FOCUS-DISCRIMINATOR" not in log, (
        "the fast path must not even reach the discriminator"
    )


# ─── 8. singlepane: nonce-first raise token + matched_by=nonce diagnostics ───────────


def test_singlepane_contention_uses_nonce_token_first(home, tmp_path):
    """SINGLEPANE contention: the raise token precedence is spawn_nonce FIRST (unguessable,
    from the JSON sidecar), task id fallback — and the fail-closed diagnostics must say
    matched_by=nonce when the target window was found by its nonce."""
    import json

    task = "fd-sp"
    nonce = "deadbeefcafef00d"
    ws = tmp_path / "repo"
    ws.mkdir()
    _seed(home, ws, task)
    ws_file = tmp_path / "sp" / f"{task}.handoff.code-workspace"
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(
        json.dumps(
            {
                "folders": [{"path": str(ws)}],
                "settings": {"window.title": f"{PROJECT} · {task} · worker · {nonce} [singlepane]"},
            }
        ),
        encoding="utf-8",
    )
    (home / PROJECT / "queue" / f"{task}.singlepane").write_text(
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
    sp_title = f"{PROJECT} · {task} · worker · {nonce} [singlepane]"
    env = _env(home, tmp_path, front_window=OWNER_WIN, code_wins=[OWNER_WIN, sp_title])
    assert _run(env, tmp_path).returncode == 0
    assert _read(tmp_path / "open.log") == "", "contended singlepane must also fail-close"
    assert _marker_count(home, task) == 1
    log = _log(home)
    assert "matched_by=nonce" in log, "nonce-found target must be diagnosed as matched_by=nonce"
    osa = _read(tmp_path / "osa.log")
    # the sink records each call as "-e <multi-line script> <token>" — the token sits right
    # after the script's closing "end run", so anchor on the FIRST enum marker and read it there
    got = re.search(r"handoff-window-enum.*?end run (\S+)", osa, re.S)
    assert got, "an enumerate call must be recorded"
    assert got.group(1) == nonce, "the FIRST enumerate must try the spawn_nonce token"
