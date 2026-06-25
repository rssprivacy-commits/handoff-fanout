"""closeout_obligations — the third retro-evidence status-vector (warn-mode · DEFAULT-OFF).

A scope-by-delivery closeout contract (sedimentation_always / audit / doc_mapping / release /
sync_pipeline / postmortem; each ✅ artifact-pass or skip+reason N/A) that turns the soft text
rule ⑬「交棒前先复盘」into a machine-checkable vector.

Covered (matches the worker-brief DoD §2):
  1. conditional-fold byte-identity   — omitting it → byte-identical payload + hash (zero regression)
  2. _validate_closeout               — good passes; unknown key / bad status / skip-no-reason raise
  3. CLI --closeout-status            — key=status[:reason] parses; malformed → clean nonzero exit
  4. warn-mode gate                   — DEFAULT-OFF no-op; enabled+missing → advisory; NEVER blocks;
                                        off-switch sentinel; fail-safe on unreadable/malformed
  5. old_ready surfacing              — present → surfaced; absent / non-dict → not surfaced
  6. realign preservation             — _attempt_realign rebuild keeps a present vector verbatim

Spec: docs/PROTOCOL.md Part II §13.5.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from handoff_fanout import config as _config
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


def _p0():
    return {k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS}


def _p1():
    return {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS}


def _write_config(home, *, warn_projects):
    (home / "config.json").write_text(
        json.dumps({"closeout_obligations_warn_projects": warn_projects}), encoding="utf-8"
    )


# ─── 1. conditional-fold byte-identity (the zero-regression basis) ────────────


def test_closeout_omitted_is_byte_identical(workspace, monkeypatch):
    """真阴 / byte-identical: omitting closeout_obligations (None / {}) yields the SAME payload +
    hash as today — the conditional-fold invariant (DEFAULT-OFF = zero behavior change).

    Freeze the time-derived fields so the three build_evidence calls cannot straddle a 1-second
    boundary under full-suite load (the same flake-isolation the backref/lesson tests use)."""
    monkeypatch.setattr(handoff_precheck, "_iso_now", lambda: "2026-06-26T00:00:00+00:00")
    monkeypatch.setattr(handoff_precheck, "_last_commit_age_sec", lambda *a, **k: 42)
    common = dict(
        task_id=TASK, project=PROJECT, workspace=workspace, nonce="fixed-nonce",
        phase0=_p0(), phase1=_p1(),
    )
    without = handoff_precheck.build_evidence(**common)
    with_none = handoff_precheck.build_evidence(**common, closeout_obligations=None)
    with_empty = handoff_precheck.build_evidence(**common, closeout_obligations={})
    assert "closeout_obligations" not in without
    assert with_none == without
    assert with_empty == without
    assert with_none["evidence_hash"] == without["evidence_hash"]
    assert with_empty["evidence_hash"] == without["evidence_hash"]


def test_closeout_present_is_hashed(workspace, monkeypatch):
    """真阳: supplying the vector includes it AND folds it into the hash (binding it — tampering
    invalidates evidence_hash)."""
    monkeypatch.setattr(handoff_precheck, "_iso_now", lambda: "2026-06-26T00:00:00+00:00")
    monkeypatch.setattr(handoff_precheck, "_last_commit_age_sec", lambda *a, **k: 42)
    common = dict(
        task_id=TASK, project=PROJECT, workspace=workspace, nonce="fixed-nonce",
        phase0=_p0(), phase1=_p1(),
    )
    payload = handoff_precheck.build_evidence(
        **common,
        closeout_obligations={
            "sedimentation_always": {"status": "✅"},
            "release": {"status": "skip", "reason": "no user-visible change"},
        },
    )
    assert payload["closeout_obligations"] == {
        "sedimentation_always": {"status": "✅"},
        "release": {"status": "skip", "reason": "no user-visible change"},
    }
    assert payload["evidence_hash"] == handoff_precheck.compute_evidence_hash(payload)
    baseline = handoff_precheck.build_evidence(**common)
    assert payload["evidence_hash"] != baseline["evidence_hash"]


# ─── 2. _validate_closeout ────────────────────────────────────────────────────


def test_validate_closeout_good_mixed(workspace):
    """A full ✅ sedimentation + a skip+reason N/A mix passes and normalizes to the canonical
    {key: {status, reason?}} shape (extra entry keys dropped)."""
    payload = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, phase0=_p0(), phase1=_p1(),
        closeout_obligations={
            "sedimentation_always": {"status": "✅", "junk": "dropped"},
            "audit": {"status": "✅"},
            "doc_mapping": {"status": "skip", "reason": "no instruction/arch/config change"},
            "release": {"status": "skip", "reason": "no user-visible delivery"},
            "sync_pipeline": {"status": "skip", "reason": "no artifact change"},
            "postmortem": {"status": "skip", "reason": "no incident this hop"},
        },
    )
    co = payload["closeout_obligations"]
    assert co["sedimentation_always"] == {"status": "✅"}  # extra key dropped
    assert co["doc_mapping"] == {"status": "skip", "reason": "no instruction/arch/config change"}


def test_validate_closeout_accepts_bare_string_status(workspace):
    """Mirroring merge_phase_status, a bare status string (not a dict) is accepted for a ✅
    item and normalized to {"status": "✅"}."""
    payload = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace, phase0=_p0(), phase1=_p1(),
        closeout_obligations={"sedimentation_always": "✅"},
    )
    assert payload["closeout_obligations"] == {"sedimentation_always": {"status": "✅"}}


@pytest.mark.parametrize(
    "bad",
    [
        {"bogus_key": {"status": "✅"}},  # unknown top-level key (enum) → reject (stricter than merge)
        {"audit": {"status": "yes"}},  # invalid status enum
        {"release": {"status": "skip"}},  # skip (N/A) without a reason
        {"audit": {"status": "skip", "reason": "  "}},  # blank reason
        {"postmortem": {"status": "❌"}},  # ❌ requires a reason
        "not-a-dict",  # not a dict
        ["not", "a", "dict"],  # not a dict
    ],
)
def test_validate_closeout_malformed_raises(workspace, bad):
    """盲区: garbage can't be hashed in — _validate_closeout raises ValueError (so build_evidence
    raises before the payload is ever hashed/written)."""
    with pytest.raises(ValueError):
        handoff_precheck.build_evidence(
            task_id=TASK, project=PROJECT, workspace=workspace, phase0=_p0(), phase1=_p1(),
            closeout_obligations=bad,
        )


def test_validate_closeout_unknown_key_message(workspace):
    """The unknown-key rejection names the offending key + the legal enum (so a typo is fixable
    without reading the source)."""
    with pytest.raises(ValueError, match="bogus"):
        handoff_precheck._validate_closeout({"bogus": {"status": "✅"}})


# ─── 3. CLI --closeout-status ─────────────────────────────────────────────────


def test_cli_closeout_flag_parses(handoff_home, workspace):
    """CLI: --closeout-status key=status and key=skip:reason parse into the evidence file."""
    out = handoff_home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    rc = handoff_precheck.main(
        [
            "--task", TASK, "--project", PROJECT, "--workspace", str(workspace),
            "--output", str(out),
            "--closeout-status", "sedimentation_always=✅",
            "--closeout-status", "release=skip:no user-visible change this hop",
        ]
    )
    assert rc == 0
    body = json.loads(out.read_text())
    assert body["closeout_obligations"] == {
        "sedimentation_always": {"status": "✅"},
        "release": {"status": "skip", "reason": "no user-visible change this hop"},
    }


def test_cli_closeout_malformed_clean_exit(handoff_home, workspace):
    """Malformed CLI closeout (skip without a reason) → clean nonzero exit, no artifact."""
    out = handoff_home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    rc = handoff_precheck.main(
        [
            "--task", TASK, "--project", PROJECT, "--workspace", str(workspace),
            "--output", str(out),
            "--closeout-status", "release=skip",  # N/A with no reason
        ]
    )
    assert rc != 0
    assert not out.exists()


def test_cli_closeout_unknown_key_clean_exit(handoff_home, workspace):
    """An unknown closeout key on the CLI → clean nonzero exit, no artifact."""
    out = handoff_home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    rc = handoff_precheck.main(
        [
            "--task", TASK, "--project", PROJECT, "--workspace", str(workspace),
            "--output", str(out),
            "--closeout-status", "not_a_real_key=✅",
        ]
    )
    assert rc != 0
    assert not out.exists()


# ─── 4. warn-mode gate: _closeout_obligations_warn_enabled + _run_..._gate ────


def test_warn_disabled_by_default(handoff_home):
    cfg = _config.load(home=handoff_home)  # no config.json → empty list
    assert dump._closeout_obligations_warn_enabled(cfg, PROJECT) is False


def test_warn_enabled_when_project_listed(handoff_home):
    _write_config(handoff_home, warn_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    assert dump._closeout_obligations_warn_enabled(cfg, PROJECT) is True


def test_warn_enabled_by_wildcard(handoff_home):
    _write_config(handoff_home, warn_projects=["*"])
    cfg = _config.load(home=handoff_home)
    assert dump._closeout_obligations_warn_enabled(cfg, "any-project") is True


def test_warn_other_project_not_affected(handoff_home):
    _write_config(handoff_home, warn_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    assert dump._closeout_obligations_warn_enabled(cfg, "other") is False


def test_warn_off_switch_per_project(handoff_home):
    _write_config(handoff_home, warn_projects=[PROJECT])
    (handoff_home / PROJECT).mkdir(parents=True, exist_ok=True)
    (handoff_home / PROJECT / ".closeout-obligations-warn-off").write_text("rollback\n")
    cfg = _config.load(home=handoff_home)
    assert dump._closeout_obligations_warn_enabled(cfg, PROJECT) is False


def test_warn_off_switch_fleet_wide(handoff_home):
    _write_config(handoff_home, warn_projects=["*"])
    (handoff_home / ".closeout-obligations-warn-off").write_text("fleet rollback\n")
    cfg = _config.load(home=handoff_home)
    assert dump._closeout_obligations_warn_enabled(cfg, PROJECT) is False


def _gate_args(ev_path, *, status="active", coordinator=True):
    return argparse.Namespace(
        status=status, coordinator=coordinator,
        retro_evidence=str(ev_path) if ev_path else None,
    )


def _capture_gate(args, project, cfg):
    """Run the warn gate, returning (rc, stderr_text). Asserts rc is ALWAYS None (never blocks)."""
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        rc = dump._run_closeout_obligations_gate(args, project, cfg)
        return rc, sys.stderr.getvalue()
    finally:
        sys.stderr = old


def test_gate_noop_when_disabled(handoff_home, tmp_path):
    """DEFAULT-OFF: even with a coordinator active handoff + present evidence, a disabled gate is
    a silent no-op (None, no advisory)."""
    cfg = _config.load(home=handoff_home)  # empty warn list
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"schema_version": "5.5.0"}), encoding="utf-8")
    rc, err = _capture_gate(_gate_args(ev), PROJECT, cfg)
    assert rc is None
    assert err == ""


def test_gate_noop_for_non_coordinator(handoff_home, tmp_path):
    _write_config(handoff_home, warn_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"schema_version": "5.5.0"}), encoding="utf-8")
    rc, err = _capture_gate(_gate_args(ev, coordinator=False), PROJECT, cfg)
    assert rc is None
    assert err == ""


def test_gate_noop_when_not_active(handoff_home, tmp_path):
    _write_config(handoff_home, warn_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"schema_version": "5.5.0"}), encoding="utf-8")
    rc, err = _capture_gate(_gate_args(ev, status="done"), PROJECT, cfg)
    assert rc is None
    assert err == ""


def test_gate_advisory_when_vector_missing(handoff_home, tmp_path):
    """ENABLED + coordinator + active + NO closeout vector → an advisory is printed but the gate
    STILL returns None (never blocks)."""
    _write_config(handoff_home, warn_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"schema_version": "5.5.0"}), encoding="utf-8")  # no closeout
    rc, err = _capture_gate(_gate_args(ev), PROJECT, cfg)
    assert rc is None
    assert "closeout-obligations-advisory" in err
    assert "non-blocking" in err


def test_gate_advisory_when_sedimentation_not_done(handoff_home, tmp_path):
    """ENABLED + vector present but sedimentation_always != ✅ → advisory, still None."""
    _write_config(handoff_home, warn_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    ev = tmp_path / "ev.json"
    ev.write_text(
        json.dumps(
            {
                "closeout_obligations": {
                    "sedimentation_always": {"status": "skip", "reason": "skipped (bad)"}
                }
            }
        ),
        encoding="utf-8",
    )
    rc, err = _capture_gate(_gate_args(ev), PROJECT, cfg)
    assert rc is None
    assert "sedimentation_always" in err


def test_gate_silent_when_vector_present_and_done(handoff_home, tmp_path):
    """ENABLED + vector present + sedimentation_always ✅ → silent None (the happy path)."""
    _write_config(handoff_home, warn_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    ev = tmp_path / "ev.json"
    ev.write_text(
        json.dumps({"closeout_obligations": {"sedimentation_always": {"status": "✅"}}}),
        encoding="utf-8",
    )
    rc, err = _capture_gate(_gate_args(ev), PROJECT, cfg)
    assert rc is None
    assert err == ""


def test_gate_off_switch_silences_advisory(handoff_home, tmp_path):
    """One-key rollback: with the per-project off-switch present, the gate is OFF — no advisory
    even though the vector is missing."""
    _write_config(handoff_home, warn_projects=[PROJECT])
    (handoff_home / PROJECT).mkdir(parents=True, exist_ok=True)
    (handoff_home / PROJECT / ".closeout-obligations-warn-off").write_text("off\n")
    cfg = _config.load(home=handoff_home)
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"schema_version": "5.5.0"}), encoding="utf-8")
    rc, err = _capture_gate(_gate_args(ev), PROJECT, cfg)
    assert rc is None
    assert err == ""


def test_gate_fail_safe_on_unreadable_evidence(handoff_home):
    """ENABLED but the evidence can't be read → fail-SAFE-OFF: None, no advisory, no crash."""
    _write_config(handoff_home, warn_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    missing = handoff_home / "does-not-exist.json"
    rc, err = _capture_gate(_gate_args(missing), PROJECT, cfg)
    assert rc is None
    assert err == ""


def test_gate_fail_safe_on_malformed_json(handoff_home, tmp_path):
    """ENABLED but the evidence is malformed JSON → fail-SAFE-OFF: None, no crash."""
    _write_config(handoff_home, warn_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    ev = tmp_path / "ev.json"
    ev.write_text("{not json", encoding="utf-8")
    rc, err = _capture_gate(_gate_args(ev), PROJECT, cfg)
    assert rc is None
    assert err == ""


def test_gate_fail_safe_on_non_dict_payload(handoff_home, tmp_path):
    """A JSON list payload (not a dict) → fail-SAFE-OFF: None, no crash."""
    _write_config(handoff_home, warn_projects=[PROJECT])
    cfg = _config.load(home=handoff_home)
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    rc, err = _capture_gate(_gate_args(ev), PROJECT, cfg)
    assert rc is None


# ─── 4b. integration through dump.main: warn-mode NEVER blocks ────────────────


def _make_evidence(home, workspace, *, closeout=None):
    payload = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace,
        phase0=_p0(), phase1=_p1(), closeout_obligations=closeout,
    )
    out = home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, out)
    return out


