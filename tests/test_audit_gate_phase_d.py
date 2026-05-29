"""Phase D pre-req — owner_ack_token verification (Component A) and the
codex_unavailable_bypass sidecar producer (Component B).

Trust model (design §1, owner ruling #1): anti-tamper + friction, NOT
cryptography. An AI running as the owner can fabricate a self-consistent
token; these tests verify the token defends against silent REUSE (finding_hash
binding), indefinite validity (7d expiry) and trace-less approval, not against
a malicious forger.

Source of truth: erp-system ``project-files/handoff/owner-ack-token-design.md``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from handoff_fanout import codex_audit, handoff_precheck

PROJECT = "demo"
TASK = "demo-task"
FHASH = "sha256:" + "a" * 64


@pytest.fixture
def handoff_home(tmp_path, monkeypatch):
    home = tmp_path / "handoff"
    home.mkdir()
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    for var in ("HANDOFF_RETRO_BYPASS", "HANDOFF_RETRO_MANDATE", "HANDOFF_AUDIT_MANDATE"):
        monkeypatch.delenv(var, raising=False)
    return home


# ─── Task 1: constants + compute_owner_ack_token + path helper ───────────────


def test_compute_owner_ack_token_is_canonical_sha256():
    approved = "2026-05-30T00:00:00+00:00"
    nonce = "nonce123"
    tok = codex_audit.compute_owner_ack_token(TASK, FHASH, nonce, approved)
    expected = "sha256:" + hashlib.sha256(
        f"{TASK}\n{FHASH}\n{nonce}\n{approved}".encode()
    ).hexdigest()
    assert tok == expected
    # deterministic
    assert tok == codex_audit.compute_owner_ack_token(TASK, FHASH, nonce, approved)
    # nonce changes the token
    assert tok != codex_audit.compute_owner_ack_token(TASK, FHASH, "nonce999", approved)


def test_owner_ack_path_uses_16hex_short():
    p = codex_audit.owner_ack_path(PROJECT, TASK, FHASH)
    assert p.name == f"{TASK}.owner_ack.{'a' * 16}.json"


def test_constants_match_design():
    assert codex_audit.OWNER_ACK_TTL_DAYS == 7
    assert codex_audit.BYPASS_FOLLOW_UP_DEADLINE_DAYS == 1
    assert codex_audit.MIN_CODEX_FAILURES == 3


# ─── Task 2: write_owner_ack / load_owner_ack / audit trail ──────────────────


def test_write_and_load_owner_ack_roundtrip(handoff_home):
    art = codex_audit.write_owner_ack(
        PROJECT,
        TASK,
        FHASH,
        "the bug title",
        "nonce123",
        "2026-05-30T00:00:00+00:00",
        "exempt: false positive, see analysis",
    )
    assert art["kind"] == "owner_ack"
    assert art["schema_version"] == "1.0"
    assert art["finding_hash"] == FHASH
    assert art["owner_ack_token"] == codex_audit.compute_owner_ack_token(
        TASK, FHASH, "nonce123", "2026-05-30T00:00:00+00:00"
    )
    # expiry = approved + 7d
    assert art["expires_at"] == "2026-06-06T00:00:00+00:00"
    loaded = codex_audit.load_owner_ack(PROJECT, TASK, FHASH)
    assert loaded == art
    # trail line written
    trail = handoff_home / PROJECT / "ack" / f"{TASK}.audit.retry_audit.jsonl"
    lines = [json.loads(x) for x in trail.read_text().splitlines() if x.strip()]
    assert any(
        e.get("event") == "owner-ack-written" and e["finding_hash"] == FHASH for e in lines
    )


def test_load_owner_ack_missing_returns_none(handoff_home):
    assert codex_audit.load_owner_ack(PROJECT, TASK, FHASH) is None


# ─── Task 3: G7 verifies the on-disk owner-ack artifact ──────────────────────

PROJECT_WS = "demo"


def _ws(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    for args in (
        ["git", "init", "--quiet", "--initial-branch=main"],
        ["git", "config", "user.email", "t@t.test"],
        ["git", "config", "user.name", "t"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(args, cwd=ws, check=True)
    (ws / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)
    monkeypatch.chdir(ws)
    return ws


def _head(ws):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, check=True
    ).stdout.strip()


def _gate_override(handoff_home, ws, *, disp_overrides=None, write_ack=None):
    """Build a full-audit block with one P0 finding owner_override'd; return outcome.

    write_ack: dict of kwargs (nonce/approved_at/finding_hash overrides) for
        write_owner_ack, or None to skip writing the artifact entirely.
    disp_overrides: dict merged into the disposition (to inject mismatches).
    """
    head = _head(ws)
    finding = {"id": "F1", "severity": "P0", "title": "bug F1"}
    rec = codex_audit.write_findings_artifact(
        PROJECT_WS,
        TASK,
        1,
        {"run_index": 1, "input_commit": head, "original_findings": [finding]},
        input_commit=head,
    )
    fhash = codex_audit.compute_finding_hash(finding)
    approved = datetime.now(UTC).isoformat(timespec="seconds")
    nonce = "nonce-xyz"
    if write_ack is not None:
        codex_audit.write_owner_ack(
            PROJECT_WS,
            TASK,
            write_ack.get("finding_hash", fhash),
            "bug F1",
            write_ack.get("nonce", nonce),
            write_ack.get("approved_at", approved),
            "exempt: false positive",
        )
    token = codex_audit.compute_owner_ack_token(TASK, fhash, nonce, approved)
    disp = {
        "finding_id": "F1",
        "finding_hash": fhash,
        "original_severity": "P0",
        "disposition": "owner_override",
        "owner_ack_token": token,
        "expires_at": codex_audit._add_days_iso(approved, 7),
    }
    if disp_overrides:
        disp.update(disp_overrides)
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    p0 = {k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS}
    p1 = {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS}
    payload = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT_WS, workspace=ws, phase0=p0, phase1=p1, codex_audit=block
    )
    return codex_audit.evaluate_audit_gate(payload, ws, PROJECT_WS, TASK)


def test_g7_override_with_valid_ack_passes(handoff_home, tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    out = _gate_override(handoff_home, ws, write_ack={})
    assert out.ok, (out.klass, out.subcode, out.detail)


def test_g7_override_no_ack_artifact_blocked(handoff_home, tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    out = _gate_override(handoff_home, ws, write_ack=None)  # token present but no file
    assert out.klass == "blocked"
    assert out.subcode == "codex-audit-override-no-ack-token"


def test_g7_override_token_mismatch_blocked(handoff_home, tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    # ack on disk is for a DIFFERENT nonce → recomputed token won't match disposition
    out = _gate_override(handoff_home, ws, write_ack={"nonce": "other-nonce"})
    assert out.klass == "blocked"
    assert out.subcode == "codex-audit-override-invalid"


def test_g7_override_finding_hash_binding_mismatch_blocked(handoff_home, tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    # disposition claims a different finding_hash than the union finding's →
    # the real finding has no disposition (unbound).
    other = "sha256:" + "b" * 64
    out = _gate_override(handoff_home, ws, write_ack={}, disp_overrides={"finding_hash": other})
    assert out.klass in ("retry", "blocked")
    assert not out.ok


def test_g7_override_expired_ack_blocked(handoff_home, tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    past = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    out = _gate_override(
        handoff_home,
        ws,
        write_ack={"approved_at": past},
        disp_overrides={"expires_at": codex_audit._add_days_iso(past, 7)},
    )
    assert out.klass == "blocked"
    assert out.subcode == "codex-audit-override-invalid"


# ─── Task 4: bypass sidecar producer ─────────────────────────────────────────


def _attempts(n):
    return [
        {
            "exit": 1,
            "stderr_hash": "sha256:" + "c" * 64,
            "timestamp": f"2026-05-30T0{i}:00:00+00:00",
        }
        for i in range(n)
    ]


def test_write_bypass_override_schema_and_deadline(handoff_home):
    created = "2026-05-30T00:00:00+00:00"
    art = codex_audit.write_bypass_override(
        PROJECT, TASK, "redo-audit-x", _attempts(3), "codex unavailable: timeout", created
    )
    assert art["schema_version"] == "1.0"
    assert art["kind"] == "codex_audit_bypass"
    assert art["task"] == TASK
    assert art["follow_up_audit_task_id"] == "redo-audit-x"
    assert art["follow_up_deadline"] == "2026-05-31T00:00:00+00:00"  # created + 1d
    assert len(art["codex_failure_attempts"]) == 3
    # on disk at the scanner-contract path
    p = codex_audit.bypass_override_path(PROJECT, TASK)
    assert p.name == f"{TASK}.audit.override.json"
    assert json.loads(p.read_text()) == art
    # audit trail records the write
    trail = handoff_home / PROJECT / "ack" / f"{TASK}.audit.retry_audit.jsonl"
    lines = [json.loads(x) for x in trail.read_text().splitlines() if x.strip()]
    assert any(e.get("event") == "bypass-override-written" for e in lines)


def test_write_bypass_override_too_few_failures_rejected(handoff_home):
    with pytest.raises(ValueError, match="MIN_CODEX_FAILURES|at least"):
        codex_audit.write_bypass_override(
            PROJECT, TASK, "redo-audit-x", _attempts(2), "codex down", "2026-05-30T00:00:00+00:00"
        )


def test_write_bypass_override_bad_follow_id_rejected(handoff_home):
    with pytest.raises(ValueError, match="follow_up_audit_task_id|slug"):
        codex_audit.write_bypass_override(
            PROJECT, TASK, "Bad Id!", _attempts(3), "codex down", "2026-05-30T00:00:00+00:00"
        )


def test_write_bypass_override_newline_follow_id_rejected(handoff_home):
    # the producer must reject a trailing-newline slug (mirror fullmatch contract)
    with pytest.raises(ValueError, match="follow_up_audit_task_id|slug"):
        codex_audit.write_bypass_override(
            PROJECT, TASK, "redo-x\n", _attempts(3), "codex down", "2026-05-30T00:00:00+00:00"
        )


# ─── Task 5: audit-close auto-writes the bypass sidecar (end-to-end) ──────────


def test_audit_close_bypass_writes_sidecar(handoff_home, tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    bypass = {
        "codex_failure_attempts": _attempts(3),
        "follow_up_audit_task_id": "redo-audit-next",
    }
    bypass_file = tmp_path / "bypass.json"
    bypass_file.write_text(json.dumps(bypass))
    argv = [
        "--task",
        TASK,
        "--project",
        PROJECT_WS,
        "--workspace",
        str(ws),
        "--next",
        "next brief",
        "--audit-mode",
        "codex_unavailable_bypass",
        "--bypass-file",
        str(bypass_file),
        "--status",
        "active",
    ]
    for k in handoff_precheck.PHASE0_KEYS:
        argv += ["--phase0-status", f"{k}=✅"]
    for k in handoff_precheck.PHASE1_KEYS:
        argv += ["--phase1-status", f"{k}=✅"]
    rc = codex_audit.main_audit_close(argv)
    assert rc == 0, rc
    sidecar = codex_audit.bypass_override_path(PROJECT_WS, TASK)
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["follow_up_audit_task_id"] == "redo-audit-next"
    assert data["kind"] == "codex_audit_bypass"
    # the follow-up deadline must be parseable ISO-8601 (scanner contract)
    datetime.fromisoformat(data["follow_up_deadline"].replace("Z", "+00:00"))


def test_audit_close_full_mode_writes_no_sidecar(handoff_home, tmp_path, monkeypatch):
    # a non-bypass close must NOT emit a bypass sidecar (no false debt).
    ws = _ws(tmp_path, monkeypatch)
    head = _head(ws)
    rec = codex_audit.write_findings_artifact(
        PROJECT_WS,
        TASK,
        1,
        {"run_index": 1, "input_commit": head, "original_findings": []},
        input_commit=head,
    )
    argv = [
        "--task",
        TASK,
        "--project",
        PROJECT_WS,
        "--workspace",
        str(ws),
        "--next",
        "next brief",
        "--audit-mode",
        "full_codex_audit",
        "--run-record",
        json.dumps(rec),
        "--status",
        "active",
    ]
    for k in handoff_precheck.PHASE0_KEYS:
        argv += ["--phase0-status", f"{k}=✅"]
    for k in handoff_precheck.PHASE1_KEYS:
        argv += ["--phase1-status", f"{k}=✅"]
    rc = codex_audit.main_audit_close(argv)
    assert rc == 0, rc
    assert not codex_audit.bypass_override_path(PROJECT_WS, TASK).exists()


# ─── Task 6: docs presence / honest-disclaimer regression guard ──────────────


def test_templates_document_owner_override_and_bypass():
    from handoff_fanout import templates

    src = Path(templates.__file__).read_text(encoding="utf-8")
    assert "owner_ack" in src
    assert "audit.override.json" in src
    # honest trust-model disclaimer must survive (no "crypto secure" over-claim)
    assert "非加密" in src or "not cryptograph" in src.lower()
