# owner_ack_token + bypass-producer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the two owner-decision deferred items that gate codex-audit Phase D: (A) a real `owner_ack_token` verification for the `owner_override` disposition path (replacing the Phase B presence-only stub), and (B) the bypass sidecar *producer* that writes `ack/<task>.audit.override.json` when codex is genuinely unavailable (the Phase C scanner already reads it, dormant).

**Architecture:** Both items live in `codex_audit.py`. Component A is a binding checksum (`sha256(task|finding_hash|nonce|approved_at)`) written to an on-disk ack artifact + read/verified in `_gate_full`'s G7 branch under the audit lock. Component B is an automatic sidecar writer invoked inside `audit-close` when `--audit-mode codex_unavailable_bypass`. The trust model is **anti-tamper + friction, NOT cryptography** (owner ruling #1): an AI running as the owner can fabricate a self-consistent token; the token defends against *silent reuse* (finding_hash binding), *indefinite validity* (7d expiry), and *trace-less approval* (audit jsonl + button friction). This honest limitation must stay in code comments and docs — no "cryptographically secure" over-claim.

**Tech Stack:** Python 3.13, stdlib only (hashlib, json, datetime), pytest, ruff 0.15.x. Repo: `~/Projects/handoff-fanout` (NOT erp-system — cross-repo handoff).

**Locked owner rulings (design §1 — implementation may NOT alter):**
1. Trust = anti-tamper + friction (non-crypto). `owner_ack_token` is a binding checksum, not a secret.
2. Only `owner_override` (exempt a finding) needs owner button click; `codex_unavailable_bypass` auto-passes (machine evidence + forced re-audit debt).
3. Approval = AskUserQuestion single button (5/28 legislation).
4. `owner_override` validity = **7 days**.

**Locked constants:**
- `OWNER_ACK_TTL_DAYS = 7`
- `BYPASS_FOLLOW_UP_DEADLINE_DAYS = 1`
- `MIN_CODEX_FAILURES = 3`

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/handoff_fanout/codex_audit.py` | audit gate + builders + CLI | Add constants, `compute_owner_ack_token`, `owner_ack_path`, `write_owner_ack`, `load_owner_ack`, `bypass_override_path`, `write_bypass_override`; upgrade G7 in `_gate_full`; call producer in `main_audit_close`; append audit-trail lines | 
| `src/handoff_fanout/templates.py` | §-1.5 audit-close docs | Add owner-override button flow + bypass sidecar auto-produce note |
| `tests/test_audit_gate_phase_d.py` | new test module for A+B | Create — owner_ack binding/expiry/mismatch/missing/self-consistency; bypass producer + Phase C scanner end-to-end overdue |
| `tests/test_audit_gate_phase_b.py` | existing G7 tests | Modify 2 tests (`test_g7_override_with_ack_token_passes`, `test_r5_owner_override_rejects_nonstring_token`) that asserted the stub now require a real on-disk ack artifact |

**Helper naming (locked, used across tasks):**
- `compute_owner_ack_token(task: str, finding_hash: str, nonce: str, approved_at: str) -> str` → returns `"sha256:" + hexdigest`
- `owner_ack_path(project: str, task: str, finding_hash: str) -> Path` → `ack/<task>.owner_ack.<finding_hash_short>.json` where `finding_hash_short` = the 16 hex chars after `sha256:`
- `write_owner_ack(project, task, finding_hash, finding_title, nonce, approved_at, reason) -> dict` → writes artifact + appends `owner-ack-written` trail line, returns the artifact dict
- `load_owner_ack(project, task, finding_hash) -> dict | None` → reads + json-parses the artifact (None on missing/unreadable)
- `bypass_override_path(project: str, task: str) -> Path` → `ack/<task>.audit.override.json`
- `write_bypass_override(project, task, follow_up_audit_task_id, codex_failure_attempts, reason, created_at) -> dict` → writes sidecar (deadline = created_at + 1d), returns artifact dict
- `_finding_hash_short(finding_hash: str) -> str` → the 16-hex slug (raises ValueError on non-`sha256:` input)

---

## Design decision: `MIN_CODEX_FAILURES` vs existing `BYPASS_MIN_FAILURES`

The gate already has `BYPASS_MIN_FAILURES = 1` (Phase B floor; `_gate_bypass` rejects fewer). The design's `MIN_CODEX_FAILURES = 3` is the **producer-side honest threshold** (how many real failures define "codex unavailable" before `write_bypass_override` will emit the sidecar). These are intentionally distinct: the gate floor stays a loose sanity minimum (changing it risks Phase B regressions and is out of this task's scope), the producer enforces the stricter honest path. `write_bypass_override` raises `ValueError` when given fewer than `MIN_CODEX_FAILURES` attempts. This is flagged for the codex R-rounds to confirm.

---

## Task 1: Constants + `compute_owner_ack_token` + path helpers

**Files:**
- Modify: `src/handoff_fanout/codex_audit.py` (constants near line 557 `BYPASS_MIN_FAILURES`; helpers in the `# ─── paths ───` region ~line 96 and a new owner-ack section)
- Test: `tests/test_audit_gate_phase_d.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_audit_gate_phase_d.py`:

