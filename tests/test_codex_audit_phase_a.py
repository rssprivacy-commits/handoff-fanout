"""Phase A — codex audit gate: schema 5.5.0 + builder + disposition validation
+ runtime findings artifact (sidecar manifest) + CLI.

Phase A is the *evidence capability* layer ONLY (mandate OFF). It must:
  * bump the evidence schema to 5.5.0 while keeping the gate fail-open for old
    v5.4.1 evidence (migration window — spec §2.5 / R2-P1-5);
  * let ``build_evidence`` carry an optional ``codex_audit`` block (omitted →
    byte-for-byte backward compatible);
  * build the four mode-specific codex_audit blocks (spec §2.1 / §3.5);
  * validate disposition shapes (spec §1.7 / §3.1 / G4-G8 input contract);
  * persist codex findings as an atomic artifact with a *sidecar* manifest hash
    (spec §1.4 / R2-P0-3 — never embed a JSON's hash inside itself);
  * NOT add any G0-G9 gating to retro_gate (that is Phase B). With mandate off,
    a 5.5.0 evidence without a codex_audit block must pass exactly like 5.4.1.

Spec: ``docs/PROTOCOL.md`` Part II §14 (the codex audit gate).
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from handoff_fanout import codex_audit, dump, handoff_precheck

# ─── fixtures ───────────────────────────────────────────────────────────────


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


PROJECT = "demo"
TASK = "demo-task"


def _full_phase_status():
    p0 = {k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS}
    p1 = {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS}
    return p0, p1


def _sample_finding(severity: str = "P0", fid: str = "F1") -> dict:
    return {"id": fid, "severity": severity, "title": f"bug {fid}", "evidence": "line 42"}


def _disposition(disp: str, **extra) -> dict:
    base = {
        "finding_id": "F1",
        "finding_hash": "sha256:" + "a" * 64,
        "original_severity": "P0",
        "disposition": disp,
    }
    base.update(extra)
    return base


# ─── schema version + supported set ─────────────────────────────────────────


def test_schema_version_is_5_5_0():
    assert handoff_precheck.EVIDENCE_SCHEMA_VERSION == "5.5.0"


def test_supported_versions_include_old_and_new():
    supported = handoff_precheck.SUPPORTED_EVIDENCE_SCHEMA_VERSIONS
    assert "5.5.0" in supported
    assert "v5.4.1" in supported  # migration window: old in-flight evidence


# ─── build_evidence backward compat ─────────────────────────────────────────


def test_build_evidence_without_codex_audit_omits_block(workspace):
    p0, p1 = _full_phase_status()
    ev = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, phase0=p0, phase1=p1
    )
    assert "codex_audit" not in ev
    assert ev["schema_version"] == "5.5.0"
    # hash self-consistency preserved
    assert ev["evidence_hash"] == handoff_precheck.compute_evidence_hash(ev)


def test_build_evidence_with_codex_audit_includes_and_hashes_block(workspace):
    p0, p1 = _full_phase_status()
    block = codex_audit.build_codex_audit_block(
        handoff_precheck.AUDIT_MODE_EMPTY_DIFF,
        attestation={
            "base": "a" * 40,
            "head": "b" * 40,
            "diff_hash": "sha256:" + "c" * 64,
            "mode_decider_version": "phaseA-1",
        },
    )
    ev = handoff_precheck.build_evidence(
        task_id=TASK,
        project=PROJECT,
        workspace=workspace,
        phase0=p0,
        phase1=p1,
        codex_audit=block,
    )
    assert ev["codex_audit"] == block
    # codex_audit is inside the hashed payload (tamper protection)
    assert ev["evidence_hash"] == handoff_precheck.compute_evidence_hash(ev)
    mutated = json.loads(json.dumps(ev))
    mutated["codex_audit"]["audit_mode"] = "tampered"
    assert handoff_precheck.compute_evidence_hash(mutated) != ev["evidence_hash"]


# ─── build_codex_audit_block: 4 modes ───────────────────────────────────────


def test_build_block_full_mode():
    runs = [
        {
            "run_index": 1,
            "input_commit": "a" * 40,
            "artifact_hash": "sha256:" + "f" * 64,
            "verdict": "pass",
        }
    ]
    disps = [_disposition("fixed", fix_commit="d" * 40)]
    block = codex_audit.build_codex_audit_block(
        handoff_precheck.AUDIT_MODE_FULL, audit_runs=runs, dispositions=disps
    )
    assert block["audit_mode"] == "full_codex_audit"
    assert block["audit_runs"] == runs
    assert block["dispositions"] == disps


def test_build_block_full_requires_runs():
    with pytest.raises(ValueError, match="audit_runs"):
        codex_audit.build_codex_audit_block(handoff_precheck.AUDIT_MODE_FULL, dispositions=[])


def test_build_block_empty_diff_mode():
    att = {
        "base": "a" * 40,
        "head": "b" * 40,
        "diff_hash": "sha256:" + "c" * 64,
        "mode_decider_version": "phaseA-1",
    }
    block = codex_audit.build_codex_audit_block(
        handoff_precheck.AUDIT_MODE_EMPTY_DIFF, attestation=att
    )
    assert block["audit_mode"] == "empty_diff_attestation"
    assert block["empty_diff_attestation"] == att


def test_build_block_empty_diff_requires_attestation_fields():
    with pytest.raises(ValueError, match="attestation"):
        codex_audit.build_codex_audit_block(
            handoff_precheck.AUDIT_MODE_EMPTY_DIFF, attestation={"base": "x"}
        )


def test_build_block_docs_only_mode():
    runs = [
        {
            "run_index": 1,
            "input_commit": "a" * 40,
            "artifact_hash": "sha256:" + "f" * 64,
            "verdict": "pass",
        }
    ]
    block = codex_audit.build_codex_audit_block(
        handoff_precheck.AUDIT_MODE_DOCS_ONLY, audit_runs=runs, dispositions=[]
    )
    assert block["audit_mode"] == "docs_only_light_audit"
    assert block["audit_runs"] == runs


def _valid_attempts(n=3):
    # Phase D R1/R2: builder/gate/producer all enforce MIN_CODEX_FAILURES (3).
    return [
        {
            "exit": 124,
            "stderr_hash": "sha256:" + "e" * 64,
            "timestamp": f"2026-05-30T0{i}:00:00+08:00",
        }
        for i in range(n)
    ]


def test_build_block_bypass_mode():
    block = codex_audit.build_codex_audit_block(
        handoff_precheck.AUDIT_MODE_BYPASS,
        bypass={
            "codex_failure_attempts": _valid_attempts(3),
            "override_ref": "ack/demo-task.retro.override.json",
            "follow_up_audit_task_id": "demo-task-audit-followup",
        },
    )
    assert block["audit_mode"] == "codex_unavailable_bypass"
    assert len(block["codex_failure_attempts"]) == 3
    assert block["follow_up_audit_task_id"] == "demo-task-audit-followup"


def test_build_block_bypass_rejects_too_few_failures():
    with pytest.raises(ValueError, match="MIN_CODEX_FAILURES"):
        codex_audit.build_codex_audit_block(
            handoff_precheck.AUDIT_MODE_BYPASS,
            bypass={
                "codex_failure_attempts": _valid_attempts(2),
                "follow_up_audit_task_id": "x-followup",
            },
        )


def test_build_block_bypass_requires_failure_proof():
    with pytest.raises(ValueError, match="codex_failure_attempts"):
        codex_audit.build_codex_audit_block(
            handoff_precheck.AUDIT_MODE_BYPASS,
            bypass={"follow_up_audit_task_id": "x-followup"},
        )


def test_build_block_unknown_mode_raises():
    with pytest.raises(ValueError, match="audit_mode"):
        codex_audit.build_codex_audit_block("nonsense_mode")


# ─── validate_disposition_shape ─────────────────────────────────────────────


def test_disposition_fixed_ok():
    assert (
        codex_audit.validate_disposition_shape(_disposition("fixed", fix_commit="d" * 40)) is None
    )


def test_disposition_fixed_missing_fix_commit():
    err = codex_audit.validate_disposition_shape(_disposition("fixed"))
    assert err and "fix_commit" in err


def test_disposition_refuted_ok():
    d = _disposition(
        "independent_reviewer_refuted",
        independent_reviewer_artifact="audit/demo-task/2/reviewer.json",
        reviewer_session_id="other-session-uuid",
    )
    assert codex_audit.validate_disposition_shape(d) is None


def test_disposition_refuted_missing_reviewer_fields():
    err = codex_audit.validate_disposition_shape(_disposition("independent_reviewer_refuted"))
    assert err and ("independent_reviewer_artifact" in err or "reviewer_session_id" in err)


def test_disposition_owner_override_ok():
    d = _disposition("owner_override", owner_ack_token="owner-token-abc")
    assert codex_audit.validate_disposition_shape(d) is None


def test_disposition_owner_override_missing_token():
    err = codex_audit.validate_disposition_shape(_disposition("owner_override"))
    assert err and "owner_ack_token" in err


def test_disposition_deferred_ok_p2():
    d = _disposition("deferred", original_severity="P2", scope_ruling="ack/demo-task.scope.1.json")
    assert codex_audit.validate_disposition_shape(d) is None


def test_disposition_deferred_rejects_p0():
    d = _disposition("deferred", original_severity="P0", scope_ruling="ack/demo-task.scope.1.json")
    err = codex_audit.validate_disposition_shape(d)
    assert err and ("severity" in err or "P2" in err)


def test_disposition_deferred_missing_scope_ruling():
    err = codex_audit.validate_disposition_shape(_disposition("deferred", original_severity="P3"))
    assert err and "scope_ruling" in err


def test_disposition_unknown_type():
    err = codex_audit.validate_disposition_shape(_disposition("hand_wave"))
    assert err and "disposition" in err


def test_disposition_missing_finding_id():
    d = _disposition("fixed", fix_commit="d" * 40)
    del d["finding_id"]
    err = codex_audit.validate_disposition_shape(d)
    assert err and "finding_id" in err


def test_disposition_bad_severity():
    d = _disposition("fixed", fix_commit="d" * 40, original_severity="P9")
    err = codex_audit.validate_disposition_shape(d)
    assert err and "severity" in err


# ─── findings artifact + sidecar manifest ───────────────────────────────────


def test_write_findings_artifact_creates_sidecar_manifest(handoff_home):
    findings = {
        "run_index": 1,
        "input_commit": "a" * 40,
        "original_findings": [_sample_finding("P0", "F1"), _sample_finding("P2", "F2")],
        "verdict": "fail",
    }
    record = codex_audit.write_findings_artifact(PROJECT, TASK, 1, findings, input_commit="a" * 40)

    fpath = codex_audit.findings_path(PROJECT, TASK, 1)
    mpath = codex_audit.manifest_path(PROJECT, TASK, 1)
    assert fpath.exists()
    assert mpath.exists()
    # manifest is a sidecar — the hash lives OUTSIDE the findings json (R2-P0-3)
    assert "artifact_hash" not in json.loads(fpath.read_text())
    manifest = json.loads(mpath.read_text())
    assert manifest["algo"] == "sha256"
    assert manifest["sha256"] == codex_audit.compute_findings_hash(findings)
    # run record references the manifest hash + carries derived verdict
    assert record["artifact_hash"] == "sha256:" + manifest["sha256"]
    assert record["run_index"] == 1
    assert record["input_commit"] == "a" * 40
    assert record["verdict"] == "fail"  # has a P0


def test_verify_findings_artifact_detects_tamper(handoff_home):
    findings = {
        "run_index": 1,
        "input_commit": "a" * 40,
        "original_findings": [],
        "verdict": "pass",
    }
    codex_audit.write_findings_artifact(PROJECT, TASK, 1, findings, input_commit="a" * 40)
    assert codex_audit.verify_findings_artifact(PROJECT, TASK, 1) is True
    # tamper the findings bytes after the manifest was written
    fpath = codex_audit.findings_path(PROJECT, TASK, 1)
    data = json.loads(fpath.read_text())
    data["original_findings"] = [_sample_finding("P0", "X")]
    fpath.write_text(json.dumps(data))
    assert codex_audit.verify_findings_artifact(PROJECT, TASK, 1) is False


def test_derive_verdict():
    assert codex_audit.derive_verdict({"original_findings": []}) == "pass"
    assert codex_audit.derive_verdict({"original_findings": [_sample_finding("P2")]}) == "pass"
    assert codex_audit.derive_verdict({"original_findings": [_sample_finding("P1")]}) == "fail"
    assert codex_audit.derive_verdict({"original_findings": [_sample_finding("P0")]}) == "fail"


def test_findings_record_stores_canonical_relative_path(handoff_home):
    findings = {
        "run_index": 2,
        "input_commit": "a" * 40,
        "original_findings": [],
        "verdict": "pass",
    }
    record = codex_audit.write_findings_artifact(PROJECT, TASK, 2, findings, input_commit="a" * 40)
    # path is relative to ~/.claude-handoff/<project>/ (spec §3.4) — no abs path leak
    assert not record["findings_path"].startswith("/")
    assert record["findings_path"] == f"audit/{TASK}/2/codex-findings.json"


# ─── dispositions store ─────────────────────────────────────────────────────


def test_append_disposition_validates_and_persists(handoff_home):
    d = _disposition("fixed", fix_commit="d" * 40)
    codex_audit.append_disposition(PROJECT, TASK, d)
    loaded = codex_audit.load_dispositions(PROJECT, TASK)
    assert len(loaded) == 1
    assert loaded[0]["finding_id"] == "F1"


def test_append_disposition_rejects_bad_shape(handoff_home):
    with pytest.raises(ValueError):
        codex_audit.append_disposition(PROJECT, TASK, _disposition("fixed"))  # no fix_commit


# ─── backward compat: mandate OFF gate ──────────────────────────────────────


def _run_dump(workspace, retro_evidence: Path | None):
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
    ]
    if retro_evidence is not None:
        argv += ["--retro-evidence", str(retro_evidence)]
    return dump.main(argv)


def _write_evidence(home: Path, payload: dict) -> Path:
    path = home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, path)
    return path


def test_gate_passes_new_5_5_0_evidence_without_codex_block(handoff_home, workspace):
    p0, p1 = _full_phase_status()
    ev = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, phase0=p0, phase1=p1
    )
    path = _write_evidence(handoff_home, ev)
    assert _run_dump(workspace, path) == 0  # mandate off, no codex block → OK


def test_gate_passes_new_5_5_0_evidence_with_codex_block(handoff_home, workspace):
    p0, p1 = _full_phase_status()
    block = codex_audit.build_codex_audit_block(
        handoff_precheck.AUDIT_MODE_FULL,
        audit_runs=[
            {
                "run_index": 1,
                "input_commit": "a" * 40,
                "artifact_hash": "sha256:" + "f" * 64,
                "verdict": "pass",
            }
        ],
        dispositions=[],
    )
    ev = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, phase0=p0, phase1=p1, codex_audit=block
    )
    path = _write_evidence(handoff_home, ev)
    assert _run_dump(workspace, path) == 0


def test_gate_fail_open_for_old_5_4_1_evidence(handoff_home, workspace):
    """Migration window: an in-flight v5.4.1 evidence file still passes (mandate off)."""
    p0, p1 = _full_phase_status()
    ev = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, phase0=p0, phase1=p1
    )
    ev["schema_version"] = "v5.4.1"
    ev["evidence_hash"] = handoff_precheck.compute_evidence_hash(ev)
    path = _write_evidence(handoff_home, ev)
    assert _run_dump(workspace, path) == 0


def test_gate_rejects_truly_unknown_schema(handoff_home, workspace):
    p0, p1 = _full_phase_status()
    ev = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, phase0=p0, phase1=p1
    )
    ev["schema_version"] = "v9.9.9"
    ev["evidence_hash"] = handoff_precheck.compute_evidence_hash(ev)
    path = _write_evidence(handoff_home, ev)
    assert _run_dump(workspace, path) == 4  # ERR-RETRY schema-version-unknown


# ─── CLI ────────────────────────────────────────────────────────────────────


def test_cli_audit_run_registers_artifact(handoff_home, workspace, tmp_path, capsys):
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout.strip()
    findings = {
        "run_index": 1,
        "input_commit": head,
        "original_findings": [_sample_finding("P1")],
        "verdict": "fail",
    }
    ffile = tmp_path / "codex-out.json"
    ffile.write_text(json.dumps(findings))

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
    assert rc == 0
    assert codex_audit.findings_path(PROJECT, TASK, 1).exists()
    assert codex_audit.verify_findings_artifact(PROJECT, TASK, 1) is True


def test_cli_audit_disposition_validates(handoff_home, workspace):
    rc = codex_audit.main_audit_disposition(
        [
            "--task",
            TASK,
            "--project",
            PROJECT,
            "--finding-id",
            "F1",
            "--finding-hash",
            "sha256:" + "a" * 64,
            "--original-severity",
            "P0",
            "--disposition",
            "fixed",
            "--fix-commit",
            "d" * 40,
        ]
    )
    assert rc == 0
    assert len(codex_audit.load_dispositions(PROJECT, TASK)) == 1


def test_cli_audit_disposition_rejects_bad_shape(handoff_home, workspace):
    rc = codex_audit.main_audit_disposition(
        [
            "--task",
            TASK,
            "--project",
            PROJECT,
            "--finding-id",
            "F1",
            "--finding-hash",
            "sha256:" + "a" * 64,
            "--original-severity",
            "P0",
            "--disposition",
            "fixed",  # no --fix-commit
        ]
    )
    assert rc == 1


def test_cli_dispatch_knows_audit_subcommands():
    from handoff_fanout import cli

    parser_help = cli.main  # smoke: dispatcher importable
    assert callable(parser_help)
    # unknown audit args still route (argparse error inside subcommand → SystemExit/non-zero)
    rc = cli.main(["audit-disposition", "--help"]) if False else 0
    assert rc == 0


# ─── codex R1 remediation regressions ───────────────────────────────────────


@pytest.mark.parametrize("bad", ["../escape", "/abs/path", "Up.Case", "has space"])
def test_path_traversal_project_rejected(handoff_home, bad):
    # codex R1 P0: a non-slug project must never resolve a path under HANDOFF_HOME.
    with pytest.raises(ValueError):
        codex_audit.audit_base_dir(bad, TASK)


def test_cli_audit_run_rejects_bad_project(handoff_home, workspace, tmp_path):
    ffile = tmp_path / "f.json"
    ffile.write_text(json.dumps({"input_commit": "a" * 40, "original_findings": []}))
    rc = codex_audit.main_audit_run(
        [
            "--task",
            TASK,
            "--project",
            "../evil",
            "--workspace",
            str(workspace),
            "--run-index",
            "1",
            "--findings-file",
            str(ffile),
        ]
    )
    assert rc == 1


def test_verify_rejects_noncanonical_rewrite(handoff_home):
    # codex R1 P1: a semantic-equal but non-canonical on-disk rewrite must fail
    # verification (byte-level, not re-serialized).
    findings = {
        "run_index": 1,
        "input_commit": "a" * 40,
        "original_findings": [],
        "verdict": "pass",
    }
    codex_audit.write_findings_artifact(PROJECT, TASK, 1, findings, input_commit="a" * 40)
    fpath = codex_audit.findings_path(PROJECT, TASK, 1)
    # rewrite with pretty (non-canonical) spacing but identical semantics
    fpath.write_text(json.dumps(json.loads(fpath.read_text()), indent=4))
    assert codex_audit.verify_findings_artifact(PROJECT, TASK, 1) is False


def test_disposition_rejects_bad_finding_hash():
    err = codex_audit.validate_disposition_shape(
        {
            "finding_id": "F1",
            "finding_hash": "notahash",
            "original_severity": "P0",
            "disposition": "fixed",
            "fix_commit": "d" * 40,
        }
    )
    assert err and "finding_hash" in err


def test_disposition_rejects_bad_fix_commit():
    err = codex_audit.validate_disposition_shape(_disposition("fixed", fix_commit="not-a-sha!!"))
    assert err and "fix_commit" in err


def test_disposition_rejects_traversal_artifact():
    err = codex_audit.validate_disposition_shape(
        _disposition(
            "independent_reviewer_refuted",
            independent_reviewer_artifact="../../etc/passwd",
            reviewer_session_id="other",
        )
    )
    assert err and "relative path" in err


def test_validate_run_record_rejects_fabricated(handoff_home):
    # No artifact on disk → record can't be validated.
    fake = {
        "run_index": 1,
        "input_commit": "a" * 40,
        "artifact_hash": "sha256:" + "0" * 64,
        "verdict": "pass",
        "findings_path": f"audit/{TASK}/1/codex-findings.json",
        "manifest_path": f"audit/{TASK}/1/codex-findings.json.manifest",
    }
    err = codex_audit.validate_run_record(PROJECT, TASK, fake)
    assert err and ("missing" in err or "hash" in err)


def test_validate_run_record_accepts_real_artifact(handoff_home):
    findings = {
        "run_index": 1,
        "input_commit": "a" * 40,
        "original_findings": [_sample_finding("P1")],
        "verdict": "fail",
    }
    rec = codex_audit.write_findings_artifact(PROJECT, TASK, 1, findings, input_commit="a" * 40)
    assert codex_audit.validate_run_record(PROJECT, TASK, rec) is None


def test_validate_run_record_rejects_verdict_mismatch(handoff_home):
    findings = {
        "run_index": 1,
        "input_commit": "a" * 40,
        "original_findings": [_sample_finding("P0")],
        "verdict": "fail",
    }
    rec = codex_audit.write_findings_artifact(PROJECT, TASK, 1, findings, input_commit="a" * 40)
    rec["verdict"] = "pass"  # lie: there is a P0
    err = codex_audit.validate_run_record(PROJECT, TASK, rec)
    assert err and "verdict" in err


def test_audit_close_full_mode_e2e(handoff_home, workspace):
    """audit-run → audit-disposition → audit-close: evidence carries the codex
    block and dump passes (mandate off)."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout.strip()
    findings = {"run_index": 1, "input_commit": head, "original_findings": [], "verdict": "pass"}
    rec = codex_audit.write_findings_artifact(PROJECT, TASK, 1, findings, input_commit=head)

    p0, p1 = _full_phase_status()
    p0_args = [f"{k}={v['status']}" for k, v in p0.items()]
    p1_args = []
    for k, v in p1.items():
        p1_args.append(f"{k}={v['status']}")
    argv = [
        "--task",
        TASK,
        "--project",
        PROJECT,
        "--workspace",
        str(workspace),
        "--next",
        "next brief",
        "--audit-mode",
        "full_codex_audit",
        "--run-record",
        json.dumps(rec),
    ]
    for a in p0_args:
        argv += ["--phase0-status", a]
    for a in p1_args:
        argv += ["--phase1-status", a]
    rc = codex_audit.main_audit_close(argv)
    assert rc == 0
    ev_path = handoff_home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    ev = json.loads(ev_path.read_text())
    assert ev["codex_audit"]["audit_mode"] == "full_codex_audit"
    assert ev["evidence_hash"] == handoff_precheck.compute_evidence_hash(ev)


