"""req3 §2.A — ``handoff audit-discharge``: the non-forgeable audited-to-terminal
signal writer.

This is a PURE PRODUCER: it writes ``ack/<task>.audit_discharged`` and validates the
SHAPE only (verdict==GREEN, SHAs well-formed, nonce hex16). All anti-forge corroboration
(git ancestry / spawn anchors / live worktree HEAD) is the CONSUMER's job and lives in
``autoclose_gate`` (tested in test_autoclose_gate.py). These tests assert the writer:
writes correct JSON, rejects a non-GREEN verdict, and rejects a malformed SHA.
"""

from __future__ import annotations

import json

import pytest

from handoff_fanout import codex_audit

PROJECT = "demo"
TASK = "wk-discharge-1"
SHA = "a" * 40
SHA2 = "b" * 40


@pytest.fixture
def handoff_home(tmp_path, monkeypatch):
    home = tmp_path / "handoff"
    home.mkdir()
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    return home


# ─── writer: happy path JSON shape ───────────────────────────────────────────


def test_writes_correct_json(handoff_home):
    rec = codex_audit.write_audit_discharged(
        PROJECT, TASK, verdict="GREEN", merge_sha=SHA, worktree_head=SHA, nonce=None,
        discharged_at="2026-06-26T00:00:00+00:00",
    )
    path = codex_audit.audit_discharged_path(PROJECT, TASK)
    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk == rec
    assert on_disk["schema_version"] == codex_audit.AUDIT_DISCHARGED_SCHEMA_VERSION
    assert on_disk["kind"] == "audit_discharged"
    assert on_disk["task"] == TASK
    assert on_disk["verdict"] == "GREEN"
    assert on_disk["merge_sha"] == SHA
    assert on_disk["worktree_head"] == SHA
    assert on_disk["nonce"] is None
    assert on_disk["discharged_at"] == "2026-06-26T00:00:00+00:00"


def test_worktree_head_defaults_to_merge_sha(handoff_home):
    rec = codex_audit.write_audit_discharged(
        PROJECT, TASK, verdict="GREEN", merge_sha=SHA, worktree_head=None, nonce=None,
        discharged_at="2026-06-26T00:00:00+00:00",
    )
    assert rec["worktree_head"] == SHA  # ff-merge: branch HEAD == merged commit


def test_nonce_recorded_when_valid(handoff_home):
    nonce = "184f6d9d2b3830af"
    rec = codex_audit.write_audit_discharged(
        PROJECT, TASK, verdict="GREEN", merge_sha=SHA, worktree_head=SHA2, nonce=nonce,
        discharged_at="2026-06-26T00:00:00+00:00",
    )
    assert rec["nonce"] == nonce
    assert rec["worktree_head"] == SHA2


# ─── writer: shape rejection (the spec §2.A "rejects" cases) ─────────────────


@pytest.mark.parametrize("verdict", ["RED", "green", "PASS", "", "GREEN "])
def test_rejects_non_green_verdict(handoff_home, verdict):
    with pytest.raises(ValueError, match="verdict must be GREEN"):
        codex_audit.write_audit_discharged(
            PROJECT, TASK, verdict=verdict, merge_sha=SHA, worktree_head=SHA, nonce=None,
            discharged_at="2026-06-26T00:00:00+00:00",
        )
    # fail-closed: NOTHING written on a rejected verdict.
    assert not codex_audit.audit_discharged_path(PROJECT, TASK).exists()


@pytest.mark.parametrize(
    "bad_sha", ["", "xyz", "nothex!!", "a" * 41, "abc", "g" * 40, "A" * 40]
)
def test_rejects_malformed_merge_sha(handoff_home, bad_sha):
    with pytest.raises(ValueError, match="merge_sha must be a git SHA"):
        codex_audit.write_audit_discharged(
            PROJECT, TASK, verdict="GREEN", merge_sha=bad_sha, worktree_head=None, nonce=None,
            discharged_at="2026-06-26T00:00:00+00:00",
        )
    assert not codex_audit.audit_discharged_path(PROJECT, TASK).exists()


