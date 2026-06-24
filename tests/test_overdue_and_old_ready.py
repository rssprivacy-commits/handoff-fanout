"""Follow-up overdue scanner (§7.9) + old_ready writer (D-3) tests.

(The v4 tab-autoclose A-series was removed with the autoclose feature, 2026-05-31;
``old_ready`` + the overdue scanner stay — load-bearing for the §0 new-session
audit and the retro / Phase C-D codex-audit gates.) Every test shells out to
``install/auto-continue.sh`` with ``HANDOFF_SKIP_SPAWN=1`` and a tmpdir
``HANDOFF_ROOT`` so the main spawn loop is bypassed and the only behaviour under
test is the overdue segment.

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
        f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{sink}"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _make_evidence(
    home: Path, task: str = TASK, project: str = PROJECT, *, nonce: str | None = None
) -> Path:
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


# ─── A-02 nonce mismatch (helper extension territory; watcher fires anyway) ─


# ─── A-03 spawned (submitted) not present → watcher skips silently ──────────


# ─── A-04..A-06 helper-extension failure markers → watcher skips next pass ──


# ─── A-07 per-task lock — concurrent runs only emit one autoclose ──────────


# ─── A-08 stale lock self-clean ─────────────────────────────────────────────


# ─── A-09 retro_evidence_hash tampered → reject ─────────────────────────────


# ─── A-10 missing retro_evidence path → reject ──────────────────────────────


# ─── A-11 BLOCKED.md present → watcher skip (no helper URI) ────────────────


# ─── A-12 unknown schema_version → reject ──────────────────────────────────


# ─── default-OFF guard ──────────────────────────────────────────────────────


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
    assert body["schema_version"] == "5.5.0"  # tracks OLD_READY_SCHEMA_VERSION bump
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


# ─── Gap 3 — cross-repo anchor surfaced into old_ready ──────────────────────────


def _git_init_commit(repo: Path) -> str:
    """Init a throwaway repo with one commit; return its HEAD sha."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    # Distinct content per repo so the two repos get distinct commit SHAs (a
    # shared tree + same-second timestamp would otherwise collide deterministically).
    (repo / "f.txt").write_text(f"{repo.name}\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_evidence_with_codex_audit(home: Path, codex_audit: dict | None) -> Path:
    """Hand-craft a minimal evidence file (+ optional codex_audit block) for
    ``dump._write_old_ready``, which only needs a dict with phase0 / nonce /
    optional codex_audit (it does not re-validate the full precheck schema)."""
    payload = {
        "schema_version": "5.5.0",
        "nonce": "gap3",
        "phase0": {"tests": {"status": "✅"}, "memory": {"status": "✅"}},
        "phase1": {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
    }
    if codex_audit is not None:
        payload["codex_audit"] = codex_audit
    out = home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")
    return out


def test_old_ready_surfaces_cross_repo_anchor(tmp_path, monkeypatch):
    """A dual-repo audit-close (--code-repo) puts code_repo/_head in the codex_audit
    block; _write_old_ready must surface both so the §0 audit can trace the
    engine-side commit that the workspace HEAD alone misses."""
    from handoff_fanout import dump

    home = tmp_path / "handoff"
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    ws = tmp_path / "ws"
    _git_init_commit(ws)
    code_repo = tmp_path / "engine"
    code_head = _git_init_commit(code_repo)

    codex_audit = {
        "audit_mode": "full_codex_audit",
        "audit_runs": [{"run_index": 1, "input_commit": "deadbeef", "original_findings": []}],
        "dispositions": [],
        "code_repo": str(code_repo),
        "code_repo_head": code_head,
    }
    evidence = _write_evidence_with_codex_audit(home, codex_audit)
    ack_dir = home / PROJECT / "ack"

    out = dump._write_old_ready(
        project=PROJECT,
        task=TASK,
        workspace=ws,
        evidence_path=evidence,
        ack_dir=ack_dir,
        home=home,
    )
    assert out is not None
    body = json.loads(out.read_text())
    assert body["code_repo"] == str(code_repo)
    assert body["code_repo_head"] == code_head
    # commit_hash still anchors the WORKSPACE HEAD, not the code repo.
    assert body["commit_hash"] != code_head
    assert body["codex_audit_mode"] == "full_codex_audit"


def test_old_ready_omits_cross_repo_anchor_for_same_repo(tmp_path, monkeypatch):
    """Same-repo audit (codex_audit block without code_repo keys) must NOT add the
    fields → old_ready stays byte-stable for the common case."""
    from handoff_fanout import dump

    home = tmp_path / "handoff"
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    ws = tmp_path / "ws"
    _git_init_commit(ws)

    codex_audit = {
        "audit_mode": "full_codex_audit",
        "audit_runs": [{"run_index": 1, "input_commit": "deadbeef", "original_findings": []}],
        "dispositions": [],
    }
    evidence = _write_evidence_with_codex_audit(home, codex_audit)
    out = dump._write_old_ready(
        project=PROJECT,
        task=TASK,
        workspace=ws,
        evidence_path=evidence,
        ack_dir=home / PROJECT / "ack",
        home=home,
    )
    assert out is not None
    body = json.loads(out.read_text())
    assert "code_repo" not in body
    assert "code_repo_head" not in body


def test_old_ready_surfaces_predecessor_lesson_backref(tmp_path, monkeypatch):
    """retrieval-pull L1: when the evidence carries predecessor_lesson_backref,
    _write_old_ready surfaces it additively (so the §0 audit / fleet canary can
    read it without re-parsing the evidence file)."""
    from handoff_fanout import dump

    home = tmp_path / "handoff"
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    ws = tmp_path / "ws"
    _git_init_commit(ws)

    backref = [
        {"predecessor_lesson": "lesson-sw-coord-p61", "disposition": "applied"},
        {
            "predecessor_lesson": "lesson-old",
            "disposition": "superseded",
            "reason": "lesson-new replaces it",
        },
    ]
    payload = {
        "schema_version": "5.5.0",
        "nonce": "backref-test",
        "phase0": {"tests": {"status": "✅"}, "memory": {"status": "✅"}},
        "phase1": {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
        "predecessor_lesson_backref": backref,
    }
    evidence = home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    out = dump._write_old_ready(
        project=PROJECT,
        task=TASK,
        workspace=ws,
        evidence_path=evidence,
        ack_dir=home / PROJECT / "ack",
        home=home,
    )
    assert out is not None
    body = json.loads(out.read_text())
    assert body["predecessor_lesson_backref"] == backref


def test_old_ready_omits_backref_when_absent(tmp_path, monkeypatch):
    """Byte-stable: evidence without the field → old_ready does NOT add the key."""
    from handoff_fanout import dump

    home = tmp_path / "handoff"
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    ws = tmp_path / "ws"
    _git_init_commit(ws)

    evidence = _write_evidence_with_codex_audit(home, None)
    out = dump._write_old_ready(
        project=PROJECT,
        task=TASK,
        workspace=ws,
        evidence_path=evidence,
        ack_dir=home / PROJECT / "ack",
        home=home,
    )
    assert out is not None
    body = json.loads(out.read_text())
    assert "predecessor_lesson_backref" not in body


def test_old_ready_ignores_non_list_backref(tmp_path, monkeypatch):
    """A malformed (non-list) backref value in evidence is ignored, not surfaced —
    old_ready never carries garbage."""
    from handoff_fanout import dump

    home = tmp_path / "handoff"
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    ws = tmp_path / "ws"
    _git_init_commit(ws)

    payload = {
        "schema_version": "5.5.0",
        "nonce": "bad-backref",
        "phase0": {"tests": {"status": "✅"}, "memory": {"status": "✅"}},
        "phase1": {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
        "predecessor_lesson_backref": "not-a-list",
    }
    evidence = home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    out = dump._write_old_ready(
        project=PROJECT,
        task=TASK,
        workspace=ws,
        evidence_path=evidence,
        ack_dir=home / PROJECT / "ack",
        home=home,
    )
    assert out is not None
    body = json.loads(out.read_text())
    assert "predecessor_lesson_backref" not in body


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


def test_overdue_scanner_survives_shadowed_python3_shim_on_path(home, tmp_path):
    """The overdue scanner is a SAFETY gate (retro/audit mandate-debt tracking). Its iso_now_past_deadline
    fail-safes a parse error to "not overdue", so a `python3` that exits non-zero would SILENTLY no-op the
    gate (debt never flagged). A dev/interactive shell may shadow bare `python3` with a wrapper-shim (e.g.
    the tob-modern-python uv-shim that exits non-zero on `python3 -c`). auto-continue.sh therefore defaults
    its interpreter to the absolute /usr/bin/python3, bypassing any PATH shim (2026-06-05 hardening). This
    test injects a broken `python3` shim FIRST on PATH and asserts the overdue marker is still written —
    catching a regression of the hardening even on CI (whose own python3 is fine)."""
    if not Path("/usr/bin/python3").exists():
        pytest.skip("hardening targets the absolute /usr/bin/python3, absent here")
    _write_override(home, TASK, "2020-01-01T00:00:00+00:00", "next-task")
    env = _stubbed_env(home, tmp_path)
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text("#!/bin/bash\necho 'ERROR: use uv run python3' >&2\nexit 1\n")
    shim.chmod(0o755)
    env["PATH"] = f"{shim_dir}:{env.get('PATH', '')}"   # a broken `python3` now shadows PATH
    env.pop("HANDOFF_PYTHON_CMD", None)                 # rely on the script's default (must dodge the shim)
    _run_script(env)
    marker = home / PROJECT / "ack" / f"{TASK}.retro_overdue.txt"
    assert marker.exists(), "overdue SAFETY gate must still fire despite a broken python3 shim shadowing PATH"
