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
    expected = (
        "sha256:" + hashlib.sha256(f"{TASK}\n{FHASH}\n{nonce}\n{approved}".encode()).hexdigest()
    )
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
    assert any(e.get("event") == "owner-ack-written" and e["finding_hash"] == FHASH for e in lines)


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


def test_g7_override_tampered_expiry_window_blocked(handoff_home, tmp_path, monkeypatch):
    # R1-P1: expires_at is NOT covered by the token. A real, EXPIRED ack (approved
    # 8 days ago) whose expires_at is hand-edited far into the future would be
    # self-consistent on the token but must be caught by the approved+TTL binding.
    ws = _ws(tmp_path, monkeypatch)
    past = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    future = (datetime.now(UTC) + timedelta(days=365)).isoformat(timespec="seconds")
    out = _gate_override(
        handoff_home,
        ws,
        write_ack={"approved_at": past},
        disp_overrides={"expires_at": future},
    )
    assert out.klass == "blocked"
    assert out.subcode == "codex-audit-override-invalid"


def test_g7_override_disk_ack_tampered_expiry_blocked(handoff_home, tmp_path, monkeypatch):
    # Same attack but tampering the ON-DISK artifact's expires_at directly.
    ws = _ws(tmp_path, monkeypatch)
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
    past = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    nonce = "n1"
    codex_audit.write_owner_ack(PROJECT_WS, TASK, fhash, "bug", nonce, past, "exempt")
    # tamper the on-disk expires_at into the future (token unchanged → still self-consistent)
    ack_path = codex_audit.owner_ack_path(PROJECT_WS, TASK, fhash)
    data = json.loads(ack_path.read_text())
    data["expires_at"] = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    ack_path.write_text(json.dumps(data, sort_keys=True))
    token = codex_audit.compute_owner_ack_token(TASK, fhash, nonce, past)
    disp = {
        "finding_id": "F1",
        "finding_hash": fhash,
        "original_severity": "P0",
        "disposition": "owner_override",
        "owner_ack_token": token,
        "expires_at": data["expires_at"],
    }
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    p0 = {k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS}
    p1 = {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS}
    payload = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT_WS, workspace=ws, phase0=p0, phase1=p1, codex_audit=block
    )
    out = codex_audit.evaluate_audit_gate(payload, ws, PROJECT_WS, TASK)
    assert out.klass == "blocked"
    assert out.subcode == "codex-audit-override-invalid"


def test_gate_bypass_enforces_min_codex_failures(handoff_home):
    # R1-P1: the gate must enforce the SAME MIN_CODEX_FAILURES floor as the
    # producer, so a hand-crafted evidence can't bypass with 1-2 failures.
    assert codex_audit.BYPASS_MIN_FAILURES == codex_audit.MIN_CODEX_FAILURES == 3
    for n in (1, 2):
        block = {
            "audit_mode": "codex_unavailable_bypass",
            "codex_failure_attempts": [
                {
                    "exit": 1,
                    "stderr_hash": "sha256:" + "0" * 64,
                    "timestamp": f"2026-05-30T0{i}:00:00+00:00",
                }
                for i in range(n)
            ],
            "follow_up_audit_task_id": "redo-audit-x",
        }
        out = codex_audit._gate_bypass(block)
        assert out.klass == "bypass"
        assert out.subcode == "codex-audit-bypass-no-failure-proof"
    # 3 is accepted
    block3 = {
        "audit_mode": "codex_unavailable_bypass",
        "codex_failure_attempts": [
            {
                "exit": 1,
                "stderr_hash": "sha256:" + "0" * 64,
                "timestamp": f"2026-05-30T0{i}:00:00+00:00",
            }
            for i in range(3)
        ],
        "follow_up_audit_task_id": "redo-audit-x",
    }
    assert codex_audit._gate_bypass(block3).ok


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


# ─── Phase D rollout — cross-repo evidence anchor ────────────────────────────
# G0 must bind the AUDITED code repo's HEAD, not the launching workspace's HEAD.
# A cross-repo handoff (code audited in repo X, dump launched from workspace Y)
# would otherwise be false-rejected 100% of the time the moment mandate flips on,
# because the gate computed head_now from the launching workspace.