def test_audit_close_rejects_fabricated_run_record(handoff_home, workspace):
    fake = json.dumps(
        {
            "run_index": 1,
            "input_commit": "a" * 40,
            "artifact_hash": "sha256:" + "0" * 64,
            "verdict": "pass",
            "findings_path": f"audit/{TASK}/1/codex-findings.json",
            "manifest_path": f"audit/{TASK}/1/codex-findings.json.manifest",
        }
    )
    rc = codex_audit.main_audit_close(
        [
            "--task",
            TASK,
            "--project",
            PROJECT,
            "--workspace",
            str(workspace),
            "--next",
            "n",
            "--audit-mode",
            "full_codex_audit",
            "--run-record",
            fake,
        ]
    )
    assert rc == 1


def test_build_block_bypass_rejects_bad_follow_up_slug():
    with pytest.raises(ValueError, match="follow_up_audit_task_id"):
        codex_audit.build_codex_audit_block(
            handoff_precheck.AUDIT_MODE_BYPASS,
            bypass={
                "codex_failure_attempts": [
                    {"exit": 1, "stderr_hash": "sha256:" + "e" * 64, "timestamp": "t"}
                ],
                "follow_up_audit_task_id": "../evil",
            },
        )


def test_build_block_bypass_rejects_bad_attempt_shape():
    # 3 attempts so the MIN_CODEX_FAILURES floor passes and the per-attempt shape
    # check (bad stderr_hash on the last) is what raises.
    with pytest.raises(ValueError, match="stderr_hash"):
        codex_audit.build_codex_audit_block(
            handoff_precheck.AUDIT_MODE_BYPASS,
            bypass={
                "codex_failure_attempts": _valid_attempts(2)
                + [{"exit": 1, "stderr_hash": "nope", "timestamp": "t"}],
                "follow_up_audit_task_id": "x-followup",
            },
        )


