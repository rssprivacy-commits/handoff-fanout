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
    expected = "sha256:" + hashlib.sha256(
        f"{TASK}\n{FHASH}\n{nonce}\n{approved}".encode()
    ).hexdigest()
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
    assert any(
        e.get("event") == "owner-ack-written" and e["finding_hash"] == FHASH for e in lines
    )


def test_load_owner_ack_missing_returns_none(handoff_home):
    assert codex_audit.load_owner_ack(PROJECT, TASK, FHASH) is None