def _init_git_repo(path: Path, marker: str = "") -> Path:
    """Create a one-commit git repo at ``path`` and return it (no chdir).

    ``marker`` distinguishes the committed content so two repos created in the
    same second get DIFFERENT HEAD SHAs (git commit hashes are deterministic over
    tree+author+timestamp; identical content in the same second would collide).
    """
    path.mkdir(parents=True)
    for cmd in (
        ["git", "init", "--quiet", "--initial-branch=main"],
        ["git", "config", "user.email", "t@t.test"],
        ["git", "config", "user.name", "t"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(cmd, cwd=path, check=True)
    (path / "README.md").write_text(f"test {path.name} {marker}\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"init {path.name}"], cwd=path, check=True)
    return path


def _evidence_with_full_audit(
    *, input_commit, code_repo, project, task, workspace, audit_mode="full_codex_audit"
):
    """A clean ``full_codex_audit`` evidence payload (one finding-free run).

    ``code_repo`` (str abs path) is added to the block iff not None; absent →
    same-repo evidence, byte-identical to today's (backward-compat). When
    ``code_repo`` is given, ``code_repo_head`` is stamped from its live HEAD
    (mirrors build_codex_audit_block so the gate's cross-repo head check passes).
    """
    rec = codex_audit.write_findings_artifact(
        project,
        task,
        1,
        {"run_index": 1, "input_commit": input_commit, "original_findings": []},
        input_commit=input_commit,
    )
    block = {"audit_mode": audit_mode, "audit_runs": [rec], "dispositions": []}
    if code_repo is not None:
        block["code_repo"] = code_repo
        # stamp the real HEAD when code_repo is a usable git repo; otherwise fall
        # back to input_commit (the gate rejects invalid code_repo before the head
        # check, so the value is irrelevant for those negative tests).
        try:
            block["code_repo_head"] = _head(Path(code_repo))
        except (subprocess.CalledProcessError, OSError):
            block["code_repo_head"] = input_commit
    p0 = {k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS}
    p1 = {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS}
    return handoff_precheck.build_evidence(
        task_id=task,
        project=project,
        workspace=workspace,
        phase0=p0,
        phase1=p1,
        codex_audit=block,
    )


def test_cross_repo_anchor_binds_code_repo_head(handoff_home, tmp_path):
    # workspace = launching repo (e.g. erp-system); code_repo = audited repo.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code")
    code_head = _head(code_repo)
    # sanity: the two repos have DIFFERENT heads, so binding the wrong one fails.
    assert code_head != _head(workspace)

    payload = _evidence_with_full_audit(
        input_commit=code_head,
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-cross",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-cross")
    assert outcome.ok, (outcome.klass, outcome.subcode, outcome.detail)


def test_same_repo_absent_code_repo_unchanged(handoff_home, tmp_path):
    # Backward-compat: no code_repo → gate uses workspace exactly as before.
    workspace = _init_git_repo(tmp_path / "ws")
    head = _head(workspace)
    payload = _evidence_with_full_audit(
        input_commit=head,
        code_repo=None,
        project=PROJECT,
        task="t-same",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-same")
    assert outcome.ok, (outcome.klass, outcome.subcode, outcome.detail)


def test_invalid_code_repo_is_retry(handoff_home, tmp_path):
    workspace = _init_git_repo(tmp_path / "ws")
    payload = _evidence_with_full_audit(
        input_commit=_head(workspace),
        code_repo=str(tmp_path / "does-not-exist"),
        project=PROJECT,
        task="t-bad",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-bad")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-invalid"


def test_non_string_code_repo_is_retry(handoff_home, tmp_path):
    # A non-string truthy code_repo (e.g. a list) must fail closed, not crash.
    workspace = _init_git_repo(tmp_path / "ws")
    payload = _evidence_with_full_audit(
        input_commit=_head(workspace),
        code_repo=None,
        project=PROJECT,
        task="t-nonstr",
        workspace=workspace,
    )
    payload["codex_audit"]["code_repo"] = ["/not/a/string"]
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-nonstr")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-invalid"


def test_relative_code_repo_is_retry(handoff_home, tmp_path):
    # A relative code_repo path must be rejected (the gate requires an abs path).
    workspace = _init_git_repo(tmp_path / "ws")
    payload = _evidence_with_full_audit(
        input_commit=_head(workspace),
        code_repo=None,
        project=PROJECT,
        task="t-rel",
        workspace=workspace,
    )
    payload["codex_audit"]["code_repo"] = "relative/path"
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-rel")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-invalid"


def test_code_repo_not_a_git_repo_is_retry(handoff_home, tmp_path):
    # An existing abs dir that is NOT a git repo must fail closed (R1 hardening:
    # a malicious code_repo can't point at an arbitrary clean directory).
    workspace = _init_git_repo(tmp_path / "ws")
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    payload = _evidence_with_full_audit(
        input_commit=_head(workspace),
        code_repo=str(plain_dir),
        project=PROJECT,
        task="t-nogit",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-nogit")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-invalid"


def test_config_parses_audit_code_repos_and_filters_junk(tmp_path):
    # config layer: audit_code_repos parses to a list[str], dropping non-string /
    # empty entries (fail-safe so a malformed allowlist can't crash the gate).
    from handoff_fanout import config as _cfg

    home = tmp_path / "h"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"audit_code_repos": ["/abs/repo", "", 123, None, "/abs/two"]}),
        encoding="utf-8",
    )
    cfg = _cfg.load(home)
    assert cfg.audit_code_repos == ["/abs/repo", "/abs/two"]
    # absent key → empty list (unconfigured = opt-in OFF)
    (home / "config.json").write_text(json.dumps({}), encoding="utf-8")
    assert _cfg.load(home).audit_code_repos == []


def _write_audit_allowlist(home, repos):
    """Write config.json with an audit_code_repos allowlist into the handoff home."""
    (home / "config.json").write_text(
        json.dumps({"audit_code_repos": [str(r) for r in repos]}), encoding="utf-8"
    )


def test_code_repo_allowlist_allows_listed_repo(handoff_home, tmp_path):
    # opt-in repo-identity allowlist (codex R1/R3 P1): a code_repo ON the list passes.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="allow-ok")
    _write_audit_allowlist(handoff_home, [code_repo])
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-allow-ok",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-allow-ok")
    assert outcome.ok, (outcome.klass, outcome.subcode, outcome.detail)


def test_code_repo_not_in_allowlist_rejected(handoff_home, tmp_path):
    # a code_repo NOT on a configured allowlist is rejected (wrong-repo selector closed).
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="not-allowed")
    other_repo = _init_git_repo(tmp_path / "other", marker="the-only-allowed")
    _write_audit_allowlist(handoff_home, [other_repo])  # code_repo is NOT listed
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-not-allowed",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-not-allowed")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-not-allowed"


