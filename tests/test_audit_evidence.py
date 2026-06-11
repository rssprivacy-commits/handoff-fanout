"""Unit tests for ``audit_evidence`` — the delivery-audit machine gate checker.

Covers the four decision paths (head-sha match / patch-id equivalence / RED-with-
owner-override / no-evidence FAIL), the fail-closed posture for MIXED-ERROR
verdicts, the tty-gated owner override CLI, and the one-time emergency bypass
(including the「bypass 不得清洗 RED」no-mix rule)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from handoff_fanout import audit_evidence
from handoff_fanout.audit_evidence import (
    BYPASS_ENV,
    CheckResult,
    check_range,
    derive_project,
    main_check,
    main_override,
    range_facts,
    validate_owner_ack,
)
from handoff_fanout.cli import main as cli_main


@pytest.fixture(autouse=True)
def _no_ambient_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BYPASS_ENV, raising=False)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, rel: str, content: str, msg: str) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo_with_range(git_repo: Path) -> tuple[Path, str, str]:
    """A repo with a base commit and one reviewed feature commit on top."""
    base = _commit(git_repo, "README.md", "seed\n", "seed")
    head = _commit(git_repo, "src/feature.py", "x = 1\n", "feature")
    return git_repo, base, head


def _write_evidence(audits: Path, name: str = "t", **fields) -> Path:
    audits.mkdir(parents=True, exist_ok=True)
    path = audits / f"{name}.evidence.json"
    data = {"schema_version": 1, "overall_verdict": "GREEN"}
    data.update(fields)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _owner_ack(patch_id: str, reason: str = "owner accepts residual risk") -> dict:
    ts = "2026-06-12T10:00:00+0800"
    checksum = hashlib.sha256(f"{patch_id}|{reason}|{ts}".encode()).hexdigest()
    return {"reason": reason, "ts": ts, "checksum": checksum}


# ─── decision path 1: head sha match ─────────────────────────────────────────


def test_pass_on_head_sha_match(repo_with_range, tmp_path):
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    _write_evidence(audits, reviewed_base_sha=base, reviewed_head_sha=head)
    r = check_range(repo, base, head, audits, env={})
    assert r.ok and r.status == "PASS"


# ─── decision path 2: patch-id equivalence (cherry-pick survives) ────────────


def test_pass_on_patch_id_after_cherry_pick(git_repo: Path, tmp_path):
    base = _commit(git_repo, "README.md", "seed\n", "seed")
    reviewed_head = _commit(git_repo, "src/feature.py", "x = 1\n", "feature")
    reviewed = range_facts(git_repo, base, reviewed_head)

    # Simulate the real-world case (审 ed2f295 → cherry-pick 成 ae37183): main moved on
    # with an unrelated commit, then the reviewed commit is cherry-picked → new SHA,
    # identical content.
    _git(git_repo, "checkout", "-q", base)
    _git(git_repo, "checkout", "-q", "-b", "rebased-main")
    drift = _commit(git_repo, "docs/other.md", "drift\n", "unrelated drift")
    _git(git_repo, "cherry-pick", reviewed_head)
    new_head = _git(git_repo, "rev-parse", "HEAD")
    assert new_head != reviewed_head

    audits = tmp_path / "audits"
    _write_evidence(
        audits,
        reviewed_head_sha=reviewed_head,
        reviewed_patch_id=reviewed.patch_id,
        changed_files=reviewed.changed_files,
    )
    r = check_range(git_repo, drift, new_head, audits, env={})
    assert r.ok and r.status == "PASS", r.reason


def test_fail_when_changed_files_differ(repo_with_range, tmp_path):
    """patch-id match alone is not enough — the changed-file sets must be identical."""
    repo, base, head = repo_with_range
    facts = range_facts(repo, base, head)
    audits = tmp_path / "audits"
    _write_evidence(
        audits,
        reviewed_patch_id=facts.patch_id,
        changed_files=["some/other/file.py"],
    )
    r = check_range(repo, base, head, audits, env={})
    assert not r.ok and "无匹配" in r.reason


# ─── head-sha path base binding (MUST-2: narrow audit ≠ wide range) ─────────


def test_narrow_evidence_does_not_clear_wider_range(git_repo: Path, tmp_path):
    """Evidence for head^..head (same head, narrower base) must NOT pass the
    head_sha direct path for an origin/main..head push range — the unreviewed
    middle commit would ride through unaudited."""
    root = _commit(git_repo, "README.md", "seed\n", "seed")
    middle = _commit(git_repo, "src/unreviewed.py", "evil = 1\n", "unreviewed middle")
    head = _commit(git_repo, "src/feature.py", "x = 1\n", "feature")
    narrow = range_facts(git_repo, middle, head)  # what the narrow audit saw

    audits = tmp_path / "audits"
    _write_evidence(
        audits,
        reviewed_base_sha=narrow.base_sha,
        reviewed_head_sha=narrow.head_sha,
        reviewed_patch_id=narrow.patch_id,
        diff_sha256=narrow.diff_sha256,
        changed_files=narrow.changed_files,
    )
    # wide range root..head: same head, different base → no head_sha pass-through,
    # and the patch-id path fails too (different diff)
    r = check_range(git_repo, root, head, audits, env={})
    assert not r.ok and "无匹配" in r.reason


def test_head_sha_passes_when_base_bound_and_equal(repo_with_range, tmp_path):
    repo, base, head = repo_with_range
    facts = range_facts(repo, base, head)
    audits = tmp_path / "audits"
    _write_evidence(audits, reviewed_base_sha=facts.base_sha, reviewed_head_sha=head)
    r = check_range(repo, base, head, audits, env={})
    assert r.ok and "head_sha" in r.reason


def test_head_sha_evidence_without_base_fails_closed(repo_with_range, tmp_path):
    """MUST-A (sw-ag-fix2): NO legacy evidence exists — the evidence-v1 runner
    (the first and only producer) has always emitted reviewed_base_sha alongside
    reviewed_head_sha, so a record missing the base is suspect, not legacy.
    head_sha alone must NOT take the direct path; with no patch-id either the
    record matches nothing."""
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    _write_evidence(audits, reviewed_head_sha=head)
    r = check_range(repo, base, head, audits, env={})
    assert not r.ok and "无匹配" in r.reason


# ─── same-base byte binding (MUST-3: patch-id ignores whitespace) ────────────


def test_same_base_whitespace_tamper_fails(git_repo: Path, tmp_path):
    """Same base, whitespace-only divergence: patch-id and changed_files both
    match, but diff_sha256 differs → no match (Python indentation IS semantics)."""
    base = _commit(git_repo, "README.md", "seed\n", "seed")
    reviewed_head = _commit(git_repo, "src/feature.py", "x = 1\n", "feature")
    reviewed = range_facts(git_repo, base, reviewed_head)

    # sibling branch off the same base: identical modulo whitespace
    _git(git_repo, "checkout", "-q", base)
    _git(git_repo, "checkout", "-q", "-b", "tampered")
    tampered_head = _commit(git_repo, "src/feature.py", "x  =  1\n", "feature ws-tampered")
    tampered = range_facts(git_repo, base, tampered_head)
    # sanity: this IS the hole being closed — patch-id can't tell them apart
    assert tampered.patch_id == reviewed.patch_id
    assert tampered.diff_sha256 != reviewed.diff_sha256

    audits = tmp_path / "audits"
    _write_evidence(
        audits,
        reviewed_base_sha=reviewed.base_sha,
        reviewed_head_sha=reviewed.head_sha,
        reviewed_patch_id=reviewed.patch_id,
        diff_sha256=reviewed.diff_sha256,
        changed_files=reviewed.changed_files,
    )
    r = check_range(git_repo, base, tampered_head, audits, env={})
    assert not r.ok and "无匹配" in r.reason


def test_same_base_evidence_missing_diff_sha256_fails_closed(repo_with_range, tmp_path):
    """MUST-B (sw-ag-fix2): on the SAME base the byte-exact diff_sha256 bind is
    mandatory — evidence-v1 always emits it, so a same-base record missing it is
    suspect, not legacy («有则校验无则放过» would let a whitespace-tampered diff
    ride through on patch-id alone)."""
    repo, base, head = repo_with_range
    facts = range_facts(repo, base, head)
    audits = tmp_path / "audits"
    _write_evidence(
        audits,
        reviewed_base_sha=facts.base_sha,
        reviewed_patch_id=facts.patch_id,
        changed_files=facts.changed_files,
        # no reviewed_head_sha → forces the patch-id path; no diff_sha256
    )
    r = check_range(repo, base, head, audits, env={})
    assert not r.ok and "无匹配" in r.reason


def test_cross_base_cherry_pick_still_passes_with_full_evidence(git_repo: Path, tmp_path):
    """MUST-3 keeps cherry-pick/rebase tolerance: across DIFFERENT bases the
    diff_sha256 binding is deliberately not enforced (full runner-style evidence
    including reviewed_base_sha + diff_sha256 must still pass)."""
    base = _commit(git_repo, "README.md", "seed\n", "seed")
    reviewed_head = _commit(git_repo, "src/feature.py", "x = 1\n", "feature")
    reviewed = range_facts(git_repo, base, reviewed_head)

    _git(git_repo, "checkout", "-q", base)
    _git(git_repo, "checkout", "-q", "-b", "rebased-main")
    drift = _commit(git_repo, "docs/other.md", "drift\n", "unrelated drift")
    _git(git_repo, "cherry-pick", reviewed_head)
    new_head = _git(git_repo, "rev-parse", "HEAD")

    audits = tmp_path / "audits"
    _write_evidence(
        audits,
        reviewed_base_sha=reviewed.base_sha,
        reviewed_head_sha=reviewed.head_sha,
        reviewed_patch_id=reviewed.patch_id,
        diff_sha256=reviewed.diff_sha256,
        changed_files=reviewed.changed_files,
    )
    r = check_range(git_repo, drift, new_head, audits, env={})
    assert r.ok and r.status == "PASS", r.reason


# ─── decision path 3: RED verdict / owner override ───────────────────────────


def test_red_fails_without_override(repo_with_range, tmp_path):
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    _write_evidence(audits, reviewed_base_sha=base, reviewed_head_sha=head, overall_verdict="RED")
    r = check_range(repo, base, head, audits, env={})
    assert not r.ok and "RED" in r.reason and "audit-override" in r.reason


def test_red_passes_with_valid_override_highlighted(repo_with_range, tmp_path):
    repo, base, head = repo_with_range
    facts = range_facts(repo, base, head)
    audits = tmp_path / "audits"
    _write_evidence(
        audits,
        reviewed_base_sha=base,
        reviewed_head_sha=head,
        reviewed_patch_id=facts.patch_id,
        overall_verdict="RED",
        decision="accept_with_red_override",
        owner_ack=_owner_ack(facts.patch_id),
    )
    r = check_range(repo, base, head, audits, env={})
    assert r.ok and r.status == "PASS_OVERRIDE"
    # the override is labelled, never laundered into a GREEN
    assert "override" in r.reason and "GREEN" in r.reason


def test_red_fails_with_tampered_checksum(repo_with_range, tmp_path):
    repo, base, head = repo_with_range
    facts = range_facts(repo, base, head)
    ack = _owner_ack(facts.patch_id)
    ack["reason"] = "tampered after signing"  # checksum no longer matches
    audits = tmp_path / "audits"
    _write_evidence(
        audits,
        reviewed_base_sha=base,
        reviewed_head_sha=head,
        reviewed_patch_id=facts.patch_id,
        overall_verdict="RED",
        decision="accept_with_red_override",
        owner_ack=ack,
    )
    r = check_range(repo, base, head, audits, env={})
    assert not r.ok


def test_validate_owner_ack_requires_all_fields():
    assert not validate_owner_ack({"reviewed_patch_id": "abc", "owner_ack": {"reason": "x"}})
    assert not validate_owner_ack({"owner_ack": _owner_ack("abc")})  # no patch id


# ─── decision path 4: no evidence ────────────────────────────────────────────


def test_fail_no_evidence_with_guidance(repo_with_range, tmp_path, capsys):
    repo, base, head = repo_with_range
    rc = main_check(
        ["--repo", str(repo), "--range", f"{base}..{head}", "--audits-dir", str(tmp_path / "audits")]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "无匹配" in err and "dual-brain-runner" in err and "audit-gate-runbook" in err


# ─── fail-closed: MIXED / ERROR are not an audit ─────────────────────────────


@pytest.mark.parametrize("verdict", ["MIXED", "ERROR"])
def test_mixed_or_error_verdict_fails_closed(repo_with_range, tmp_path, verdict):
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    _write_evidence(audits, reviewed_base_sha=base, reviewed_head_sha=head, overall_verdict=verdict)
    r = check_range(repo, base, head, audits, env={})
    assert not r.ok and verdict in r.reason


def test_green_plus_mixed_conflict_fails(repo_with_range, tmp_path):
    """P2 (sw-ag-fix2): a matched-but-never-completed audit (MIXED/ERROR) is a
    conflict even when a GREEN also matches — same「冲突即拒」semantics as the
    RED priority; a GREEN must not outvote a half-dead audit of the same content."""
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    _write_evidence(audits, name="green", reviewed_base_sha=base, reviewed_head_sha=head)
    _write_evidence(
        audits, name="mixed",
        reviewed_base_sha=base, reviewed_head_sha=head, overall_verdict="MIXED",
    )
    r = check_range(repo, base, head, audits, env={})
    assert not r.ok and r.status == "FAIL"
    assert "MIXED" in r.reason and "冲突" in r.reason


@pytest.mark.parametrize(
    ("older", "newer"),
    [("GREEN", "RED"), ("RED", "GREEN")],
    ids=["old-green-new-red", "old-red-new-green"],
)
def test_red_without_override_fails_regardless_of_green(repo_with_range, tmp_path, older, newer):
    """MUST-1: conflicting verdicts on the SAME content fail-closed in BOTH
    directions — a GREEN (older or newer) never washes a matching un-overridden
    RED. Only door out: owner red-override. (A real fix changes the content,
    hence patch-id/head-sha, so the old RED stops matching — content that still
    matches a RED is unfixed content.)"""
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    p_old = _write_evidence(
        audits, name="older", reviewed_base_sha=base, reviewed_head_sha=head, overall_verdict=older
    )
    _write_evidence(
        audits, name="newer", reviewed_base_sha=base, reviewed_head_sha=head, overall_verdict=newer
    )
    # pin mtime ordering so the test exercises recency in the direction it claims
    import os as _os
    old_stat = p_old.stat()
    _os.utime(p_old, (old_stat.st_atime - 100, old_stat.st_mtime - 100))
    r = check_range(repo, base, head, audits, env={})
    assert not r.ok and r.status == "FAIL"
    assert "RED" in r.reason and "冲突" in r.reason and "audit-override" in r.reason


def test_red_with_valid_override_outranks_plain_green(repo_with_range, tmp_path):
    """Priority pin: PASS_OVERRIDE (RED + owner ack) > PASS — the loud override
    label must survive even when a GREEN also matches."""
    repo, base, head = repo_with_range
    facts = range_facts(repo, base, head)
    audits = tmp_path / "audits"
    _write_evidence(
        audits, name="green", reviewed_base_sha=base, reviewed_head_sha=head, overall_verdict="GREEN"
    )
    _write_evidence(
        audits, name="red-ov",
        reviewed_base_sha=base,
        reviewed_head_sha=head,
        reviewed_patch_id=facts.patch_id,
        overall_verdict="RED",
        decision="accept_with_red_override",
        owner_ack=_owner_ack(facts.patch_id),
    )
    r = check_range(repo, base, head, audits, env={})
    assert r.ok and r.status == "PASS_OVERRIDE"


# ─── owner override CLI (tty-gated) ──────────────────────────────────────────


def test_override_refuses_without_tty(repo_with_range, tmp_path, capsys):
    repo, base, head = repo_with_range
    facts = range_facts(repo, base, head)
    ev = _write_evidence(
        tmp_path / "audits", reviewed_head_sha=head,
        reviewed_patch_id=facts.patch_id, overall_verdict="RED",
    )
    # pytest's captured stdin/stdout are not ttys — exactly the AI-session condition.
    rc = main_override(["--evidence", str(ev), "--reason", "x"])
    assert rc == 1
    assert "tty" in capsys.readouterr().err


def test_override_writes_valid_ack(repo_with_range, tmp_path, monkeypatch):
    repo, base, head = repo_with_range
    facts = range_facts(repo, base, head)
    audits = tmp_path / "audits"
    ev = _write_evidence(
        audits, reviewed_base_sha=base, reviewed_head_sha=head,
        reviewed_patch_id=facts.patch_id, overall_verdict="RED",
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "OVERRIDE")
    rc = main_override(["--evidence", str(ev), "--reason", "owner accepts residual risk"])
    assert rc == 0
    data = json.loads(ev.read_text(encoding="utf-8"))
    assert data["decision"] == "accept_with_red_override"
    assert data["overall_verdict"] == "RED"  # verdict is preserved, not rewritten
    assert validate_owner_ack(data)
    # and the gate now passes — as a labelled override
    r = check_range(repo, base, head, audits, env={})
    assert r.ok and r.status == "PASS_OVERRIDE"


def test_override_refuses_unconfirmed(repo_with_range, tmp_path, monkeypatch):
    repo, base, head = repo_with_range
    facts = range_facts(repo, base, head)
    ev = _write_evidence(
        tmp_path / "audits", reviewed_head_sha=head,
        reviewed_patch_id=facts.patch_id, overall_verdict="RED",
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "yes")  # not the magic word
    assert main_override(["--evidence", str(ev), "--reason", "x"]) == 1
    assert "decision" not in json.loads(ev.read_text(encoding="utf-8"))


def test_override_refuses_non_red_evidence(repo_with_range, tmp_path, monkeypatch):
    _repo, _base, head = repo_with_range
    ev = _write_evidence(tmp_path / "audits", reviewed_head_sha=head, overall_verdict="GREEN")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert main_override(["--evidence", str(ev), "--reason", "x"]) == 1


# ─── emergency bypass (audit_unavailable path — one-time, fully-filled) ──────


def _bypass_record(repo: Path, base: str, head: str, **overrides) -> dict:
    facts = range_facts(repo, base, head)
    rec = {
        "reason": "external brains down",
        "scope": f"{facts.base_sha}..{facts.head_sha}",
        "attempt_counter": 1,
        "follow_up_task_id": "sw-followup-1",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    rec.update(overrides)
    return rec


def _write_bypass(audits: Path, rec: dict, name: str = "b1") -> Path:
    bdir = audits / "bypasses"
    bdir.mkdir(parents=True, exist_ok=True)
    p = bdir / f"{name}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def test_bypass_full_fields_passes_once_then_rejected(repo_with_range, tmp_path):
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    bp = _write_bypass(audits, _bypass_record(repo, base, head))
    env = {BYPASS_ENV: "1"}
    r1 = check_range(repo, base, head, audits, env=env)
    assert r1.ok and r1.status == "PASS_BYPASS" and "BYPASS" in r1.reason
    assert json.loads(bp.read_text(encoding="utf-8")).get("used_at")  # consumed + 留痕
    r2 = check_range(repo, base, head, audits, env=env)
    assert not r2.ok and "已使用" in r2.reason


@pytest.mark.parametrize("missing", ["reason", "follow_up_task_id", "expires_at", "attempt_counter"])
def test_bypass_missing_field_rejected(repo_with_range, tmp_path, missing):
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    rec = _bypass_record(repo, base, head)
    rec.pop(missing)
    _write_bypass(audits, rec)
    r = check_range(repo, base, head, audits, env={BYPASS_ENV: "1"})
    assert not r.ok and "缺字段" in r.reason


def test_bypass_expired_rejected(repo_with_range, tmp_path):
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    _write_bypass(audits, _bypass_record(repo, base, head, expires_at="2020-01-01T00:00:00+00:00"))
    r = check_range(repo, base, head, audits, env={BYPASS_ENV: "1"})
    assert not r.ok and "过期" in r.reason


def test_bypass_without_env_is_inert(repo_with_range, tmp_path):
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    _write_bypass(audits, _bypass_record(repo, base, head))
    r = check_range(repo, base, head, audits, env={})
    assert not r.ok


def test_bypass_does_not_clear_matched_red(repo_with_range, tmp_path):
    """audit_unavailable_bypass and red_override are two distinct doors (codex MUST):
    a matched RED verdict can never be washed away by the emergency bypass."""
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    _write_evidence(audits, reviewed_base_sha=base, reviewed_head_sha=head, overall_verdict="RED")
    _write_bypass(audits, _bypass_record(repo, base, head))
    r = check_range(repo, base, head, audits, env={BYPASS_ENV: "1"})
    assert not r.ok and "RED" in r.reason


def test_bypass_scope_mismatch_rejected(repo_with_range, tmp_path):
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    _write_bypass(audits, _bypass_record(repo, base, head, scope="deadbeef..cafebabe"))
    r = check_range(repo, base, head, audits, env={BYPASS_ENV: "1"})
    assert not r.ok and "无 scope 匹配" in r.reason


def test_bypass_bare_head_scope_rejected(repo_with_range, tmp_path):
    """P2-1: only the full base..head scope authorizes — a bare head sha would
    cover ANY base ending at that head (too wide for a one-time bypass)."""
    repo, base, head = repo_with_range
    facts = range_facts(repo, base, head)
    audits = tmp_path / "audits"
    _write_bypass(audits, _bypass_record(repo, base, head, scope=facts.head_sha))
    r = check_range(repo, base, head, audits, env={BYPASS_ENV: "1"})
    assert not r.ok and "无 scope 匹配" in r.reason


# ─── CLI plumbing / pending marker / misc ────────────────────────────────────


def test_cli_audit_check_dispatch(repo_with_range, tmp_path, capsys):
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    _write_evidence(audits, reviewed_base_sha=base, reviewed_head_sha=head)
    rc = cli_main(
        ["audit-check", "--repo", str(repo), "--range", f"{base}..{head}",
         "--audits-dir", str(audits)]
    )
    assert rc == 0
    assert "PASS" in capsys.readouterr().out


def test_cli_bad_range_is_usage_error(repo_with_range, tmp_path):
    repo, _base, _head = repo_with_range
    rc = main_check(["--repo", str(repo), "--range", "not-a-range",
                     "--audits-dir", str(tmp_path / "audits")])
    assert rc == 2


def test_pending_marker_written_on_fail_and_cleared_on_pass(repo_with_range, tmp_path):
    repo, base, head = repo_with_range
    audits = tmp_path / "audits"
    args = ["--repo", str(repo), "--range", f"{base}..{head}",
            "--audits-dir", str(audits), "--pending-marker-on-fail"]
    assert main_check(args) == 1
    marker = audits / ".audit_pending"
    assert marker.exists()
    assert f"{base}..{head}" in marker.read_text(encoding="utf-8")
    _write_evidence(audits, reviewed_base_sha=base, reviewed_head_sha=head)
    assert main_check(args) == 0
    assert not marker.exists(), "a passing check must clear the pending marker"


def test_derive_project_uses_main_repo_name(git_repo: Path):
    _commit(git_repo, "a.txt", "x\n", "seed")
    assert derive_project(git_repo) == git_repo.name
    # worktree-safe: a linked worktree still resolves to the MAIN repo's name
    wt = git_repo.parent / "linked-wt"
    _git(git_repo, "worktree", "add", "-q", str(wt), "-b", "tmp-branch")
    assert derive_project(wt) == git_repo.name


def test_default_audits_dir_honours_handoff_home(repo_with_range, isolated_handoff_home):
    """Without --audits-dir the checker resolves $HANDOFF_HOME/<project>/audits."""
    repo, base, head = repo_with_range
    audits = isolated_handoff_home / repo.name / "audits"
    _write_evidence(audits, reviewed_base_sha=base, reviewed_head_sha=head)
    rc = main_check(["--repo", str(repo), "--range", f"{base}..{head}"])
    assert rc == 0


def test_empty_tree_base_supported(git_repo: Path, tmp_path):
    """First-push case: the empty-tree sha works as the diff base."""
    head = _commit(git_repo, "a.txt", "x\n", "root")
    audits = tmp_path / "audits"
    _write_evidence(audits, reviewed_base_sha=audit_evidence.EMPTY_TREE_SHA, reviewed_head_sha=head)
    r = check_range(git_repo, audit_evidence.EMPTY_TREE_SHA, head, audits, env={})
    assert r.ok


def test_check_result_is_dataclass_smoke():
    r = CheckResult(True, "PASS", "ok")
    assert r.ok and r.evidence_path is None and r.lines == []
