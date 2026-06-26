"""closure_attestation — the ship-live「闭环证书」gate (BLOCK-mode · DEFAULT-ON).

The machine-checkable form of the「彻底闭环」law: when a closing session declares a user-visible
delivery (structured `closeout_obligations.release == ✅`), the retro evidence must carry a
`closure_attestation` binding that delivery to LIVE evidence (deployed + behavior-verified), else
the dump is REFUSED (retry→block). The gate checks ONLY existence + structural completeness +
binding (decidable) — it NEVER parses prose to judge whether the claim is true, which is precisely
how it sidesteps the undecidability wall that killed field-verify (lesson-sw-coord-p67).

Covered:
  1. conditional-fold byte-identity   — omitting it → byte-identical payload + hash (zero regression)
  2. _validate_closure_attestation    — good (shipped+skip) passes; every malformed shape raises
  3. CLI --closure-evidence / -file   — parses; malformed → clean nonzero exit, no artifact; file-wins
  4. off-switch resolution            — DEFAULT-ON; env / config / per-project / fleet sentinel / untrusted-config OFF
  5. _validate_closure_gate           — release=✅ couples; absent/all-skip→retry; shipped→pass; not-triggered→pass; malformed→retry
  6. dump.main integration            — release=✅ no closure BLOCKS (no .uri); shipped passes; off-switch passes; forensic skipped
  7. realign preservation             — a sibling-HEAD rebuild keeps a present vector verbatim
  8. old_ready surfacing              — present → surfaced; absent / non-list → not surfaced
  9. audit-close --closure-evidence   — round-trips into the SAME retro evidence; malformed → clean exit
 10. onboarding template             — §0.9 teaches the vector (heading, flag, kinds, rollback path)

Spec: docs/PROTOCOL.md Part II §13.6.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from handoff_fanout import codex_audit
from handoff_fanout import config as _config
from handoff_fanout import dump, handoff_precheck, retro_gate, templates

PROJECT = "demo"
TASK = "demo-task"


@pytest.fixture
def handoff_home(tmp_path, monkeypatch):
    home = tmp_path / "handoff"
    home.mkdir()
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.delenv("HANDOFF_RETRO_BYPASS", raising=False)
    monkeypatch.delenv("HANDOFF_RETRO_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_AUDIT_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_CLOSURE_OFF", raising=False)
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


def _p0():
    return {k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS}


def _p1():
    return {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS}


_SHIPPED = {
    "deliverable": "login flow",
    "kind": "shipped",
    "deployed": "abc1234",
    "verified": "curl /login → 200 + session cookie",
}
_SKIP = {"deliverable": "docs", "kind": "skip", "reason": "no doc change this hop"}


# ─── 1. conditional-fold byte-identity (the zero-regression basis) ────────────


def test_closure_omitted_is_byte_identical(workspace, monkeypatch):
    """真阴: omitting closure_attestation (None / []) yields the SAME payload + hash as today."""
    monkeypatch.setattr(handoff_precheck, "_iso_now", lambda: "2026-06-27T00:00:00+00:00")
    monkeypatch.setattr(handoff_precheck, "_last_commit_age_sec", lambda *a, **k: 42)
    common = dict(
        task_id=TASK, project=PROJECT, workspace=workspace, nonce="fixed", phase0=_p0(), phase1=_p1()
    )
    without = handoff_precheck.build_evidence(**common)
    with_none = handoff_precheck.build_evidence(**common, closure_attestation=None)
    with_empty = handoff_precheck.build_evidence(**common, closure_attestation=[])
    assert "closure_attestation" not in without
    assert with_none == without
    assert with_empty == without
    assert with_none["evidence_hash"] == without["evidence_hash"]


def test_closure_present_is_hashed(workspace, monkeypatch):
    """真阳: supplying the vector includes it AND binds it into evidence_hash (tamper → invalid)."""
    monkeypatch.setattr(handoff_precheck, "_iso_now", lambda: "2026-06-27T00:00:00+00:00")
    monkeypatch.setattr(handoff_precheck, "_last_commit_age_sec", lambda *a, **k: 42)
    common = dict(
        task_id=TASK, project=PROJECT, workspace=workspace, nonce="fixed", phase0=_p0(), phase1=_p1()
    )
    payload = handoff_precheck.build_evidence(**common, closure_attestation=[dict(_SHIPPED)])
    assert payload["closure_attestation"] == [_SHIPPED]
    assert payload["evidence_hash"] == handoff_precheck.compute_evidence_hash(payload)
    baseline = handoff_precheck.build_evidence(**common)
    assert payload["evidence_hash"] != baseline["evidence_hash"]


# ─── 2. _validate_closure_attestation ─────────────────────────────────────────


def test_validate_good_mixed(workspace):
    payload = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, phase0=_p0(), phase1=_p1(),
        closure_attestation=[
            {**_SHIPPED, "junk": "dropped"},  # extra key dropped
            dict(_SKIP),
        ],
    )
    ca = payload["closure_attestation"]
    assert ca[0] == _SHIPPED  # extra key dropped → canonical shape
    assert ca[1] == _SKIP


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-list",  # not a list
        {"deliverable": "x"},  # dict, not list
        [["x"]],  # entry not a dict
        [{"kind": "shipped", "deployed": "a", "verified": "b"}],  # missing deliverable
        [{"deliverable": "  ", "kind": "skip", "reason": "r"}],  # blank deliverable
        [{"deliverable": "x", "kind": "bogus"}],  # bad kind enum
        [{"deliverable": "x", "kind": "shipped", "verified": "b"}],  # shipped missing deployed
        [{"deliverable": "x", "kind": "shipped", "deployed": "a"}],  # shipped missing verified
        [{"deliverable": "x", "kind": "shipped", "deployed": "a", "verified": "  "}],  # blank verified
        [{"deliverable": "x", "kind": "skip"}],  # skip missing reason
        [{"deliverable": "x", "kind": "skip", "reason": "  "}],  # blank skip reason
    ],
)
def test_validate_malformed_raises(workspace, bad):
    """盲区: garbage can never be hashed in — build_evidence raises BEFORE hashing/writing."""
    with pytest.raises(ValueError):
        handoff_precheck.build_evidence(
            task_id=TASK, project=PROJECT, workspace=workspace, phase0=_p0(), phase1=_p1(),
            closure_attestation=bad,
        )


def test_validate_empty_list_rejected_by_validator():
    """The validator itself rejects an empty list (a present-but-empty「闭环证书」is malformed).
    build_evidence's conditional-fold treats a falsy [] as *omitted* (no key), but the gate's
    defence-in-depth structural re-validation calls the validator directly on a hand-crafted
    present-[] payload — which must raise."""
    with pytest.raises(ValueError):
        handoff_precheck._validate_closure_attestation([])


# ─── 3. CLI --closure-evidence / --closure-evidence-file ──────────────────────


def _precheck_out(home):
    return home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"


def test_cli_closure_flag_parses(handoff_home, workspace):
    out = _precheck_out(handoff_home)
    rc = handoff_precheck.main(
        [
            "--task", TASK, "--project", PROJECT, "--workspace", str(workspace), "--output", str(out),
            "--closure-evidence", "login flow=shipped:abc1234::curl /login → 200 + session cookie",
            "--closure-evidence", "docs=skip:no doc change this hop",
        ]
    )
    assert rc == 0
    body = json.loads(out.read_text())
    assert body["closure_attestation"] == [_SHIPPED, _SKIP]


@pytest.mark.parametrize(
    "flag",
    [
        "no-equals-sign",  # missing =
        "x=noColon",  # missing kind:payload colon
        "x=shipped:onlydeployed",  # shipped without :: separator
        "x=skip:",  # skip with empty reason → builder rejects
        "x=shipped:a::",  # shipped with empty verified → builder rejects
    ],
)
def test_cli_closure_malformed_clean_exit(handoff_home, workspace, flag):
    out = _precheck_out(handoff_home)
    err = io.StringIO()
    old = sys.stderr
    sys.stderr = err
    try:
        rc = handoff_precheck.main(
            ["--task", TASK, "--project", PROJECT, "--workspace", str(workspace),
             "--output", str(out), "--closure-evidence", flag]
        )
    except SystemExit as e:  # grammar errors raise SystemExit(msg)
        rc = e.code if isinstance(e.code, int) else 1
    finally:
        sys.stderr = old
    assert rc != 0
    assert not out.exists()


def test_cli_closure_file_wins(handoff_home, workspace):
    out = _precheck_out(handoff_home)
    cf = handoff_home / "closure.json"
    cf.write_text(json.dumps([_SHIPPED]), encoding="utf-8")
    err = io.StringIO()
    old = sys.stderr
    sys.stderr = err
    try:
        rc = handoff_precheck.main(
            ["--task", TASK, "--project", PROJECT, "--workspace", str(workspace), "--output", str(out),
             "--closure-evidence", "ignored=skip:dropped because file wins",
             "--closure-evidence-file", str(cf)]
        )
    finally:
        sys.stderr = old
    assert rc == 0
    assert json.loads(out.read_text())["closure_attestation"] == [_SHIPPED]
    assert "closure-file-wins" in err.getvalue()


# ─── 4. off-switch resolution (_closure_attestation_mandate_enabled) ──────────


def _cfg(home, **kw):
    if kw:
        (home / "config.json").write_text(json.dumps(kw), encoding="utf-8")
    return _config.load(home=home)


def test_mandate_default_on(handoff_home):
    assert dump._closure_attestation_mandate_enabled(_config.load(home=handoff_home), PROJECT) is True


def test_mandate_env_off(handoff_home, monkeypatch):
    monkeypatch.setenv("HANDOFF_CLOSURE_OFF", "1")
    assert dump._closure_attestation_mandate_enabled(_config.load(home=handoff_home), PROJECT) is False


def test_mandate_config_false(handoff_home):
    cfg = _cfg(handoff_home, closure_attestation_mandate=False)
    assert dump._closure_attestation_mandate_enabled(cfg, PROJECT) is False


def test_mandate_config_string_false(handoff_home):
    """Kill-switch-safe parse: JSON string "false" genuinely disables (no bool('false')==True footgun)."""
    cfg = _cfg(handoff_home, closure_attestation_mandate="false")
    assert dump._closure_attestation_mandate_enabled(cfg, PROJECT) is False


def test_mandate_per_project_sentinel(handoff_home):
    (handoff_home / PROJECT).mkdir(parents=True, exist_ok=True)
    (handoff_home / PROJECT / ".closure-gate-off").write_text("rollback\n")
    assert dump._closure_attestation_mandate_enabled(_config.load(home=handoff_home), PROJECT) is False


def test_mandate_fleet_sentinel(handoff_home):
    (handoff_home / ".closure-gate-off").write_text("fleet rollback\n")
    assert dump._closure_attestation_mandate_enabled(_config.load(home=handoff_home), PROJECT) is False


def test_mandate_untrusted_config_off(handoff_home):
    """A present-but-corrupt config (config_trusted=False) disables the BLOCKING gate (never run a
    block off an unparseable config)."""
    (handoff_home / "config.json").write_text("{not valid json", encoding="utf-8")
    cfg = _config.load(home=handoff_home)
    assert cfg.config_trusted is False
    assert dump._closure_attestation_mandate_enabled(cfg, PROJECT) is False


# ─── 5. _validate_closure_gate (the core decision matrix) ─────────────────────


def _payload(*, release=None, closure=None):
    p: dict = {"phase0": _p0(), "phase1": _p1()}
    if release is not None:
        p["closeout_obligations"] = {"release": {"status": release}}
    if closure is not None:
        p["closure_attestation"] = closure
    return p


def test_gate_release_done_no_closure_retries():
    r = retro_gate._validate_closure_gate(_payload(release="✅"))
    assert r is not None and r.exit_code == retro_gate.EXIT_RETRY
    assert r.subcode == "closure-attestation-missing"


def test_gate_release_done_all_skip_retries():
    r = retro_gate._validate_closure_gate(_payload(release="✅", closure=[dict(_SKIP)]))
    assert r is not None and r.subcode == "closure-attestation-all-skip"


def test_gate_release_done_with_shipped_passes():
    assert retro_gate._validate_closure_gate(_payload(release="✅", closure=[dict(_SHIPPED)])) is None


def test_gate_release_skip_not_triggered():
    """release=skip (or anything ≠ ✅) → binding NOT required (coordination / internal hop)."""
    assert retro_gate._validate_closure_gate(_payload(release="skip")) is None


def test_gate_release_absent_not_triggered():
    assert retro_gate._validate_closure_gate(_payload()) is None


def test_gate_bare_string_release_form_triggers():
    """A bare-string release status (merge_phase_status leniency) still triggers."""
    p = {"phase0": _p0(), "phase1": _p1(), "closeout_obligations": {"release": "✅"}}
    r = retro_gate._validate_closure_gate(p)
    assert r is not None and r.subcode == "closure-attestation-missing"


def test_gate_structural_revalidation_defence_in_depth():
    """A hand-crafted (hash-bypassing) malformed closure is caught structurally even when release
    is NOT ✅ — defence in depth against a payload that skipped the builder."""
    p = _payload(closure=[{"deliverable": "x", "kind": "shipped", "deployed": "a"}])  # no verified
    r = retro_gate._validate_closure_gate(p)
    assert r is not None and r.subcode == "closure-attestation-malformed"


# ─── 6. dump.main integration (the end-to-end block / pass / off-switch) ──────


def _make_evidence(home, workspace, *, closeout=None, closure=None):
    payload = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, phase0=_p0(), phase1=_p1(),
        closeout_obligations=closeout, closure_attestation=closure,
    )
    out = _precheck_out(home)
    handoff_precheck.write_evidence(payload, out)
    return out


def _run_dump(workspace, ev, *, coordinator=True):
    argv = ["--task", TASK, "--next", "next brief", "--project", PROJECT,
            "--workspace", str(workspace), "--status", "active", "--retro-evidence", str(ev)]
    if coordinator:
        argv.append("--coordinator")
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        return dump.main(argv), sys.stderr.getvalue()
    finally:
        sys.stderr = old


def _uri(home):
    return home / PROJECT / "queue" / f"{TASK}.uri"


def test_dump_blocks_release_done_no_closure(handoff_home, workspace):
    """🔴 The load-bearing guarantee: DEFAULT-ON + release=✅ + NO closure → dump REFUSED (nonzero,
    no .uri published). This is the gap closed — 声称用户可见交付却无 closure attestation 被拦。"""
    ev = _make_evidence(handoff_home, workspace, closeout={"release": {"status": "✅"}})
    rc, err = _run_dump(workspace, ev)
    assert rc != 0
    assert not _uri(handoff_home).exists()
    assert "closure-attestation-missing" in err


def test_dump_passes_release_done_with_shipped(handoff_home, workspace):
    ev = _make_evidence(
        handoff_home, workspace,
        closeout={"release": {"status": "✅"}}, closure=[dict(_SHIPPED)],
    )
    rc, _ = _run_dump(workspace, ev)
    assert rc == 0
    assert _uri(handoff_home).exists()


def test_dump_passes_release_skip_no_closure(handoff_home, workspace):
    """A coordination hop (release=skip, no closure) proceeds — narrow trigger = no FP."""
    ev = _make_evidence(
        handoff_home, workspace,
        closeout={"release": {"status": "skip", "reason": "no user-visible delivery"}},
    )
    rc, _ = _run_dump(workspace, ev)
    assert rc == 0
    assert _uri(handoff_home).exists()


def test_dump_off_by_default_path_no_closeout(handoff_home, workspace):
    """No closeout vector at all (the common coordination dump) → gate not triggered → proceeds,
    byte-identical to the pre-closure path."""
    ev = _make_evidence(handoff_home, workspace)
    rc, _ = _run_dump(workspace, ev)
    assert rc == 0
    assert _uri(handoff_home).exists()


def test_dump_off_switch_lets_release_done_through(handoff_home, workspace):
    """Off-switch (per-project sentinel): release=✅ + no closure proceeds because the gate is OFF."""
    (handoff_home / PROJECT).mkdir(parents=True, exist_ok=True)
    (handoff_home / PROJECT / ".closure-gate-off").write_text("rollback\n")
    ev = _make_evidence(handoff_home, workspace, closeout={"release": {"status": "✅"}})
    rc, _ = _run_dump(workspace, ev)
    assert rc == 0
    assert _uri(handoff_home).exists()


def test_dump_env_off_lets_release_done_through(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_CLOSURE_OFF", "1")
    ev = _make_evidence(handoff_home, workspace, closeout={"release": {"status": "✅"}})
    rc, _ = _run_dump(workspace, ev)
    assert rc == 0
    assert _uri(handoff_home).exists()


def test_dump_forensic_mode_skips_closure(handoff_home, workspace):
    """forensic_retro relaxes the retro-side checks (a recovering session can't attest a dead
    session's deploy) — so release=✅ + no closure does NOT block in forensic mode."""
    payload = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, mode=handoff_precheck.MODE_FORENSIC_RETRO,
        phase0=_p0(), phase1=_p1(), closeout_obligations={"release": {"status": "✅"}},
    )
    ev = _precheck_out(handoff_home)
    handoff_precheck.write_evidence(payload, ev)
    rc, _ = _run_dump(workspace, ev)
    assert rc == 0
    assert _uri(handoff_home).exists()