@pytest.mark.parametrize("bad_sha", ["xyz", "a" * 41, "g" * 40])
def test_rejects_malformed_worktree_head(handoff_home, bad_sha):
    with pytest.raises(ValueError, match="worktree_head must be a git SHA"):
        codex_audit.write_audit_discharged(
            PROJECT, TASK, verdict="GREEN", merge_sha=SHA, worktree_head=bad_sha, nonce=None,
            discharged_at="2026-06-26T00:00:00+00:00",
        )


@pytest.mark.parametrize("bad_nonce", ["xyz", "abc", "z" * 16, "a" * 15, "a" * 17])
def test_rejects_malformed_nonce(handoff_home, bad_nonce):
    with pytest.raises(ValueError, match="nonce must be 16 hex"):
        codex_audit.write_audit_discharged(
            PROJECT, TASK, verdict="GREEN", merge_sha=SHA, worktree_head=SHA, nonce=bad_nonce,
            discharged_at="2026-06-26T00:00:00+00:00",
        )


def test_invalid_project_or_task_slug_rejected(handoff_home):
    with pytest.raises(ValueError):
        codex_audit.write_audit_discharged(
            "../escape", TASK, verdict="GREEN", merge_sha=SHA, worktree_head=SHA, nonce=None,
            discharged_at="2026-06-26T00:00:00+00:00",
        )
    with pytest.raises(ValueError):
        codex_audit.write_audit_discharged(
            PROJECT, "bad task", verdict="GREEN", merge_sha=SHA, worktree_head=SHA, nonce=None,
            discharged_at="2026-06-26T00:00:00+00:00",
        )


# ─── CLI surface ─────────────────────────────────────────────────────────────


