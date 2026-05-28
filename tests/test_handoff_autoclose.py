"""v5.4 Phase 4d / v4 path-D autoclose watcher tests.

Implements ``v5.4-retro-mandate-draft.md §7.11 A-01 .. A-12`` plus follow-up
overdue scanner cases (§7.9). Every test shells out to
``install/auto-continue.sh`` with ``HANDOFF_SKIP_SPAWN=1`` and a tmpdir
``HANDOFF_ROOT`` so the main launchd-driven spawn loop is bypassed and the
only behaviour under test is the autoclose / overdue segments.

External commands the script depends on (``open``, ``osascript``,
``shasum``, ``code``) are stubbed via shell scripts that simply record
their arguments to a sink file. This keeps the test hermetic and gives us
visibility into what the watcher actually tried to dispatch.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from handoff_fanout import handoff_precheck

# ─── locate the script under test ───────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "install" / "auto-continue.sh"

PROJECT = "demo"
TASK = "demo-task"


# ─── helpers ────────────────────────────────────────────────────────────────


def _write_stub(path: Path, sink: Path, *, exit_code: int = 0) -> None:
    """Create a stub command that appends its argv to ``sink``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{sink}"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _make_evidence(home: Path, task: str = TASK, project: str = PROJECT, *, nonce: str | None = None) -> Path:
    """Generate a valid retro evidence file via the real precheck builder."""
    payload = handoff_precheck.build_evidence(
        task_id=task,
        project=project,
        workspace=Path("/tmp"),  # cwd contents irrelevant for the gate
        nonce=nonce,
        phase0={k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS},
        phase1={k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
    )
    out = home / project / "precheck" / f"{task}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, out)
    return out


def _write_old_ready(
    home: Path,
    task: str,
    evidence_file: Path,
    *,
    project: str = PROJECT,
    nonce: str = "demo-nonce",
    schema_version: str = "v5.4.1",
    override_hash: str | None = None,
    rel_path: str | None = None,
) -> Path:
    ack_dir = home / project / "ack"
    ack_dir.mkdir(parents=True, exist_ok=True)
    out = ack_dir / f"{task}.old_ready"
    file_hash = override_hash or hashlib.sha256(evidence_file.read_bytes()).hexdigest()
    if rel_path is None:
        rel_path = str(evidence_file.relative_to(home / project))
    payload = {
        "schema_version": schema_version,
        "task_id": task,
        "nonce": nonce,
        "session_id": "fp-test-session",
        "session_id_kind": "fallback-fingerprint",
        "commit_hash": "abc1234",
        "push_completed_at": "2026-05-29T10:00:00+00:00",
        "tests_passed": True,
        "memory_updated": True,
        "dump_success": True,
        "retro_evidence_hash": file_hash,
        "retro_evidence_path": rel_path,
        "retro_evidence_path_absolute": str(evidence_file),
    }
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def _touch_submitted(home: Path, task: str, project: str = PROJECT) -> Path:
    ack = home / project / "ack"
    ack.mkdir(parents=True, exist_ok=True)
    f = ack / f"{task}.submitted"
    f.write_text("2026-05-29 10:00:00\nstubbed submit\n")
    return f


def _stubbed_env(home: Path, tmp_path: Path, *, autoclose: bool = True) -> dict[str, str]:
    """Build the env that points every external dependency at a stub."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(exist_ok=True)
    open_sink = tmp_path / "open.log"
    osascript_sink = tmp_path / "osascript.log"
    open_stub = stub_dir / "open"
    osascript_stub = stub_dir / "osascript"
    _write_stub(open_stub, open_sink)
    _write_stub(osascript_stub, osascript_sink)
    env = dict(os.environ)
    env.update(
        {
            "HANDOFF_ROOT": str(home),
            "HANDOFF_OPEN_CMD": str(open_stub),
            "HANDOFF_OSASCRIPT_CMD": str(osascript_stub),
            "HANDOFF_SKIP_SPAWN": "1",
            "HANDOFF_VSCODE_CHECK": "0",
            "HANDOFF_AUTOCLOSE_ENABLED": "1" if autoclose else "0",
        },
    )
    # Hide the test runner's own HANDOFF_HOME so the script doesn't see it.
    env.pop("HANDOFF_HOME", None)
    env["_OPEN_SINK"] = str(open_sink)
    env["_OSASCRIPT_SINK"] = str(osascript_sink)
    return env


def _run_script(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Tmp ``~/.claude-handoff`` for one test."""
    root = tmp_path / "claude-handoff"
    root.mkdir()
    (root / PROJECT).mkdir()
    (root / PROJECT / "queue").mkdir()
    (root / PROJECT / "ack").mkdir()
    (root / PROJECT / "precheck").mkdir()
    return root


@pytest.fixture
def stubbed_env(home: Path, tmp_path: Path) -> dict[str, str]:
    return _stubbed_env(home, tmp_path)


# ─── A-01 happy path ────────────────────────────────────────────────────────


def test_A01_full_old_ready_triggers_autoclose(home, tmp_path, stubbed_env):
    evidence = _make_evidence(home, nonce="abc123")
    _write_old_ready(home, TASK, evidence, nonce="abc123")
    _touch_submitted(home, TASK)

    proc = _run_script(stubbed_env)
    assert proc.returncode == 0, proc.stderr

    done = home / PROJECT / "ack" / f"{TASK}.autoclose_done"
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    assert done.exists(), f"expected autoclose_done, got dir: {list((home/PROJECT/'ack').iterdir())}"
    assert not failed.exists()
    body = done.read_text()
    assert TASK in body
    assert "vscode://dharmaxis.handoff-helper/autoclose" in body
    # Open stub was actually invoked with the right URI.
    open_sink = Path(stubbed_env["_OPEN_SINK"])
    assert open_sink.exists()
    assert f"task_id={TASK}" in open_sink.read_text()
    assert "nonce=abc123" in open_sink.read_text()


# ─── A-02 nonce mismatch (helper extension territory; watcher fires anyway) ─


def test_A02_watcher_passes_nonce_to_helper(home, tmp_path, stubbed_env):
    """The watcher itself does not enforce nonce equality — the helper
    extension does. This test pins the URI emission so a regression in the
    nonce wiring would fail loudly.
    """
    evidence = _make_evidence(home, nonce="orig")
    _write_old_ready(home, TASK, evidence, nonce="orig")
    _touch_submitted(home, TASK)

    _run_script(stubbed_env)
    sink = Path(stubbed_env["_OPEN_SINK"]).read_text()
    assert "nonce=orig" in sink


# ─── A-03 spawned (submitted) not present → watcher skips silently ──────────


def test_A03_no_submitted_marker_no_autoclose(home, stubbed_env):
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence)
    # Deliberately omit the .submitted ack — the spawn never confirmed.

    _run_script(stubbed_env)
    done = home / PROJECT / "ack" / f"{TASK}.autoclose_done"
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    assert not done.exists()
    assert not failed.exists()
    open_sink = Path(stubbed_env["_OPEN_SINK"])
    assert not open_sink.exists() or "task_id" not in open_sink.read_text()


