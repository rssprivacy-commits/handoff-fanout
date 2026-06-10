"""Step1 G4 收口 — one-time succession authority tokens (tribrain MUST#1).

``handoff spawn --role supervisor_succession`` is the ONLY spawn path that closes a
predecessor (coordinator) window. Before Step1 it was a bare public CLI — a coordinator
could relay (交棒) through it with ZERO retro gate (root cause G4). Step1 locks it:

  * a MANUAL CLI succession spawn (no ``--succession-token``) is REJECTED, with the
    error pointing at the retro-gated path (``handoff audit-close --coordinator
    --status active``);
  * ``audit-close`` issues a one-time, path/permission-based token (0600, TTL-bound)
    ONLY AFTER the retro gate passed; ``spawn`` validates + CONSUMES it (unlink) —
    replay / expiry / project-mismatch / tamper all fail closed.

The internal-vs-manual discriminator is PATH/permission-based (a token file under
``$HANDOFF_HOME/<project>/authority/``) — never session-name or content sniffing
(§6b red line).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from handoff_fanout import spawn
from handoff_fanout import succession_authority as _authority

PROJECT = "wilde-hexe"
TASK = "wh-succ-next"
CLOSING_TASK = "wh-coord-leg"
PRED_NONCE = "feedfacecafebeef"


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "HANDOFF_RETRO_MANDATE",
        "HANDOFF_RETRO_BYPASS",
        "HANDOFF_AUDIT_MANDATE",
        "HANDOFF_WORKTREE_ISOLATION",
    ):
        monkeypatch.delenv(var, raising=False)


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str = "{}") -> Path:
    home = tmp_path / "handoff"
    home.mkdir()
    (home / "config.json").write_text(config)
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    return home


def _plain_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n")
    return repo


def _succession_argv(
    repo: Path,
    *,
    task: str = TASK,
    project: str = PROJECT,
    token: str | None = None,
) -> list[str]:
    argv = [
        "--project",
        project,
        "--task-id",
        task,
        "--role",
        "supervisor_succession",
        "--isolation",
        "singlepane",
        "--workspace",
        str(repo),
        "--prompt",
        "succeed the coordinator",
        "--predecessor-nonce",
        PRED_NONCE,
    ]
    if token is not None:
        argv += ["--succession-token", token]
    return argv


# ─── G4 收口: the manual CLI entry is closed (discriminating test) ────────────


def test_manual_succession_without_token_rejected(tmp_path, monkeypatch, capsys):
    """A bare ``handoff spawn --role supervisor_succession`` (no token) must be
    REJECTED and point the operator at the retro-gated path. On pre-Step1 code this
    spawn succeeded (rc 0 + artifacts) — this test FAILING there is the proof of
    discriminating power the brief requires."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)

    rc = spawn.main(_succession_argv(repo))

    assert rc == 2, "manual succession spawn must fail closed without a token"
    err = capsys.readouterr().err
    assert "audit-close --coordinator --status active" in err, (
        "rejection must point at the retro-gated path"
    )
    queue = home / PROJECT / "queue"
    assert not (queue / f"{TASK}.uri").exists(), "no spawn intent may be published"
    assert not (queue / f"{TASK}.singlepane").exists(), "no sidecar may be published"


# ─── the retro-gated key: valid token → spawn + consume ───────────────────────