def test_cli_happy_path(handoff_home, capsys):
    rc = codex_audit.main_audit_discharge(
        [TASK, "--project", PROJECT, "--verdict", "GREEN", "--merge-sha", SHA]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["merge_sha"] == SHA
    assert out["verdict"] == "GREEN"
    assert codex_audit.audit_discharged_path(PROJECT, TASK).exists()


def test_cli_rejects_non_green(handoff_home, capsys):
    rc = codex_audit.main_audit_discharge(
        [TASK, "--project", PROJECT, "--verdict", "RED", "--merge-sha", SHA]
    )
    assert rc == 1
    assert "audit-discharge-invalid" in capsys.readouterr().err
    assert not codex_audit.audit_discharged_path(PROJECT, TASK).exists()


def test_cli_rejects_malformed_sha(handoff_home, capsys):
    rc = codex_audit.main_audit_discharge(
        [TASK, "--project", PROJECT, "--verdict", "GREEN", "--merge-sha", "nothex"]
    )
    assert rc == 1
    assert "audit-discharge-invalid" in capsys.readouterr().err


def test_cli_dispatches_through_handoff(handoff_home, capsys):
    from handoff_fanout import cli

    rc = cli.main(
        ["audit-discharge", TASK, "--project", PROJECT, "--verdict", "GREEN", "--merge-sha", SHA]
    )
    assert rc == 0
    assert codex_audit.audit_discharged_path(PROJECT, TASK).exists()


# ─── --and-close: immediate gate+close via the autoclose --task driver ────────
#
# These assert the additive --and-close path: the signal write is unchanged, and the
# close is best-effort + hard-bounded (timeout=60, all failures swallowed to a WARN with
# rc 0 — a close failure must NEVER fail the discharge, the signal persists for the sweep).


def test_no_and_close_is_pure_producer(handoff_home, monkeypatch, capsys):
    """Backward-compat: WITHOUT --and-close, the signal is written and NO close fires."""
    called = []
    monkeypatch.setattr(
        codex_audit.subprocess, "run", lambda *a, **k: called.append((a, k))
    )
    rc = codex_audit.main_audit_discharge(
        [TASK, "--project", PROJECT, "--verdict", "GREEN", "--merge-sha", SHA]
    )
    assert rc == 0
    assert called == []  # the close driver was NEVER invoked
    path = codex_audit.audit_discharged_path(PROJECT, TASK)
    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["verdict"] == "GREEN"
    assert on_disk["merge_sha"] == SHA


def test_and_close_invokes_driver(handoff_home, tmp_path, monkeypatch, capsys):
    """WITH --and-close, the driver is invoked with the exact argv + timeout=60."""
    fake_driver = tmp_path / "autoclose-audited-workers.py"
    fake_driver.write_text("# fake driver\n")
    recorded = {}

    class _Proc:
        returncode = 0
        stdout = "closed window for wk-discharge-1\n"
        stderr = ""

    def _fake_run(argv, **kwargs):
        recorded["argv"] = argv
        recorded["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(codex_audit.subprocess, "run", _fake_run)
    rc = codex_audit.main_audit_discharge(
        [TASK, "--project", PROJECT, "--verdict", "GREEN", "--merge-sha", SHA,
         "--and-close", "--autoclose-driver", str(fake_driver)]
    )
    assert rc == 0
    argv = recorded["argv"]
    # [sys.executable, <driver>, "--project", <p>, "--task", <task>, "--execute"]
    assert argv[1] == str(fake_driver)
    assert argv[-5:] == ["--project", PROJECT, "--task", TASK, "--execute"]
    assert recorded["kwargs"]["timeout"] == 60
    # signal still written
    assert codex_audit.audit_discharged_path(PROJECT, TASK).exists()


def test_and_close_driver_missing_warns_rc0(handoff_home, monkeypatch, capsys):
    """--and-close with a missing driver → WARN, rc 0, signal still written, no subprocess."""
    called = []
    monkeypatch.setattr(
        codex_audit.subprocess, "run", lambda *a, **k: called.append((a, k))
    )
    rc = codex_audit.main_audit_discharge(
        [TASK, "--project", PROJECT, "--verdict", "GREEN", "--merge-sha", SHA,
         "--and-close", "--autoclose-driver", "/nonexistent/autoclose-audited-workers.py"]
    )
    assert rc == 0
    assert called == []  # never even tried to launch a missing driver
    err = capsys.readouterr().err
    assert "WARN audit-discharge-and-close" in err
    assert "driver not found" in err
    assert codex_audit.audit_discharged_path(PROJECT, TASK).exists()


def test_and_close_driver_timeout_warns_rc0(handoff_home, tmp_path, monkeypatch, capsys):
    """--and-close where the driver times out → WARN, rc 0, signal persists."""
    import subprocess as _sp

    fake_driver = tmp_path / "autoclose-audited-workers.py"
    fake_driver.write_text("# fake driver\n")

    def _raise_timeout(argv, **kwargs):
        raise _sp.TimeoutExpired(cmd=argv, timeout=60)

    monkeypatch.setattr(codex_audit.subprocess, "run", _raise_timeout)
    rc = codex_audit.main_audit_discharge(
        [TASK, "--project", PROJECT, "--verdict", "GREEN", "--merge-sha", SHA,
         "--and-close", "--autoclose-driver", str(fake_driver)]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "WARN audit-discharge-and-close" in err
    assert "timed out" in err
    # signal persists even though the close failed
    assert codex_audit.audit_discharged_path(PROJECT, TASK).exists()


def test_and_close_driver_nonzero_warns_rc0(handoff_home, tmp_path, monkeypatch, capsys):
    """--and-close where the driver exits non-zero → WARN, rc 0, signal persists."""
    fake_driver = tmp_path / "autoclose-audited-workers.py"
    fake_driver.write_text("# fake driver\n")

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "gate refused: spawn anchor missing\n"

    monkeypatch.setattr(codex_audit.subprocess, "run", lambda *a, **k: _Proc())
    rc = codex_audit.main_audit_discharge(
        [TASK, "--project", PROJECT, "--verdict", "GREEN", "--merge-sha", SHA,
         "--and-close", "--autoclose-driver", str(fake_driver)]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "WARN audit-discharge-and-close" in err
    assert "rc=1" in err
    assert codex_audit.audit_discharged_path(PROJECT, TASK).exists()