# ─── A-04..A-06 helper-extension failure markers → watcher skips next pass ──


@pytest.mark.parametrize(
    "reason",
    ["no_candidate", "multiple_candidates", "is_active_tab"],
)
def test_A04_A05_A06_failed_marker_short_circuits_retry(home, stubbed_env, reason):
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence)
    _touch_submitted(home, TASK)
    ack = home / PROJECT / "ack"
    pre_existing = ack / f"{TASK}.autoclose_failed.txt"
    pre_existing.write_text(f"task_id: {TASK}\nreason: {reason}\n")

    _run_script(stubbed_env)
    # Watcher must not have re-dispatched (open stub silent).
    open_sink = Path(stubbed_env["_OPEN_SINK"])
    assert not open_sink.exists() or "task_id" not in open_sink.read_text()
    # And must not have flipped failure into success.
    done = ack / f"{TASK}.autoclose_done"
    assert not done.exists()
    # Pre-existing marker unchanged.
    assert reason in pre_existing.read_text()


# ─── A-07 per-task lock — concurrent runs only emit one autoclose ──────────


def test_A07_per_task_lock_serializes_two_runs(home, tmp_path, stubbed_env):
    evidence = _make_evidence(home, nonce="lockx")
    _write_old_ready(home, TASK, evidence, nonce="lockx")
    _touch_submitted(home, TASK)

    # Slow down the `open` stub a touch so race chance is real.
    open_stub = Path(stubbed_env["HANDOFF_OPEN_CMD"])
    open_stub.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$_OPEN_SINK"\n'
        "sleep 0.5\n"
        "exit 0\n",
    )
    open_stub.chmod(0o755)

    proc_a = subprocess.Popen(
        ["/bin/bash", str(SCRIPT)],
        env=stubbed_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.05)  # ensure proc_a acquires lock first
    proc_b = subprocess.Popen(
        ["/bin/bash", str(SCRIPT)],
        env=stubbed_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc_a.wait(timeout=20)
    proc_b.wait(timeout=20)

    done = home / PROJECT / "ack" / f"{TASK}.autoclose_done"
    assert done.exists()
    # Exactly one URI was dispatched even though two scripts raced.
    sink = Path(stubbed_env["_OPEN_SINK"]).read_text()
    assert sink.count("task_id=") == 1, sink


# ─── A-08 stale lock self-clean ─────────────────────────────────────────────


def test_A08_stale_lock_is_recycled(home, stubbed_env):
    evidence = _make_evidence(home, nonce="stale")
    _write_old_ready(home, TASK, evidence, nonce="stale")
    _touch_submitted(home, TASK)
    # Stamp a stale lock (10 minutes old) — older than the 5-min TTL.
    locks = home / PROJECT / "locks"
    locks.mkdir()
    stale = locks / f"{TASK}.autoclose.lock"
    stale.mkdir()
    old = time.time() - 600
    os.utime(stale, (old, old))

    _run_script(stubbed_env)

    done = home / PROJECT / "ack" / f"{TASK}.autoclose_done"
    assert done.exists()


# ─── A-09 retro_evidence_hash tampered → reject ─────────────────────────────


def test_A09_evidence_hash_mismatch_rejects(home, stubbed_env):
    evidence = _make_evidence(home, nonce="tamper")
    _write_old_ready(home, TASK, evidence, nonce="tamper", override_hash="0" * 64)
    _touch_submitted(home, TASK)

    _run_script(stubbed_env)
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    done = home / PROJECT / "ack" / f"{TASK}.autoclose_done"
    assert failed.exists()
    assert not done.exists()
    body = failed.read_text()
    assert "retro_evidence_invalid" in body
    # Helper URI never fired.
    open_sink = Path(stubbed_env["_OPEN_SINK"])
    assert not open_sink.exists() or "task_id" not in open_sink.read_text()


# ─── A-10 missing retro_evidence path → reject ──────────────────────────────


def test_A10_missing_evidence_file_rejects(home, stubbed_env):
    # Build old_ready that points at a path that doesn't exist.
    fake = home / PROJECT / "precheck" / "missing.retro.evidence.json"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text("{}")  # write so we can compute a hash
    _write_old_ready(home, TASK, fake)
    fake.unlink()  # delete after writing old_ready so paths inside resolve to nothing
    _touch_submitted(home, TASK)

    _run_script(stubbed_env)
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    assert failed.exists()
    assert "missing_retro_evidence" in failed.read_text()


# ─── A-11 BLOCKED.md present → watcher skip (no helper URI) ────────────────


def test_A11_BLOCKED_md_skips_autoclose(home, stubbed_env):
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence)
    _touch_submitted(home, TASK)
    blocked = home / PROJECT / "queue" / f"{TASK}.BLOCKED.md"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("# BLOCKED — manual hold\n")

    _run_script(stubbed_env)
    open_sink = Path(stubbed_env["_OPEN_SINK"])
    assert not open_sink.exists() or "task_id" not in open_sink.read_text()
    done = home / PROJECT / "ack" / f"{TASK}.autoclose_done"
    assert not done.exists()
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    assert not failed.exists()


