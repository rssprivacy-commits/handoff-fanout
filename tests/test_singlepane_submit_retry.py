"""SINGLEPANE bounded Enter retry — jsonl confirm + input gating (sw-sp-enter-retry / 2026-06-10).

THE BUG (owner: "经常手动 Enter"): the singlepane path submitted through the WARM one-shot gate
(one osascript title-nonce assertion + bare Enter, no retry). A singlepane spawn is a
cold-rendering NEW window — the Enter can fire while the URI paste has not landed (swallowed)
→ no second chance. The cold worktree fix (``cold_submit_with_retry``, transcript line-GROWTH
gating) cannot be reused: a singlepane session writes into the SHARED project transcript dir
where a SIBLING session's growth would false-confirm.

THE FIX (dual-brain GREEN + coordinator arbitration, implemented in
``singlepane_submit_with_retry``):

  1. baseline = the STRICT file-set of ``*.jsonl`` in the project transcript dir, captured
     BEFORE the URI dispatch; confirm = a NEW jsonl (∉ baseline) carrying ``🆔<task>``.
     mtime is BANNED — a resume/re-dispatch leaves OLD files with the same 🆔 (the
     false-positive MAIN path, locked by test 1);
  2. re-probe BEFORE every retry — confirmed → ack submitted, never press again;
  3. retry Enter gate (ONE osascript): Code frontmost ∧ front title contains the nonce ∧
     focused element is the Claude input ∧ focused value contains 🆔<task> → only then press.
     Empty/markerless input → DO NOT press, keep polling; polls exhausted →
     ``ambiguous-after-first-enter``. Front without the nonce → nonce-first raise, retry;
  4. bounded: retries ≤ HANDOFF_SP_RETRY_MAX (default 2), confirm poll
     HANDOFF_SP_POLL_SECS × HANDOFF_SP_POLL_TRIES (default 2s×3);
  5. diagnostics: ``SP-SUBMIT: attempt=N outcome=<no-new-jsonl|new-jsonl-no-marker|
     front-mismatch|input-not-ready|confirmed>``.

C′ sandbox: every external exit (``osascript``/``open``/``code``) is BOTH env-seam stubbed AND
PATH-shadowed, with a per-binary positive-control tripwire that runs BEFORE the tested action.
No live queue, no real windows, no real keystrokes.
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
NONCE = "deadbeefcafef00d"

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


def _sp_title(task: str) -> str:
    return f"{PROJECT} · {task} · worker · {NONCE} [singlepane]"


def _seed_singlepane(home: Path, tmp_path: Path, task: str) -> Path:
    """Seed a singlepane spawn intent: .uri (WORKSPACE = the real repo) + .md + the JSON
    sidecar pointing at a generated .handoff.code-workspace whose title binds the nonce."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    q = home / PROJECT / "queue"
    (q / f"{task}.uri").write_text(
        f"WORKSPACE={repo}\nURI=vscode://anthropic.claude-code/open?prompt=x\n", encoding="utf-8"
    )
    (q / f"{task}.md").write_text("# prompt\n", encoding="utf-8")
    ws_file = tmp_path / "sp" / f"{task}.handoff.code-workspace"
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(
        json.dumps(
            {
                "folders": [{"path": str(repo)}],
                "settings": {"window.title": _sp_title(task)},
            }
        ),
        encoding="utf-8",
    )
    (q / f"{task}.singlepane").write_text(
        json.dumps(
            {
                "workspace": str(ws_file),
                "role": "worker",
                "close_policy": "keep",
                "spawn_nonce": NONCE,
                "predecessor_nonce": None,
            }
        ),
        encoding="utf-8",
    )
    return repo


def _transcript_dir(tmp_path: Path, repo: Path) -> Path:
    """The project transcript dir the script derives from WORKSPACE (slug: '/'+'.' → '-')."""
    slug = re.sub(r"[/.]", "-", str(repo))
    tdir = tmp_path / "transcripts" / slug
    tdir.mkdir(parents=True, exist_ok=True)
    return tdir