def _run_dump(workspace, ev, *, coordinator=True):
    argv = [
        "--task", TASK, "--next", "next brief", "--project", PROJECT,
        "--workspace", str(workspace), "--status", "active", "--retro-evidence", str(ev),
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


def test_dump_warn_enabled_missing_vector_still_succeeds(handoff_home, workspace):
    """🔴 The load-bearing warn-mode guarantee: ENABLED + coordinator + MISSING closeout vector
    → the dump STILL proceeds (rc 0, .uri published). Warn-mode never blocks a handoff."""
    _write_config(handoff_home, warn_projects=[PROJECT])
    ev = _make_evidence(handoff_home, workspace, closeout=None)
    rc = _run_dump(workspace, ev, coordinator=True)
    assert rc == 0
    assert _uri(handoff_home).exists()


def test_dump_off_by_default_succeeds(handoff_home, workspace):
    """DEFAULT-OFF: a coordinator dump with no closeout vector proceeds (byte-identical path)."""
    ev = _make_evidence(handoff_home, workspace, closeout=None)
    rc = _run_dump(workspace, ev, coordinator=True)
    assert rc == 0
    assert _uri(handoff_home).exists()


def test_dump_warn_enabled_with_vector_succeeds(handoff_home, workspace):
    """ENABLED + a fully-recorded closeout vector → proceeds (rc 0, .uri published)."""
    _write_config(handoff_home, warn_projects=[PROJECT])
    ev = _make_evidence(
        handoff_home, workspace,
        closeout={
            "sedimentation_always": {"status": "✅"},
            "release": {"status": "skip", "reason": "no user-visible change"},
        },
    )
    rc = _run_dump(workspace, ev, coordinator=True)
    assert rc == 0
    assert _uri(handoff_home).exists()


# ─── 5. old_ready surfacing ───────────────────────────────────────────────────


def _evidence_payload(closeout=None):
    payload = {
        "schema_version": "5.5.0",
        "nonce": "closeout-test",
        "phase0": {"tests": {"status": "✅"}, "memory": {"status": "✅"}},
        "phase1": {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
    }
    if closeout is not None:
        payload["closeout_obligations"] = closeout
    return payload


def _git_init_commit(repo):
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _write_old_ready_for(home, payload):
    ws = home.parent / "ws-or"
    _git_init_commit(ws)
    evidence = home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    out = dump._write_old_ready(
        project=PROJECT, task=TASK, workspace=ws, evidence_path=evidence,
        ack_dir=home / PROJECT / "ack", home=home,
    )
    assert out is not None
    return json.loads(out.read_text())


def test_old_ready_surfaces_closeout(handoff_home):
    """retrieval-pull-style surfacing: a present closeout vector is copied into old_ready so the
    next §0 audit can read it without re-parsing the evidence."""
    closeout = {
        "sedimentation_always": {"status": "✅"},
        "doc_mapping": {"status": "skip", "reason": "no instruction change"},
    }
    body = _write_old_ready_for(handoff_home, _evidence_payload(closeout))
    assert body["closeout_obligations"] == closeout


def test_old_ready_omits_closeout_when_absent(handoff_home):
    """Byte-stable: evidence without the vector → old_ready does NOT add the key."""
    body = _write_old_ready_for(handoff_home, _evidence_payload(None))
    assert "closeout_obligations" not in body


def test_old_ready_ignores_non_dict_closeout(handoff_home):
    """A malformed (non-dict) closeout value in evidence is ignored — old_ready never carries
    garbage."""
    body = _write_old_ready_for(handoff_home, _evidence_payload("not-a-dict"))
    assert "closeout_obligations" not in body


# ─── 6. realign preservation ──────────────────────────────────────────────────


def test_realign_preserves_closeout(handoff_home, workspace):
    """A sibling-HEAD re-align must NOT silently erase a present closeout vector (same
    preservation guarantee codex_audit / backref / lesson_disposition have) — re-align refreshes
    the HEAD binding, it does not re-decide the closeout."""
    closeout = {
        "sedimentation_always": {"status": "✅"},
        "release": {"status": "skip", "reason": "no user-visible change"},
    }
    payload = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT, workspace=workspace,
        phase0=_p0(), phase1=_p1(), closeout_obligations=closeout,
    )
    h0 = payload["head_at_precheck"]
    payload["head_at_precheck_timestamp"] = (
        datetime.now(UTC) - timedelta(seconds=120)
    ).isoformat(timespec="seconds")
    payload["evidence_hash"] = handoff_precheck.compute_evidence_hash(payload)
    ev = handoff_home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, ev)

    (workspace / "sibling.txt").write_text("x")
    subprocess.run(["git", "add", "sibling.txt"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "sibling work"], cwd=workspace, check=True)
    h1 = handoff_precheck._git(["rev-parse", "HEAD"], workspace)
    assert h0 != h1

    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        rc = dump.main(
            [
                "--task", TASK, "--next", "next", "--project", PROJECT,
                "--workspace", str(workspace), "--status", "active",
                "--retro-evidence", str(ev),
            ]
        )
    finally:
        sys.stderr = old
    assert rc == 0
    new_payload = json.loads(ev.read_text())
    assert new_payload["head_at_precheck"] == h1
    assert new_payload["closeout_obligations"] == closeout
    assert new_payload["evidence_hash"] == handoff_precheck.compute_evidence_hash(new_payload)