```python
"""Phase D pre-req — owner_ack_token verification (Component A) and the
codex_unavailable_bypass sidecar producer (Component B).

Trust model (design §1, owner ruling #1): anti-tamper + friction, NOT
cryptography. An AI running as the owner can fabricate a self-consistent
token; these tests verify the token defends against silent REUSE (finding_hash
binding), indefinite validity (7d expiry) and trace-less approval, not against
a malicious forger.
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


def test_compute_owner_ack_token_is_canonical_sha256():
    approved = "2026-05-30T00:00:00+00:00"
    tok = codex_audit.compute_owner_ack_token(TASK, FHASH, "nonce123", approved)
    expected = "sha256:" + hashlib.sha256(
        f"{TASK}\n{FHASH}\n{nonce}\n{approved}".encode("utf-8")
    ).hexdigest() if (nonce := "nonce123") else None
    assert tok == expected
    # deterministic
    assert tok == codex_audit.compute_owner_ack_token(TASK, FHASH, "nonce123", approved)
    # nonce changes the token
    assert tok != codex_audit.compute_owner_ack_token(TASK, FHASH, "nonce999", approved)


def test_owner_ack_path_uses_16hex_short():
    p = codex_audit.owner_ack_path(PROJECT, TASK, FHASH)
    assert p.name == f"{TASK}.owner_ack.{'a' * 16}.json"


def test_constants_match_design():
    assert codex_audit.OWNER_ACK_TTL_DAYS == 7
    assert codex_audit.BYPASS_FOLLOW_UP_DEADLINE_DAYS == 1
    assert codex_audit.MIN_CODEX_FAILURES == 3
```