def _env(
    home: Path,
    tmp_path: Path,
    *,
    front_window: str,
    code_wins: list[str] | None = None,
    input_value: str = "",
    new_jsonl: Path | None = None,
    new_jsonl_text: str = "",
    jsonl_on_press: int | None = None,
    jsonl_on_open: bool = False,
    raise_sets_front: str | None = None,
    enum_hit_only_after_open: bool = False,
    retry_max: int | None = None,
) -> dict:
    """Stub harness: env seams + PATH shadow over ``osascript``/``open``/``code``.

    The focused Claude input's value lives in a FILE (``_SP_INPUT_FILE``) so a successful
    press can EMPTY it (a real submit consumes the prompt); the front window name lives in
    ``_FRONT_WIN_FILE`` so the raise stub can flip it (``raise_sets_front``).
    ``jsonl_on_press=N`` makes the Nth press "work": it writes ``new_jsonl_text`` to
    ``new_jsonl`` (the NEW session transcript) and empties the input — press 1..N-1 are
    swallowed. ``jsonl_on_open`` makes the URI ``open`` itself drop the marked jsonl
    (a pre-press confirm: manual Enter raced us / proves the baseline predates the URI).
    """
    stub = tmp_path / "stubs"
    stub.mkdir(exist_ok=True)
    code_sink = tmp_path / "code.log"
    open_sink = tmp_path / "open.log"
    key_sink = tmp_path / "key.log"
    front_file = tmp_path / "front_win.txt"
    front_file.write_text(front_window, encoding="utf-8")
    input_file = tmp_path / "sp_input.txt"
    input_file.write_text(input_value, encoding="utf-8")

    _w(stub / "lockprobe", "#!/bin/bash\necho unlocked\n")
    _w(stub / "code", '#!/bin/bash\nprintf "%s\\n" "$*" >> "$_CODE_SINK"\nexit 0\n')
    _w(
        stub / "open",
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$_OPEN_SINK"\n'
        # the shadow-probe tripwire call must NOT birth the jsonl — only a real URI open may
        # (else the file pre-exists the launcher run and lands in the baseline set)
        'case "$*" in *"--handoff-shadow-probe"*) exit 0 ;; esac\n'
        'if [ -n "$_SP_JSONL_ON_OPEN" ] && [ -n "$_SP_NEW_JSONL" ]; then '
        'printf "%s\\n" "$_SP_NEW_JSONL_TEXT" > "$_SP_NEW_JSONL"; fi\nexit 0\n',
    )
    # osascript stub. Case order matters: the sp retry gate carries handoff-sp-retry-gate AND
    # generic substrings (on run argv / AXFocusedUIElement / keystroke return) — it MUST be
    # routed before those generic cases. _press() models one Enter: count it, record k, and
    # when the press-count reaches $_SP_JSONL_ON_PRESS the press "works" — the NEW marked
    # session jsonl is born and the input empties (a submit consumes the prompt). Below the
    # threshold the press is SWALLOWED (the owner-reported cold-render bug shape).
    _w(
        stub / "osascript",
        "#!/bin/bash\n"
        'args="$*"\n'
        'printf "%s\\n" "$args" >> "$_OSA_SINK"\n'
        '_front() { cat "$_FRONT_WIN_FILE" 2>/dev/null; }\n'
        "_press() {\n"
        '  printf k >> "$_KEY_SINK"\n'
        '  n=$(cat "$_SUBMIT_COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$_SUBMIT_COUNT"\n'
        '  if [ -n "$_SP_JSONL_ON_PRESS" ] && [ "$n" -ge "$_SP_JSONL_ON_PRESS" ] && [ -n "$_SP_NEW_JSONL" ]; then\n'
        '    printf "%s\\n" "$_SP_NEW_JSONL_TEXT" > "$_SP_NEW_JSONL"\n'
        '    : > "$_SP_INPUT_FILE"\n'
        "  fi\n"
        "}\n"
        'case "$args" in\n'
        '  *"handoff-sp-retry-gate"*)\n'
        '      tok="${@: -2:1}"; marker="${@: -1}"\n'
        '      case "$(_front)" in\n'
        '        *"$tok"*)\n'
        '          v=$(cat "$_SP_INPUT_FILE" 2>/dev/null)\n'
        '          if [ -z "$v" ]; then echo emptyinput\n'
        '          else case "$v" in\n'
        '            *"$marker"*) _press; echo sent ;;\n'
        "            *) echo wronginput ;;\n"
        "          esac; fi ;;\n"
        "        *) echo mismatch ;;\n"
        "      esac ;;\n"
        '  *"handoff-window-probe"*)\n'
        '      echo "PROBE:OK"\n'
        '      echo "FRONT_APP:Code"\n'
        '      echo "FRONT_WIN:$(_front)"\n'
        '      if [ -n "$_CODE_WINS" ]; then printf "%s\\n" "$_CODE_WINS" | while IFS= read -r w; do echo "WIN:$w"; done; fi ;;\n'
        '  *"handoff-window-enum"*)\n'
        '      tok="${@: -1}"\n'
        '      if [ -n "$_ENUM_AFTER_OPEN" ] && [ ! -s "$_OPEN_SINK" ]; then echo nohit\n'
        '      elif [ -n "$_CODE_WINS" ] && printf "%s\\n" "$_CODE_WINS" | grep -Fq -- "$tok"; then echo hit\n'
        "      else echo nohit; fi ;;\n"
        '  *"handoff-window-raise"*)\n'
        '      if [ -n "$_RAISE_SETS_FRONT" ]; then printf "%s" "$_RAISE_SETS_FRONT" > "$_FRONT_WIN_FILE"; fi\n'
        "      echo raised ;;\n"
        '  *"UI elements enabled"*) echo true ;;\n'
        # the warm atomic first press (on run argv … keystroke return, ONE token arg):
        # title-gated bare Enter — the input is NOT consulted (status quo for press #1).
        '  *"on run argv"*)\n'
        '      tok="${@: -1}"\n'
        '      case "$(_front)" in\n'
        '        *"$tok"*) _press; echo sent ;;\n'
        "        *) echo mismatch ;;\n"
        "      esac ;;\n"
        '  *"keystroke return"*) printf k >> "$_KEY_SINK"; echo ok ;;\n'
        '  *"name of front window"*) _front ;;\n'
        '  *"frontmost is true"*) echo Code ;;\n'
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
            "HANDOFF_WIN_FRONT_SECS": "1",
            "HANDOFF_WIN_FRONT_SECS_WARM": "1",
            "HANDOFF_COLD_RENDER_SECS": "0",
            "HANDOFF_TRANSCRIPT_ROOT": str(tmp_path / "transcripts"),
            # fast knobs for the machinery under test (1s×1 confirm poll)
            "HANDOFF_SP_POLL_SECS": "1",
            "HANDOFF_SP_POLL_TRIES": "1",
            **({"HANDOFF_SP_RETRY_MAX": str(retry_max)} if retry_max is not None else {}),
            "_FRONT_WIN_FILE": str(front_file),
            "_CODE_WINS": "\n".join(code_wins) if code_wins else "",
            "_KEY_SINK": str(key_sink),
            "_CODE_SINK": str(code_sink),
            "_OPEN_SINK": str(open_sink),
            "_OSA_SINK": str(tmp_path / "osa.log"),
            "_SUBMIT_COUNT": str(tmp_path / "submit_count.txt"),
            "_SP_INPUT_FILE": str(input_file),
            "_SP_NEW_JSONL": str(new_jsonl) if new_jsonl else "",
            "_SP_NEW_JSONL_TEXT": new_jsonl_text,
            "_SP_JSONL_ON_PRESS": str(jsonl_on_press) if jsonl_on_press else "",
            "_SP_JSONL_ON_OPEN": "1" if jsonl_on_open else "",
            "_RAISE_SETS_FRONT": raise_sets_front or "",
            "_ENUM_AFTER_OPEN": "1" if enum_hit_only_after_open else "",
        }
    )
    env.pop("HANDOFF_HOME", None)
    env.pop("HANDOFF_FOCUS_DEFER_MAX", None)
    env.pop("HANDOFF_SP_RETRY_MAX", None) if retry_max is None else None
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
        ["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def _read(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def _log(home: Path) -> str:
    return _read(home / "auto-continue.log")


def _ack(home: Path, task: str, status: str) -> Path:
    return home / PROJECT / "ack" / f"{task}.{status}"


def _presses(tmp_path: Path) -> int:
    return len(_read(tmp_path / "key.log"))


# ─── 1. mtime trap: an OLD jsonl carrying the SAME 🆔 must NEVER confirm ─────────────


def test_old_jsonl_with_same_marker_never_confirms(home, tmp_path):
    """Resume/re-dispatch of the same task leaves OLD transcript files containing the same
    🆔<task>. Under an mtime/content-only design they would false-confirm (the design
    difference this test locks in); under the strict new-file-set design they are baseline
    members → NOT confirm sources. With no NEW jsonl ever appearing the submit must end in
    an HONEST failed ack — not a false 'submitted'."""
    task = "sp-mtime-trap"
    repo = _seed_singlepane(home, tmp_path, task)
    tdir = _transcript_dir(tmp_path, repo)
    # the OLD transcript from a previous dispatch of the SAME task — marker and all
    (tdir / "old-sess.jsonl").write_text(f'{{"text":"🆔{task} previous run"}}\n', encoding="utf-8")
    env = _env(
        home,
        tmp_path,
        front_window=f"{_sp_title(task)} - x.py",
        input_value=f"🆔{task} the pasted prompt",
        retry_max=1,  # bounded fast: 1 retry is enough to prove no-confirm
    )
    assert _run(env, tmp_path).returncode == 0
    assert not _ack(home, task, "submitted").exists(), (
        "an OLD marked jsonl must NEVER count as the confirm signal (mtime trap)"
    )
    failed = _ack(home, task, "failed")
    assert failed.exists(), "no confirm → an HONEST failed ack"
    assert "no new 🆔-marked jsonl" in failed.read_text()
    log = _log(home)
    assert "SP-SUBMIT-START" in log
    assert "outcome=no-new-jsonl" in log, "diagnostic enum must say which step fell empty"
    assert _presses(tmp_path) >= 1, "the title-gated first Enter did fire (it just never confirmed)"


# ─── 2. happy path: our Enter births the NEW marked jsonl → submitted (verified) ─────


def test_new_marked_jsonl_confirms_submitted(home, tmp_path):
    """Press 1 works: a NEW jsonl (∉ baseline) carrying 🆔<task> appears → the first confirm
    poll hits → submitted ack (script-verified), exactly ONE Enter, outcome=confirmed."""
    task = "sp-happy"
    repo = _seed_singlepane(home, tmp_path, task)
    tdir = _transcript_dir(tmp_path, repo)
    env = _env(
        home,
        tmp_path,
        front_window=f"{_sp_title(task)} - x.py",
        input_value=f"🆔{task} the pasted prompt",
        new_jsonl=tdir / "new-sess.jsonl",
        new_jsonl_text=f'{{"text":"🆔{task} session started"}}',
        jsonl_on_press=1,
    )
    assert _run(env, tmp_path).returncode == 0
    sub = _ack(home, task, "submitted")
    assert sub.exists(), "new marked jsonl = the confirm signal → submitted"
    assert "verified" in sub.read_text()
    assert _presses(tmp_path) == 1, "confirmed on the first press — no extra Enter"
    assert "outcome=confirmed" in _log(home)


# ─── 3. sibling guard: a NEW jsonl WITHOUT the marker is not a confirm ───────────────


def test_new_jsonl_without_marker_does_not_confirm(home, tmp_path):
    """A sibling session in the SHARED project transcript dir births its own NEW jsonl —
    without 🆔<task> it must NOT confirm (the false-positive the cold-style growth gate
    would have committed). Diagnostics must say new-jsonl-no-marker; with the input then
    empty (our press consumed the prompt) the machinery must NEVER press again and end
    ambiguous-after-first-enter."""
    task = "sp-sibling"
    repo = _seed_singlepane(home, tmp_path, task)
    tdir = _transcript_dir(tmp_path, repo)
    env = _env(
        home,
        tmp_path,
        front_window=f"{_sp_title(task)} - x.py",
        input_value=f"🆔{task} the pasted prompt",
        new_jsonl=tdir / "sibling-sess.jsonl",
        new_jsonl_text='{"text":"a sibling session, no marker"}',
        jsonl_on_press=1,
        retry_max=1,
    )
    assert _run(env, tmp_path).returncode == 0
    assert not _ack(home, task, "submitted").exists(), "a markerless jsonl must not confirm"
    failed = _ack(home, task, "failed")
    assert failed.exists()
    assert "ambiguous-after-first-enter" in failed.read_text()
    log = _log(home)
    assert "outcome=new-jsonl-no-marker" in log, "diagnostics must name the sibling shape"
    assert _presses(tmp_path) == 1, "input went empty after press 1 → NEVER pressed again"


# ─── 4. the owner bug: swallowed first Enter → marker-gated retry press confirms ─────


def test_swallowed_enter_retries_and_confirms(home, tmp_path):
    """THE owner-reported shape: the cold render swallows Enter #1 (prompt still sitting in
    the input). The retry gate proves it (focused value still carries 🆔<task>) → press #2 →
    the NEW marked jsonl appears → submitted (script-verified). The pre-fix one-shot path had
    no second chance — the owner pressed Enter by hand."""
    task = "sp-swallow"
    repo = _seed_singlepane(home, tmp_path, task)
    tdir = _transcript_dir(tmp_path, repo)
    env = _env(
        home,
        tmp_path,
        front_window=f"{_sp_title(task)} - x.py",
        input_value=f"🆔{task} the pasted prompt",
        new_jsonl=tdir / "new-sess.jsonl",
        new_jsonl_text=f'{{"text":"🆔{task} session started"}}',
        jsonl_on_press=2,  # press 1 swallowed, press 2 works
    )
    assert _run(env, tmp_path).returncode == 0
    sub = _ack(home, task, "submitted")
    assert sub.exists(), "the bounded retry must recover a swallowed first Enter"
    assert "verified" in sub.read_text()
    assert _presses(tmp_path) == 2, "exactly one retry press (marker-gated)"
    log = _log(home)
    assert "attempt=2 outcome=confirmed" in log, "the retry attempt confirmed"


# ─── 5. front-mismatch at submit time → nonce-first raise → retry confirms ───────────


def test_front_mismatch_raises_then_retry_confirms(home, tmp_path):
    """The URI dispatched via the title-lag discriminator (front = a fresh untitled window ∉
    snapshot), so at submit time the front window lacks the nonce → attempt 1 presses nothing
    (mismatch) → the machinery raises THE task window (nonce-first, existing
    raise_task_window) → the retry gate then matches + the input still carries 🆔 → ONE press
    → confirmed."""
    task = "sp-raise"
    repo = _seed_singlepane(home, tmp_path, task)
    tdir = _transcript_dir(tmp_path, repo)
    title = _sp_title(task)
    env = _env(
        home,
        tmp_path,
        front_window="Untitled (Workspace)",  # ∉ snapshot → discriminator dispatches
        code_wins=[OWNER_WIN, title],
        input_value=f"🆔{task} the pasted prompt",
        new_jsonl=tdir / "new-sess.jsonl",
        new_jsonl_text=f'{{"text":"🆔{task} session started"}}',
        jsonl_on_press=1,
        raise_sets_front=title,  # the raise actually brings OUR window front
        enum_hit_only_after_open=True,  # pre-URI raise misses; submit-time raise hits
    )
    assert _run(env, tmp_path).returncode == 0
    sub = _ack(home, task, "submitted")
    assert sub.exists(), "raise + marker-gated retry must recover a front-mismatch"
    assert _presses(tmp_path) == 1, "no press ever lands on the wrong window"
    log = _log(home)
    assert "outcome=front-mismatch" in log
    assert "outcome=confirmed" in log
    assert "handoff-window-raise" in _read(tmp_path / "osa.log"), "the nonce-first raise fired"


# ─── 6. empty input → retry NEVER presses → ambiguous-after-first-enter ──────────────


def test_empty_input_withholds_retry_press_ambiguous(home, tmp_path):
    """After Enter #1 the input reads EMPTY but no marked jsonl ever appears (submitted with
    a hung transcript? swallowed with the paste lost?). The retry gate must NOT press (an
    empty input is the double-submit hazard) — polls run out → failed ack names
    ambiguous-after-first-enter + the manual instruction."""
    task = "sp-ambig"
    repo = _seed_singlepane(home, tmp_path, task)
    _transcript_dir(tmp_path, repo)
    env = _env(
        home,
        tmp_path,
        front_window=f"{_sp_title(task)} - x.py",
        input_value="",  # the input never shows our prompt
        retry_max=2,
    )
    assert _run(env, tmp_path).returncode == 0
    assert not _ack(home, task, "submitted").exists()
    failed = _ack(home, task, "failed")
    assert failed.exists()
    body = failed.read_text()
    assert "ambiguous-after-first-enter" in body
    assert "手动" in body or "核实" in body, "the ack must carry actionable manual guidance"
    assert _presses(tmp_path) == 1, "ONLY the title-gated first Enter — never a blind retry press"
    assert "outcome=input-not-ready" in _log(home)


# ─── 7. pre-press confirm → rc 3 honesty + baseline-before-URI proof ─────────────────


def test_confirm_without_press_is_not_script_verified(home, tmp_path):
    """A marked NEW jsonl exists already at the FIRST pre-press probe (the URI raced /
    owner pressed Enter by hand): the machinery must claim NOTHING — zero presses, submitted
    ack explicitly NOT script-verified. Doubles as the baseline-timing proof: the file is
    created BY the open stub (= after the baseline snapshot), so it only reads as NEW because
    the baseline predates the URI dispatch."""
    task = "sp-prepress"
    repo = _seed_singlepane(home, tmp_path, task)
    tdir = _transcript_dir(tmp_path, repo)
    env = _env(
        home,
        tmp_path,
        front_window=f"{_sp_title(task)} - x.py",
        input_value=f"🆔{task} the pasted prompt",
        new_jsonl=tdir / "manual-sess.jsonl",
        new_jsonl_text=f'{{"text":"🆔{task} manual start"}}',
        jsonl_on_open=True,
    )
    assert _run(env, tmp_path).returncode == 0
    sub = _ack(home, task, "submitted")
    assert sub.exists(), "the session IS running — submitted, honestly attributed"
    assert "NOT script-verified" in sub.read_text()
    assert _presses(tmp_path) == 0, "confirmed pre-press → the machinery never touches Enter"
    assert "pre-press probe" in _log(home)


# ─── 8. tripwire: the focus-contended give-up path never reaches the machinery ───────


def test_focus_giveup_never_invokes_submit_machinery(home, tmp_path):
    """focus-contended give-up happens BEFORE the URI dispatch — the submit machinery must
    see ZERO invocations (no SP-SUBMIT log, no sp gate osascript, no Enter): a visible-park
    window never receives an Enter."""
    task = "sp-giveup"
    _seed_singlepane(home, tmp_path, task)
    (home / PROJECT / "queue" / f"{task}.focus_contended").write_text(
        "count=4\nfirst_epoch=1770000000\n", encoding="utf-8"
    )
    env = _env(
        home,
        tmp_path,
        front_window=OWNER_WIN,  # the owner's old window holds front, ∈ snapshot
        code_wins=[OWNER_WIN],
        input_value=f"🆔{task} the pasted prompt",
    )
    assert _run(env, tmp_path).returncode == 0
    assert _read(tmp_path / "open.log") == "", "URI withheld on the give-up tick"
    failed = _ack(home, task, "failed")
    assert failed.exists() and "focus-contended" in failed.read_text()
    log = _log(home)
    assert "SP-SUBMIT" not in log, "submit machinery must never engage (tripwire)"
    assert "handoff-sp-retry-gate" not in _read(tmp_path / "osa.log")
    assert _presses(tmp_path) == 0, "a visible-park window never receives an Enter"