def test_code_repo_unconfigured_allowlist_is_unrestricted(handoff_home, tmp_path):
    # backward-compat: no allowlist configured → any valid code_repo is accepted
    # (the single-user friction + disclaimer still apply; this is opt-in).
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="no-allowlist")
    # no config.json written → allowlist empty
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-no-allowlist",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-no-allowlist")
    assert outcome.ok, (outcome.klass, outcome.subcode, outcome.detail)


def test_code_repo_allowlist_resolves_symlink(handoff_home, tmp_path):
    # a symlink pointing at an allowed repo is normalized (realpath) → accepted,
    # so the allowlist can't be evaded or falsely-rejected via a symlink alias.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="symlink")
    link = tmp_path / "code-link"
    link.symlink_to(code_repo)
    _write_audit_allowlist(handoff_home, [code_repo])  # list the real path
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(link),  # evidence names the symlink
        project=PROJECT,
        task="t-symlink",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-symlink")
    assert outcome.ok, (outcome.klass, outcome.subcode, outcome.detail)


def test_resolve_audit_ws_returns_resolved_path_not_symlink(handoff_home, tmp_path):
    # codex P1-1 (TOCTOU): the gate must operate on the RESOLVED realpath, not the
    # symlink it was handed, so a symlink can't be repointed between check and use.
    code_repo = _init_git_repo(tmp_path / "code", marker="resolved-return")
    link = tmp_path / "code-link"
    link.symlink_to(code_repo)
    block = {"code_repo": str(link)}
    audit_ws, err = codex_audit._resolve_audit_ws(block, tmp_path / "ws")
    assert err is None
    assert audit_ws == code_repo.resolve()  # resolved, not the symlink path