# ─── A-12 unknown schema_version → reject ──────────────────────────────────


def test_A12_unknown_schema_version_rejects(home, stubbed_env):
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence, schema_version="v9.9.9-future")
    _touch_submitted(home, TASK)

    _run_script(stubbed_env)
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    assert failed.exists()
    assert "schema_version_unknown" in failed.read_text()


# ─── default-OFF guard ──────────────────────────────────────────────────────


def test_autoclose_disabled_by_default_no_helper_call(home, tmp_path):
    """Without ``HANDOFF_AUTOCLOSE_ENABLED=1`` and no sentinel file, the
    section must short-circuit per spec §7.6 / 改进 #6.
    """
    env = _stubbed_env(home, tmp_path, autoclose=False)
    evidence = _make_evidence(home)
    _write_old_ready(home, TASK, evidence)
    _touch_submitted(home, TASK)
    _run_script(env)
    done = home / PROJECT / "ack" / f"{TASK}.autoclose_done"
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    assert not done.exists()
    assert not failed.exists()


# ─── follow-up overdue scanner (§7.9) ──────────────────────────────────────


def _write_override(home: Path, task: str, deadline: str, follow_task: str) -> Path:
    ack = home / PROJECT / "ack"
    ack.mkdir(parents=True, exist_ok=True)
    out = ack / f"{task}.retro.override.json"
    out.write_text(
        json.dumps(
            {
                "follow_up_retro_task_id": follow_task,
                "follow_up_deadline": deadline,
                "reason": "P0 outage",
            },
            indent=2,
        )
        + "\n"
    )
    return out


