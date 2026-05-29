"""Phase B — codex audit gate enforcement (G0-G9) over retro evidence.

Phase A shipped the *evidence capability* (record a codex_audit block, mandate
OFF). Phase B wires :func:`codex_audit.evaluate_audit_gate` into the dump-side
retro gate behind ``HANDOFF_AUDIT_MANDATE`` and adds an ISOLATED audit attempt
counter. The mandate is still off by default (flag path); these tests exercise
the gate by either calling ``evaluate_audit_gate`` directly or by setting the
flag for the end-to-end dump path.

The matrix follows spec §2.6 — the five documented escape paths the gate must
close: empty-diff-跳审, 旧 HEAD pass, run1-P1/run2-pass disposition drop,
fabricated owner override (no ack token), fabricated independent refute (same
session), bypass without failure proof — plus each G-check's own subcode.

Spec: ``project-files/handoff/codex-audit-gate-spec-draft.md`` v0.2 §1/§2/§5.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from handoff_fanout import codex_audit, dump, handoff_precheck, retro_gate

# ─── fixtures ───────────────────────────────────────────────────────────────


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


PROJECT = "demo"
TASK = "demo-task"


def _head(ws: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(ws: Path, fname: str, content: str, msg: str) -> str:
    (ws / fname).write_text(content)
    subprocess.run(["git", "add", fname], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ws, check=True)
    return _head(ws)


def _full_phase_status():
    p0 = {k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS}
    p1 = {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS}
    return p0, p1


def _finding(severity: str, fid: str, title: str | None = None) -> dict:
    return {"id": fid, "severity": severity, "title": title or f"bug {fid}"}


def _write_run(ws: Path, ri: int, head: str, findings_list: list[dict]) -> dict:
    findings = {"run_index": ri, "input_commit": head, "original_findings": findings_list}
    return codex_audit.write_findings_artifact(PROJECT, TASK, ri, findings, input_commit=head)


def _evidence(ws: Path, block: dict | None) -> dict:
    p0, p1 = _full_phase_status()
    return handoff_precheck.build_evidence(
        task_id=TASK,
        project=PROJECT,
        workspace=ws,
        phase0=p0,
        phase1=p1,
        codex_audit=block,
    )


def _gate(ws: Path, block: dict | None, **payload_overrides) -> codex_audit.AuditGateOutcome:
    payload = _evidence(ws, block)
    payload.update(payload_overrides)
    return codex_audit.evaluate_audit_gate(payload, ws, PROJECT, TASK)


def _disp(disp: str, fhash: str, severity: str = "P1", **extra) -> dict:
    base = {
        "finding_id": "F",
        "finding_hash": fhash,
        "original_severity": severity,
        "disposition": disp,
    }
    base.update(extra)
    return base


def _reviewer_artifact(home: Path, art_rel: str, fhash: str, reviewer_sid: str = "sess-B") -> None:
    """Write a spec §1.7-complete reviewer artifact bound to ``fhash``."""
    art_abs = home / PROJECT / art_rel
    art_abs.parent.mkdir(parents=True, exist_ok=True)
    art_abs.write_text(
        json.dumps(
            {
                "independent_run_id": "ir-1",
                "reviewer_session_id": reviewer_sid,
                "original_finding_hash": fhash,
                "verdict": "refuted",
                "artifact_hash": "sha256:" + "a" * 64,
            }
        )
    )


# ─── full-mode happy path ───────────────────────────────────────────────────


def test_full_clean_passes(handoff_home, workspace):
    head = _head(workspace)
    rec = _write_run(workspace, 1, head, [])
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    assert _gate(workspace, block).ok


def test_no_codex_block_is_required(handoff_home, workspace):
    out = _gate(workspace, None)
    assert out.klass == "retry" and out.subcode == "codex-audit-required"


def test_unknown_mode_is_required(handoff_home, workspace):
    out = _gate(workspace, {"audit_mode": "nonsense", "audit_runs": []})
    assert out.klass == "retry" and out.subcode == "codex-audit-required"


# ─── G0: HEAD binding (旧 HEAD pass / sibling-commit-after-audit) ────────────


def test_g0_head_moved_after_audit(handoff_home, workspace):
    head0 = _head(workspace)
    rec = _write_run(workspace, 1, head0, [])
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    # A sibling commit moves HEAD after the audit ran against head0.
    _commit(workspace, "sibling.txt", "x", "sibling work")
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-head-moved"


# ─── empty_diff mode (spec §1.2 — empty diff 跳审) ───────────────────────────


def _empty_diff_block(base: str, head: str, diff_hash: str) -> dict:
    return {
        "audit_mode": "empty_diff_attestation",
        "empty_diff_attestation": {
            "base": base,
            "head": head,
            "diff_hash": diff_hash,
            "mode_decider_version": "phaseB-1",
        },
    }


def test_empty_diff_clean_passes(handoff_home, workspace):
    head = _head(workspace)
    empty_hash = "sha256:" + hashlib.sha256(b"").hexdigest()
    out = _gate(workspace, _empty_diff_block(head, head, empty_hash))
    assert out.ok


def test_empty_diff_with_real_changes_rejected(handoff_home, workspace):
    base = _head(workspace)
    head = _commit(workspace, "feature.py", "x = 1\n", "real code change")
    empty_hash = "sha256:" + hashlib.sha256(b"").hexdigest()
    # Claim an empty diff base..head, but the gate recomputes a non-empty diff.
    out = _gate(workspace, _empty_diff_block(base, head, empty_hash))
    assert out.klass == "retry" and out.subcode == "codex-audit-required"


def test_empty_diff_head_moved_rejected(handoff_home, workspace):
    head0 = _head(workspace)
    empty_hash = "sha256:" + hashlib.sha256(b"").hexdigest()
    block = _empty_diff_block(head0, head0, empty_hash)
    _commit(workspace, "sibling.txt", "x", "sibling")
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-head-moved"


def test_empty_diff_tampered_hash_is_fatal(handoff_home, workspace):
    head = _head(workspace)
    out = _gate(workspace, _empty_diff_block(head, head, "sha256:" + "0" * 64))
    assert out.klass == "fatal" and out.subcode == "codex-audit-tampered"


# ─── G2: artifact integrity ─────────────────────────────────────────────────


def test_g2_artifact_missing_is_retry(handoff_home, workspace):
    head = _head(workspace)
    # A record for run 2, but no artifact written for run 2.
    fake = {
        "run_index": 2,
        "input_commit": head,
        "artifact_hash": "sha256:" + "0" * 64,
        "verdict": "pass",
        "findings_path": f"audit/{TASK}/2/codex-findings.json",
        "manifest_path": f"audit/{TASK}/2/codex-findings.json.manifest",
    }
    block = {"audit_mode": "full_codex_audit", "audit_runs": [fake], "dispositions": []}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-missing"


def test_g2_artifact_tampered_is_fatal(handoff_home, workspace):
    head = _head(workspace)
    rec = _write_run(workspace, 1, head, [])
    # Corrupt the findings bytes after the manifest vouched for them.
    fpath = codex_audit.findings_path(PROJECT, TASK, 1)
    data = json.loads(fpath.read_text())
    data["original_findings"] = [_finding("P0", "X")]
    fpath.write_text(json.dumps(data))
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    out = _gate(workspace, block)
    assert out.klass == "fatal" and out.subcode == "codex-audit-tampered"


# ─── G3: findings union across rounds (run1 P1 / run2 pass drops disposition) ─


def test_g3_p1_from_round1_unbound(handoff_home, workspace):
    head = _head(workspace)
    f = _finding("P1", "F1", "boundary bug")
    _write_run(workspace, 1, head, [f])  # run1 surfaced a P1
    rec2 = _write_run(workspace, 2, head, [])  # run2 clean — the P1 vanished
    rec1 = {
        "run_index": 1,
        "input_commit": head,
        "artifact_hash": "sha256:"
        + codex_audit.compute_findings_hash(
            {"run_index": 1, "input_commit": head, "original_findings": [f]}
        ),
        "verdict": "fail",
        "findings_path": f"audit/{TASK}/1/codex-findings.json",
        "manifest_path": f"audit/{TASK}/1/codex-findings.json.manifest",
    }
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec1, rec2], "dispositions": []}
    out = _gate(workspace, block)
    # The round-1 P1 has no disposition even though round 2 is clean.
    assert out.klass == "retry" and out.subcode == "codex-audit-findings-unbound"


def test_g3_p1_fixed_then_passes(handoff_home, workspace):
    head = _head(workspace)
    f = _finding("P1", "F1", "boundary bug")
    rec1 = _write_run(workspace, 1, head, [f])
    rec2 = _write_run(workspace, 2, head, [])  # fixed → gone in last run
    fhash = codex_audit.compute_finding_hash(f)
    disp = _disp("fixed", fhash, severity="P1", fix_commit="d" * 40)
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec1, rec2], "dispositions": [disp]}
    assert _gate(workspace, block).ok


# ─── G5: fix must be gone in the last run ────────────────────────────────────


def test_g5_fix_unverified_when_finding_persists(handoff_home, workspace):
    head = _head(workspace)
    f = _finding("P1", "F1", "still here")
    rec1 = _write_run(workspace, 1, head, [f])
    rec2 = _write_run(workspace, 2, head, [f])  # claimed fixed but STILL present
    fhash = codex_audit.compute_finding_hash(f)
    disp = _disp("fixed", fhash, severity="P1", fix_commit="d" * 40)
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec1, rec2], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-fix-unverified"


def test_g5_fixed_without_commit_unbound(handoff_home, workspace):
    head = _head(workspace)
    f = _finding("P0", "F1")
    rec1 = _write_run(workspace, 1, head, [f])
    rec2 = _write_run(workspace, 2, head, [])
    fhash = codex_audit.compute_finding_hash(f)
    disp = _disp("fixed", fhash, severity="P0")  # no fix_commit
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec1, rec2], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-findings-unbound"


# ─── G4: P0/P1 may not be deferred ───────────────────────────────────────────


def test_g4_p0_deferred_blocked(handoff_home, workspace):
    head = _head(workspace)
    f = _finding("P0", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    disp = _disp("deferred", fhash, severity="P0", scope_ruling="ack/x.json")
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "blocked" and out.subcode == "codex-audit-p0p1-unresolved"


# ─── G6: independent refute anti-forgery (same session) ──────────────────────


def test_g6_refute_same_session_blocked(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    head = _head(workspace)
    f = _finding("P1", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    disp = _disp(
        "independent_reviewer_refuted",
        fhash,
        severity="P1",
        reviewer_session_id="sess-A",  # SAME as evidence session → forgery
        independent_reviewer_artifact=f"audit/{TASK}/reviewer.json",
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "blocked" and out.subcode == "codex-audit-refute-same-session"


def test_g6_refute_missing_artifact_retry(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    head = _head(workspace)
    f = _finding("P1", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    disp = _disp(
        "independent_reviewer_refuted",
        fhash,
        severity="P1",
        reviewer_session_id="sess-B",
        independent_reviewer_artifact=f"audit/{TASK}/missing-reviewer.json",
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-refute-no-reviewer"


def test_g6_refute_valid_passes(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    head = _head(workspace)
    f = _finding("P1", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    art_rel = f"audit/{TASK}/reviewer.json"
    _reviewer_artifact(handoff_home, art_rel, fhash)
    disp = _disp(
        "independent_reviewer_refuted",
        fhash,
        severity="P1",
        reviewer_session_id="sess-B",  # different session
        independent_reviewer_artifact=art_rel,
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    assert _gate(workspace, block).ok


def test_g6_refute_incomplete_artifact_retry(handoff_home, workspace, monkeypatch):
    # codex R1-F3: a dummy artifact missing §1.7 fields must NOT pass.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    head = _head(workspace)
    f = _finding("P1", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    art_rel = f"audit/{TASK}/reviewer.json"
    art_abs = handoff_home / PROJECT / art_rel
    art_abs.parent.mkdir(parents=True, exist_ok=True)
    art_abs.write_text(json.dumps({"verdict": "refuted"}))  # missing required fields
    disp = _disp(
        "independent_reviewer_refuted",
        fhash,
        severity="P1",
        reviewer_session_id="sess-B",
        independent_reviewer_artifact=art_rel,
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-refute-no-reviewer"


def test_g6_refute_artifact_bound_to_wrong_finding_retry(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    head = _head(workspace)
    f = _finding("P1", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    art_rel = f"audit/{TASK}/reviewer.json"
    # Artifact bound to a DIFFERENT finding hash than the one being refuted.
    _reviewer_artifact(handoff_home, art_rel, "sha256:" + "9" * 64)
    disp = _disp(
        "independent_reviewer_refuted",
        fhash,
        severity="P1",
        reviewer_session_id="sess-B",
        independent_reviewer_artifact=art_rel,
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-refute-no-reviewer"


def test_indep_review_exceeded_blocked(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    head = _head(workspace)
    findings = [_finding("P1", f"F{i}", f"bug number {i}") for i in range(3)]
    rec = _write_run(workspace, 1, head, findings)
    disps = []
    for i, f in enumerate(findings):
        fhash = codex_audit.compute_finding_hash(f)
        art_rel = f"audit/{TASK}/reviewer-{i}.json"
        _reviewer_artifact(handoff_home, art_rel, fhash)
        disps.append(
            _disp(
                "independent_reviewer_refuted",
                fhash,
                severity="P1",
                reviewer_session_id="sess-B",
                independent_reviewer_artifact=art_rel,
            )
        )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": disps}
    out = _gate(workspace, block)
    # 3 valid refutes > MAX_INDEP_REVIEW (2)
    assert out.klass == "blocked" and out.subcode == "codex-audit-indep-review-exceeded"


# ─── G7: owner override anti-forgery (no ack token) ──────────────────────────


def test_g7_override_without_ack_token_blocked(handoff_home, workspace):
    head = _head(workspace)
    f = _finding("P0", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    disp = _disp("owner_override", fhash, severity="P0")  # AI-fabricated, no token
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "blocked" and out.subcode == "codex-audit-override-no-ack-token"


def test_g7_override_with_ack_token_passes(handoff_home, workspace):
    # Phase D: G7 now requires a real on-disk owner-ack artifact (binding +
    # self-consistent + unexpired), not a bare token. Write one, then pass.
    head = _head(workspace)
    f = _finding("P0", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    approved = datetime.now(UTC).isoformat(timespec="seconds")
    nonce = "n1"
    codex_audit.write_owner_ack(PROJECT, TASK, fhash, "bug", nonce, approved, "exempt")
    token = codex_audit.compute_owner_ack_token(TASK, fhash, nonce, approved)
    disp = _disp(
        "owner_override",
        fhash,
        severity="P0",
        owner_ack_token=token,
        expires_at=codex_audit._add_days_iso(approved, 7),
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    assert _gate(workspace, block).ok


def test_g7_override_expired_blocked(handoff_home, workspace):
    # Phase D: an expired on-disk ack still blocks (override-invalid). The ack is
    # written with a >7d-old approved_at so its derived expiry is in the past.
    head = _head(workspace)
    f = _finding("P0", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    past = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    nonce = "n1"
    codex_audit.write_owner_ack(PROJECT, TASK, fhash, "bug", nonce, past, "exempt")
    token = codex_audit.compute_owner_ack_token(TASK, fhash, nonce, past)
    disp = _disp(
        "owner_override",
        fhash,
        severity="P0",
        owner_ack_token=token,
        expires_at=codex_audit._add_days_iso(past, 7),
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "blocked" and out.subcode == "codex-audit-override-invalid"


# ─── G8: deferred P2/P3 must carry a scope ruling ────────────────────────────


def test_g8_deferred_p2_missing_scope_retry(handoff_home, workspace):
    head = _head(workspace)
    rec = _write_run(workspace, 1, head, [])  # clean (no P0/P1)
    fhash = codex_audit.compute_finding_hash(_finding("P2", "F1"))
    disp = _disp("deferred", fhash, severity="P2")  # no scope_ruling
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-defer-invalid"


def test_g8_deferred_p2_with_scope_passes(handoff_home, workspace):
    head = _head(workspace)
    rec = _write_run(workspace, 1, head, [])
    fhash = codex_audit.compute_finding_hash(_finding("P2", "F1"))
    disp = _disp("deferred", fhash, severity="P2", scope_ruling="ack/scope.1.json")
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    assert _gate(workspace, block).ok


# ─── G9: audit-round cap ─────────────────────────────────────────────────────


def test_g9_rounds_exceeded_blocked(handoff_home, workspace):
    head = _head(workspace)
    runs = [{"run_index": i, "input_commit": head, "verdict": "pass"} for i in range(1, 5)]
    block = {"audit_mode": "full_codex_audit", "audit_runs": runs, "dispositions": []}
    out = _gate(workspace, block)
    assert out.klass == "blocked" and out.subcode == "codex-audit-rounds-exceeded"


# ─── bypass mode (spec §1.3 — bypass without failure proof) ──────────────────


def test_bypass_valid_passes(handoff_home, workspace):
    block = {
        "audit_mode": "codex_unavailable_bypass",
        "codex_failure_attempts": [
            {
                "exit": 124,
                "stderr_hash": "sha256:" + "e" * 64,
                "timestamp": "2026-05-30T00:00:00+08:00",
            }
        ],
        "follow_up_audit_task_id": "demo-task-audit-followup",
    }
    assert _gate(workspace, block).ok


def test_bypass_without_failure_proof_rejected(handoff_home, workspace):
    block = {
        "audit_mode": "codex_unavailable_bypass",
        "codex_failure_attempts": [],
        "follow_up_audit_task_id": "demo-task-audit-followup",
    }
    out = _gate(workspace, block)
    assert out.klass == "bypass" and out.subcode == "codex-audit-bypass-no-failure-proof"


def test_bypass_without_follow_up_rejected(handoff_home, workspace):
    block = {
        "audit_mode": "codex_unavailable_bypass",
        "codex_failure_attempts": [
            {"exit": 1, "stderr_hash": "sha256:" + "e" * 64, "timestamp": "t"}
        ],
    }
    out = _gate(workspace, block)
    assert out.klass == "bypass" and out.subcode == "codex-audit-bypass-no-failure-proof"


# ─── docs_only legitimacy (content-level diff) ───────────────────────────────


def test_docs_only_pure_docs_passes(handoff_home, workspace):
    head = _commit(workspace, "notes.md", "# notes\n", "doc-only change")
    rec = _write_run(workspace, 1, head, [])
    block = {"audit_mode": "docs_only_light_audit", "audit_runs": [rec], "dispositions": []}
    out = _gate(workspace, block, session_commits=[head])
    assert out.ok, out


def test_docs_only_with_code_change_forces_full(handoff_home, workspace):
    head = _commit(workspace, "engine.py", "x = 1\n", "code change mislabeled docs")
    rec = _write_run(workspace, 1, head, [])
    block = {"audit_mode": "docs_only_light_audit", "audit_runs": [rec], "dispositions": []}
    out = _gate(workspace, block, session_commits=[head])
    assert out.klass == "retry" and out.subcode == "codex-audit-required"


def test_docs_only_claude_md_forces_full(handoff_home, workspace):
    head = _commit(workspace, "CLAUDE.md", "rules\n", "steering file change")
    rec = _write_run(workspace, 1, head, [])
    block = {"audit_mode": "docs_only_light_audit", "audit_runs": [rec], "dispositions": []}
    out = _gate(workspace, block, session_commits=[head])
    assert out.klass == "retry" and out.subcode == "codex-audit-required"


# ─── end-to-end through dump (exit-code mapping + flag gating) ───────────────


def _write_evidence(home: Path, payload: dict) -> Path:
    path = home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, path)
    return path


def _dump(ws: Path, path: Path) -> int:
    return dump.main(
        [
            "--task",
            TASK,
            "--next",
            "next brief",
            "--project",
            PROJECT,
            "--workspace",
            str(ws),
            "--status",
            "active",
            "--retro-evidence",
            str(path),
        ]
    )


def test_e2e_mandate_on_clean_passes(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    head = _head(workspace)
    rec = _write_run(workspace, 1, head, [])
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    ev = _evidence(workspace, block)
    path = _write_evidence(handoff_home, ev)
    assert _dump(workspace, path) == 0


def test_e2e_mandate_off_skips_gate(handoff_home, workspace, monkeypatch):
    # Same head-moved evidence the gate would reject — but mandate OFF → pass.
    head0 = _head(workspace)
    rec = _write_run(workspace, 1, head0, [])
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    ev = _evidence(workspace, block)
    path = _write_evidence(handoff_home, ev)
    _commit(workspace, "sibling.txt", "x", "sibling")  # moves HEAD
    # mandate off (not set): the audit gate never runs.
    assert _dump(workspace, path) == 0


def test_e2e_head_moved_returns_retry(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    head0 = _head(workspace)
    rec = _write_run(workspace, 1, head0, [])
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    ev = _evidence(workspace, block)
    path = _write_evidence(handoff_home, ev)
    _commit(workspace, "sibling.txt", "x", "sibling")
    assert _dump(workspace, path) == retro_gate.EXIT_RETRY


def test_e2e_p0p1_deferred_returns_blocked_and_writes_md(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    head = _head(workspace)
    f = _finding("P0", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    disp = _disp("deferred", fhash, severity="P0", scope_ruling="ack/x.json")
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    ev = _evidence(workspace, block)
    path = _write_evidence(handoff_home, ev)
    assert _dump(workspace, path) == retro_gate.EXIT_BLOCKED
    blocked_md = handoff_home / PROJECT / "queue" / f"{TASK}.BLOCKED.md"
    assert blocked_md.exists()
    assert "codex-audit-p0p1-unresolved" in blocked_md.read_text()


def test_e2e_tampered_returns_fatal(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    head = _head(workspace)
    rec = _write_run(workspace, 1, head, [])
    fpath = codex_audit.findings_path(PROJECT, TASK, 1)
    data = json.loads(fpath.read_text())
    data["original_findings"] = [_finding("P0", "X")]
    fpath.write_text(json.dumps(data))
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    ev = _evidence(workspace, block)
    path = _write_evidence(handoff_home, ev)
    assert _dump(workspace, path) == retro_gate.EXIT_FATAL


def test_e2e_bypass_no_proof_returns_bypass_exit(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    block = {
        "audit_mode": "codex_unavailable_bypass",
        "codex_failure_attempts": [],
        "follow_up_audit_task_id": "x-followup",
    }
    ev = _evidence(workspace, block)
    path = _write_evidence(handoff_home, ev)
    assert _dump(workspace, path) == retro_gate.EXIT_BYPASS


# ─── audit attempt counter isolation ─────────────────────────────────────────


def _audit_attempt(home: Path) -> Path:
    return home / PROJECT / "ack" / f"{TASK}.audit.attempt_n.txt"


def _retro_attempt(home: Path) -> Path:
    return home / PROJECT / "ack" / f"{TASK}.retro.attempt_n.txt"


def test_audit_counter_isolated_from_retro(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    head0 = _head(workspace)
    rec = _write_run(workspace, 1, head0, [])
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    ev = _evidence(workspace, block)
    path = _write_evidence(handoff_home, ev)
    _commit(workspace, "sibling.txt", "x", "sibling")  # head-moved → audit RETRY
    assert _dump(workspace, path) == retro_gate.EXIT_RETRY
    # The isolated audit counter bumped; the retro counter is untouched.
    assert _audit_attempt(handoff_home).read_text().strip() == "1"
    assert not _retro_attempt(handoff_home).exists()


def test_audit_attempt_exhausted_blocks(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    head0 = _head(workspace)
    rec = _write_run(workspace, 1, head0, [])
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    ev = _evidence(workspace, block)
    path = _write_evidence(handoff_home, ev)
    _commit(workspace, "sibling.txt", "x", "sibling")  # persistent head-moved
    # attempt_n 0→1 (retry), 1→2 (retry), then 2 == ATTEMPT_MAX → BLOCKED.
    assert _dump(workspace, path) == retro_gate.EXIT_RETRY
    assert _dump(workspace, path) == retro_gate.EXIT_RETRY
    assert _dump(workspace, path) == retro_gate.EXIT_BLOCKED
    blocked_md = handoff_home / PROJECT / "queue" / f"{TASK}.BLOCKED.md"
    assert "codex-audit-attempt-exhausted" in blocked_md.read_text()


def test_e2e_no_codex_block_required_when_mandate_on(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    p0, p1 = _full_phase_status()
    ev = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, phase0=p0, phase1=p1
    )  # no codex_audit block
    path = _write_evidence(handoff_home, ev)
    assert _dump(workspace, path) == retro_gate.EXIT_RETRY


# ─── codex R1 remediation regressions (F1 / F5 / F6 / F7) ───────────────────


def test_f1_forensic_mode_does_not_skip_audit_gate(handoff_home, workspace, monkeypatch):
    # F1 P0: a self-declared mode="forensic_retro" must NOT bypass the audit gate
    # when the audit mandate is on (else any evidence sets forensic to skip it).
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    p0, p1 = _full_phase_status()
    ev = handoff_precheck.build_evidence(
        task_id=TASK,
        project=PROJECT,
        workspace=workspace,
        mode=handoff_precheck.MODE_FORENSIC_RETRO,
        phase0=p0,
        phase1=p1,
    )  # forensic, no codex_audit block
    path = _write_evidence(handoff_home, ev)
    assert _dump(workspace, path) == retro_gate.EXIT_RETRY


def test_f5_distinct_findings_same_title_do_not_collide(handoff_home, workspace):
    # F5 P1: two distinct findings sharing a title (different id) must hash apart.
    a = _finding("P1", "F1", "off by one")
    b = _finding("P1", "F2", "off by one")
    assert codex_audit.compute_finding_hash(a) != codex_audit.compute_finding_hash(b)


def test_f5_identityless_finding_rejected(handoff_home, workspace):
    # F5 P1: a P0/P1 finding with no id / location / text can't be bound → reject.
    head = _head(workspace)
    blank = {"severity": "P0"}  # no id, no text, no location
    rec = _write_run(workspace, 1, head, [blank])
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-findings-unbound"


def test_f5_same_id_dedups_across_rounds(handoff_home, workspace):
    # Same id, drifted wording across rounds → one identity → one disposition.
    head = _head(workspace)
    r1 = _finding("P1", "F1", "boundary bug in parser")
    r2 = _finding("P1", "F1", "boundary bug (reworded)")
    assert codex_audit.compute_finding_hash(r1) == codex_audit.compute_finding_hash(r2)
    rec1 = _write_run(workspace, 1, head, [r1])
    rec2 = _write_run(workspace, 2, head, [])  # fixed → gone
    disp = _disp("fixed", codex_audit.compute_finding_hash(r1), severity="P1", fix_commit="d" * 40)
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec1, rec2], "dispositions": [disp]}
    assert _gate(workspace, block).ok


def test_f6_corrupt_manifest_is_tampered(handoff_home, workspace):
    # F6 P2: a PRESENT but corrupt manifest is tamper (FATAL), not missing (RETRY).
    head = _head(workspace)
    rec = _write_run(workspace, 1, head, [])
    mpath = codex_audit.manifest_path(PROJECT, TASK, 1)
    mpath.write_text("{ this is not valid json")
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    out = _gate(workspace, block)
    assert out.klass == "fatal" and out.subcode == "codex-audit-tampered"


def _dump_no_evidence(ws: Path) -> int:
    return dump.main(
        [
            "--task",
            TASK,
            "--next",
            "next brief",
            "--project",
            PROJECT,
            "--workspace",
            str(ws),
            "--status",
            "active",
        ]
    )


def test_f7_no_evidence_audit_mandate_bumps_counter_then_blocks(
    handoff_home, workspace, monkeypatch
):
    # F7 P2: audit mandate on + no evidence must progress 0→1→2→BLOCKED via the
    # isolated audit counter, not RETRY forever.
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    assert _dump_no_evidence(workspace) == retro_gate.EXIT_RETRY
    assert _audit_attempt(handoff_home).read_text().strip() == "1"
    assert _dump_no_evidence(workspace) == retro_gate.EXIT_RETRY
    assert _dump_no_evidence(workspace) == retro_gate.EXIT_BLOCKED
    # retro counter must be untouched by audit-driven retries.
    assert not _retro_attempt(handoff_home).exists()


# ─── codex R2 remediation regressions ───────────────────────────────────────


def test_r2_omitted_failing_run_rejected(handoff_home, workspace):
    # R2-1 P0: a failing run-1 persisted on disk cannot be omitted from
    # audit_runs (listing only the clean run-2) to hide its P0/P1.
    head = _head(workspace)
    f = _finding("P1", "F1", "real bug")
    _write_run(workspace, 1, head, [f])  # failing run 1 on disk (NOT listed below)
    rec2 = _write_run(workspace, 2, head, [])  # clean run 2
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec2], "dispositions": []}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-findings-unbound"


def test_r2_noncontiguous_runs_rejected(handoff_home, workspace):
    head = _head(workspace)
    rec1 = _write_run(workspace, 1, head, [])
    rec3 = _write_run(workspace, 3, head, [])  # gap at 2
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec1, rec3], "dispositions": []}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-findings-unbound"


def test_r2_same_rule_distinct_files_no_collide(handoff_home, workspace):
    # R2-2 P1: a rule id alone must not be the identity — two sites collide.
    a = {"severity": "P1", "rule": "B001", "file": "a.py", "title": "bug"}
    b = {"severity": "P1", "rule": "B001", "file": "b.py", "title": "bug"}
    assert codex_audit.compute_finding_hash(a) != codex_audit.compute_finding_hash(b)


def test_r2_rule_only_no_location_rejected(handoff_home, workspace):
    # R2-2: a finding with only a rule id (no location, no text) is unbindable.
    head = _head(workspace)
    bad = {"severity": "P0", "rule": "B001"}
    rec = _write_run(workspace, 1, head, [bad])
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-findings-unbound"


def test_r2_both_mandates_no_evidence_uses_audit_counter(handoff_home, workspace, monkeypatch):
    # R2-3 P2: with BOTH mandates on and no evidence, route through the isolated
    # audit counter (not a bare retro-evidence-missing that never blocks).
    monkeypatch.setenv("HANDOFF_AUDIT_MANDATE", "1")
    monkeypatch.setenv("HANDOFF_RETRO_MANDATE", "1")
    assert _dump_no_evidence(workspace) == retro_gate.EXIT_RETRY
    assert _audit_attempt(handoff_home).read_text().strip() == "1"


# ─── codex R3 convergence regressions ───────────────────────────────────────


def test_r3_bypass_rejected_when_audit_runs_exist(handoff_home, workspace):
    # R3-2 P0: a real audit run with a P1 is persisted → can't switch to bypass
    # mode to dodge the union.
    head = _head(workspace)
    _write_run(workspace, 1, head, [_finding("P1", "F1")])
    block = {
        "audit_mode": "codex_unavailable_bypass",
        "codex_failure_attempts": [
            {"exit": 1, "stderr_hash": "sha256:" + "e" * 64, "timestamp": "t"}
        ],
        "follow_up_audit_task_id": "x-followup",
    }
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-required"


def test_r3_empty_diff_rejected_when_audit_runs_exist(handoff_home, workspace):
    # R3-2 P0: ditto for empty_diff mode.
    head = _head(workspace)
    _write_run(workspace, 1, head, [_finding("P0", "F1")])
    empty_hash = "sha256:" + hashlib.sha256(b"").hexdigest()
    out = _gate(workspace, _empty_diff_block(head, head, empty_hash))
    assert out.klass == "retry" and out.subcode == "codex-audit-required"


def test_r4_refute_rejected_when_evidence_has_no_session_id(handoff_home, workspace, monkeypatch):
    # R4-1 P0: evidence that omits session_id can't make the "reviewer != audited
    # session" independence check vacuous (None == None is False).
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    head = _head(workspace)
    f = _finding("P1", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    art_rel = f"audit/{TASK}/reviewer.json"
    _reviewer_artifact(handoff_home, art_rel, fhash, reviewer_sid="")  # would-be vacuous
    disp = _disp(
        "independent_reviewer_refuted",
        fhash,
        severity="P1",
        reviewer_session_id="",
        independent_reviewer_artifact=art_rel,
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    # Build evidence then strip session_id (hand-crafted), re-pointing the gate.
    out = _gate(workspace, block, session_id="")
    assert out.klass == "retry" and out.subcode == "codex-audit-refute-no-reviewer"


def test_r10_line_shift_does_not_hide_presence(handoff_home, workspace):
    # R10 P1: a line-number shift (unrelated edit above) must NOT make a still-
    # present issue look "gone" — presence is line-insensitive.
    head = _head(workspace)
    f1 = {"severity": "P1", "rule": "sqli", "file": "src/db.py", "line": 10, "text": "unsafe SQL"}
    rec1 = _write_run(workspace, 1, head, [f1])
    rec2 = _write_run(
        workspace,
        2,
        head,
        [{"severity": "P2", "rule": "sqli", "file": "src/db.py", "line": 11, "text": "unsafe SQL"}],
    )
    disp = _disp("fixed", codex_audit.compute_finding_hash(f1), severity="P1", fix_commit="d" * 40)
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec1, rec2], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-fix-unverified"


def test_r9_duplicate_id_distinct_sites_one_unfixed(handoff_home, workspace):
    # R9 P1: two distinct P1s share an id; "fix" one site, leave the other
    # (downgraded) — the shared-hash presence set must catch the survivor.
    head = _head(workspace)
    auth = {"id": "dup", "severity": "P1", "file": "src/auth.py", "line": 10, "message": "sqli"}
    pay = {"id": "dup", "severity": "P1", "file": "src/pay.py", "line": 20, "message": "token"}
    rec1 = _write_run(workspace, 1, head, [auth, pay])
    # Last run: auth site still present (downgraded to P2); pay site gone.
    rec2 = _write_run(
        workspace,
        2,
        head,
        [{"id": "dup", "severity": "P2", "file": "src/auth.py", "line": 10, "message": "sqli"}],
    )
    disp = _disp(
        "fixed", codex_audit.compute_finding_hash(auth), severity="P1", fix_commit="d" * 40
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec1, rec2], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-fix-unverified"


def test_r8_fixed_reappears_under_new_id_downgraded(handoff_home, workspace):
    # R8-1 P0: same issue (same file/line/title) reappears downgraded under a
    # NEW id — id-neutral presence must still catch it as not-gone.
    head = _head(workspace)
    p1 = {"id": "F1", "severity": "P1", "file": "src/auth.py", "line": 42, "title": "auth bypass"}
    rec1 = _write_run(workspace, 1, head, [p1])
    rec2 = _write_run(
        workspace,
        2,
        head,
        [{"id": "F2", "severity": "P2", "file": "src/auth.py", "line": 42, "title": "auth bypass"}],
    )
    disp = _disp("fixed", codex_audit.compute_finding_hash(p1), severity="P1", fix_commit="d" * 40)
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec1, rec2], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-fix-unverified"


def test_r8_id_only_fixed_fails_closed(handoff_home, workspace):
    # R8-1 P0: a blocking finding with only an id (no loc/text) can't be proven
    # gone — fix is fail-closed.
    head = _head(workspace)
    idonly = {"id": "F1", "severity": "P0"}
    rec1 = _write_run(workspace, 1, head, [idonly])
    rec2 = _write_run(workspace, 2, head, [])
    disp = _disp(
        "fixed", codex_audit.compute_finding_hash(idonly), severity="P0", fix_commit="d" * 40
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec1, rec2], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-findings-unbound"


def test_r8_severity_whitespace_not_evaded(handoff_home, workspace):
    # R8-2 P0: "P1 " (trailing space) must normalize to P1 and enter the union.
    head = _head(workspace)
    rec = _write_run(workspace, 1, head, [{"id": "F1", "severity": "P1 ", "title": "bug"}])
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-findings-unbound"


def test_r8_unknown_severity_fails_closed(handoff_home, workspace):
    # R8-2 P0: an unrecognized non-empty severity must fail closed, not pass.
    head = _head(workspace)
    rec = _write_run(workspace, 1, head, [{"id": "F1", "severity": "CRITICAL", "title": "x"}])
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-missing"


def test_r7_fixed_finding_downgraded_not_gone(handoff_home, workspace):
    # R7-1 P0: a P1 marked "fixed" that reappears DOWNGRADED to P2 in the last
    # run is NOT gone — the severity-neutral presence check must catch it.
    head = _head(workspace)
    p1 = _finding("P1", "F1", "boundary bug")
    rec1 = _write_run(workspace, 1, head, [p1])
    # Last run: same finding (same id), but downgraded to P2 — still present.
    rec2 = _write_run(workspace, 2, head, [_finding("P2", "F1", "boundary bug")])
    fhash = codex_audit.compute_finding_hash(p1)
    disp = _disp("fixed", fhash, severity="P1", fix_commit="d" * 40)
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec1, rec2], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-fix-unverified"


def test_r6_dict_original_findings_hides_nothing(handoff_home, workspace):
    # R6-1 P0: a dict-shaped original_findings (hiding a P1) must fail closed.
    head = _head(workspace)
    rec = codex_audit.write_findings_artifact(
        PROJECT,
        TASK,
        1,
        {
            "run_index": 1,
            "input_commit": head,
            "original_findings": {"sneaky": _finding("P1", "X")},
        },
        input_commit=head,
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": []}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-missing"


def test_r6_audit_run_rejects_nonlist_original_findings(handoff_home, workspace, tmp_path):
    head = _head(workspace)
    ffile = tmp_path / "bad.json"
    ffile.write_text(json.dumps({"input_commit": head, "original_findings": {"x": 1}}))
    rc = codex_audit.main_audit_run(
        [
            "--task",
            TASK,
            "--project",
            PROJECT,
            "--workspace",
            str(workspace),
            "--run-index",
            "1",
            "--findings-file",
            str(ffile),
        ]
    )
    assert rc == 1


@pytest.mark.parametrize("field", ["independent_run_id", "artifact_hash"])
def test_r6_reviewer_artifact_nonstring_field_rejected(handoff_home, workspace, monkeypatch, field):
    # R6-2 P1: §1.7 reviewer fields must be typed, not truthy.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    head = _head(workspace)
    f = _finding("P1", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    art_rel = f"audit/{TASK}/reviewer.json"
    art_abs = handoff_home / PROJECT / art_rel
    art_abs.parent.mkdir(parents=True, exist_ok=True)
    art = {
        "independent_run_id": "ir-1",
        "reviewer_session_id": "sess-B",
        "original_finding_hash": fhash,
        "verdict": "refuted",
        "artifact_hash": "sha256:" + "a" * 64,
    }
    art[field] = {"fake": True}  # truthy non-string
    art_abs.write_text(json.dumps(art))
    disp = _disp(
        "independent_reviewer_refuted",
        fhash,
        severity="P1",
        reviewer_session_id="sess-B",
        independent_reviewer_artifact=art_rel,
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-refute-no-reviewer"


@pytest.mark.parametrize("bad_token", ["   ", {"fake": True}, ["x"], 1])
def test_r5_owner_override_rejects_nonstring_token(handoff_home, workspace, bad_token):
    # R5-2 P0: owner_ack_token must be a real non-empty string, not any truthy value.
    head = _head(workspace)
    f = _finding("P0", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    disp = _disp("owner_override", fhash, severity="P0", owner_ack_token=bad_token)
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "blocked" and out.subcode == "codex-audit-override-no-ack-token"


@pytest.mark.parametrize("bad_sid", ["   ", ["x"], {"a": 1}])
def test_r5_refute_rejects_nonstring_reviewer_session(
    handoff_home, workspace, monkeypatch, bad_sid
):
    # R5-1 P0: reviewer_session_id must be a real non-empty string.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    head = _head(workspace)
    f = _finding("P1", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    art_rel = f"audit/{TASK}/reviewer.json"
    _reviewer_artifact(handoff_home, art_rel, fhash)
    disp = _disp(
        "independent_reviewer_refuted",
        fhash,
        severity="P1",
        reviewer_session_id=bad_sid,
        independent_reviewer_artifact=art_rel,
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    out = _gate(workspace, block)
    assert out.klass == "retry" and out.subcode == "codex-audit-refute-no-reviewer"


def test_r3_audit_run_refuses_overwrite(handoff_home, workspace, tmp_path):
    # R3-1 P0 (honest path): audit runs are append-only via the CLI — a failing
    # run-1 can't be silently overwritten by a clean run-1 at the same index.
    head = _head(workspace)
    failing = tmp_path / "f1.json"
    failing.write_text(
        json.dumps(
            {"run_index": 1, "input_commit": head, "original_findings": [_finding("P1", "F1")]}
        )
    )
    args = [
        "--task",
        TASK,
        "--project",
        PROJECT,
        "--workspace",
        str(workspace),
        "--run-index",
        "1",
        "--findings-file",
        str(failing),
    ]
    assert codex_audit.main_audit_run(args) == 0
    # Second write to the SAME index (now "clean") must be refused.
    clean = tmp_path / "f1clean.json"
    clean.write_text(json.dumps({"run_index": 1, "input_commit": head, "original_findings": []}))
    args[-1] = str(clean)
    assert codex_audit.main_audit_run(args) == 1
    # Original failing artifact is intact (still has the P1).
    on_disk = json.loads(codex_audit.findings_path(PROJECT, TASK, 1).read_text())
    assert on_disk["original_findings"][0]["severity"] == "P1"