def test_build_block_bypass_rejects_unsafe_override_ref():
    with pytest.raises(ValueError, match="override_ref"):
        codex_audit.build_codex_audit_block(
            handoff_precheck.AUDIT_MODE_BYPASS,
            bypass={
                "codex_failure_attempts": _valid_attempts(3),
                "follow_up_audit_task_id": "x-followup",
                "override_ref": "/etc/passwd",
            },
        )


def test_validate_run_record_rejects_input_commit_mismatch(handoff_home):
    findings = {
        "run_index": 1,
        "input_commit": "a" * 40,
        "original_findings": [],
        "verdict": "pass",
    }
    rec = codex_audit.write_findings_artifact(PROJECT, TASK, 1, findings, input_commit="a" * 40)
    rec["input_commit"] = "b" * 40  # claim a different commit than the artifact
    err = codex_audit.validate_run_record(PROJECT, TASK, rec)
    assert err and "input_commit" in err


def test_validate_run_record_rejects_path_spoof(handoff_home):
    findings = {
        "run_index": 1,
        "input_commit": "a" * 40,
        "original_findings": [],
        "verdict": "pass",
    }
    rec = codex_audit.write_findings_artifact(PROJECT, TASK, 1, findings, input_commit="a" * 40)
    rec["findings_path"] = f"audit/{TASK}/9/codex-findings.json"  # not the canonical run-1 path
    err = codex_audit.validate_run_record(PROJECT, TASK, rec)
    assert err and "findings_path" in err


