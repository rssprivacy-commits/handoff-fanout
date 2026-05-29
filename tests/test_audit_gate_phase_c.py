"""Phase C — codex audit gate handoff plumbing (spec §6 row C, mandate OFF).

Covers the four Phase C deliverables:

  1. ``old_ready`` carries ``codex_audit_hash`` / ``codex_audit_mode`` and, for a
     bypass, ``next_session_forced_task`` (forced follow-up, spec §1.3).
  2. The pure helpers ``compute_codex_audit_hash`` / ``forced_follow_up_task``.
  3. templates §0 (new-session forced-follow-up self-check) + §-1 (old-session
     audit-close flow) carry the right text.
  4. The overdue scanner (install/auto-continue.sh) handles the codex-audit
     bypass debt kind via the same machinery as the retro kind.

Everything here is mandate-OFF behaviour: the fields are recorded / surfaced,
nothing is hard-enforced yet (that is Phase D).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from handoff_fanout import codex_audit, dump, handoff_precheck, templates

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "install" / "auto-continue.sh"
PROJECT = "erp-system"
TASK = "phase-c-next-task"


# ─── pure helpers: compute_codex_audit_hash / forced_follow_up_task ──────────


def _full_block() -> dict:
    return codex_audit.build_codex_audit_block(
        handoff_precheck.AUDIT_MODE_FULL,
        audit_runs=[{"run_index": 1, "input_commit": "abc", "verdict": "pass"}],
        dispositions=[],
    )


def _bypass_block(follow: str = "audit-redo-phase-c") -> dict:
    # Phase D R1/R2: builder enforces MIN_CODEX_FAILURES (3).
    return codex_audit.build_codex_audit_block(
        handoff_precheck.AUDIT_MODE_BYPASS,
        bypass={
            "codex_failure_attempts": [
                {
                    "exit": 1,
                    "stderr_hash": "sha256:" + "0" * 64,
                    "timestamp": f"2026-05-30T0{i}:00:00+00:00",
                }
                for i in range(3)
            ],
            "follow_up_audit_task_id": follow,
        },
    )


def test_compute_hash_is_canonical_and_deterministic():
    # Key order must not change the hash (canonical = sorted keys).
    a = {"audit_mode": "full_codex_audit", "audit_runs": [], "dispositions": []}
    b = {"dispositions": [], "audit_runs": [], "audit_mode": "full_codex_audit"}
    assert codex_audit.compute_codex_audit_hash(a) == codex_audit.compute_codex_audit_hash(b)
    # Plain 64-hex (matches old_ready.retro_evidence_hash style, no sha256: prefix).
    h = codex_audit.compute_codex_audit_hash(a)
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    # Content change ⇒ hash change.
    c = dict(a, audit_mode="codex_unavailable_bypass")
    assert codex_audit.compute_codex_audit_hash(c) != h
    # Matches the canonical-bytes contract directly.
    assert h == hashlib.sha256(handoff_precheck.canonical_json_bytes(a)).hexdigest()


def test_forced_follow_up_only_for_bypass():
    assert codex_audit.forced_follow_up_task(_bypass_block("audit-redo-x")) == "audit-redo-x"
    # full / empty_diff / docs_only impose no forced task.
    assert codex_audit.forced_follow_up_task(_full_block()) is None
    empty = codex_audit.build_codex_audit_block(
        handoff_precheck.AUDIT_MODE_EMPTY_DIFF,
        attestation={"base": "a", "head": "b", "diff_hash": "d", "mode_decider_version": "1"},
    )
    assert codex_audit.forced_follow_up_task(empty) is None


@pytest.mark.parametrize(
    "block",
    [
        None,
        "not-a-dict",
        {"audit_mode": "codex_unavailable_bypass"},  # missing follow id
        {"audit_mode": "codex_unavailable_bypass", "follow_up_audit_task_id": "../foreign"},
        {"audit_mode": "codex_unavailable_bypass", "follow_up_audit_task_id": ["list"]},
        # R2 P2: ``$`` matches before a trailing newline — fullmatch must reject.
        {"audit_mode": "codex_unavailable_bypass", "follow_up_audit_task_id": "audit-redo-x\n"},
    ],
)
def test_forced_follow_up_fail_open_on_malformed(block):
    assert codex_audit.forced_follow_up_task(block) is None


def test_build_block_rejects_newline_follow_id():
    # R2 P2: the producer must reject a newline-bearing slug so it never reaches
    # evidence / old_ready.next_session_forced_task.
    with pytest.raises(ValueError, match="follow_up_audit_task_id"):
        codex_audit.build_codex_audit_block(
            handoff_precheck.AUDIT_MODE_BYPASS,
            bypass={
                "codex_failure_attempts": [
                    {
                        "exit": 1,
                        "stderr_hash": "sha256:" + "0" * 64,
                        "timestamp": "2026-05-30T00:00:00+00:00",
                    }
                ],
                "follow_up_audit_task_id": "audit-redo-x\n",
            },
        )


def _bypass_gate_block(follow) -> dict:
    # Phase D R1-P1: gate enforces MIN_CODEX_FAILURES (3) — give 3 valid attempts
    # so this helper isolates the follow-id check from the failure-count check.
    return {
        "audit_mode": "codex_unavailable_bypass",
        "codex_failure_attempts": [
            {
                "exit": 1,
                "stderr_hash": "sha256:" + "0" * 64,
                "timestamp": f"2026-05-30T0{i}:00:00+00:00",
            }
            for i in range(3)
        ],
        "follow_up_audit_task_id": follow,
    }


@pytest.mark.parametrize("follow", ["audit-redo-x\n", 123, ["x"], "", "../evil"])
def test_gate_bypass_rejects_bad_follow_id(follow):
    # R3 P1: the gate must mirror producer/reader (isinstance str + fullmatch) so
    # it can't accept a bypass whose owed follow-up then vanishes from old_ready.
    out = codex_audit._gate_bypass(_bypass_gate_block(follow))
    assert out.klass != "ok"


def test_gate_bypass_accepts_valid_follow_id():
    out = codex_audit._gate_bypass(_bypass_gate_block("audit-redo-x"))
    assert out.klass == "ok"


def test_scanner_nonbypass_enum_matches_python_constants():
    # R4 P3: the bash follow_up_satisfied enum is hardcoded (the heredoc stays
    # stdlib-only on purpose, like iso_now_past_deadline, so it can't break on a
    # bare python3). Guard against silent drift from the Python source of truth.
    import re

    script = SCRIPT.read_text()
    m = re.search(r"NON_BYPASS = \{([^}]*)\}", script)
    assert m, "NON_BYPASS literal not found in auto-continue.sh"
    in_script = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    expected = set(handoff_precheck.AUDIT_MODES) - {handoff_precheck.AUDIT_MODE_BYPASS}
    assert in_script == expected, f"scanner NON_BYPASS {in_script} != python {expected}"
    assert handoff_precheck.AUDIT_MODE_BYPASS not in in_script


# ─── old_ready integration via dump.main ─────────────────────────────────────


@pytest.fixture
def git_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for cmd in (
        ["git", "init", "--quiet", "--initial-branch=main"],
        ["git", "config", "user.email", "t@t.test"],
        ["git", "config", "user.name", "t"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(cmd, cwd=ws, check=True)
    (ws / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)
    return ws


def _make_evidence(home: Path, *, codex_audit_block: dict | None = None) -> Path:
    payload = handoff_precheck.build_evidence(
        task_id=TASK,
        project=PROJECT,
        workspace=Path("/tmp"),
        nonce="phasec",
        phase0={k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS},
        phase1={k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
        codex_audit=codex_audit_block,
    )
    out = home / PROJECT / "precheck" / f"{TASK}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, out)
    return out


def _dump(home: Path, ws: Path, evidence: Path, monkeypatch) -> dict:
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.delenv("HANDOFF_RETRO_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_AUDIT_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_RETRO_BYPASS", raising=False)
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
        ]
    )
    assert rc == 0
    old_ready = home / PROJECT / "ack" / f"{TASK}.old_ready"
    assert old_ready.exists()
    return json.loads(old_ready.read_text())


def test_old_ready_full_mode_records_hash_no_forced_task(tmp_path, git_ws, monkeypatch):
    home = tmp_path / "handoff"
    (home / PROJECT / "precheck").mkdir(parents=True)
    block = _full_block()
    body = _dump(home, git_ws, _make_evidence(home, codex_audit_block=block), monkeypatch)
    assert body["codex_audit_hash"] == codex_audit.compute_codex_audit_hash(block)
    assert body["codex_audit_mode"] == "full_codex_audit"
    assert "next_session_forced_task" not in body


def test_old_ready_bypass_records_forced_task(tmp_path, git_ws, monkeypatch):
    home = tmp_path / "handoff"
    (home / PROJECT / "precheck").mkdir(parents=True)
    block = _bypass_block("audit-redo-phase-c")
    body = _dump(home, git_ws, _make_evidence(home, codex_audit_block=block), monkeypatch)
    assert body["codex_audit_hash"] == codex_audit.compute_codex_audit_hash(block)
    assert body["codex_audit_mode"] == "codex_unavailable_bypass"
    assert body["next_session_forced_task"] == "audit-redo-phase-c"


def test_old_ready_no_block_is_backward_compatible(tmp_path, git_ws, monkeypatch):
    home = tmp_path / "handoff"
    (home / PROJECT / "precheck").mkdir(parents=True)
    body = _dump(home, git_ws, _make_evidence(home, codex_audit_block=None), monkeypatch)
    for k in ("codex_audit_hash", "codex_audit_mode", "next_session_forced_task"):
        assert k not in body
    # Pre-existing fields untouched.
    assert body["schema_version"] == "5.5.0"
    assert body["dump_success"] is True


def test_audit_overdue_marker_blocks_dump(tmp_path, git_ws, monkeypatch):
    # R1 P1-1 e2e: an *.audit_overdue.txt marker must block the next dump in the
    # same project (exit 6 ERR-BYPASS), exactly like *.retro_overdue.txt does.
    home = tmp_path / "handoff"
    ack = home / PROJECT / "ack"
    ack.mkdir(parents=True)
    (home / PROJECT / "precheck").mkdir(parents=True)
    (ack / "other-task.audit_overdue.txt").write_text("overdue marker")
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    monkeypatch.delenv("HANDOFF_RETRO_MANDATE", raising=False)
    monkeypatch.delenv("HANDOFF_AUDIT_MANDATE", raising=False)
    rc = dump.main(
        [
            "--task",
            TASK,
            "--next",
            "n",
            "--project",
            PROJECT,
            "--workspace",
            str(git_ws),
            "--status",
            "active",
            "--retro-evidence",
            str(_make_evidence(home, codex_audit_block=_full_block())),
        ]
    )
    assert rc == 6  # ERR-BYPASS follow-up-overdue


# ─── template content (§0 / §-1) ────────────────────────────────────────────


def _render() -> str:
    return templates.build_handoff_md(
        task=TASK,
        project=PROJECT,
        workspace=Path("/Users/x/Projects/erp-system"),
        next_brief="brief",
        status="active",
        tests=None,
        baseline={"git_head": "abc1234", "last_3_commits": "x"},
        roadmap_excerpt="roadmap",
        inject_blocks=[],
        handoff_home=Path("/Users/x/.claude-handoff"),
        handoff_md_path=Path("/tmp/handoff.md"),
    )


def test_template_s0_has_forced_followup_check():
    md = _render()
    assert "next_session_forced_task" in md
    assert "codex_audit_hash" in md
    # The self-check compares the recorded forced task against the current task.
    assert "FORCED" in md and '"$FORCED" != "$TASK"' in md


def test_template_s_minus1_has_audit_close_flow():
    md = _render()
    assert "handoff audit-close" in md
    assert "codex_unavailable_bypass" in md
    assert "follow_up_audit_task_id" in md
    # Deferred-before-Phase-D items must stay visible (not silently dropped).
    assert "owner_ack_token" in md


# ─── overdue scanner (install/auto-continue.sh) codex-audit kind ─────────────


def _scanner_env(home: Path, tmp_path: Path) -> dict[str, str]:
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(exist_ok=True)
    osa = stub_dir / "osascript"
    osa_sink = tmp_path / "osascript.log"
    osa.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{osa_sink}"\nexit 0\n')
    osa.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "HANDOFF_ROOT": str(home),
            "HANDOFF_OSASCRIPT_CMD": str(osa),
            "HANDOFF_SKIP_SPAWN": "1",
            "HANDOFF_VSCODE_CHECK": "0",
            "HANDOFF_AUTOCLOSE_ENABLED": "0",
        }
    )
    env.pop("HANDOFF_HOME", None)
    env["_OSASCRIPT_SINK"] = str(osa_sink)
    return env


def _write_audit_override(home: Path, task: str, deadline: str, follow_task: str) -> Path:
    ack = home / PROJECT / "ack"
    ack.mkdir(parents=True, exist_ok=True)
    out = ack / f"{task}.audit.override.json"
    out.write_text(
        json.dumps(
            {
                "follow_up_audit_task_id": follow_task,
                "follow_up_deadline": deadline,
                "reason": "codex outage",
            },
            indent=2,
        )
        + "\n"
    )
    return out


@pytest.fixture
def scan_home(tmp_path: Path) -> Path:
    root = tmp_path / "claude-handoff"
    (root / PROJECT / "ack").mkdir(parents=True)
    (root / PROJECT / "precheck").mkdir(parents=True)
    (root / PROJECT / "queue").mkdir(parents=True)
    return root


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False, timeout=20
    )


def test_codex_audit_overdue_writes_marker(scan_home, tmp_path):
    env = _scanner_env(scan_home, tmp_path)
    _write_audit_override(scan_home, TASK, "2020-01-01T00:00:00+00:00", "audit-redo-x")
    _run(env)
    marker = scan_home / PROJECT / "ack" / f"{TASK}.audit_overdue.txt"
    assert marker.exists()
    body = marker.read_text()
    assert "overdue" in body
    assert "codex-audit" in body
    assert "audit-redo-x" not in body  # marker tracks the originating task
    assert "Follow-up codex-audit overdue" in Path(env["_OSASCRIPT_SINK"]).read_text()


def _write_followup_evidence(scan_home: Path, follow: str, *, block: dict | None) -> Path:
    """Place a precheck/<follow>.retro.evidence.json the scanner will inspect."""
    payload = handoff_precheck.build_evidence(
        task_id=follow,
        project=PROJECT,
        workspace=Path("/tmp"),
        phase0={k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS},
        phase1={k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS},
        codex_audit=block,
    )
    out = scan_home / PROJECT / "precheck" / f"{follow}.retro.evidence.json"
    handoff_precheck.write_evidence(payload, out)
    return out


def test_codex_audit_overdue_clears_on_real_audit_followup(scan_home, tmp_path):
    # A follow-up that ACTUALLY ran the owed audit (non-bypass block) clears it.
    env = _scanner_env(scan_home, tmp_path)
    follow = "audit-redo-x"
    _write_audit_override(scan_home, TASK, "2020-01-01T00:00:00+00:00", follow)
    _write_followup_evidence(scan_home, follow, block=_full_block())
    _run(env)
    assert not (scan_home / PROJECT / "ack" / f"{TASK}.audit_overdue.txt").exists()
    assert not (scan_home / PROJECT / "ack" / f"{TASK}.audit.override.json").exists()


def test_codex_audit_debt_not_cleared_by_unaudited_evidence(scan_home, tmp_path):
    # R1 P1-2 regression: a plain retro evidence (no codex_audit block) — or a
    # follow-up that itself bypassed — must NOT discharge the audit debt. The
    # override survives and the overdue marker is written.
    env = _scanner_env(scan_home, tmp_path)
    follow = "audit-redo-x"
    _write_audit_override(scan_home, TASK, "2020-01-01T00:00:00+00:00", follow)
    _write_followup_evidence(scan_home, follow, block=None)  # no audit block
    _run(env)
    assert (scan_home / PROJECT / "ack" / f"{TASK}.audit_overdue.txt").exists()
    assert (scan_home / PROJECT / "ack" / f"{TASK}.audit.override.json").exists()


def test_codex_audit_debt_not_cleared_by_bypass_followup(scan_home, tmp_path):
    # A chained bypass does not discharge the debt either (still owes a real audit).
    env = _scanner_env(scan_home, tmp_path)
    follow = "audit-redo-x"
    _write_audit_override(scan_home, TASK, "2020-01-01T00:00:00+00:00", follow)
    _write_followup_evidence(scan_home, follow, block=_bypass_block("audit-redo-again"))
    _run(env)
    assert (scan_home / PROJECT / "ack" / f"{TASK}.audit_overdue.txt").exists()


def test_codex_audit_debt_not_cleared_by_decoy_audit_mode(scan_home, tmp_path):
    # R2 P1: a stray "audit_mode" (no real top-level codex_audit block) must NOT
    # clear the debt — the structural parser only honours codex_audit.audit_mode.
    env = _scanner_env(scan_home, tmp_path)
    follow = "audit-redo-x"
    _write_audit_override(scan_home, TASK, "2020-01-01T00:00:00+00:00", follow)
    # No codex_audit block; a decoy audit_mode buried in an unrelated field.
    (scan_home / PROJECT / "precheck" / f"{follow}.retro.evidence.json").write_text(
        json.dumps({"audit_mode": "full_codex_audit", "phase0": {"audit": {"status": "✅"}}}) + "\n"
    )
    _run(env)
    assert (scan_home / PROJECT / "ack" / f"{TASK}.audit_overdue.txt").exists()
    assert (scan_home / PROJECT / "ack" / f"{TASK}.audit.override.json").exists()


def test_codex_audit_future_deadline_no_marker(scan_home, tmp_path):
    env = _scanner_env(scan_home, tmp_path)
    _write_audit_override(scan_home, TASK, "2099-01-01T00:00:00+00:00", "audit-redo-x")
    _run(env)
    assert not (scan_home / PROJECT / "ack" / f"{TASK}.audit_overdue.txt").exists()


def test_codex_audit_unsafe_follow_task_skipped(scan_home, tmp_path):
    env = _scanner_env(scan_home, tmp_path)
    _write_audit_override(scan_home, TASK, "2020-01-01T00:00:00+00:00", "../foreign")
    _run(env)
    assert not (scan_home / PROJECT / "ack" / f"{TASK}.audit_overdue.txt").exists()


def test_retro_and_audit_kinds_independent(scan_home, tmp_path):
    env = _scanner_env(scan_home, tmp_path)
    # A retro override and a codex-audit override for distinct tasks both overdue.
    ack = scan_home / PROJECT / "ack"
    (ack / "retro-task.retro.override.json").write_text(
        json.dumps(
            {
                "follow_up_retro_task_id": "retro-follow",
                "follow_up_deadline": "2020-01-01T00:00:00+00:00",
            }
        )
        + "\n"
    )
    _write_audit_override(scan_home, "audit-task", "2020-01-01T00:00:00+00:00", "audit-follow")
    _run(env)
    assert (ack / "retro-task.retro_overdue.txt").exists()
    assert (ack / "audit-task.audit_overdue.txt").exists()
