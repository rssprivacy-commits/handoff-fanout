"""retrieval-pull warn-mode validator (component 6 / L1).

The warn validator runs AFTER an active dump's retro gate has passed. It inspects
the closing session's evidence for ``predecessor_lesson_backref`` and appends a
structured JSON line to ``<HANDOFF_HOME>/<project>/retrieval-pull-shadow.log``.
It NEVER blocks, NEVER raises, NEVER changes the dump exit code — it only logs the
would-block condition a FUTURE (owner-gated) enforce-flip would act on.

``would_block`` = ``is_coordinator and not has_backref``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from handoff_fanout import dump, handoff_precheck

PROJECT = "demo"
TASK = "demo-task"


@pytest.fixture
def handoff_home(tmp_path, monkeypatch):
    home = tmp_path / "handoff"
    home.mkdir()
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.delenv("HANDOFF_RETRO_BYPASS", raising=False)
    monkeypatch.delenv("HANDOFF_RETRO_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_SAFE_COMMIT_LOCK", raising=False)
    monkeypatch.delenv("HANDOFF_SAFE_COMMIT_BYPASS", raising=False)
    return home


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=ws, check=True)
    (ws / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)
    monkeypatch.chdir(ws)
    return ws


def _make_evidence(home: Path, workspace: Path, *, backref=None) -> Path:
    payload = handoff_precheck.build_evidence(
        task_id=TASK,
        project=PROJECT,
        workspace=workspace,
        phase0={k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS},
        phase1={k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
        predecessor_lesson_backref=backref,
    )
    out = home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, out)
    return out


def _shadow_log(home: Path) -> Path:
    return home / PROJECT / "retrieval-pull-shadow.log"


def _run_dump(workspace: Path, ev: Path, *, coordinator: bool = False) -> int:
    argv = [
        "--task",
        TASK,
        "--next",
        "next brief",
        "--project",
        PROJECT,
        "--workspace",
        str(workspace),
        "--status",
        "active",
        "--retro-evidence",
        str(ev),
    ]
    if coordinator:
        argv.append("--coordinator")
    # Stderr swallow (os.write to fd 2 not captured by capsys).
    import io

    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        return dump.main(argv)
    finally:
        sys.stderr = old


# ─── unit: the validator directly ─────────────────────────────────────────────


def test_warn_logs_coordinator_no_backref_would_block(handoff_home):
    """coordinator + no backref → would_block True, logged (never blocks)."""
    ev = handoff_home / "ev.json"
    ev.write_text(json.dumps({"schema_version": "5.5.0"}), encoding="utf-8")
    dump._retrieval_pull_warn(
        home=handoff_home,
        project=PROJECT,
        task=TASK,
        evidence_path=ev,
        is_coordinator=True,
    )
    line = json.loads(_shadow_log(handoff_home).read_text().splitlines()[-1])
    assert line["is_coordinator"] is True
    assert line["has_backref"] is False
    assert line["backref_count"] == 0
    assert line["would_block"] is True
    assert line["task"] == TASK
    assert line["project"] == PROJECT
    assert "ts" in line


def test_warn_coordinator_with_backref_no_would_block(handoff_home):
    """coordinator + backref present → would_block False."""
    ev = handoff_home / "ev.json"
    ev.write_text(
        json.dumps(
            {
                "schema_version": "5.5.0",
                "predecessor_lesson_backref": [
                    {"predecessor_lesson": "l1", "disposition": "applied"},
                    {"predecessor_lesson": "l2", "disposition": "applied"},
                ],
            }
        ),
        encoding="utf-8",
    )
    dump._retrieval_pull_warn(
        home=handoff_home,
        project=PROJECT,
        task=TASK,
        evidence_path=ev,
        is_coordinator=True,
    )
    line = json.loads(_shadow_log(handoff_home).read_text().splitlines()[-1])
    assert line["has_backref"] is True
    assert line["backref_count"] == 2
    assert line["would_block"] is False


def test_warn_non_coordinator_never_would_block(handoff_home):
    """A non-coordinator close never would-blocks even without a backref."""
    ev = handoff_home / "ev.json"
    ev.write_text(json.dumps({"schema_version": "5.5.0"}), encoding="utf-8")
    dump._retrieval_pull_warn(
        home=handoff_home,
        project=PROJECT,
        task=TASK,
        evidence_path=ev,
        is_coordinator=False,
    )
    line = json.loads(_shadow_log(handoff_home).read_text().splitlines()[-1])
    assert line["is_coordinator"] is False
    assert line["would_block"] is False


def test_warn_appends_not_overwrites(handoff_home):
    """Repeated calls append (the shadow log is a running ledger)."""
    ev = handoff_home / "ev.json"
    ev.write_text(json.dumps({"schema_version": "5.5.0"}), encoding="utf-8")
    for _ in range(3):
        dump._retrieval_pull_warn(
            home=handoff_home,
            project=PROJECT,
            task=TASK,
            evidence_path=ev,
            is_coordinator=True,
        )
    assert len(_shadow_log(handoff_home).read_text().splitlines()) == 3


def test_warn_fail_soft_on_unwritable(handoff_home, monkeypatch):
    """fail-soft: any error writing the shadow log is swallowed (never raises)."""
    ev = handoff_home / "ev.json"
    ev.write_text(json.dumps({"schema_version": "5.5.0"}), encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk full")

    # Make every filesystem write path raise — the validator must swallow it.
    monkeypatch.setattr(Path, "mkdir", boom)
    monkeypatch.setattr(Path, "open", boom)
    # Must NOT raise.
    dump._retrieval_pull_warn(
        home=handoff_home,
        project=PROJECT,
        task=TASK,
        evidence_path=ev,
        is_coordinator=True,
    )


def test_warn_fail_soft_on_unreadable_evidence(handoff_home):
    """A vanished / unreadable evidence file degrades to has_backref=False, no raise."""
    missing = handoff_home / "does-not-exist.json"
    dump._retrieval_pull_warn(
        home=handoff_home,
        project=PROJECT,
        task=TASK,
        evidence_path=missing,
        is_coordinator=True,
    )
    line = json.loads(_shadow_log(handoff_home).read_text().splitlines()[-1])
    assert line["has_backref"] is False
    assert line["would_block"] is True


# ─── integration: through dump.main ───────────────────────────────────────────


def test_dump_active_logs_shadow_no_backref(handoff_home, workspace):
    """An active (non-coordinator) dump with no backref still logs a shadow line,
    exit code unchanged (0)."""
    ev = _make_evidence(handoff_home, workspace, backref=None)
    rc = _run_dump(workspace, ev, coordinator=False)
    assert rc == 0
    line = json.loads(_shadow_log(handoff_home).read_text().splitlines()[-1])
    assert line["has_backref"] is False
    assert line["is_coordinator"] is False
    assert line["would_block"] is False


def test_dump_coordinator_no_backref_logs_would_block(handoff_home, workspace):
    """A coordinator active dump with NO backref logs would_block=True but still
    returns 0 — warn-mode never blocks."""
    ev = _make_evidence(handoff_home, workspace, backref=None)
    rc = _run_dump(workspace, ev, coordinator=True)
    assert rc == 0
    line = json.loads(_shadow_log(handoff_home).read_text().splitlines()[-1])
    assert line["is_coordinator"] is True
    assert line["would_block"] is True


def test_dump_coordinator_with_backref_no_would_block(handoff_home, workspace):
    """A coordinator active dump WITH backref logs would_block=False."""
    ev = _make_evidence(
        handoff_home,
        workspace,
        backref=[{"predecessor_lesson": "lesson-p61", "disposition": "applied"}],
    )
    rc = _run_dump(workspace, ev, coordinator=True)
    assert rc == 0
    line = json.loads(_shadow_log(handoff_home).read_text().splitlines()[-1])
    assert line["has_backref"] is True
    assert line["backref_count"] == 1
    assert line["would_block"] is False
