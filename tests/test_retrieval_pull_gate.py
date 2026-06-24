"""B1 — retrieval-pull ENFORCE gate (learning-loop component 6 / L1).

The gate runs in ``dump.main`` right after the retro gate. A COORDINATOR ACTIVE handoff
whose retro evidence carries NO ``predecessor_lesson_backref`` AND NO
``no_novel_lesson_attested`` disposition is REFUSED (``EXIT_RETRY``) BEFORE any artifact is
written — the closing coordinator must read its predecessor's lesson + record a backref, or
honestly attest there was nothing novel.

DEFAULT-OFF (empty ``retrieval_pull_enforce_projects``) → no-op / byte-identical. Owner flips
a project (or ``"*"``) in. Emergency one-key rollback = a kill-switch sentinel file
(``$HANDOFF_HOME/<project>/.retrieval-pull-enforce-off`` or the fleet-wide
``$HANDOFF_HOME/.retrieval-pull-enforce-off``). Fail-SAFE-OFF on any read error.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys

import pytest

from handoff_fanout import config as _config
from handoff_fanout import dump, handoff_precheck, retro_gate

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


def _make_evidence(home, workspace, *, backref=None, disposition=None):
    """A VALID retro evidence (correct self-hash) carrying the requested backref /
    lesson_disposition — built via the real producer so the retro gate accepts it."""
    payload = handoff_precheck.build_evidence(
        task_id=TASK,
        project=PROJECT,
        workspace=workspace,
        phase0={k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS},
        phase1={k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
        predecessor_lesson_backref=backref,
        lesson_disposition=disposition,
    )
    out = home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, out)
    return out


def _write_config(home, *, enforce_projects):
    (home / "config.json").write_text(
        json.dumps({"retrieval_pull_enforce_projects": enforce_projects}), encoding="utf-8"
    )


def _run_dump(workspace, ev, *, coordinator=False):
    argv = [
        "--task", TASK, "--next", "next brief", "--project", PROJECT,
        "--workspace", str(workspace), "--status", "active",
        "--retro-evidence", str(ev),
    ]
    if coordinator:
        argv.append("--coordinator")
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        return dump.main(argv)
    finally:
        sys.stderr = old


def _uri(home):
    return home / PROJECT / "queue" / f"{TASK}.uri"


# ─── unit: _retrieval_pull_enforce_enabled ────────────────────────────────────


def test_enforce_disabled_by_default(handoff_home):
    cfg = _config.load(home=handoff_home)  # no config.json → empty list
    assert dump._retrieval_pull_enforce_enabled(cfg, PROJECT) is False


def test_enforce_enabled_when_project_listed(handoff_home):
    _write_config(handoff_home, enforce_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    assert dump._retrieval_pull_enforce_enabled(cfg, PROJECT) is True


def test_enforce_enabled_by_wildcard(handoff_home):
    _write_config(handoff_home, enforce_projects=["*"])
    cfg = _config.load(home=handoff_home)
    assert dump._retrieval_pull_enforce_enabled(cfg, "any-project") is True


def test_enforce_other_project_not_affected(handoff_home):
    _write_config(handoff_home, enforce_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    assert dump._retrieval_pull_enforce_enabled(cfg, "other") is False


def test_kill_switch_per_project_disables(handoff_home):
    _write_config(handoff_home, enforce_projects=[PROJECT])
    (handoff_home / PROJECT).mkdir(parents=True, exist_ok=True)
    (handoff_home / PROJECT / ".retrieval-pull-enforce-off").write_text("rollback\n")
    cfg = _config.load(home=handoff_home)
    assert dump._retrieval_pull_enforce_enabled(cfg, PROJECT) is False


def test_kill_switch_fleet_wide_disables(handoff_home):
    _write_config(handoff_home, enforce_projects=["*"])
    (handoff_home / ".retrieval-pull-enforce-off").write_text("fleet rollback\n")
    cfg = _config.load(home=handoff_home)
    assert dump._retrieval_pull_enforce_enabled(cfg, PROJECT) is False


# ─── unit: _run_retrieval_pull_gate edge cases (status / non-coordinator / fail-safe) ──


def _gate_args(ev_path, *, status="active", coordinator=True):
    return argparse.Namespace(
        status=status, coordinator=coordinator, retro_evidence=str(ev_path) if ev_path else None
    )


def test_gate_noop_when_not_active(handoff_home):
    _write_config(handoff_home, enforce_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    ev = handoff_home / "ev.json"
    ev.write_text(json.dumps({}), encoding="utf-8")  # no backref
    # status=done → no successor to pull a lesson → never gated
    assert dump._run_retrieval_pull_gate(_gate_args(ev, status="done"), PROJECT, cfg) is None


def test_gate_noop_for_non_coordinator(handoff_home):
    _write_config(handoff_home, enforce_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    ev = handoff_home / "ev.json"
    ev.write_text(json.dumps({}), encoding="utf-8")
    assert dump._run_retrieval_pull_gate(_gate_args(ev, coordinator=False), PROJECT, cfg) is None


def test_gate_fail_safe_on_unreadable_evidence(handoff_home):
    _write_config(handoff_home, enforce_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    missing = handoff_home / "does-not-exist.json"
    # enforce ON + coordinator + active, but the evidence can't be read → fail-SAFE-OFF (None)
    assert dump._run_retrieval_pull_gate(_gate_args(missing), PROJECT, cfg) is None


def test_gate_blocks_when_enabled_no_backref(handoff_home):
    _write_config(handoff_home, enforce_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    ev = handoff_home / "ev.json"
    ev.write_text(json.dumps({"schema_version": "5.5.0"}), encoding="utf-8")  # no backref/disp
    assert dump._run_retrieval_pull_gate(_gate_args(ev), PROJECT, cfg) == retro_gate.EXIT_RETRY


def test_gate_passes_with_backref(handoff_home):
    _write_config(handoff_home, enforce_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    ev = handoff_home / "ev.json"
    ev.write_text(
        json.dumps({"predecessor_lesson_backref": [{"predecessor_lesson": "l1", "disposition": "applied"}]}),
        encoding="utf-8",
    )
    assert dump._run_retrieval_pull_gate(_gate_args(ev), PROJECT, cfg) is None


def test_gate_passes_with_no_novel_attestation(handoff_home):
    _write_config(handoff_home, enforce_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    ev = handoff_home / "ev.json"
    ev.write_text(
        json.dumps({"lesson_disposition": {"disposition": "no_novel_lesson_attested", "reason": "routine"}}),
        encoding="utf-8",
    )
    assert dump._run_retrieval_pull_gate(_gate_args(ev), PROJECT, cfg) is None


# ─── integration: through dump.main (artifacts + exit code) ───────────────────


def test_dump_off_by_default_no_backref_succeeds(handoff_home, workspace):
    """DEFAULT-OFF: a coordinator dump with no backref proceeds (byte-identical) — the gate
    is a no-op and the .uri is published."""
    ev = _make_evidence(handoff_home, workspace, backref=None)
    rc = _run_dump(workspace, ev, coordinator=True)
    assert rc == 0
    assert _uri(handoff_home).exists()


def test_dump_enforce_coordinator_no_backref_blocked(handoff_home, workspace):
    """ENFORCE ON + coordinator + no backref + no attestation → REFUSED (EXIT_RETRY), and
    NO .uri is published (fail-closed, no half-product)."""
    _write_config(handoff_home, enforce_projects=[PROJECT])
    ev = _make_evidence(handoff_home, workspace, backref=None)
    rc = _run_dump(workspace, ev, coordinator=True)
    assert rc == retro_gate.EXIT_RETRY
    assert not _uri(handoff_home).exists()


def test_dump_enforce_coordinator_with_backref_succeeds(handoff_home, workspace):
    """ENFORCE ON + coordinator + backref present → proceeds (.uri published)."""
    _write_config(handoff_home, enforce_projects=[PROJECT])
    ev = _make_evidence(
        handoff_home, workspace,
        backref=[{"predecessor_lesson": "lesson-p64", "disposition": "applied"}],
    )
    rc = _run_dump(workspace, ev, coordinator=True)
    assert rc == 0
    assert _uri(handoff_home).exists()


def test_dump_enforce_coordinator_attestation_succeeds(handoff_home, workspace):
    """ENFORCE ON + coordinator + no_novel_lesson_attested → escape valve passes."""
    _write_config(handoff_home, enforce_projects=[PROJECT])
    ev = _make_evidence(
        handoff_home, workspace,
        disposition={"disposition": "no_novel_lesson_attested", "reason": "routine hop"},
    )
    rc = _run_dump(workspace, ev, coordinator=True)
    assert rc == 0
    assert _uri(handoff_home).exists()


def test_dump_enforce_non_coordinator_not_blocked(handoff_home, workspace):
    """ENFORCE ON but a NON-coordinator (worker) dump with no backref proceeds — only
    coordinator handoffs are gated."""
    _write_config(handoff_home, enforce_projects=[PROJECT])
    ev = _make_evidence(handoff_home, workspace, backref=None)
    rc = _run_dump(workspace, ev, coordinator=False)
    assert rc == 0
    assert _uri(handoff_home).exists()


def test_dump_enforce_wildcard_blocks(handoff_home, workspace):
    """ENFORCE ON via ``"*"`` wildcard → a coordinator no-backref dump is blocked."""
    _write_config(handoff_home, enforce_projects=["*"])
    ev = _make_evidence(handoff_home, workspace, backref=None)
    rc = _run_dump(workspace, ev, coordinator=True)
    assert rc == retro_gate.EXIT_RETRY
    assert not _uri(handoff_home).exists()


def test_dump_enforce_kill_switch_rolls_back(handoff_home, workspace):
    """One-key rollback: with the per-project kill-switch sentinel present, the enforced
    gate is disabled and the no-backref coordinator dump proceeds again."""
    _write_config(handoff_home, enforce_projects=[PROJECT])
    (handoff_home / PROJECT).mkdir(parents=True, exist_ok=True)
    (handoff_home / PROJECT / ".retrieval-pull-enforce-off").write_text("rollback\n")
    ev = _make_evidence(handoff_home, workspace, backref=None)
    rc = _run_dump(workspace, ev, coordinator=True)
    assert rc == 0
    assert _uri(handoff_home).exists()