def test_V01_overdue_override_writes_marker(home, stubbed_env):
    # Deadline in the past, no follow-up evidence present.
    _write_override(home, TASK, "2020-01-01T00:00:00+00:00", "next-task")
    _run_script(stubbed_env)
    marker = home / PROJECT / "ack" / f"{TASK}.retro_overdue.txt"
    assert marker.exists()
    body = marker.read_text()
    assert "overdue" in body
    assert "next-task" not in body  # marker tracks the originating task
    # osascript was invoked with a notification.
    osascript_sink = Path(stubbed_env["_OSASCRIPT_SINK"])
    assert osascript_sink.exists()
    assert "Follow-up retro overdue" in osascript_sink.read_text()


def test_V02_overdue_with_followup_evidence_clears(home, stubbed_env):
    follow_task = "next-task"
    _write_override(home, TASK, "2020-01-01T00:00:00+00:00", follow_task)
    # Seed the follow-up retro evidence in the precheck dir.
    (home / PROJECT / "precheck").mkdir(parents=True, exist_ok=True)
    (home / PROJECT / "precheck" / f"{follow_task}.retro.evidence.json").write_text("{}")
    _run_script(stubbed_env)
    marker = home / PROJECT / "ack" / f"{TASK}.retro_overdue.txt"
    override = home / PROJECT / "ack" / f"{TASK}.retro.override.json"
    assert not marker.exists()
    assert not override.exists()


def test_V03_future_deadline_no_marker(home, stubbed_env):
    _write_override(home, TASK, "2099-01-01T00:00:00+00:00", "next-task")
    _run_script(stubbed_env)
    marker = home / PROJECT / "ack" / f"{TASK}.retro_overdue.txt"
    assert not marker.exists()


def test_V04_marker_is_idempotent_across_runs(home, stubbed_env):
    _write_override(home, TASK, "2020-01-01T00:00:00+00:00", "next-task")
    _run_script(stubbed_env)
    marker = home / PROJECT / "ack" / f"{TASK}.retro_overdue.txt"
    assert marker.exists()
    first_mtime = marker.stat().st_mtime
    time.sleep(1.1)  # mtime resolution is 1s on darwin
    _run_script(stubbed_env)
    # marker must not be rewritten (no second notification spam).
    assert marker.stat().st_mtime == first_mtime
    osascript_sink = Path(stubbed_env["_OSASCRIPT_SINK"])
    # Only one notification — count occurrences of the title string.
    assert osascript_sink.read_text().count("Follow-up retro overdue") == 1


# ─── D-3 old_ready writer round-trip ───────────────────────────────────────


def test_D3_dump_writes_old_ready_when_retro_evidence_supplied(tmp_path, monkeypatch):
    """End-to-end sanity: dump.main → ack/<task>.old_ready with §7.6 fields."""
    from handoff_fanout import dump

    home = tmp_path / "handoff"
    home.mkdir()
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.delenv("HANDOFF_RETRO_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_RETRO_BYPASS", raising=False)

    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=ws, check=True)
    (ws / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)

    evidence = _make_evidence(home, nonce="d3check")
    rc = dump.main(
        [
            "--task",
            TASK,
            "--next",
            "next thing",
            "--project",
            PROJECT,
            "--workspace",
            str(ws),
            "--status",
            "active",
            "--retro-evidence",
            str(evidence),
        ],
    )
    assert rc == 0
    old_ready = home / PROJECT / "ack" / f"{TASK}.old_ready"
    assert old_ready.exists()
    body = json.loads(old_ready.read_text())
    assert body["schema_version"] == "v5.4.1"
    assert body["task_id"] == TASK
    assert body["nonce"] == "d3check"
    assert body["dump_success"] is True
    assert body["tests_passed"] is True
    assert body["memory_updated"] is True
    assert body["retro_evidence_path"] == f"precheck/{TASK}.retro.evidence.json"
    expected_hash = hashlib.sha256(evidence.read_bytes()).hexdigest()
    assert body["retro_evidence_hash"] == expected_hash
    assert body["session_id"]
    assert body["session_id_kind"] in {"claude-uuid", "fallback-fingerprint"}
    assert body["commit_hash"] != "(unknown)"


