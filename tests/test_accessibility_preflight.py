"""Accessibility preflight — auto-submit must not silently degrade.

Observed in erp-system on 2026-05-28: two auto-submit attempts hit
`osascript is not allowed to send keystrokes (1002)` (Accessibility permission
missing). The old code logged a buried per-task WARN and the spawned tab just
sat there with the prompt pasted but never sent — the operator had no signal.

auto-continue.sh now runs a non-destructive `UI elements enabled` preflight
before pressing Enter. When untrusted it skips the doomed keystroke, writes a
clear `accessibility-missing` ack, and raises one rate-limited notification.
These tests drive the real script (spawn loop enabled) with a smart osascript
stub.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "install" / "auto-continue.sh"

PROJECT = "demo"
TASK = "demo-task"


def _osascript_stub(path: Path, sink: Path, *, ui_enabled: str, keystroke_exit: int = 0) -> None:
    """Smart osascript stub: answers the probe, frontmost, and records calls."""
    path.write_text(
        "#!/bin/bash\n"
        'args="$*"\n'
        f'printf "%s\\n" "$args" >> "{sink}"\n'
        'case "$args" in\n'
        f'  *"UI elements enabled"*) echo "{ui_enabled}" ;;\n'
        '  *"frontmost is true"*) echo "Code" ;;\n'
        f'  *"keystroke return"*) exit {keystroke_exit} ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _recording_stub(path: Path, sink: Path) -> None:
    path.write_text(
        f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{sink}"\nexit 0\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _setup(home: Path, tmp_path: Path, *, ui_enabled: str, keystroke_exit: int = 0) -> dict:
    queue = home / PROJECT / "queue"
    queue.mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    (queue / f"{TASK}.uri").write_text(f"WORKSPACE={ws}\nURI=cursor://demo/{TASK}\n")

    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    open_sink = tmp_path / "open.log"
    osa_sink = tmp_path / "osa.log"
    open_stub = stub_dir / "open"
    code_stub = stub_dir / "code"
    osa_stub = stub_dir / "osascript"
    _recording_stub(open_stub, open_sink)
    _recording_stub(code_stub, tmp_path / "code.log")
    _osascript_stub(osa_stub, osa_sink, ui_enabled=ui_enabled, keystroke_exit=keystroke_exit)

    env = dict(os.environ)
    env.update(
        {
            "HANDOFF_ROOT": str(home),
            "HANDOFF_OPEN_CMD": str(open_stub),
            "HANDOFF_CODE_BIN": str(code_stub),
            "HANDOFF_OSASCRIPT_CMD": str(osa_stub),
            "HANDOFF_SKIP_SPAWN": "0",  # exercise the spawn loop
            "HANDOFF_VSCODE_CHECK": "0",
            "HANDOFF_AUTOCLOSE_ENABLED": "0",
        },
    )
    env.pop("HANDOFF_HOME", None)
    return {"env": env, "queue": queue, "osa_sink": osa_sink, "home": home}


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


def test_missing_accessibility_skips_keystroke_and_notifies(tmp_path):
    home = tmp_path / "handoff"
    home.mkdir()
    ctx = _setup(home, tmp_path, ui_enabled="false")

    _run(ctx["env"])

    osa = ctx["osa_sink"].read_text()
    # the doomed keystroke must NOT be attempted
    assert "keystroke return" not in osa
    # an actionable notification fired instead
    assert "display notification" in osa
    assert "辅助功能" in osa
    # rate-limit marker created
    assert (home / ".accessibility-warned").exists()
    # ack records the accessibility-missing reason, not a generic failure
    failed = ctx["queue"].parent / "ack" / f"{TASK}.failed"
    assert failed.exists()
    assert "accessibility-missing" in failed.read_text()


def test_trusted_accessibility_presses_enter(tmp_path):
    home = tmp_path / "handoff"
    home.mkdir()
    ctx = _setup(home, tmp_path, ui_enabled="true", keystroke_exit=0)

    _run(ctx["env"])

    osa = ctx["osa_sink"].read_text()
    assert "keystroke return" in osa  # Enter pressed
    assert "display notification" not in osa  # no accessibility warning
    assert not (home / ".accessibility-warned").exists()
    submitted = ctx["queue"].parent / "ack" / f"{TASK}.submitted"
    assert submitted.exists()


def test_keystroke_failure_after_trusted_preflight_warns(tmp_path):
    """Preflight trusted but keystroke still errors → accessibility-class warn."""
    home = tmp_path / "handoff"
    home.mkdir()
    ctx = _setup(home, tmp_path, ui_enabled="true", keystroke_exit=1)

    _run(ctx["env"])

    osa = ctx["osa_sink"].read_text()
    assert "keystroke return" in osa  # it was attempted
    assert "display notification" in osa  # and the failure escalated to a notification
    assert (home / ".accessibility-warned").exists()
    failed = ctx["queue"].parent / "ack" / f"{TASK}.failed"
    assert failed.exists()
    assert "post-preflight" in failed.read_text()


def test_notification_rate_limited_within_6h(tmp_path):
    """A fresh marker <6h old suppresses a second notification across runs."""
    home = tmp_path / "handoff"
    home.mkdir()
    # pre-create a fresh marker (mtime = now)
    (home / ".accessibility-warned").write_text("")
    ctx = _setup(home, tmp_path, ui_enabled="false")

    _run(ctx["env"])

    osa = ctx["osa_sink"].read_text()
    # preflight still skips keystroke, but the notification is rate-limited away
    assert "keystroke return" not in osa
    assert "display notification" not in osa