# ─── 7. realign preservation ──────────────────────────────────────────────────


def test_realign_preserves_closure(handoff_home, workspace):
    """A sibling-HEAD re-align must NOT silently erase a present closure_attestation (same
    preservation guarantee codex_audit / closeout have)."""
    payload = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, phase0=_p0(), phase1=_p1(),
        closeout_obligations={"release": {"status": "✅"}}, closure_attestation=[dict(_SHIPPED)],
    )
    h0 = payload["head_at_precheck"]
    payload["head_at_precheck_timestamp"] = (
        datetime.now(UTC) - timedelta(seconds=120)
    ).isoformat(timespec="seconds")
    payload["evidence_hash"] = handoff_precheck.compute_evidence_hash(payload)
    ev = _precheck_out(handoff_home)
    handoff_precheck.write_evidence(payload, ev)

    (workspace / "sibling.txt").write_text("x")
    subprocess.run(["git", "add", "sibling.txt"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "sibling work"], cwd=workspace, check=True)
    h1 = handoff_precheck._git(["rev-parse", "HEAD"], workspace)
    assert h0 != h1

    rc, _ = _run_dump(workspace, ev)
    assert rc == 0
    new_payload = json.loads(ev.read_text())
    assert new_payload["head_at_precheck"] == h1
    assert new_payload["closure_attestation"] == [_SHIPPED]
    assert new_payload["evidence_hash"] == handoff_precheck.compute_evidence_hash(new_payload)