def test_D3_no_old_ready_when_retro_evidence_omitted(tmp_path, monkeypatch):
    """Legacy path (no --retro-evidence + no mandate) must not write old_ready."""
    from handoff_fanout import dump

    home = tmp_path / "handoff"
    home.mkdir()
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.delenv("HANDOFF_RETRO_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_RETRO_BYPASS", raising=False)

    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=ws, check=True)
    (ws / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)

    rc = dump.main(
        [
            "--task",
            TASK,
            "--next",
            "next thing",
            "--project",
            PROJECT,
            "--workspace",
            str(ws),
            "--status",
            "active",
        ],
    )
    assert rc == 0
    old_ready = home / PROJECT / "ack" / f"{TASK}.old_ready"
    assert not old_ready.exists()


# ─── Phase 4e R2 gap-close — codex R1 P0/P1 regressions ─────────────────────
#
# These pin the fixes for the 2026-05-29 codex R1 audit of the Phase 4d
# implementation. The overdue-scanner timezone cases (V05-V07) lock in
# tz-correct ISO-8601 handling across the formats live overrides actually use
# (Z / ±offset / naive). V08 + A13 + A14 are strict regressions that FAIL on
# the pre-fix code.


def test_V05_non_utc_past_deadline_writes_marker(home, stubbed_env):
    # P0-1: a +08:00 deadline must be parsed timezone-aware, not lexically.
    _write_override(home, TASK, "2020-06-05T00:00:00+08:00", "next-task")
    _run_script(stubbed_env)
    marker = home / PROJECT / "ack" / f"{TASK}.retro_overdue.txt"
    assert marker.exists()


def test_V06_naive_date_only_past_deadline_writes_marker(home, stubbed_env):
    # P0-1: a bare date (no time, no tz) is assumed UTC midnight.
    _write_override(home, TASK, "2020-01-01", "next-task")
    _run_script(stubbed_env)
    marker = home / PROJECT / "ack" / f"{TASK}.retro_overdue.txt"
    assert marker.exists()


def test_V07_future_non_utc_deadline_no_marker(home, stubbed_env):
    # P0-1: a far-future +08:00 deadline must NOT be flagged overdue.
    _write_override(home, TASK, "2099-01-01T00:00:00+08:00", "next-task")
    _run_script(stubbed_env)
    marker = home / PROJECT / "ack" / f"{TASK}.retro_overdue.txt"
    assert not marker.exists()


def test_V08_path_traversal_follow_task_is_skipped(home, stubbed_env):
    # P0-2: a follow_task with path separators must be skipped, never used to
    # resolve a foreign evidence file. Deadline is past, so WITHOUT the guard
    # the scanner would write an overdue marker; the guard suppresses it.
    _write_override(home, TASK, "2020-01-01T00:00:00+00:00", "../../../tmp/evil")
    _run_script(stubbed_env)
    marker = home / PROJECT / "ack" / f"{TASK}.retro_overdue.txt"
    override = home / PROJECT / "ack" / f"{TASK}.retro.override.json"
    assert not marker.exists()
    assert override.exists()  # not cleared by a crafted follow_task


def test_A13_unsafe_nonce_rejected_before_uri(home, stubbed_env):
    # P0-3: a nonce with URI-significant chars must fail closed, never reach the
    # helper URI (which could otherwise be steered onto the wrong tab).
    evidence = _make_evidence(home, nonce="ok")
    _write_old_ready(home, TASK, evidence, nonce="bad&project=other")
    _touch_submitted(home, TASK)

    _run_script(stubbed_env)
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    done = home / PROJECT / "ack" / f"{TASK}.autoclose_done"
    assert failed.exists()
    assert not done.exists()
    assert "unsafe_uri_param" in failed.read_text()
    open_sink = Path(stubbed_env["_OPEN_SINK"])
    assert not open_sink.exists() or "task_id" not in open_sink.read_text()


def test_A14_prior_schema_version_v540_accepted(home, stubbed_env):
    # P1-2: an old_ready written by an earlier build (v5.4.0) must still
    # autoclose — the watcher allow-list keeps prior versions for compat.
    evidence = _make_evidence(home, nonce="compat")
    _write_old_ready(home, TASK, evidence, nonce="compat", schema_version="v5.4.0")
    _touch_submitted(home, TASK)

    _run_script(stubbed_env)
    done = home / PROJECT / "ack" / f"{TASK}.autoclose_done"
    failed = home / PROJECT / "ack" / f"{TASK}.autoclose_failed.txt"
    assert done.exists(), list((home / PROJECT / "ack").iterdir())
    assert not failed.exists()