def test_allowlist_present_but_all_invalid_fails_closed(handoff_home, tmp_path):
    # codex P1-2 (fail-open): an audit_code_repos KEY present but yielding no valid
    # entries means the owner INTENDED a restriction but mis-wrote it → fail closed
    # for cross-repo, not silently degrade to unrestricted.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="all-junk")
    (handoff_home / "config.json").write_text(
        json.dumps({"audit_code_repos": ["", 123, None]}), encoding="utf-8"
    )
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-junklist",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-junklist")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-not-allowed"


def test_allowlist_absent_key_stays_unrestricted(handoff_home, tmp_path):
    # opt-in default: a config.json WITHOUT the audit_code_repos key (or no config
    # at all) leaves cross-repo unrestricted (distinguishes absent from empty).
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="absent-key")
    (handoff_home / "config.json").write_text(
        json.dumps({"workspace_root": "~/Projects"}), encoding="utf-8"
    )
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-absentkey",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-absentkey")
    assert outcome.ok, (outcome.klass, outcome.subcode, outcome.detail)


def _root_sha(repo: Path) -> str:
    """Root-commit SHA of ``repo`` (the path-independent repo identity)."""
    out = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.splitlines()[0].strip()


def _write_audit_root_allowlist(home, roots, repos=None):
    """Write config.json with an audit_code_repo_roots allowlist (and optional paths)."""
    data = {"audit_code_repo_roots": [str(r) for r in roots]}
    if repos is not None:
        data["audit_code_repos"] = [str(r) for r in repos]
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")


def test_code_repo_root_allowlist_allows_listed_root(handoff_home, tmp_path):
    # opt-in root-SHA identity allowlist (Phase D P1 / owner ruling): a code_repo
    # whose root-commit SHA is listed passes.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="root-ok")
    _write_audit_root_allowlist(handoff_home, [_root_sha(code_repo)])
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-root-ok",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-root-ok")
    assert outcome.ok, (outcome.klass, outcome.subcode, outcome.detail)


def test_code_repo_root_not_in_allowlist_rejected(handoff_home, tmp_path):
    # a code_repo whose root SHA is NOT on a configured root allowlist is rejected.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="root-bad")
    other = _init_git_repo(tmp_path / "other", marker="the-only-allowed-root")
    _write_audit_root_allowlist(handoff_home, [_root_sha(other)])  # code_repo's root NOT listed
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-root-bad",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-root-bad")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-root-not-allowed"


def test_code_repo_root_allowlist_is_path_independent(handoff_home, tmp_path):
    # the value-add over the path allowlist: a repo moved to a DIFFERENT path still
    # passes (identity = root SHA, not location). Verify by listing the root SHA but
    # NOT the path, and naming the repo at its real (unlisted-as-path) location.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "moved-here", marker="path-indep")
    _write_audit_root_allowlist(handoff_home, [_root_sha(code_repo)])  # root only, no path
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-path-indep",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-path-indep")
    assert outcome.ok, (outcome.klass, outcome.subcode, outcome.detail)


def test_code_repo_root_present_but_all_invalid_fails_closed(handoff_home, tmp_path):
    # mirror the path allowlist fail-closed (codex P1-2): a root KEY present but
    # yielding no valid entries means the owner intended a restriction → fail closed.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="root-junk")
    (handoff_home / "config.json").write_text(
        json.dumps({"audit_code_repo_roots": ["", 123, None]}), encoding="utf-8"
    )
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-root-junk",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-root-junk")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-root-not-allowed"