# ─── 8. old_ready surfacing ───────────────────────────────────────────────────


def _git_init_commit(repo):
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _write_old_ready_for(home, closure):
    ws = home.parent / "ws-or"
    _git_init_commit(ws)
    payload = {
        "schema_version": "5.5.0",
        "phase0": {k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS},
        "phase1": {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
    }
    if closure is not None:
        payload["closure_attestation"] = closure
    evidence = _precheck_out(home)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    out = dump._write_old_ready(
        project=PROJECT, task=TASK, workspace=ws, evidence_path=evidence,
        ack_dir=home / PROJECT / "ack", home=home,
    )
    assert out is not None
    return json.loads(out.read_text())


def test_old_ready_surfaces_closure(handoff_home):
    body = _write_old_ready_for(handoff_home, [_SHIPPED, _SKIP])
    assert body["closure_attestation"] == [_SHIPPED, _SKIP]


def test_old_ready_omits_closure_when_absent(handoff_home):
    body = _write_old_ready_for(handoff_home, None)
    assert "closure_attestation" not in body


def test_old_ready_ignores_non_list_closure(handoff_home):
    body = _write_old_ready_for(handoff_home, {"not": "a list"})
    assert "closure_attestation" not in body


# ─── 9. audit-close --closure-evidence (the 中枢交棒 engine path) ───────────────


def _audit_close_argv(workspace, *, closeout=None, closure=None):
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout.strip()
    argv = ["--task", TASK, "--project", PROJECT, "--workspace", str(workspace),
            "--next", "spawn next task", "--audit-mode", "empty_diff_attestation",
            "--status", "active", "--audit-base", head]
    for k in handoff_precheck.PHASE0_KEYS:
        argv += ["--phase0-status", f"{k}=✅"]
    for k in handoff_precheck.PHASE1_KEYS:
        argv += ["--phase1-status", f"{k}=✅"]
    for pair in closeout or []:
        argv += ["--closeout-status", pair]
    for pair in closure or []:
        argv += ["--closure-evidence", pair]
    return argv


def test_audit_close_closure_round_trips(handoff_home, workspace, monkeypatch):
    """真阳: audit-close --closure-evidence folds the (normalized) vector into the retro evidence +
    binds it into evidence_hash — and the release=✅ gate is satisfied (rc 0)."""
    monkeypatch.delenv("HANDOFF_AUDIT_MANDATE", raising=False)
    rc = codex_audit.main_audit_close(
        _audit_close_argv(
            workspace,
            closeout=["sedimentation_always=✅", "release=✅"],
            closure=["login flow=shipped:abc1234::curl /login → 200 + session cookie"],
        )
    )
    assert rc == 0, rc
    ev = json.loads(_precheck_out(handoff_home).read_text())
    assert ev["closure_attestation"] == [_SHIPPED]
    assert ev["evidence_hash"] == handoff_precheck.compute_evidence_hash(ev)
    assert _uri(handoff_home).exists()


def test_audit_close_release_done_no_closure_blocks(handoff_home, workspace, monkeypatch):
    """🔴 The 中枢交棒 path is also gated: release=✅ via audit-close with NO --closure-evidence →
    the inner dump's closure gate REFUSES (nonzero, no .uri)."""
    monkeypatch.delenv("HANDOFF_AUDIT_MANDATE", raising=False)
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        rc = codex_audit.main_audit_close(
            _audit_close_argv(workspace, closeout=["sedimentation_always=✅", "release=✅"])
        )
        err = sys.stderr.getvalue()
    finally:
        sys.stderr = old
    assert rc != 0
    assert not _uri(handoff_home).exists()
    assert "closure-attestation-missing" in err


@pytest.mark.parametrize("bad", ["x=noColon", "x=shipped:onlydeployed", "x=skip:"])
def test_audit_close_closure_malformed_clean_exit(handoff_home, workspace, monkeypatch, bad):
    """盲区: a malformed --closure-evidence → clean nonzero exit BEFORE the lock / any artifact."""
    monkeypatch.delenv("HANDOFF_AUDIT_MANDATE", raising=False)
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        rc = codex_audit.main_audit_close(_audit_close_argv(workspace, closure=[bad]))
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    finally:
        sys.stderr = old
    assert rc != 0
    assert not _precheck_out(handoff_home).exists()
    assert not _uri(handoff_home).exists()


def test_audit_close_closure_omitted_byte_identical(handoff_home, workspace, monkeypatch):
    """🔴 invariant: omitting --closure-evidence (and release≠✅) yields evidence with NO
    closure_attestation key, byte-identical to the pre-patch payload (the conditional-fold guarantee
    proven by stripping the key from a WITH-flag run)."""
    monkeypatch.delenv("HANDOFF_AUDIT_MANDATE", raising=False)
    frozen = handoff_precheck._iso_now()
    monkeypatch.setattr(handoff_precheck, "_iso_now", lambda: frozen)
    monkeypatch.setattr(handoff_precheck, "_last_commit_age_sec", lambda *a, **k: 42)

    assert codex_audit.main_audit_close(_audit_close_argv(workspace)) == 0
    without = json.loads(_precheck_out(handoff_home).read_text())
    assert "closure_attestation" not in without

    assert (
        codex_audit.main_audit_close(
            _audit_close_argv(workspace, closure=["x=skip:nothing shipped"])
        )
        == 0
    )
    with_flag = json.loads(_precheck_out(handoff_home).read_text())
    assert with_flag["closure_attestation"] == [
        {"deliverable": "x", "kind": "skip", "reason": "nothing shipped"}
    ]
    stripped = {k: v for k, v in with_flag.items() if k != "closure_attestation"}
    stripped["evidence_hash"] = handoff_precheck.compute_evidence_hash(stripped)
    assert stripped == without


# ─── 10. onboarding template teaches the closure vector ───────────────────────


def test_template_renders_closure_guidance():
    from pathlib import Path

    md = templates.build_handoff_md(
        task=TASK, project=PROJECT, workspace=Path("/tmp/ws"), next_brief="x", status="active",
        tests=None, baseline={}, roadmap_excerpt="r", inject_blocks=[],
        handoff_home=Path("/tmp/hh"), handoff_md_path=Path("/tmp/h.md"),
    )
    assert "§0.9 closure attestation" in md
    assert "--closure-evidence" in md
    for kind in handoff_precheck.CLOSURE_KINDS:
        assert kind in md
    assert f"/{PROJECT}/.closure-gate-off" in md  # {project} placeholder rendered
    # no regression: the adjacent §0.5–§0.8 blocks all still render
    for s in ("§0.5 retrieval-pull", "§0.6 closeout obligations", "§0.7 parked-backlog scan",
              "§0.8 window placement"):
        assert s in md