def test_succession_with_valid_token_spawns_and_consumes(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    token = _authority.issue_token(home=home, project=PROJECT, task=CLOSING_TASK)
    assert token.exists()
    assert (token.stat().st_mode & 0o777) == 0o600, "token must be owner-only"

    rc = spawn.main(_succession_argv(repo, token=str(token)))

    assert rc == 0
    sc = json.loads((home / PROJECT / "queue" / f"{TASK}.singlepane").read_text())
    assert sc["role"] == "supervisor_succession"
    assert sc["close_policy"] == "close_predecessor"
    assert sc["predecessor_nonce"] == PRED_NONCE
    assert not token.exists(), "the authority is one-time — consumed on use"
    log = (home / PROJECT / "authority" / _authority.AUDIT_LOG_NAME).read_text()
    assert "ISSUED" in log and "CONSUMED" in log, "issue+consume must be auditable"


def test_token_replay_rejected(tmp_path, monkeypatch, capsys):
    """One-time means ONE spawn: replaying the same (now consumed) token fails closed."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    token = _authority.issue_token(home=home, project=PROJECT, task=CLOSING_TASK)
    assert spawn.main(_succession_argv(repo, token=str(token))) == 0

    rc = spawn.main(_succession_argv(repo, task="wh-succ-replay", token=str(token)))

    assert rc == 2
    err = capsys.readouterr().err
    assert "succession authority rejected" in err
    queue = home / PROJECT / "queue"
    assert not (queue / "wh-succ-replay.uri").exists()
    log = (home / PROJECT / "authority" / _authority.AUDIT_LOG_NAME).read_text()
    assert "REJECTED" in log


def test_token_expired_rejected(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    token = _authority.issue_token(home=home, project=PROJECT, task=CLOSING_TASK)
    # Cross the TTL by moving the module clock, not by sleeping.
    expired_at = datetime.now(UTC) + timedelta(seconds=_authority.TOKEN_TTL_SECONDS + 1)
    monkeypatch.setattr(_authority, "_now", lambda: expired_at)

    rc = spawn.main(_succession_argv(repo, token=str(token)))

    assert rc == 2
    assert "expired" in capsys.readouterr().err
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_token_for_other_project_rejected(tmp_path, monkeypatch, capsys):
    """A token issued under project A's authority dir must not authorize a spawn into
    project B (path containment is part of the identity)."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    token = _authority.issue_token(home=home, project="other-proj", task=CLOSING_TASK)

    rc = spawn.main(_succession_argv(repo, token=str(token)))

    assert rc == 2
    assert "succession authority rejected" in capsys.readouterr().err
    assert not (home / PROJECT / "queue" / f"{TASK}.uri").exists()


def test_token_tampered_payload_rejected(tmp_path, monkeypatch, capsys):
    """A payload whose identity disagrees with the filename (renamed/forged file) is
    rejected — the filename↔payload binding is part of the authority."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    token = _authority.issue_token(home=home, project=PROJECT, task=CLOSING_TASK)
    payload = json.loads(token.read_text())
    payload["nonce"] = "0" * 16  # disagree with the filename nonce
    token.write_text(json.dumps(payload))

    rc = spawn.main(_succession_argv(repo, token=str(token)))

    assert rc == 2
    assert "succession authority rejected" in capsys.readouterr().err


def test_token_loose_permissions_rejected(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    token = _authority.issue_token(home=home, project=PROJECT, task=CLOSING_TASK)
    token.chmod(0o644)

    rc = spawn.main(_succession_argv(repo, token=str(token)))

    assert rc == 2
    assert "succession authority rejected" in capsys.readouterr().err


def test_worker_spawn_with_token_rejected(tmp_path, monkeypatch, capsys):
    """--succession-token on a worker spawn is contradictory metadata — fail closed
    (and never consume the token)."""
    home = _home(tmp_path, monkeypatch)
    repo = _plain_repo(tmp_path)
    token = _authority.issue_token(home=home, project=PROJECT, task=CLOSING_TASK)

    rc = spawn.main(
        [
            "--project",
            PROJECT,
            "--task-id",
            "wh-worker",
            "--role",
            "worker",
            "--isolation",
            "singlepane",
            "--workspace",
            str(repo),
            "--prompt",
            "work",
            "--succession-token",
            str(token),
        ]
    )

    assert rc == 2
    assert "only valid with --role supervisor_succession" in capsys.readouterr().err
    assert token.exists(), "a rejected worker spawn must not burn the authority"


def test_expired_tokens_swept_on_next_issue(tmp_path, monkeypatch):
    """Issuing sweeps expired token files so authority can't accumulate on disk."""
    home = _home(tmp_path, monkeypatch)
    old = _authority.issue_token(home=home, project=PROJECT, task=CLOSING_TASK)
    future = datetime.now(UTC) + timedelta(seconds=_authority.TOKEN_TTL_SECONDS + 1)
    monkeypatch.setattr(_authority, "_now", lambda: future)

    fresh = _authority.issue_token(home=home, project=PROJECT, task="wh-coord-leg2")

    assert not old.exists(), "expired token must be swept"
    assert fresh.exists()
