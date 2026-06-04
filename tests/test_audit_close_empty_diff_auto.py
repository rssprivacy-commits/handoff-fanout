"""#48 fix (A1' conservative-safe / owner ruling 2026-06-04) — audit-close
auto-produces an empty_diff_attestation for a no-code (pure-dispatcher /
post-done-dump) session so its `active` dump clears the audit mandate WITHOUT a
hand-crafted attestation file, while never silently defaulting the base.

Safety hinge (codex R, accepted over gemini in CC arbitration): `empty_diff`'s
safety is the base being the session's TRUE start. So the auto path REQUIRES an
explicit `--audit-base` (never a silent upstream default) and refuses when
`base..HEAD` is non-empty — a session that committed/pushed code can't ride the
no-code path. The gate (`_gate_empty_diff`) still independently re-verifies.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from handoff_fanout import codex_audit, handoff_precheck

PROJECT = "demo"
TASK = "demo-task"
EMPTY_HASH = "sha256:" + hashlib.sha256(b"").hexdigest()


@pytest.fixture
def handoff_home(tmp_path, monkeypatch):
    home = tmp_path / "handoff"
    home.mkdir()
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    for var in (
        "HANDOFF_RETRO_BYPASS",
        "HANDOFF_RETRO_MANDATE",
        "HANDOFF_AUDIT_MANDATE",
        "HANDOFF_SAFE_COMMIT_LOCK",
        "HANDOFF_SAFE_COMMIT_BYPASS",
    ):
        monkeypatch.delenv(var, raising=False)
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


def _head(ws: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(ws: Path, fname: str, content: str, msg: str) -> str:
    (ws / fname).write_text(content)
    subprocess.run(["git", "add", fname], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ws, check=True)
    return _head(ws)


def _close_argv(ws: Path, *, audit_base: str | None = None, attestation_file: str | None = None):
    argv = [
        "--task",
        TASK,
        "--project",
        PROJECT,
        "--workspace",
        str(ws),
        "--next",
        "spawn next task",
        "--audit-mode",
        "empty_diff_attestation",
        "--status",
        "active",
    ]
    if audit_base is not None:
        argv += ["--audit-base", audit_base]
    if attestation_file is not None:
        argv += ["--attestation-file", attestation_file]
    for k in handoff_precheck.PHASE0_KEYS:
        argv += ["--phase0-status", f"{k}=✅"]
    for k in handoff_precheck.PHASE1_KEYS:
        argv += ["--phase1-status", f"{k}=✅"]
    return argv


# ─── auto_empty_diff_attestation helper ──────────────────────────────────────


def test_auto_attestation_clean_returns_empty_hash(handoff_home, workspace):
    head = _head(workspace)
    att = codex_audit.auto_empty_diff_attestation(workspace, head)
    assert att is not None
    assert att["base"] == head and att["head"] == head
    assert att["diff_hash"] == EMPTY_HASH
    assert att["mode_decider_version"]  # non-empty provenance stamp


def test_auto_attestation_nonempty_diff_returns_none(handoff_home, workspace):
    base = _head(workspace)
    _commit(workspace, "feature.py", "x = 1\n", "real code")  # HEAD now ahead of base
    assert codex_audit.auto_empty_diff_attestation(workspace, base) is None


def test_auto_attestation_unresolvable_base_returns_none(handoff_home, workspace):
    assert codex_audit.auto_empty_diff_attestation(workspace, "deadbeef") is None


# ─── audit-close end-to-end (mandate ON) ─────────────────────────────────────


def test_audit_close_auto_empty_diff_passes_under_mandate(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    head = _head(workspace)
    rc = codex_audit.main_audit_close(_close_argv(workspace, audit_base=head))
    assert rc == 0, rc
    ev_path = handoff_home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    ev = json.loads(ev_path.read_text())
    block = ev["codex_audit"]
    assert block["audit_mode"] == "empty_diff_attestation"
    assert block["empty_diff_attestation"]["base"] == head
    assert block["empty_diff_attestation"]["head"] == head
    assert block["empty_diff_attestation"]["diff_hash"] == EMPTY_HASH
    # provenance recorded (R: forensics)
    assert block["empty_diff_base_source"] == "explicit_audit_base"
    assert ev["evidence_hash"] == handoff_precheck.compute_evidence_hash(ev)


def test_audit_close_auto_empty_diff_requires_base(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    # No --attestation-file AND no --audit-base → must fail (no silent upstream default).
    rc = codex_audit.main_audit_close(_close_argv(workspace, audit_base=None))
    assert rc == 1


def test_audit_close_auto_empty_diff_rejects_real_changes(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    base = _head(workspace)
    _commit(workspace, "feature.py", "x = 1\n", "real code change")  # HEAD ahead of base
    # Claiming empty_diff against the OLD base while real code landed → refuse.
    rc = codex_audit.main_audit_close(_close_argv(workspace, audit_base=base))
    assert rc == 1


def test_audit_close_explicit_attestation_file_still_works(
    handoff_home, workspace, monkeypatch, tmp_path
):
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    head = _head(workspace)
    att = {
        "base": head,
        "head": head,
        "diff_hash": EMPTY_HASH,
        "mode_decider_version": "hand-1",
    }
    att_file = tmp_path / "att.json"
    att_file.write_text(json.dumps(att))
    rc = codex_audit.main_audit_close(_close_argv(workspace, attestation_file=str(att_file)))
    assert rc == 0, rc
    ev_path = handoff_home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    block = json.loads(ev_path.read_text())["codex_audit"]
    assert block["empty_diff_attestation"]["mode_decider_version"] == "hand-1"
    # no auto base_source provenance when the caller supplied the attestation
    assert "empty_diff_base_source" not in block