def test_code_repo_root_absent_key_stays_unrestricted(handoff_home, tmp_path):
    # opt-in default: no audit_code_repo_roots key → no root restriction.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="root-absent")
    (handoff_home / "config.json").write_text(
        json.dumps({"workspace_root": "~/Projects"}), encoding="utf-8"
    )
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-root-absent",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-root-absent")
    assert outcome.ok, (outcome.klass, outcome.subcode, outcome.detail)


def test_code_repo_path_and_root_both_configured_both_must_pass(handoff_home, tmp_path):
    # both allowlists configured → independent gates, BOTH must pass (never weakens).
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="both-gates")
    other = _init_git_repo(tmp_path / "other", marker="other-root")
    # path matches but root is some OTHER repo's root → must REJECT (root gate fails).
    _write_audit_root_allowlist(handoff_home, [_root_sha(other)], repos=[code_repo])
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-both",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-both")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-root-not-allowed"
    # now list BOTH correctly → passes.
    _write_audit_root_allowlist(handoff_home, [_root_sha(code_repo)], repos=[code_repo])
    outcome2 = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-both")
    assert outcome2.ok, (outcome2.klass, outcome2.subcode, outcome2.detail)


def test_code_repo_multi_root_requires_all_roots_listed(handoff_home, tmp_path):
    # codex P1: a repo that merged unrelated history has >1 root. Listing only ONE
    # allowed root must NOT let it pass (subset semantics) — else an attacker grafts
    # an allowed root onto its own unlisted history. Listing ALL roots → passes.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="multi-root-A")
    root_a = _root_sha(code_repo)
    # second, unrelated root via an orphan branch, then merge into main.
    subprocess.run(["git", "checkout", "--orphan", "side"], cwd=code_repo, check=True)
    (code_repo / "SIDE.md").write_text("unrelated B\n")
    subprocess.run(["git", "add", "SIDE.md"], cwd=code_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "side root B"], cwd=code_repo, check=True)
    root_b = _root_sha(code_repo)  # on 'side' this is B
    subprocess.run(["git", "checkout", "main"], cwd=code_repo, check=True)
    subprocess.run(
        ["git", "merge", "--allow-unrelated-histories", "--no-edit", "side"],
        cwd=code_repo,
        check=True,
    )
    assert root_a != root_b
    head = _head(code_repo)

    # only root_a listed → subset fails → reject.
    _write_audit_root_allowlist(handoff_home, [root_a])
    payload = _evidence_with_full_audit(
        input_commit=head,
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-multiroot",
        workspace=workspace,
    )
    out1 = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-multiroot")
    assert out1.klass == "retry"
    assert out1.subcode == "codex-audit-code-repo-root-not-allowed"

    # both roots listed → subset holds → pass.
    _write_audit_root_allowlist(handoff_home, [root_a, root_b])
    out2 = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-multiroot")
    assert out2.ok, (out2.klass, out2.subcode, out2.detail)


def test_code_repo_graft_cannot_fake_root(handoff_home, tmp_path):
    # codex R2 P1: a repo-local .git/info/grafts file can rewrite parentage so HEAD
    # appears rooted at an allowlisted SHA it doesn't truly descend from. The gate must
    # neutralize grafts (GIT_GRAFT_FILE=/dev/null) and bind the TRUE root.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="graft")
    root_a = _root_sha(code_repo)  # true root A (current HEAD)
    (code_repo / "B.md").write_text("b\n")
    subprocess.run(["git", "add", "B.md"], cwd=code_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "B"], cwd=code_repo, check=True)
    # allowlisted decoy root X via an orphan branch (no parents)
    subprocess.run(["git", "checkout", "--orphan", "decoy"], cwd=code_repo, check=True)
    (code_repo / "X.md").write_text("x\n")
    subprocess.run(["git", "add", "X.md"], cwd=code_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "X decoy root"], cwd=code_repo, check=True)
    root_x = _root_sha(code_repo)
    subprocess.run(["git", "checkout", "main"], cwd=code_repo, check=True)
    head = _head(code_repo)
    # plant a graft: A's parent := X → with grafts honored, the root becomes X.
    (code_repo / ".git" / "info").mkdir(exist_ok=True)
    (code_repo / ".git" / "info" / "grafts").write_text(f"{root_a} {root_x}\n")
    raw_roots = {
        s.strip().lower()
        for s in subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=code_repo,
            capture_output=True,
            text=True,
        ).stdout.split()
    }
    if root_x.lower() not in raw_roots:
        pytest.skip("this git build does not honor .git/info/grafts; bypass vector absent")
    # grafts honored → WITHOUT the defense the gate would see X (allowlisted) and PASS.
    _write_audit_root_allowlist(handoff_home, [root_x])  # only the decoy is listed
    payload = _evidence_with_full_audit(
        input_commit=head,
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-graft",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-graft")
    # WITH GIT_GRAFT_FILE=/dev/null the gate sees the TRUE root A (unlisted) → reject.
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-root-not-allowed"


