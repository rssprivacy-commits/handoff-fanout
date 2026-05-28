"""v5.4 retro-evidence gate test matrix.

Implements the cases in ``v5.4-retro-mandate-draft.md §7.11`` whose
ownership lives in ``test_handoff_retro.py``:

  * **R-01 .. R-14** — single-axis behaviours (full pass, missing phase,
    bypass, HEAD freshness, forensic mode, attempt counter, lock
    contention, hash tampering, multi-process race).
  * **C-01 .. C-04** — combinations where two axes interact (bypass +
    counter, nonce + counter, HEAD-stale + bypass, follow-up overdue).

Test strategy: every case invokes ``dump.main(argv)`` directly with a
monkeypatched ``HANDOFF_HOME`` so it never touches the user's real state.
R-14 (multi-tab race) shells out via subprocess for true parallelism.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from handoff_fanout import dump, handoff_precheck

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


def _make_payload(
    ws: Path,
    *,
    task: str = TASK,
    project: str = PROJECT,
    mode: str = "normal",
    nonce: str | None = None,
    phase0_overrides: dict | None = None,
    phase1_overrides: dict | None = None,
):
    p0 = {k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS}
    p1 = {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS}
    if phase0_overrides:
        p0.update(phase0_overrides)
    if phase1_overrides:
        p1.update(phase1_overrides)
    return handoff_precheck.build_evidence(
        task_id=task,
        project=project,
        workspace=ws,
        mode=mode,
        nonce=nonce,
        phase0=p0,
        phase1=p1,
    )


def _write_evidence(home: Path, payload: dict, *, project=PROJECT, task=TASK) -> Path:
    path = home / project / "precheck" / f"{task}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, path)
    return path


def _run_dump(
    *,
    project=PROJECT,
    task=TASK,
    workspace: Path,
    retro_evidence: Path | None = None,
    status: str = "active",
    extra: list[str] | None = None,
) -> tuple[int, str]:
    """Invoke dump.main and capture stderr."""
    argv = [
        "--task",
        task,
        "--next",
        "test next",
        "--project",
        project,
        "--workspace",
        str(workspace),
        "--status",
        status,
    ]
    if retro_evidence is not None:
        argv += ["--retro-evidence", str(retro_evidence)]
    if extra:
        argv += extra

    # Capture stderr via redirection (capsys doesn't catch os.write to fd 2).
    import io

    old_stderr = sys.stderr
    buf = io.StringIO()
    sys.stderr = buf
    try:
        code = dump.main(argv)
    finally:
        sys.stderr = old_stderr
    return code, buf.getvalue()


# ─── R-01 .. R-14 ───────────────────────────────────────────────────────────


def test_R01_full_evidence_passes(handoff_home, workspace):
    ev = _write_evidence(handoff_home, _make_payload(workspace))
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 0, err
    assert (handoff_home / PROJECT / "queue" / f"{TASK}.md").exists()
    # success audit recorded
    audit = handoff_home / PROJECT / "ack" / f"{TASK}.retro.retry_audit.jsonl"
    assert audit.exists()
    lines = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
    assert any(e["event"] == "success" for e in lines)


def test_R02_phase0_status_missing_returns_retry(handoff_home, workspace):
    payload = _make_payload(workspace)
    # Drop status from phase0.memory to simulate a half-recorded item.
    del payload["phase0"]["memory"]["status"]
    payload["evidence_hash"] = handoff_precheck.compute_evidence_hash(payload)
    ev = _write_evidence(handoff_home, payload)
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 4
    assert "ERR-RETRY" in err
    assert "phase0-status-missing" in err
    counter = handoff_home / PROJECT / "ack" / f"{TASK}.retro.attempt_n.txt"
    assert counter.read_text().strip() == "1"


def test_R03_bypass_with_complete_override_passes(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_RETRO_BYPASS", "1")
    override = handoff_home / PROJECT / "ack" / f"{TASK}.retro.override.json"
    override.parent.mkdir(parents=True, exist_ok=True)
    deadline = (datetime.now(UTC) + timedelta(minutes=30)).isoformat(timespec="seconds")
    override.write_text(
        json.dumps(
            {
                "follow_up_retro_task_id": "demo-task-followup",
                "follow_up_deadline": deadline,
            }
        )
    )
    code, err = _run_dump(workspace=workspace)
    assert code == 0, err
    assert (handoff_home / PROJECT / "queue" / f"{TASK}.md").exists()


def test_R04_bypass_missing_deadline_returns_bypass_error(handoff_home, workspace, monkeypatch):
    monkeypatch.setenv("HANDOFF_RETRO_BYPASS", "1")
    override = handoff_home / PROJECT / "ack" / f"{TASK}.retro.override.json"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(json.dumps({"follow_up_retro_task_id": "x"}))
    code, err = _run_dump(workspace=workspace)
    assert code == 6
    assert "ERR-BYPASS" in err
    assert "missing-follow-up-deadline" in err


def test_R04b_bypass_path_traversal_follow_task_rejected(handoff_home, workspace, monkeypatch):
    # Phase 4e R2 / P0-2: a follow_up_retro_task_id with path separators must be
    # rejected at the validation boundary so the shell overdue scanner can never
    # resolve an out-of-tree evidence file from it.
    monkeypatch.setenv("HANDOFF_RETRO_BYPASS", "1")
    override = handoff_home / PROJECT / "ack" / f"{TASK}.retro.override.json"
    override.parent.mkdir(parents=True, exist_ok=True)
    deadline = (datetime.now(UTC) + timedelta(minutes=30)).isoformat(timespec="seconds")
    override.write_text(
        json.dumps(
            {
                "follow_up_retro_task_id": "../../../tmp/evil",
                "follow_up_deadline": deadline,
            }
        )
    )
    code, err = _run_dump(workspace=workspace)
    assert code == 6
    assert "ERR-BYPASS" in err
    assert "invalid-follow-up-task" in err


def test_R05_head_drift_within_tolerance_passes_with_warning(handoff_home, workspace):
    payload = _make_payload(workspace)
    # Simulate "another commit landed since precheck": evidence head is OK
    # for the precheck-time snapshot, but a new commit happens after.
    payload_head = payload["head_at_precheck"]
    (workspace / "another.txt").write_text("x")
    subprocess.run(["git", "add", "another.txt"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "two"], cwd=workspace, check=True)
    # head_at_precheck_timestamp is "just now" → drift < 30s, head differs.
    payload["evidence_hash"] = handoff_precheck.compute_evidence_hash(payload)
    ev = _write_evidence(handoff_home, payload)
    assert payload_head != handoff_precheck._git(["rev-parse", "HEAD"], workspace)
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 0, err
    warnings = handoff_home / PROJECT / "ack" / f"{TASK}.retro.warnings.txt"
    assert warnings.exists()
    assert "head-drift-within-tolerance" in warnings.read_text()


def test_R06_head_stale_with_block_action_returns_blocked(handoff_home, workspace):
    payload = _make_payload(workspace)
    # Make precheck timestamp 60s old (> 30s tolerance).
    old_ts = (datetime.now(UTC) - timedelta(seconds=60)).isoformat(timespec="seconds")
    payload["head_at_precheck_timestamp"] = old_ts
    # Add a new commit so the head differs.
    (workspace / "b.txt").write_text("x")
    subprocess.run(["git", "add", "b.txt"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "two"], cwd=workspace, check=True)
    payload["evidence_hash"] = handoff_precheck.compute_evidence_hash(payload)
    ev = _write_evidence(handoff_home, payload)
    # Configure block action
    cfg_path = handoff_home / PROJECT / "handoff.config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"head_freshness": {"head_stale_action": "block"}}))
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 2
    assert "ERR-BLOCKED" in err
    assert "head-stale-fatal" in err
    blocked = handoff_home / PROJECT / "queue" / f"{TASK}.BLOCKED.md"
    assert blocked.exists()
    assert "head-stale-fatal" in blocked.read_text()


def test_R07_forensic_retro_passes_without_strict_checks(handoff_home, workspace):
    payload = _make_payload(workspace, mode="forensic_retro")
    # Strip phase0 status to prove the lenient gate ignores it under forensic mode.
    del payload["phase0"]["memory"]["status"]
    payload["evidence_hash"] = handoff_precheck.compute_evidence_hash(payload)
    ev = _write_evidence(handoff_home, payload)
    # Pre-seed counter at 1; forensic must NOT touch it.
    counter = handoff_home / PROJECT / "ack" / f"{TASK}.retro.attempt_n.txt"
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text("1\n")
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 0, err
    assert counter.read_text().strip() == "1"


def test_R08_attempt0_failure_bumps_to_1(handoff_home, workspace):
    payload = _make_payload(workspace)
    del payload["phase0"]["memory"]["status"]
    payload["evidence_hash"] = handoff_precheck.compute_evidence_hash(payload)
    ev = _write_evidence(handoff_home, payload)
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 4
    counter = handoff_home / PROJECT / "ack" / f"{TASK}.retro.attempt_n.txt"
    assert counter.read_text().strip() == "1"


def test_R09_attempt1_failure_bumps_to_2(handoff_home, workspace):
    counter = handoff_home / PROJECT / "ack" / f"{TASK}.retro.attempt_n.txt"
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text("1\n")
    payload = _make_payload(workspace)
    del payload["phase0"]["memory"]["status"]
    payload["evidence_hash"] = handoff_precheck.compute_evidence_hash(payload)
    ev = _write_evidence(handoff_home, payload)
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 4
    assert counter.read_text().strip() == "2"


def test_R10_attempt2_failure_returns_blocked(handoff_home, workspace):
    counter = handoff_home / PROJECT / "ack" / f"{TASK}.retro.attempt_n.txt"
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text("2\n")
    payload = _make_payload(workspace)
    del payload["phase0"]["memory"]["status"]
    payload["evidence_hash"] = handoff_precheck.compute_evidence_hash(payload)
    ev = _write_evidence(handoff_home, payload)
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 2
    assert "ERR-BLOCKED" in err
    assert "retro-attempt-exhausted" in err
    blocked = handoff_home / PROJECT / "queue" / f"{TASK}.BLOCKED.md"
    assert blocked.exists()


def test_R11_corrupt_counter_quarantines_and_blocks(handoff_home, workspace):
    counter = handoff_home / PROJECT / "ack" / f"{TASK}.retro.attempt_n.txt"
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text("9\n")  # illegal value
    payload = _make_payload(workspace)
    del payload["phase0"]["memory"]["status"]
    payload["evidence_hash"] = handoff_precheck.compute_evidence_hash(payload)
    ev = _write_evidence(handoff_home, payload)
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 2
    assert "counter-corrupted" in err
    # corrupt copy preserved
    quarantines = list(counter.parent.glob(f"{counter.name}.corrupt-*"))
    assert len(quarantines) >= 1
    assert quarantines[0].read_text().strip() == "9"


def test_R12_attempt_lock_held_returns_locked(handoff_home, workspace):
    # Pre-create the attempt lock dir to simulate another tab holding it.
    locks = handoff_home / PROJECT / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    held = locks / f"{TASK}.retro.attempt.lock"
    held.mkdir()
    try:
        payload = _make_payload(workspace)
        del payload["phase0"]["memory"]["status"]
        payload["evidence_hash"] = handoff_precheck.compute_evidence_hash(payload)
        ev = _write_evidence(handoff_home, payload)
        code, err = _run_dump(workspace=workspace, retro_evidence=ev)
        assert code == 3
        assert "ERR-LOCKED" in err
        assert "attempt-lock-held" in err
    finally:
        with contextlib.suppress(OSError):
            held.rmdir()


def test_R13_evidence_hash_mismatch_returns_retry(handoff_home, workspace):
    payload = _make_payload(workspace)
    ev = _write_evidence(handoff_home, payload)
    # Tamper file bytes WITHOUT recomputing the canonical hash.
    raw = json.loads(ev.read_text())
    raw["task_id"] = raw["task_id"] + "-tampered"
    # Leave evidence_hash unchanged → canonical recompute will differ.
    ev.write_text(json.dumps(raw, indent=2, sort_keys=True))
    code, err = _run_dump(workspace=workspace, retro_evidence=ev, task=raw["task_id"])
    assert code == 4
    assert "evidence-hash-mismatch" in err
    # fatal-class: counter NOT incremented
    counter = handoff_home / PROJECT / "ack" / f"{raw['task_id']}.retro.attempt_n.txt"
    assert not counter.exists()


def test_R14_concurrent_dumps_one_wins_rest_locked(handoff_home, workspace):
    """5 parallel ``handoff-dump`` processes race on precheck.lock.

    Exactly one acquires both ordered locks; the other four exit 3 with
    ``ERR-LOCKED precheck-lock-held``.
    """
    payload = _make_payload(workspace)
    ev = _write_evidence(handoff_home, payload)
    cli = [sys.executable, "-m", "handoff_fanout.dump"]
    base_argv = [
        "--task",
        TASK,
        "--next",
        "race",
        "--project",
        PROJECT,
        "--workspace",
        str(workspace),
        "--status",
        "active",
        "--retro-evidence",
        str(ev),
    ]
    env = dict(os.environ)
    env["HANDOFF_HOME"] = str(handoff_home)
    env.pop("HANDOFF_RETRO_BYPASS", None)
    env.pop("HANDOFF_RETRO_MANDATE", None)
    procs = [
        subprocess.Popen(
            cli + base_argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(5)
    ]
    results = [(p.wait(timeout=30), p.stdout.read(), p.stderr.read()) for p in procs]
    codes = [r[0] for r in results]
    successes = sum(1 for c in codes if c == 0)
    locked = sum(1 for c in codes if c == 3)
    assert successes == 1, f"codes={codes}; details={results}"
    assert locked == 4, f"codes={codes}; details={results}"


# ─── C-01 .. C-04 ───────────────────────────────────────────────────────────


def test_C01_bypass_with_existing_counter_keeps_counter(handoff_home, workspace, monkeypatch):
    counter = handoff_home / PROJECT / "ack" / f"{TASK}.retro.attempt_n.txt"
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text("1\n")
    monkeypatch.setenv("HANDOFF_RETRO_BYPASS", "1")
    override = handoff_home / PROJECT / "ack" / f"{TASK}.retro.override.json"
    deadline = (datetime.now(UTC) + timedelta(minutes=30)).isoformat(timespec="seconds")
    override.write_text(
        json.dumps(
            {"follow_up_retro_task_id": "follow", "follow_up_deadline": deadline}
        )
    )
    code, err = _run_dump(workspace=workspace)
    assert code == 0, err
    assert counter.read_text().strip() == "1"  # bypass path doesn't touch the counter


def test_C02_nonce_mismatch_is_fatal_class_no_counter_bump(handoff_home, workspace):
    counter = handoff_home / PROJECT / "ack" / f"{TASK}.retro.attempt_n.txt"
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text("1\n")
    payload = _make_payload(workspace, nonce="nonce-A")
    ev = _write_evidence(handoff_home, payload)
    code, err = _run_dump(
        workspace=workspace,
        retro_evidence=ev,
        extra=["--nonce", "nonce-B"],
    )
    assert code == 4
    assert "nonce-mismatch" in err
    assert counter.read_text().strip() == "1"  # fatal-class doesn't bump


def test_C03_head_stale_with_bypass_passes(handoff_home, workspace, monkeypatch):
    # Make HEAD genuinely stale.
    (workspace / "c.txt").write_text("x")
    subprocess.run(["git", "add", "c.txt"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "stale"], cwd=workspace, check=True)
    # Configure aggressive block action — would normally reject — but bypass wins.
    cfg = handoff_home / PROJECT / "handoff.config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"head_freshness": {"head_stale_action": "block"}}))
    monkeypatch.setenv("HANDOFF_RETRO_BYPASS", "1")
    override = handoff_home / PROJECT / "ack" / f"{TASK}.retro.override.json"
    override.parent.mkdir(parents=True, exist_ok=True)
    deadline = (datetime.now(UTC) + timedelta(minutes=30)).isoformat(timespec="seconds")
    override.write_text(
        json.dumps(
            {"follow_up_retro_task_id": "follow", "follow_up_deadline": deadline}
        )
    )
    code, err = _run_dump(workspace=workspace)
    assert code == 0, err


def test_C04_follow_up_overdue_blocks_new_dumps(handoff_home, workspace):
    # Another task in the same project has an overdue retro marker.
    other_ack = handoff_home / PROJECT / "ack"
    other_ack.mkdir(parents=True, exist_ok=True)
    (other_ack / "other-task.retro_overdue.txt").write_text("overdue marker")
    payload = _make_payload(workspace)
    ev = _write_evidence(handoff_home, payload)
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 6
    assert "ERR-BYPASS" in err
    assert "follow-up-overdue" in err


# ─── library-level sanity ───────────────────────────────────────────────────


def test_canonical_hash_excludes_self_field():
    base = {"a": 1, "b": "x"}
    base["evidence_hash"] = "deadbeef" * 8
    h = handoff_precheck.compute_evidence_hash(base)
    # Removing the field should not change the canonical hash.
    bare = dict(base)
    bare.pop("evidence_hash", None)
    assert h == handoff_precheck.compute_evidence_hash(bare)


def test_fingerprint_is_deterministic(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "test-entry")
    a = handoff_precheck.session_fingerprint()
    b = handoff_precheck.session_fingerprint()
    assert a == b
    assert a.startswith("fp-")
    assert len(a) == len("fp-") + 32


def test_resolve_session_id_prefers_uuid_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-def-123")
    sid, kind = handoff_precheck.resolve_session_id()
    assert kind == "claude-uuid"
    assert sid == "abc-def-123"


def test_resolve_session_id_falls_back_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    sid, kind = handoff_precheck.resolve_session_id()
    assert kind == "fallback-fingerprint"
    assert sid.startswith("fp-")


# ─── §7.13 reason-on-non-pass enforcement (B1 invariant) ─────────────────────


def _run_precheck(
    *,
    workspace: Path,
    project=PROJECT,
    task=TASK,
    phase0: list[str] | None = None,
    phase1: list[str] | None = None,
    extra: list[str] | None = None,
) -> tuple[int, str]:
    """Invoke handoff_precheck.main and capture stderr."""
    argv = ["--task", task, "--project", project, "--workspace", str(workspace), "--no-lock"]
    for kv in phase0 or []:
        argv += ["--phase0-status", kv]
    for kv in phase1 or []:
        argv += ["--phase1-status", kv]
    if extra:
        argv += extra
    import io

    old_stderr = sys.stderr
    buf = io.StringIO()
    sys.stderr = buf
    try:
        code = handoff_precheck.main(argv)
    finally:
        sys.stderr = old_stderr
    return code, buf.getvalue()


def _all_pass_flags() -> tuple[list[str], list[str]]:
    return (
        [f"{k}=✅" for k in handoff_precheck.PHASE0_KEYS],
        [f"{k}=✅" for k in handoff_precheck.PHASE1_KEYS],
    )


def test_parse_phase_kv_inline_reason():
    out = handoff_precheck._parse_phase_kv(["audit=⚠️:codex pending: see R1"])
    assert out["audit"]["status"] == "⚠️"
    # colon inside the reason is preserved (split on first colon only)
    assert out["audit"]["reason"] == "codex pending: see R1"


def test_parse_phase_kv_status_only_has_no_reason():
    out = handoff_precheck._parse_phase_kv(["memory=✅"])
    assert out["memory"] == {"status": "✅"}


def test_cli_warning_without_reason_rejected(handoff_home, workspace):
    p0, p1 = _all_pass_flags()
    # override audit to ⚠️ with no reason
    p0 = [f for f in p0 if not f.startswith("audit=")] + ["audit=⚠️"]
    code, err = _run_precheck(workspace=workspace, phase0=p0, phase1=p1)
    assert code == 1
    assert "ERR-FATAL reason-required" in err
    assert "phase0.audit" in err
    # no evidence file should have been written
    assert not (handoff_home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json").exists()


def test_cli_skip_without_reason_rejected(handoff_home, workspace):
    p0, p1 = _all_pass_flags()
    p1 = [f for f in p1 if not f.startswith("codex=")] + ["codex=skip"]
    code, err = _run_precheck(workspace=workspace, phase0=p0, phase1=p1)
    assert code == 1
    assert "reason-required" in err
    assert "phase1.codex" in err


def test_cli_status_with_reason_accepted(handoff_home, workspace):
    p0, p1 = _all_pass_flags()
    p0 = [f for f in p0 if not f.startswith("audit=")] + ["audit=⚠️:codex pending review"]
    p1 = [f for f in p1 if not f.startswith("codex=")] + ["codex=skip:not a code change"]
    code, err = _run_precheck(workspace=workspace, phase0=p0, phase1=p1)
    assert code == 0, err
    ev = handoff_home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    assert ev.exists()
    payload = json.loads(ev.read_text())
    assert payload["phase0"]["audit"]["reason"] == "codex pending review"
    assert payload["phase1"]["codex"]["reason"] == "not a code change"


def test_cli_all_pass_without_reason_accepted(handoff_home, workspace):
    """Backward compat: ✅ statuses need no reason."""
    p0, p1 = _all_pass_flags()
    code, err = _run_precheck(workspace=workspace, phase0=p0, phase1=p1)
    assert code == 0, err
    ev = handoff_home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    assert ev.exists()


def test_cli_emitted_evidence_passes_gate(handoff_home, workspace):
    """End-to-end: CLI evidence with reasons survives the dump gate."""
    p0, p1 = _all_pass_flags()
    p0 = [f for f in p0 if not f.startswith("audit=")] + ["audit=⚠️:codex queued"]
    code, err = _run_precheck(workspace=workspace, phase0=p0, phase1=p1)
    assert code == 0, err
    ev = handoff_home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    dcode, derr = _run_dump(workspace=workspace, retro_evidence=ev)
    assert dcode == 0, derr


def test_gate_warning_without_reason_rejected(handoff_home, workspace):
    """Defence-in-depth: hand-crafted evidence bypassing the CLI is rejected."""
    payload = _make_payload(
        workspace, phase0_overrides={"audit": {"status": "⚠️"}}
    )
    ev = _write_evidence(handoff_home, payload)
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 4
    assert "ERR-RETRY" in err
    assert "phase0-status-missing-reason" in err


def test_gate_failed_status_without_reason_rejected(handoff_home, workspace):
    payload = _make_payload(
        workspace, phase1_overrides={"prs": {"status": "❌"}}
    )
    ev = _write_evidence(handoff_home, payload)
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 4
    assert "phase1-status-missing-reason" in err


def test_gate_non_pass_with_reason_accepted(handoff_home, workspace):
    payload = _make_payload(
        workspace,
        phase0_overrides={"tests": {"status": "skip", "reason": "no test surface"}},
        phase1_overrides={"prs": {"status": "❌", "reason": "no PR opened"}},
    )
    ev = _write_evidence(handoff_home, payload)
    code, err = _run_dump(workspace=workspace, retro_evidence=ev)
    assert code == 0, err