(Note: the `:=` trick above is awkward — Step 3's real test will compute `expected` plainly. Use the version in Step 3.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/Projects/handoff-fanout && source .venv/bin/activate && python -m pytest tests/test_audit_gate_phase_d.py -x -q`
Expected: FAIL with `AttributeError: module 'handoff_fanout.codex_audit' has no attribute 'compute_owner_ack_token'`

- [ ] **Step 3: Replace the first test with the clean version + write implementation**

Clean test (replace `test_compute_owner_ack_token_is_canonical_sha256`):

```python
def test_compute_owner_ack_token_is_canonical_sha256():
    approved = "2026-05-30T00:00:00+00:00"
    nonce = "nonce123"
    tok = codex_audit.compute_owner_ack_token(TASK, FHASH, nonce, approved)
    expected = "sha256:" + hashlib.sha256(
        f"{TASK}\n{FHASH}\n{nonce}\n{approved}".encode("utf-8")
    ).hexdigest()
    assert tok == expected
    assert tok == codex_audit.compute_owner_ack_token(TASK, FHASH, nonce, approved)
    assert tok != codex_audit.compute_owner_ack_token(TASK, FHASH, "nonce999", approved)
```

Implementation in `codex_audit.py` — add constants beside `BYPASS_MIN_FAILURES`:

```python
# Owner-ack / bypass-producer constants (Phase D pre-req; design §2.2 / §3.1).
OWNER_ACK_TTL_DAYS = 7  # owner_override exemption validity (owner ruling #4)
BYPASS_FOLLOW_UP_DEADLINE_DAYS = 1  # short debt; next session should re-audit
MIN_CODEX_FAILURES = 3  # producer honest threshold (design §3.1; distinct from gate's BYPASS_MIN_FAILURES floor)
OWNER_ACK_SCHEMA_VERSION = "1.0"
BYPASS_OVERRIDE_SCHEMA_VERSION = "1.0"
SUPPORTED_OWNER_ACK_SCHEMA_VERSIONS = ("1.0",)
```

Add a new section (after the paths section, before disposition validation), with the honest-limitation docstring:

```python
# ─── owner_ack_token (Component A) — design §2 ──────────────────────────────
# TRUST MODEL (owner ruling #1, NOT cryptography): an AI running with the
# owner's identity can write any file and therefore fabricate a self-consistent
# token. This binding checksum defends against (a) reusing one finding's
# approval on a DIFFERENT finding (finding_hash binding), (b) an approval that
# never expires (7d TTL), and (c) a trace-less approval (audit jsonl trail +
# the AskUserQuestion button's friction). It does NOT defend against a
# malicious forger; that needs an owner-held key (deferred, see design §6).


def _finding_hash_short(finding_hash: str) -> str:
    """The 16 hex chars after ``sha256:`` — the ack artifact filename slug.

    Raising on a non-canonical hash keeps a malformed value from producing a
    surprising filename (e.g. one containing ``/`` or ``..``).
    """
    if not isinstance(finding_hash, str) or not _SHA256_REF_RE.match(finding_hash):
        raise ValueError(f"finding_hash must be sha256:<64 hex>; got {finding_hash!r}")
    return finding_hash[len("sha256:") : len("sha256:") + 16]


def compute_owner_ack_token(task: str, finding_hash: str, nonce: str, approved_at: str) -> str:
    """Binding checksum = ``sha256(task | finding_hash | nonce | approved_at)``.

    NOT a secret (see module trust-model note): it pins an approval to one
    (task, finding, nonce, approval-instant) tuple so it can't be silently
    re-pointed at another finding. Newline-joined canonical form.
    """
    canonical = f"{task}\n{finding_hash}\n{nonce}\n{approved_at}"
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def owner_ack_path(project: str, task: str, finding_hash: str) -> Path:
    """``$HANDOFF_HOME/<project>/ack/<task>.owner_ack.<short>.json``."""
    _validate_ids(project, task)
    short = _finding_hash_short(finding_hash)
    return _config.home_dir() / project / "ack" / f"{task}.owner_ack.{short}.json"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_audit_gate_phase_d.py -x -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/handoff-fanout
git add src/handoff_fanout/codex_audit.py tests/test_audit_gate_phase_d.py docs/plans/2026-05-30-owner-ack-token-and-bypass-producer.md
git commit -m "feat(audit-gate): Phase D pre-req — owner_ack_token constants + compute helper + path"
```

---

## Task 2: `write_owner_ack` + `load_owner_ack` + audit trail

**Files:**
- Modify: `src/handoff_fanout/codex_audit.py` (owner-ack section)
- Test: `tests/test_audit_gate_phase_d.py`

- [ ] **Step 1: Write the failing test**

```python
def test_write_and_load_owner_ack_roundtrip(handoff_home):
    art = codex_audit.write_owner_ack(
        PROJECT, TASK, FHASH, "the bug title", "nonce123",
        "2026-05-30T00:00:00+00:00", "exempt: false positive, see analysis",
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
    assert any(e.get("event") == "owner-ack-written" and e["finding_hash"] == FHASH for e in lines)


def test_load_owner_ack_missing_returns_none(handoff_home):
    assert codex_audit.load_owner_ack(PROJECT, TASK, FHASH) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_audit_gate_phase_d.py::test_write_and_load_owner_ack_roundtrip -x -q`
Expected: FAIL with `AttributeError: ... has no attribute 'write_owner_ack'`

- [ ] **Step 3: Write implementation**

Add to the owner-ack section in `codex_audit.py`:

```python
def _audit_trail_path(project: str, task: str) -> Path:
    """The closing-audit jsonl the Phase C scanner also appends to."""
    return _config.home_dir() / project / "ack" / f"{task}.audit.retry_audit.jsonl"


def _append_audit_trail(project: str, task: str, event: dict) -> None:
    """Append one JSON line to the task's audit trail (best-effort, fsync'd)."""
    path = _audit_trail_path(project, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()


def _add_days_iso(iso: str, days: int) -> str:
    """Return ``iso`` shifted by ``days``, normalized to an offset-aware ISO-8601."""
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt + timedelta(days=days)).isoformat()


def write_owner_ack(
    project: str,
    task: str,
    finding_hash: str,
    finding_title: str,
    nonce: str,
    approved_at: str,
    reason: str,
) -> dict:
    """Write the owner-ack artifact (after the owner clicks the button) and append
    an ``owner-ack-written`` trail line. Returns the artifact dict.

    expires_at = approved_at + OWNER_ACK_TTL_DAYS (owner ruling #4).
    """
    _validate_ids(project, task)
    token = compute_owner_ack_token(task, finding_hash, nonce, approved_at)
    expires_at = _add_days_iso(approved_at, OWNER_ACK_TTL_DAYS)
    artifact = {
        "schema_version": OWNER_ACK_SCHEMA_VERSION,
        "kind": "owner_ack",
        "task": task,
        "finding_hash": finding_hash,
        "finding_title": finding_title,
        "nonce": nonce,
        "approved_at": approved_at,
        "expires_at": expires_at,
        "reason": reason,
        "owner_ack_token": token,
    }
    path = owner_ack_path(project, task, finding_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_atomic(path, json.dumps(artifact, ensure_ascii=False, sort_keys=True) + "\n")
    _append_audit_trail(
        project,
        task,
        {
            "event": "owner-ack-written",
            "finding_hash": finding_hash,
            "nonce": nonce,
            "approved_at": approved_at,
            "expires_at": expires_at,
        },
    )
    return artifact


def load_owner_ack(project: str, task: str, finding_hash: str) -> dict | None:
    """Read the on-disk owner-ack artifact; ``None`` if missing / unreadable."""
    try:
        path = owner_ack_path(project, task, finding_hash)
    except ValueError:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
```

(Verify `atomic.write_atomic` is the correct primitive name first; if the module exposes a different name, use that — Step 3a.)

- [ ] **Step 3a: Verify the atomic write primitive name**

Run: `grep -n "^def write_atomic\|^def atomic_write\|^def write_" src/handoff_fanout/atomic.py`
Use whichever name the module exports in `write_owner_ack` / later writers.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_audit_gate_phase_d.py -x -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_fanout/codex_audit.py tests/test_audit_gate_phase_d.py
git commit -m "feat(audit-gate): Phase D pre-req — write/load owner_ack artifact + audit trail"
```

---

## Task 3: Upgrade G7 in `_gate_full` to verify the on-disk ack artifact

**Files:**
- Modify: `src/handoff_fanout/codex_audit.py:1283-1296` (G7 branch in `_gate_full`)
- Test: `tests/test_audit_gate_phase_d.py` + update `tests/test_audit_gate_phase_b.py`

This is the core enforcement change. The G7 branch must: keep the non-empty token check; load the ack artifact (exist + dict + schema in supported set); verify `ack.finding_hash == disposition.finding_hash`; recompute `sha256(task|finding_hash|nonce|approved_at) == ack.owner_ack_token == disposition.owner_ack_token`; require `expires_at` present + valid + not expired. Any failure → BLOCKED with the existing subcodes.

`_gate_full` receives `project` and `task` already (its signature) so it can resolve the ack path. The disposition carries `owner_ack_token` and `finding_hash` (= `fhash`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_audit_gate_phase_d.py`. First a shared helper mirroring the Phase B gate harness (self-contained so this module doesn't import Phase B internals):

```python
PROJECT_WS = "demo"


def _ws(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    for args in (
        ["git", "init", "--quiet", "--initial-branch=main"],
        ["git", "config", "user.email", "t@t.test"],
        ["git", "config", "user.name", "t"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(args, cwd=ws, check=True)
    (ws / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)
    monkeypatch.chdir(ws)
    return ws


def _head(ws):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, check=True
    ).stdout.strip()


def _gate_override(handoff_home, ws, *, disp_overrides=None, write_ack=None):
    """Build a full-audit block with one P0 finding owner_override'd; return outcome.

    write_ack: dict of kwargs for write_owner_ack, or None to skip writing it.
    disp_overrides: dict merged into the disposition (to inject mismatches).
    """
    head = _head(ws)
    finding = {"id": "F1", "severity": "P0", "title": "bug F1"}
    rec = codex_audit.write_findings_artifact(
        PROJECT_WS, TASK, 1,
        {"run_index": 1, "input_commit": head, "original_findings": [finding]},
        input_commit=head,
    )
    fhash = codex_audit.compute_finding_hash(finding)
    approved = datetime.now(UTC).isoformat(timespec="seconds")
    nonce = "nonce-xyz"
    if write_ack is not None:
        codex_audit.write_owner_ack(
            PROJECT_WS, TASK, write_ack.get("finding_hash", fhash), "bug F1",
            write_ack.get("nonce", nonce), write_ack.get("approved_at", approved),
            "exempt: false positive",
        )
    token = codex_audit.compute_owner_ack_token(TASK, fhash, nonce, approved)
    disp = {
        "finding_id": "F1", "finding_hash": fhash, "original_severity": "P0",
        "disposition": "owner_override", "owner_ack_token": token,
        "expires_at": codex_audit._add_days_iso(approved, 7),
    }
    if disp_overrides:
        disp.update(disp_overrides)
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    p0 = {k: {"status": "✅"} for k in handoff_precheck.PHASE0_KEYS}
    p1 = {k: {"status": "✅"} for k in handoff_precheck.PHASE1_KEYS}
    payload = handoff_precheck.build_evidence(
        task_id=TASK, project=PROJECT_WS, workspace=ws, phase0=p0, phase1=p1, codex_audit=block
    )
    return codex_audit.evaluate_audit_gate(payload, ws, PROJECT_WS, TASK)


def test_g7_override_with_valid_ack_passes(handoff_home, tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    out = _gate_override(handoff_home, ws, write_ack={})
    assert out.ok, (out.klass, out.subcode, out.detail)


def test_g7_override_no_ack_artifact_blocked(handoff_home, tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    out = _gate_override(handoff_home, ws, write_ack=None)  # token present but no file
    assert out.klass == "blocked"
    assert out.subcode == "codex-audit-override-no-ack-token"


def test_g7_override_token_mismatch_blocked(handoff_home, tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    # ack on disk is for a DIFFERENT nonce → recomputed token won't match disposition
    out = _gate_override(handoff_home, ws, write_ack={"nonce": "other-nonce"})
    assert out.klass == "blocked"
    assert out.subcode == "codex-audit-override-invalid"


def test_g7_override_finding_hash_binding_mismatch_blocked(handoff_home, tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    # disposition claims a different finding_hash than its ack artifact binds
    other = "sha256:" + "b" * 64
    out = _gate_override(handoff_home, ws, write_ack={}, disp_overrides={"finding_hash": other})
    # finding_hash mismatch means the union finding has NO disposition → unbound
    assert out.klass in ("retry", "blocked")


def test_g7_override_expired_ack_blocked(handoff_home, tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    past = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    out = _gate_override(
        handoff_home, ws,
        write_ack={"approved_at": past},
        disp_overrides={"expires_at": codex_audit._add_days_iso(past, 7)},
    )
    assert out.klass == "blocked"
    assert out.subcode == "codex-audit-override-invalid"
```

- [ ] **Step 2: Run tests to verify they fail correctly**

Run: `python -m pytest tests/test_audit_gate_phase_d.py -x -q -k g7`
Expected: `test_g7_override_with_valid_ack_passes` FAILS (current stub passes on token presence but doesn't write/verify against disk — actually it would PASS with token presence; the real failures are the mismatch/no-artifact tests which the stub lets through). The point: `test_g7_override_no_ack_artifact_blocked` and `test_g7_override_token_mismatch_blocked` FAIL under the stub (stub passes them). Confirm those two fail.

- [ ] **Step 3: Write the G7 upgrade**

Replace the G7 branch (lines ~1283-1296) in `_gate_full`:

```python
        # G7: an owner override must be backed by a real on-disk owner-ack
        # artifact, bound to THIS finding, self-consistent, and unexpired.
        # TRUST MODEL (design §1 / owner ruling #1): this is anti-tamper +
        # friction, NOT cryptography — an AI with the owner's identity can write
        # a self-consistent ack. It catches silent reuse (finding_hash binding),
        # indefinite validity (expiry), and trace-less approval; it does NOT stop
        # a malicious forger (deferred owner-held-key design §6).
        elif disp == _pc.DISPOSITION_OWNER_OVERRIDE:
            token = _nonempty_str(d.get("owner_ack_token"))
            if not token:
                return AuditGateOutcome(
                    "blocked",
                    "codex-audit-override-no-ack-token",
                    f"finding {fhash} owner_override without a non-empty owner_ack_token",
                )
            ack = load_owner_ack(project, task, fhash)
            if not isinstance(ack, dict):
                return AuditGateOutcome(
                    "blocked",
                    "codex-audit-override-no-ack-token",
                    f"finding {fhash} owner_override has no on-disk owner-ack artifact",
                )
            if ack.get("schema_version") not in SUPPORTED_OWNER_ACK_SCHEMA_VERSIONS:
                return AuditGateOutcome(
                    "blocked",
                    "codex-audit-override-invalid",
                    f"finding {fhash} owner-ack schema_version "
                    f"{ack.get('schema_version')!r} unsupported",
                )
            # Binding: the ack must be FOR this finding (catch reuse on another).
            if ack.get("finding_hash") != fhash:
                return AuditGateOutcome(
                    "blocked",
                    "codex-audit-override-invalid",
                    f"finding {fhash} owner-ack binds a different finding "
                    f"{ack.get('finding_hash')!r}",
                )
            # Self-consistency: recompute the binding checksum; the disposition
            # token, the ack token, and the recompute must all agree.
            nonce = ack.get("nonce")
            approved_at = ack.get("approved_at")
            if not isinstance(nonce, str) or not isinstance(approved_at, str):
                return AuditGateOutcome(
                    "blocked",
                    "codex-audit-override-invalid",
                    f"finding {fhash} owner-ack missing nonce/approved_at",
                )
            recomputed = compute_owner_ack_token(task, fhash, nonce, approved_at)
            if not (recomputed == ack.get("owner_ack_token") == token):
                return AuditGateOutcome(
                    "blocked",
                    "codex-audit-override-invalid",
                    f"finding {fhash} owner-ack token not self-consistent",
                )
            exp = ack.get("expires_at")
            if not _nonempty_str(exp) or _is_expired(exp):
                return AuditGateOutcome(
                    "blocked",
                    "codex-audit-override-invalid",
                    f"finding {fhash} owner_override ack expired/invalid at {exp!r}",
                )
            _append_audit_trail(
                project,
                task,
                {
                    "event": "owner-override-consumed",
                    "finding_hash": fhash,
                    "nonce": nonce,
                    "approved_at": approved_at,
                    "expires_at": exp,
                },
            )
```

- [ ] **Step 4: Run the Phase D tests**

Run: `python -m pytest tests/test_audit_gate_phase_d.py -x -q`
Expected: PASS

- [ ] **Step 5: Update the now-stale Phase B G7 tests**

Two Phase B tests asserted the stub behavior. Update them to write a real ack OR re-target them at the no-ack BLOCKED case.

In `tests/test_audit_gate_phase_b.py`, replace `test_g7_override_with_ack_token_passes` (line ~471):

```python
def test_g7_override_with_ack_token_passes(handoff_home, workspace):
    head = _head(workspace)
    f = _finding("P0", "F1")
    rec = _write_run(workspace, 1, head, [f])
    fhash = codex_audit.compute_finding_hash(f)
    approved = datetime.now(UTC).isoformat(timespec="seconds")
    nonce = "n1"
    codex_audit.write_owner_ack(PROJECT, TASK, fhash, "bug", nonce, approved, "exempt")
    token = codex_audit.compute_owner_ack_token(TASK, fhash, nonce, approved)
    disp = _disp(
        "owner_override", fhash, severity="P0",
        owner_ack_token=token, expires_at=codex_audit._add_days_iso(approved, 7),
    )
    block = {"audit_mode": "full_codex_audit", "audit_runs": [rec], "dispositions": [disp]}
    assert _gate(workspace, block).ok
```

`test_g7_override_without_ack_token_blocked` (no token) stays valid — the new G7 still blocks a missing token with the same subcode. `test_g7_override_expired_blocked` and `test_r5_owner_override_rejects_nonstring_token` also stay valid (a non-string / missing token blocks before the artifact load). Run them to confirm:

Run: `python -m pytest tests/test_audit_gate_phase_b.py -q -k "g7 or owner_override"`
Expected: PASS (after the one edit above)

- [ ] **Step 6: Commit**

```bash
git add src/handoff_fanout/codex_audit.py tests/test_audit_gate_phase_d.py tests/test_audit_gate_phase_b.py
git commit -m "feat(audit-gate): Phase D pre-req — G7 verifies on-disk owner-ack (binding+self-consistency+expiry)"
```

---

## Task 4: bypass sidecar producer `write_bypass_override`

**Files:**
- Modify: `src/handoff_fanout/codex_audit.py` (new bypass-producer section)
- Test: `tests/test_audit_gate_phase_d.py`

- [ ] **Step 1: Write the failing test**

```python
def _attempts(n):
    return [
        {"exit": 1, "stderr_hash": "sha256:" + "c" * 64, "timestamp": f"2026-05-30T0{i}:00:00+00:00"}
        for i in range(n)
    ]


def test_write_bypass_override_schema_and_deadline(handoff_home):
    created = "2026-05-30T00:00:00+00:00"
    art = codex_audit.write_bypass_override(
        PROJECT, TASK, "redo-audit-x", _attempts(3), "codex unavailable: timeout", created
    )
    assert art["schema_version"] == "1.0"
    assert art["kind"] == "codex_audit_bypass"
    assert art["task"] == TASK
    assert art["follow_up_audit_task_id"] == "redo-audit-x"
    assert art["follow_up_deadline"] == "2026-05-31T00:00:00+00:00"  # created + 1d
    assert len(art["codex_failure_attempts"]) == 3
    # on disk at the scanner-contract path
    p = codex_audit.bypass_override_path(PROJECT, TASK)
    assert p.name == f"{TASK}.audit.override.json"
    assert json.loads(p.read_text()) == art


def test_write_bypass_override_too_few_failures_rejected(handoff_home):
    with pytest.raises(ValueError, match="MIN_CODEX_FAILURES|at least"):
        codex_audit.write_bypass_override(
            PROJECT, TASK, "redo-audit-x", _attempts(2), "codex down", "2026-05-30T00:00:00+00:00"
        )


def test_write_bypass_override_bad_follow_id_rejected(handoff_home):
    with pytest.raises(ValueError, match="follow_up_audit_task_id|slug"):
        codex_audit.write_bypass_override(
            PROJECT, TASK, "Bad Id!", _attempts(3), "codex down", "2026-05-30T00:00:00+00:00"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_audit_gate_phase_d.py -x -q -k bypass`
Expected: FAIL with `AttributeError: ... 'write_bypass_override'`

- [ ] **Step 3: Write implementation**

Add a bypass-producer section in `codex_audit.py`:

```python
# ─── bypass sidecar producer (Component B) — design §3 ──────────────────────
# When codex is genuinely unavailable, audit-close auto-writes this sidecar so
# the Phase C overdue scanner (auto-continue.sh scan_overdue_kind) and the dump
# gate (_check_follow_up_overdue) can enforce the re-audit debt. No owner click:
# codex being down is a MACHINE fact (owner ruling #2); the safety net is the
# machine failure proof + the forced follow-up + the overdue deadline.


def bypass_override_path(project: str, task: str) -> Path:
    """``$HANDOFF_HOME/<project>/ack/<task>.audit.override.json`` — the sidecar the
    Phase C scanner reads (follow_up_audit_task_id + follow_up_deadline)."""
    _validate_ids(project, task)
    return _config.home_dir() / project / "ack" / f"{task}.audit.override.json"


def write_bypass_override(
    project: str,
    task: str,
    follow_up_audit_task_id: str,
    codex_failure_attempts: list[dict],
    reason: str,
    created_at: str,
) -> dict:
    """Write the codex_unavailable_bypass sidecar. Returns the artifact dict.

    Validates the honest-path threshold (>= MIN_CODEX_FAILURES machine-proven
    failures) and the follow-up slug (isinstance str + fullmatch — same contract
    as build_codex_audit_block / _gate_bypass / forced_follow_up_task). Deadline
    = created_at + BYPASS_FOLLOW_UP_DEADLINE_DAYS.
    """
    _validate_ids(project, task)
    if not isinstance(follow_up_audit_task_id, str) or not _pc.TASK_ID_RE.fullmatch(
        follow_up_audit_task_id
    ):
        raise ValueError(
            "follow_up_audit_task_id must be a slug [a-z0-9-] "
            f"(got {follow_up_audit_task_id!r})"
        )
    if not isinstance(codex_failure_attempts, list) or len(codex_failure_attempts) < MIN_CODEX_FAILURES:
        raise ValueError(
            f"bypass needs at least MIN_CODEX_FAILURES={MIN_CODEX_FAILURES} "
            f"machine-proven codex failures; got {len(codex_failure_attempts) if isinstance(codex_failure_attempts, list) else 'non-list'}"
        )
    for a in codex_failure_attempts:
        if not isinstance(a, dict):
            raise ValueError("each codex_failure_attempt must be an object")
        if not isinstance(a.get("exit"), int) or isinstance(a.get("exit"), bool):
            raise ValueError("codex_failure_attempt.exit must be an int")
        if not _SHA256_REF_RE.match(str(a.get("stderr_hash", ""))):
            raise ValueError("codex_failure_attempt.stderr_hash must be sha256:<64 hex>")
        if not isinstance(a.get("timestamp"), str) or not a["timestamp"].strip():
            raise ValueError("codex_failure_attempt.timestamp must be a non-empty string")
    deadline = _add_days_iso(created_at, BYPASS_FOLLOW_UP_DEADLINE_DAYS)
    artifact = {
        "schema_version": BYPASS_OVERRIDE_SCHEMA_VERSION,
        "kind": "codex_audit_bypass",
        "task": task,
        "follow_up_audit_task_id": follow_up_audit_task_id,
        "follow_up_deadline": deadline,
        "codex_failure_attempts": list(codex_failure_attempts),
        "created_at": created_at,
        "reason": reason,
    }
    path = bypass_override_path(project, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_atomic(path, json.dumps(artifact, ensure_ascii=False, sort_keys=True) + "\n")
    _append_audit_trail(
        project,
        task,
        {
            "event": "bypass-override-written",
            "follow_up_audit_task_id": follow_up_audit_task_id,
            "follow_up_deadline": deadline,
            "failure_count": len(codex_failure_attempts),
        },
    )
    return artifact
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_audit_gate_phase_d.py -x -q -k bypass`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_fanout/codex_audit.py tests/test_audit_gate_phase_d.py
git commit -m "feat(audit-gate): Phase D pre-req — bypass sidecar producer write_bypass_override"
```

---

## Task 5: Wire producer into `main_audit_close` (bypass mode)

**Files:**
- Modify: `src/handoff_fanout/codex_audit.py:main_audit_close` (~1510-1648)
- Test: `tests/test_audit_gate_phase_d.py`

When `--audit-mode codex_unavailable_bypass`, after `build_codex_audit_block` succeeds (so the bypass fields are already validated), write the sidecar from the parsed `bypass` dict, inside the held lock. `created_at` is computed from `datetime.now(UTC)` (the audit-close moment).

- [ ] **Step 1: Write the failing test (end-to-end via audit-close + Phase C scanner)**

This test exercises the full loop: audit-close writes the sidecar; then simulate the Phase C scanner by checking the sidecar fields the scanner reads. We invoke `main_audit_close` with a bypass file. Because audit-close calls `dump.main`, set up a real workspace.

```python
def test_audit_close_bypass_writes_sidecar(handoff_home, tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    bypass = {
        "codex_failure_attempts": _attempts(3),
        "follow_up_audit_task_id": "redo-audit-next",
    }
    bypass_file = tmp_path / "bypass.json"
    bypass_file.write_text(json.dumps(bypass))
    argv = [
        "--task", TASK, "--project", PROJECT_WS, "--workspace", str(ws),
        "--next", "next brief", "--audit-mode", "codex_unavailable_bypass",
        "--bypass-file", str(bypass_file), "--status", "active",
    ]
    for k in handoff_precheck.PHASE0_KEYS:
        argv += ["--phase0-status", f"{k}=✅"]
    for k in handoff_precheck.PHASE1_KEYS:
        argv += ["--phase1-status", f"{k}=✅"]
    rc = codex_audit.main_audit_close(argv)
    assert rc == 0, rc
    sidecar = codex_audit.bypass_override_path(PROJECT_WS, TASK)
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["follow_up_audit_task_id"] == "redo-audit-next"
    assert data["kind"] == "codex_audit_bypass"
    # the follow-up deadline must be parseable ISO-8601 (scanner contract)
    datetime.fromisoformat(data["follow_up_deadline"].replace("Z", "+00:00"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_audit_gate_phase_d.py::test_audit_close_bypass_writes_sidecar -x -q`
Expected: FAIL (sidecar not written — `sidecar.exists()` is False)

- [ ] **Step 3: Write implementation**

In `main_audit_close`, after the `build_codex_audit_block(...)` try/except succeeds and before/after `build_evidence`, inside the lock, add:

```python
            # Component B: when codex is unavailable, auto-emit the bypass
            # sidecar the Phase C overdue scanner reads (design §3 / owner
            # ruling #2 — no owner click; codex down is a machine fact).
            if args.audit_mode == _pc.AUDIT_MODE_BYPASS and bypass is not None:
                try:
                    write_bypass_override(
                        project,
                        args.task,
                        bypass.get("follow_up_audit_task_id"),
                        bypass.get("codex_failure_attempts") or [],
                        bypass.get("reason") or "codex unavailable",
                        datetime.now(UTC).isoformat(timespec="seconds"),
                    )
                except ValueError as e:
                    sys.stderr.write(f"ERR-FATAL bypass-sidecar-invalid: {e}\n")
                    return 1
```

Place this immediately after the `block = build_codex_audit_block(...)` except-block (so the fields are pre-validated by the builder) and before `evidence = _pc.build_evidence(...)`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_audit_gate_phase_d.py -x -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all PASS (was 377; now 377 + new Phase D tests, minus none — the 2 Phase B edits stay green)

- [ ] **Step 6: Commit**

```bash
git add src/handoff_fanout/codex_audit.py tests/test_audit_gate_phase_d.py
git commit -m "feat(audit-gate): Phase D pre-req — audit-close auto-writes bypass sidecar"
```

---

## Task 6: Docs — `templates.py` §-1.5 owner-override button + bypass auto-produce

**Files:**
- Modify: `src/handoff_fanout/templates.py` (§-1.5 audit-close section, ~line 260-300)
- Test: `tests/test_audit_gate_phase_c.py` or a docs-presence assertion in Phase D test

- [ ] **Step 1: Find the §-1.5 section**

Run: `grep -n "§-1.5\|audit-close\|bypass = 欠债\|owner_override\|owner_ack" src/handoff_fanout/templates.py`

- [ ] **Step 2: Add the owner-override button flow + bypass auto-produce note**

Add to the §-1.5 docs string (Chinese, matching surrounding style), documenting:
- owner_override 路径: AI 遇到主张豁免的 P0/P1 → AskUserQuestion 单按钮「确认豁免」→ 主人点击 → AI 写 `ack/<task>.owner_ack.<short>.json`（绑定 finding_hash + 7天过期）→ disposition 带 owner_ack_token。
- bypass 路径: codex 真不可用（≥3 次机器失败）→ audit-close 自动写 `ack/<task>.audit.override.json`（follow_up_audit_task_id + 1天 deadline），无需主人点击。
- 诚实声明: token = 防篡改+摩擦，非加密；不防恶意 AI 伪造主人批准（design §1/§6）。

- [ ] **Step 3: Assert the docs presence (regression guard)**

Add to `tests/test_audit_gate_phase_d.py`:

```python
def test_templates_document_owner_override_and_bypass():
    from handoff_fanout import templates

    src = Path(templates.__file__).read_text(encoding="utf-8")
    assert "owner_ack" in src
    assert "audit.override.json" in src
    # honest trust-model disclaimer must survive
    assert "非加密" in src or "not cryptograph" in src.lower()
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_audit_gate_phase_d.py::test_templates_document_owner_override_and_bypass -x -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_fanout/templates.py tests/test_audit_gate_phase_d.py
git commit -m "docs(audit-gate): Phase D pre-req — §-1.5 owner-override button + bypass sidecar flow"
```

---

## Task 7: ruff + full suite + 4-round codex audit + push + CI

- [ ] **Step 1: ruff**

Run: `ruff check src/ tests/ && ruff format --check src/ tests/`
Expected: clean (fix + `ruff format` if needed).

- [ ] **Step 2: Full suite**

Run: `python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 3: 4-round codex audit (≥5 files architecture rule)**

For each round R1-R4, `touch ~/.claude-handoff/erp-system/STOP_AUTO` first, run `timeout 480 codex exec --skip-git-repo-check "<prompt>" </dev/null`, then `rm` the STOP_AUTO. Prompts:
- R1 correctness/security: G7 verification logic, traversal, fail-closed paths, token canonicalization.
- R2 impl re-audit: code-detail gaps in the diff.
- R3 business goal: does this actually deliver the design's two deferred items + honest trust model; is the owner-button friction real.
- R4 data/migration: artifact schema, scanner contract alignment, follow-id 3-way consistency, no mutation of Phase C's existing bytes.

Fix every P0/P1 finding (owner #1 legislation — no deferral).

- [ ] **Step 4: Commit fixes, push**

```bash
git push origin main
gh run list --limit 1
gh run view <id>  # confirm CI green (Linux ≠ local macOS)
```

- [ ] **Step 5: Memory lesson + MEMORY.md index**

Write `~/.claude/projects/-Users-chenmingzhong-Projects-erp-system/memory/lesson-codex-audit-gate-phase-d-prereq-owner-ack-bypass-2026-05-30.md` + one MEMORY.md index line.

- [ ] **Step 6: Handoff dump (next baton)**

Per §-1 SOP: `handoff precheck` + `handoff dump --retro-evidence` (or `handoff audit-close` since code changed). Next task = codex-audit of this impl OR Phase D mandate flip design confirm — decide at closure + 三段式预告 to owner. Do NOT flip `HANDOFF_AUDIT_MANDATE` here.

---

## Self-Review

**Spec coverage:**
- design §2 (owner_ack_token): Tasks 1-3 (compute, write/load, G7 verify) ✓
- design §2.4 (audit trail): Task 2 (`owner-ack-written`) + Task 3 (`owner-override-consumed`) ✓
- design §3 (bypass producer): Tasks 4-5 ✓
- design §3.2 schema + scanner contract: Task 4 (fields) + Task 5 (end-to-end) ✓
- design §4 改动清单: codex_audit ✓ / templates ✓ / tests ✓. (`cli.py` no new subcommand — design says "主流走 AI 写"; AskUserQuestion writes via `write_owner_ack`, no CLI entry needed.)
- design §1 honest disclaimer: in module docstrings (Task 1/3/4) + templates (Task 6) + test guard (Task 6) ✓
- Locked constants (7d / 1d / 3): Task 1 + `test_constants_match_design` ✓

**Placeholder scan:** none — every code step has full code.

**Type consistency:** `compute_owner_ack_token`, `owner_ack_path`, `write_owner_ack`, `load_owner_ack`, `bypass_override_path`, `write_bypass_override`, `_add_days_iso`, `_append_audit_trail`, `_finding_hash_short` used consistently across tasks. `atomic.write_atomic` to be confirmed in Task 2 Step 3a (use the real exported name).

**Open item for codex rounds:** `MIN_CODEX_FAILURES=3` (producer) vs `BYPASS_MIN_FAILURES=1` (gate floor) intentional divergence — confirm not a hole.