def test_code_repo_shallow_repo_rejected(handoff_home, tmp_path):
    # codex R3 P1: a shallow repo treats its .git/shallow boundary as a root, so its
    # true root can't be established — the identity gate must reject it outright.
    src = _init_git_repo(tmp_path / "src", marker="shallow-src")
    (src / "B.md").write_text("b\n")
    subprocess.run(["git", "add", "B.md"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "B"], cwd=src, check=True)
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth=1", f"file://{src}", str(shallow)],
        check=True,
    )
    # sanity: the clone really is shallow.
    is_shallow = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=shallow,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert is_shallow == "true"
    workspace = _init_git_repo(tmp_path / "launcher")
    # allowlist the shallow boundary's apparent root — without the shallow guard it
    # would pass; with it, the repo is rejected as shallow.
    boundary = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=shallow,
        capture_output=True,
        text=True,
    ).stdout.split()
    _write_audit_root_allowlist(handoff_home, boundary)
    payload = _evidence_with_full_audit(
        input_commit=_head(shallow),
        code_repo=str(shallow),
        project=PROJECT,
        task="t-shallow",
        workspace=workspace,
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-shallow")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-shallow"


def test_config_parses_audit_code_repo_roots_and_filters_junk(tmp_path):
    from handoff_fanout import config as _cfg

    home = tmp_path / "hh"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"audit_code_repo_roots": ["abc123", "", 123, None, "DEF456"]}),
        encoding="utf-8",
    )
    cfg = _cfg.load(home)
    assert cfg.audit_code_repo_roots == ["abc123", "DEF456"]
    assert cfg.audit_code_roots_configured is True
    # absent key → unconfigured
    (home / "config.json").write_text(json.dumps({"workspace_root": "~/x"}), encoding="utf-8")
    cfg2 = _cfg.load(home)
    assert cfg2.audit_code_repo_roots == []
    assert cfg2.audit_code_roots_configured is False


def test_cross_repo_missing_code_repo_head_is_retry(handoff_home, tmp_path):
    # codex R2/R4: a cross-repo block MUST carry code_repo_head; absent → retry.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="missing-head")
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-nohead",
        workspace=workspace,
    )
    del payload["codex_audit"]["code_repo_head"]
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-nohead")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-head-mismatch"


def test_cross_repo_stale_code_repo_head_is_retry(handoff_home, tmp_path):
    # codex R3 friction: code_repo_head must equal the live code_repo HEAD.
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="stale-head")
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-stalehead",
        workspace=workspace,
    )
    payload["codex_audit"]["code_repo_head"] = "0" * 40  # not the live HEAD
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-stalehead")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-code-repo-head-mismatch"


def test_docs_only_not_allowed_cross_repo(handoff_home, tmp_path):
    # codex R1: docs_only_light_audit derives its diff base from the LAUNCHER's
    # session_commits, unrelated to code_repo → forbidden cross-repo (full only).
    workspace = _init_git_repo(tmp_path / "launcher")
    code_repo = _init_git_repo(tmp_path / "code", marker="docs-only")
    payload = _evidence_with_full_audit(
        input_commit=_head(code_repo),
        code_repo=str(code_repo),
        project=PROJECT,
        task="t-docsx",
        workspace=workspace,
        audit_mode="docs_only_light_audit",
    )
    outcome = codex_audit.evaluate_audit_gate(payload, workspace, PROJECT, "t-docsx")
    assert outcome.klass == "retry"
    assert outcome.subcode == "codex-audit-docs-only-cross-repo"