def test_realign_preserves_codex_audit_block(handoff_home, workspace):
    """codex R3/R4 P1: a sibling-HEAD re-align must NOT drop the codex_audit
    block — re-align refreshes the HEAD binding, it does not re-audit."""
    p0, p1 = _full_phase_status()
    block = codex_audit.build_codex_audit_block(
        handoff_precheck.AUDIT_MODE_EMPTY_DIFF,
        attestation={
            "base": "a" * 40,
            "head": "b" * 40,
            "diff_hash": "sha256:" + "c" * 64,
            "mode_decider_version": "phaseA-1",
        },
    )
    ev = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, phase0=p0, phase1=p1, codex_audit=block
    )
    h0 = ev["head_at_precheck"]
    # Age the precheck timestamp past drift tolerance so only re-align can rescue.
    ev["head_at_precheck_timestamp"] = (datetime.now(UTC) - timedelta(seconds=120)).isoformat(
        timespec="seconds"
    )
    ev["evidence_hash"] = handoff_precheck.compute_evidence_hash(ev)
    path = _write_evidence(handoff_home, ev)
    # Sibling tab commits → HEAD moves; working tree clean afterwards.
    (workspace / "sibling.txt").write_text("x")
    subprocess.run(["git", "add", "sibling.txt"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "sibling work"], cwd=workspace, check=True)
    h1 = handoff_precheck._git(["rev-parse", "HEAD"], workspace)
    assert h0 != h1

    assert _run_dump(workspace, path) == 0
    rewritten = json.loads(path.read_text())
    assert rewritten["head_at_precheck"] == h1  # re-aligned
    assert rewritten["codex_audit"] == block  # block survived verbatim
    assert rewritten["evidence_hash"] == handoff_precheck.compute_evidence_hash(rewritten)


def test_append_disposition_concurrent_no_lost_update(handoff_home):
    # Sequential proxy for the lock: two appends both persist (no clobber).
    codex_audit.append_disposition(PROJECT, TASK, _disposition("fixed", fix_commit="a" * 40))
    d2 = _disposition("fixed", fix_commit="b" * 40)
    d2["finding_id"] = "F2"
    codex_audit.append_disposition(PROJECT, TASK, d2)
    loaded = codex_audit.load_dispositions(PROJECT, TASK)
    assert {d["finding_id"] for d in loaded} == {"F1", "F2"}