def test_audit_run_code_repo_sources_input_commit(handoff_home, tmp_path, monkeypatch, capsys):
    # audit-run --code-repo (no --input-commit) records the CODE repo's HEAD,
    # not the launching workspace's, as the run's input_commit.
    workspace = _ws(tmp_path, monkeypatch)  # chdir'd launcher
    code_repo = _init_git_repo(tmp_path / "code", marker="audit-run")
    code_head = _head(code_repo)
    assert code_head != _head(workspace)

    findings = {"run_index": 1, "original_findings": []}
    ffile = tmp_path / "findings.json"
    ffile.write_text(json.dumps(findings))
    rc = codex_audit.main_audit_run(
        [
            "--task",
            "t-crun",
            "--project",
            PROJECT_WS,
            "--workspace",
            str(workspace),
            "--run-index",
            "1",
            "--findings-file",
            str(ffile),
            "--code-repo",
            str(code_repo),
        ]
    )
    assert rc == 0, rc
    # the printed run record carries input_commit = the CODE repo HEAD.
    record = json.loads(capsys.readouterr().out.strip())
    assert record["input_commit"] == code_head


def test_audit_close_code_repo_writes_cross_repo_evidence(handoff_home, tmp_path, monkeypatch):
    # End-to-end: a cross-repo close stamps code_repo + code_repo_head into the
    # evidence, and that evidence passes the gate against the LAUNCHER workspace.
    workspace = _ws(tmp_path, monkeypatch)  # chdir'd launcher
    code_repo = _init_git_repo(tmp_path / "code", marker="audit-close")
    code_head = _head(code_repo)
    assert code_head != _head(workspace)

    rec = codex_audit.write_findings_artifact(
        PROJECT_WS,
        "t-cclose",
        1,
        {"run_index": 1, "input_commit": code_head, "original_findings": []},
        input_commit=code_head,
    )
    argv = [
        "--task",
        "t-cclose",
        "--project",
        PROJECT_WS,
        "--workspace",
        str(workspace),
        "--next",
        "next brief",
        "--audit-mode",
        "full_codex_audit",
        "--run-record",
        json.dumps(rec),
        "--code-repo",
        str(code_repo),
        "--status",
        "active",
    ]
    for k in handoff_precheck.PHASE0_KEYS:
        argv += ["--phase0-status", f"{k}=✅"]
    for k in handoff_precheck.PHASE1_KEYS:
        argv += ["--phase1-status", f"{k}=✅"]
    rc = codex_audit.main_audit_close(argv)
    assert rc == 0, rc

    evidence = json.loads(
        (handoff_precheck.precheck_dir(PROJECT_WS) / "t-cclose.retro.evidence.json").read_text()
    )
    assert evidence["codex_audit"]["code_repo"] == str(code_repo)
    assert evidence["codex_audit"]["code_repo_head"] == code_head
    # the written evidence passes the gate against the launcher workspace.
    outcome = codex_audit.evaluate_audit_gate(evidence, workspace, PROJECT_WS, "t-cclose")
    assert outcome.ok, (outcome.klass, outcome.subcode, outcome.detail)


def test_build_block_records_code_repo_and_head(tmp_path):
    # build_codex_audit_block stamps code_repo + code_repo_head when given a repo;
    # emits NEITHER key when absent (same-repo evidence stays byte-identical).
    code_repo = _init_git_repo(tmp_path / "code")
    code_head = _head(code_repo)
    rec = {"run_index": 1, "input_commit": code_head, "verdict": "pass"}

    with_repo = codex_audit.build_codex_audit_block(
        "full_codex_audit", audit_runs=[rec], code_repo=str(code_repo)
    )
    assert with_repo["code_repo"] == str(code_repo)
    assert with_repo["code_repo_head"] == code_head

    without = codex_audit.build_codex_audit_block("full_codex_audit", audit_runs=[rec])
    assert "code_repo" not in without
    assert "code_repo_head" not in without
