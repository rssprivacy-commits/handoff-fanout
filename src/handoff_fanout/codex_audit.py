"""Phase A — codex audit gate: builders, disposition validation, runtime
findings artifacts (sidecar manifest), and the ``audit-run`` / ``audit-disposition``
/ ``audit-close`` CLI surface.

This module is the *evidence capability* layer of the audit-before-handoff gate
(design / spec v0.2). It does NOT add any G0-G9 gating to ``retro_gate`` — that
is Phase B. With the audit mandate OFF (the only state Phase A ships in), a
5.5.0 evidence carrying a ``codex_audit`` block passes the retro gate exactly
like one without it; the block is recorded, not yet enforced.

Authority principle (R1/R2): the *machine artifact* is the source of truth.
codex emits a structured ``codex-findings.json``; its hash lives in a **sidecar
manifest** (a JSON can't contain its own hash — R2-P0-3); evidence stores only
the per-finding *dispositions*, each bound to an original finding id + hash.

Runtime artifacts live under ``$HANDOFF_HOME/<project>/audit/<task>/<run>/`` and
are written via the shared atomic + fsync primitives. Evidence references them
by canonical *relative* path (relative to ``$HANDOFF_HOME/<project>/``), never an
absolute path (spec §3.4).

Spec source of truth: ``project-files/handoff/codex-audit-gate-spec-draft.md``
v0.2 and ``codex-audit-gate-design.md`` v0.2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from handoff_fanout import atomic
from handoff_fanout import config as _config
from handoff_fanout import handoff_precheck as _pc

FINDINGS_FILENAME = "codex-findings.json"
MANIFEST_FILENAME = "codex-findings.json.manifest"
DISPOSITIONS_FILENAME = "dispositions.json"
CANONICAL_DESC = "sorted-keys,utf-8,no-bom,lf"

# Machine-verifiability formats (codex R1 P1): the binding fields a later gate
# (Phase B G2-G8) must be able to verify can't be free-form strings.
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_VERDICT_VALID = ("pass", "fail")


def _validate_ids(project: str, task: str) -> None:
    """Reject project/task slugs that aren't ``[a-z0-9-]`` (codex R1 P0).

    Every audit path is built as ``$HANDOFF_HOME / project / audit / task / ...``.
    Without this guard ``--project /abs`` (``Path`` discards the left side on an
    absolute right side) or ``--project ../escape`` would write outside the
    handoff home. The slug rule (same as ``handoff_precheck.TASK_ID_RE``)
    forbids ``/``, ``.`` and ``..`` entirely, so traversal is impossible.
    """
    if not _pc.TASK_ID_RE.match(project):
        raise ValueError(f"invalid project slug: {project!r} (need [a-z0-9-])")
    if not _pc.TASK_ID_RE.match(task):
        raise ValueError(f"invalid task id: {task!r} (need [a-z0-9-])")


def _is_safe_relpath(p: object) -> bool:
    """True iff ``p`` is a non-empty relative POSIX path with no ``..`` escape.

    Used for the artifact / scope-ruling / override references stored in a
    disposition so they always resolve under the project audit tree.
    """
    if not isinstance(p, str) or not p:
        return False
    if p.startswith("/") or "\\" in p:
        return False
    parts = p.split("/")
    return ".." not in parts and "" not in parts


def _nonempty_str(v: object) -> str | None:
    """Return the stripped value iff ``v`` is a non-blank string, else ``None``.

    Trust tokens (reviewer session id, owner ack token) must be real non-empty
    strings — a truthy non-string (``["x"]``, ``{"a":1}``) or a whitespace-only
    string must NOT satisfy a ``if not token`` presence check (codex R5).
    """
    return v.strip() if isinstance(v, str) and v.strip() else None


def _audit_lock_path(project: str, task: str) -> Path:
    return _pc.locks_dir(project) / f"{task}.audit.lock"


# ─── paths (spec §3.4) ──────────────────────────────────────────────────────


def _project_home(project: str) -> Path:
    return _config.home_dir() / project


def audit_base_dir(project: str, task: str) -> Path:
    """``$HANDOFF_HOME/<project>/audit/<task>`` — all runtime audit material.

    All other audit paths derive from this one, so the traversal guard lives
    here (defence in depth beyond the CLI argument validation).
    """
    _validate_ids(project, task)
    return _project_home(project) / "audit" / task


def audit_run_dir(project: str, task: str, run_index: int) -> Path:
    return audit_base_dir(project, task) / str(run_index)


def findings_path(project: str, task: str, run_index: int) -> Path:
    return audit_run_dir(project, task, run_index) / FINDINGS_FILENAME


def manifest_path(project: str, task: str, run_index: int) -> Path:
    return audit_run_dir(project, task, run_index) / MANIFEST_FILENAME


def dispositions_path(project: str, task: str) -> Path:
    return audit_base_dir(project, task) / DISPOSITIONS_FILENAME


def _relative_to_project(project: str, path: Path) -> str:
    """Canonical POSIX path relative to ``$HANDOFF_HOME/<project>/`` (no abs leak)."""
    return path.relative_to(_project_home(project)).as_posix()


# ─── owner_ack_token (Component A) — design §2 ──────────────────────────────
# TRUST MODEL (owner ruling #1, NOT cryptography): an AI running with the
# owner's identity can write any file and therefore fabricate a self-consistent
# token. This binding checksum defends against (a) reusing one finding's
# approval on a DIFFERENT finding (finding_hash binding), (b) an approval that
# never expires (7-day TTL), and (c) a trace-less approval (audit jsonl trail +
# the AskUserQuestion button's friction). It does NOT defend against a malicious
# forger; that needs an owner-held key (deferred, see design §6).


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

    NOT a secret (see the trust-model note above): it pins an approval to one
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


def _audit_trail_path(project: str, task: str) -> Path:
    """The closing-audit jsonl the Phase C overdue scanner also appends to
    (``ack/<task>.audit.retry_audit.jsonl``)."""
    _validate_ids(project, task)
    return _config.home_dir() / project / "ack" / f"{task}.audit.retry_audit.jsonl"


def _append_audit_trail(project: str, task: str, event: dict) -> None:
    """Append one JSON line to the task's audit trail (best-effort, fsync'd)."""
    path = _audit_trail_path(project, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()


def _parse_iso_utc(iso: str) -> datetime:
    """Parse an ISO-8601 string to an offset-aware datetime (raises on bad input)."""
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _add_days_iso(iso: str, days: int) -> str:
    """Return ``iso`` shifted by ``days``, normalized to an offset-aware ISO-8601."""
    return (_parse_iso_utc(iso) + timedelta(days=days)).isoformat()


def write_owner_ack(
    project: str,
    task: str,
    finding_hash: str,
    finding_title: str,
    nonce: str,
    approved_at: str,
    reason: str,
) -> dict:
    """Write the owner-ack artifact (after the owner clicks the AskUserQuestion
    button) and append an ``owner-ack-written`` trail line. Returns the artifact.

    ``expires_at`` = ``approved_at`` + :data:`OWNER_ACK_TTL_DAYS` (owner ruling #4).
    NOT cryptographic — see the module trust-model note.
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
    atomic.atomic_replace(path, json.dumps(artifact, ensure_ascii=False, sort_keys=True) + "\n")
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
    """Read the on-disk owner-ack artifact; ``None`` if missing / unreadable /
    not a JSON object."""
    try:
        path = owner_ack_path(project, task, finding_hash)
    except ValueError:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ─── bypass sidecar producer (Component B) — design §3 ──────────────────────
# When codex is genuinely unavailable, audit-close auto-writes this sidecar so
# the Phase C overdue scanner (auto-continue.sh scan_overdue_kind) and the dump
# gate (_check_follow_up_overdue) can enforce the re-audit debt. NO owner click:
# codex being down is a MACHINE fact (owner ruling #2); the safety net is the
# machine failure proof + the forced follow-up task + the overdue deadline.


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

    Validates the honest-path threshold (>= :data:`MIN_CODEX_FAILURES`
    machine-proven failures) and the follow-up slug (isinstance str + fullmatch —
    the SAME contract as build_codex_audit_block / _gate_bypass /
    forced_follow_up_task; a trailing-newline or non-str slug must be rejected
    here too or the owed follow-up silently never reaches old_ready). Deadline =
    ``created_at`` + :data:`BYPASS_FOLLOW_UP_DEADLINE_DAYS`.
    """
    _validate_ids(project, task)
    if not isinstance(follow_up_audit_task_id, str) or not _pc.TASK_ID_RE.fullmatch(
        follow_up_audit_task_id
    ):
        raise ValueError(
            f"follow_up_audit_task_id must be a slug [a-z0-9-] (got {follow_up_audit_task_id!r})"
        )
    if (
        not isinstance(codex_failure_attempts, list)
        or len(codex_failure_attempts) < MIN_CODEX_FAILURES
    ):
        count = (
            len(codex_failure_attempts) if isinstance(codex_failure_attempts, list) else "non-list"
        )
        raise ValueError(
            f"bypass needs at least MIN_CODEX_FAILURES={MIN_CODEX_FAILURES} "
            f"machine-proven codex failures; got {count}"
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
    atomic.atomic_replace(path, json.dumps(artifact, ensure_ascii=False, sort_keys=True) + "\n")
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


# ─── findings hashing (sidecar manifest — R2-P0-3) ──────────────────────────


def compute_findings_hash(findings: dict) -> str:
    """SHA-256 hex over the *canonical* bytes of the findings dict.

    Canonical = sorted keys, UTF-8, no BOM, no insignificant whitespace (the
    same layout :func:`handoff_precheck.canonical_json_bytes` produces). The
    on-disk findings file is written in this exact form, so a later verifier can
    recompute the hash from either the parsed object or the raw bytes and get an
    identical result.
    """
    return hashlib.sha256(_pc.canonical_json_bytes(findings)).hexdigest()


def compute_codex_audit_hash(block: dict) -> str:
    """SHA-256 hex over the *canonical* bytes of a ``codex_audit`` block.

    Written into ``old_ready.codex_audit_hash`` (Phase C) so a new session (§0)
    and the autoclose watcher can detect tampering of the audit block between
    the dump and the next spawn. Plain hex, matching the
    ``old_ready.retro_evidence_hash`` style (not the ``sha256:`` ref form).
    """
    return hashlib.sha256(_pc.canonical_json_bytes(block)).hexdigest()


def forced_follow_up_task(block: dict) -> str | None:
    """The task the *next* session is forced to run, or ``None`` (Phase C §1.3).

    Only ``codex_unavailable_bypass`` constrains the next task: bypass means the
    session skipped its codex audit, so it *owes* one — the next session's first
    task MUST be the bypass's ``follow_up_audit_task_id`` (it is not free to
    continue roadmap on un-audited code). Every other mode (full / empty_diff /
    docs_only) audited in place and imposes no constraint, so returns ``None``.

    Returns ``None`` (no constraint, fail-open for the non-bypass majority) when
    the block isn't a dict, isn't bypass mode, or its follow-up isn't a valid
    kebab slug — a malformed bypass block is caught at build time
    (:func:`build_codex_audit_block`), so this stays a pure, defensive reader.
    """
    if not isinstance(block, dict):
        return None
    if block.get("audit_mode") != _pc.AUDIT_MODE_BYPASS:
        return None
    follow = block.get("follow_up_audit_task_id")
    # fullmatch, not match: ``$`` matches before a trailing newline (R2 P2), so a
    # ``"audit-redo-x\n"`` slug must not slip through into next_session_forced_task.
    if isinstance(follow, str) and _pc.TASK_ID_RE.fullmatch(follow):
        return follow
    return None


def derive_verdict(findings: dict) -> str:
    """``"pass"`` iff no original finding is P0/P1, else ``"fail"`` (spec §3.1).

    The verdict is *derived*, never trusted from the AI: a run is clean only
    when codex surfaced no blocking-severity finding. Fail closed (``"fail"``)
    when ``original_findings`` is not a list (codex R6: a dict-shaped value would
    otherwise iterate its keys, never see the P0/P1, and derive a false pass).
    """
    of = findings.get("original_findings")
    if not isinstance(of, list):
        return "fail"
    for f in of:
        if not isinstance(f, dict):
            continue
        sev = _severity(f)
        # Blocking, OR an unrecognized non-empty severity → fail closed (codex
        # R8-2): a typo'd / spoofed severity must never read as a clean pass.
        if sev in ("P0", "P1") or (sev and sev not in _pc.AUDIT_SEVERITIES):
            return "fail"
    return "pass"


def write_findings_artifact(
    project: str,
    task: str,
    run_index: int,
    findings: dict,
    *,
    input_commit: str,
) -> dict:
    """Persist ``findings`` canonically + write the sidecar manifest atomically.

    Returns the *run record* the evidence ``audit_runs[]`` array stores:
    ``{run_index, input_commit, artifact_hash, verdict, findings_path,
    manifest_path}`` (paths are canonical-relative). The hash is NOT embedded in
    the findings file — it lives only in the sidecar manifest (R2-P0-3).
    """
    run_dir = audit_run_dir(project, task, run_index)
    run_dir.mkdir(parents=True, exist_ok=True)

    canonical = _pc.canonical_json_bytes(findings).decode("utf-8")
    digest = compute_findings_hash(findings)
    fpath = findings_path(project, task, run_index)
    mpath = manifest_path(project, task, run_index)

    manifest = {
        "findings_path": FINDINGS_FILENAME,
        "sha256": digest,
        "algo": "sha256",
        "canonical": CANONICAL_DESC,
    }
    # Findings first, then the manifest that vouches for it — if a crash lands
    # between the two, a lone findings file (no manifest) reads as "unverified"
    # rather than a manifest pointing at nothing.
    atomic.atomic_replace(fpath, canonical)
    atomic.atomic_replace(
        mpath, json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )

    return {
        "run_index": run_index,
        "input_commit": input_commit,
        "artifact_hash": "sha256:" + digest,
        "verdict": derive_verdict(findings),
        "findings_path": _relative_to_project(project, fpath),
        "manifest_path": _relative_to_project(project, mpath),
    }


def read_findings_manifest(project: str, task: str, run_index: int) -> dict | None:
    mpath = manifest_path(project, task, run_index)
    if not mpath.exists():
        return None
    try:
        return json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify_findings_artifact(project: str, task: str, run_index: int) -> bool:
    """True iff the on-disk findings bytes are canonical AND match the manifest.

    Byte-level (codex R1 P1): we hash the *raw* on-disk bytes (not a
    re-serialization) so any tamper is caught, and additionally require the
    bytes to already be in canonical form — a semantic-equal but non-canonical
    rewrite is rejected. Both hold because :func:`write_findings_artifact`
    persists exactly ``canonical_json_bytes(findings)``.
    """
    manifest = read_findings_manifest(project, task, run_index)
    if not manifest or manifest.get("algo") != "sha256":
        return False
    fpath = findings_path(project, task, run_index)
    if not fpath.exists():
        return False
    try:
        raw = fpath.read_bytes()
    except OSError:
        return False
    if hashlib.sha256(raw).hexdigest() != manifest.get("sha256"):
        return False
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return _pc.canonical_json_bytes(parsed) == raw


# ─── codex_audit block builder (4 modes — spec §2.1 / §3.5) ─────────────────


def _build_code_repo_keys(code_repo: str | None) -> dict:
    """Validate ``code_repo`` and return ``{"code_repo": abs, "code_repo_head": sha}``
    (or ``{}`` when absent). Mirrors the gate's ``_resolve_audit_ws`` admission so a
    block the gate would reject can't be assembled: requires an existing, absolute
    git repo whose HEAD resolves. Same-repo (None) → no keys (hash-stable)."""
    if code_repo is None:
        return {}
    if not isinstance(code_repo, str) or not code_repo:
        raise ValueError("code_repo must be a non-empty absolute path string")
    candidate = Path(code_repo)
    if not candidate.is_absolute() or not candidate.is_dir():
        raise ValueError(f"code_repo must be an existing absolute directory: {code_repo!r}")
    rc, head = _audit_git(["rev-parse", "HEAD"], candidate)
    head = head.strip()
    if rc != 0 or not head:
        raise ValueError(f"code_repo is not a readable git repo: {code_repo!r}")
    return {"code_repo": str(candidate), "code_repo_head": head}


def build_codex_audit_block(
    audit_mode: str,
    *,
    audit_runs: list[dict] | None = None,
    dispositions: list[dict] | None = None,
    attestation: dict | None = None,
    bypass: dict | None = None,
    code_repo: str | None = None,
) -> dict:
    """Assemble the mode-specific ``codex_audit`` block embedded in evidence.

    Each of the four modes has its own schema (R2-P1-1); this builder validates
    that the caller supplied the pieces that mode requires and shapes the block
    accordingly. It does NOT decide the mode (that is the gate's machine
    ruling via ``git diff`` in Phase B) — the caller passes the chosen mode.

    ``code_repo`` (cross-repo anchor): when given, the audited code lives in a
    repo distinct from the launching workspace; the block records its absolute
    path + current HEAD so the gate (``_resolve_audit_ws``) binds G0 to the code
    repo, not the launcher. Absent → no extra keys (same-repo evidence stays
    byte-identical → schema/canonical hash stable).
    """
    if audit_mode not in _pc.AUDIT_MODES:
        raise ValueError(f"audit_mode must be one of {list(_pc.AUDIT_MODES)}; got {audit_mode!r}")

    code_repo_keys = _build_code_repo_keys(code_repo)

    if audit_mode in (_pc.AUDIT_MODE_FULL, _pc.AUDIT_MODE_DOCS_ONLY):
        if not audit_runs:
            raise ValueError(f"{audit_mode} requires a non-empty audit_runs list")
        return {
            "audit_mode": audit_mode,
            "audit_runs": list(audit_runs),
            "dispositions": list(dispositions or []),
            **code_repo_keys,
        }

    if audit_mode == _pc.AUDIT_MODE_EMPTY_DIFF:
        required = ("base", "head", "diff_hash", "mode_decider_version")
        if not attestation or any(k not in attestation for k in required):
            raise ValueError(
                f"empty_diff_attestation requires an attestation with {list(required)}"
            )
        return {
            "audit_mode": audit_mode,
            "empty_diff_attestation": {k: attestation[k] for k in required},
            **code_repo_keys,
        }

    # AUDIT_MODE_BYPASS
    if not bypass or not bypass.get("codex_failure_attempts"):
        raise ValueError(
            "codex_unavailable_bypass requires bypass.codex_failure_attempts (machine "
            "proof of >=N codex failures)"
        )
    follow = bypass.get("follow_up_audit_task_id")
    # fullmatch, not match: Python's ``$`` matches before a trailing newline, so
    # ``.match("audit-redo-x\n")`` would pass and a newline-bearing slug would
    # land in evidence + old_ready.next_session_forced_task (R2 P2).
    if not follow or not isinstance(follow, str) or not _pc.TASK_ID_RE.fullmatch(follow):
        raise ValueError(
            "codex_unavailable_bypass requires follow_up_audit_task_id as a slug [a-z0-9-]"
        )
    # R2 P1: the failure proof must be machine-verifiable, not free-form — each
    # attempt needs an exit code, a hashed stderr, and a timestamp.
    attempts = bypass["codex_failure_attempts"]
    # R2-P1 (Phase D): the builder enforces the SAME MIN_CODEX_FAILURES floor as
    # the gate (_gate_bypass) and the producer (write_bypass_override), so a block
    # the gate would later reject can never be assembled here in the first place.
    if not isinstance(attempts, list) or len(attempts) < MIN_CODEX_FAILURES:
        raise ValueError(
            f"codex_failure_attempts must have >= MIN_CODEX_FAILURES={MIN_CODEX_FAILURES} entries"
        )
    for a in attempts:
        if not isinstance(a, dict):
            raise ValueError("each codex_failure_attempt must be an object")
        if not isinstance(a.get("exit"), int) or isinstance(a.get("exit"), bool):
            raise ValueError("codex_failure_attempt.exit must be an int")
        if not _SHA256_REF_RE.match(str(a.get("stderr_hash", ""))):
            raise ValueError("codex_failure_attempt.stderr_hash must be sha256:<64 hex>")
        if not isinstance(a.get("timestamp"), str) or not a["timestamp"].strip():
            raise ValueError("codex_failure_attempt.timestamp must be a non-empty string")
    block = {
        "audit_mode": audit_mode,
        "codex_failure_attempts": list(attempts),
        "follow_up_audit_task_id": follow,
        **code_repo_keys,
    }
    override_ref = bypass.get("override_ref")
    if override_ref is not None:
        if not _is_safe_relpath(override_ref):
            raise ValueError("override_ref must be a safe relative path (no abs, no ..)")
        block["override_ref"] = override_ref
    return block


# ─── run record validation (codex R1 P1: assembled evidence must be real) ───


def validate_run_record(project: str, task: str, record: dict) -> str | None:
    """Return an error when ``record`` is malformed OR not backed by a real
    on-disk findings artifact, else ``None``.

    ``audit-close`` assembles ``audit_runs[]`` from caller-supplied records; this
    check stops a fabricated ``{verdict: pass}`` / fake ``artifact_hash`` from
    landing in evidence with no findings file behind it. It cross-checks the
    sidecar manifest hash and the *derived* verdict so the recorded run cannot
    disagree with what codex actually found.
    """
    if not isinstance(record, dict):
        return "run record must be a JSON object"
    ri = record.get("run_index")
    if not isinstance(ri, int) or isinstance(ri, bool) or ri < 1:
        return "run_index must be an int >= 1"
    input_commit = record.get("input_commit")
    if not isinstance(input_commit, str) or not _GIT_SHA_RE.match(input_commit):
        return "input_commit must be a git SHA (7-40 hex chars)"
    artifact_hash = record.get("artifact_hash")
    if not isinstance(artifact_hash, str) or not _SHA256_REF_RE.match(artifact_hash):
        return "artifact_hash must be sha256:<64 hex chars>"
    if record.get("verdict") not in _VERDICT_VALID:
        return f"verdict must be one of {list(_VERDICT_VALID)}"
    for key in ("findings_path", "manifest_path"):
        if not _is_safe_relpath(record.get(key)):
            return f"{key} must be a safe relative path (no abs, no ..)"
    # R2 P1: bind the record's paths to the canonical artifact location so a
    # real run-N artifact cannot back a record pointing readers elsewhere.
    expected_fp = _relative_to_project(project, findings_path(project, task, ri))
    expected_mp = _relative_to_project(project, manifest_path(project, task, ri))
    if record.get("findings_path") != expected_fp:
        return f"findings_path must equal canonical {expected_fp!r}"
    if record.get("manifest_path") != expected_mp:
        return f"manifest_path must equal canonical {expected_mp!r}"
    if not verify_findings_artifact(project, task, ri):
        return f"findings artifact for run {ri} missing or hash-mismatched"
    manifest = read_findings_manifest(project, task, ri)
    if (manifest or {}).get("findings_path") != FINDINGS_FILENAME:
        return f"manifest findings_path is not the canonical {FINDINGS_FILENAME!r}"
    if "sha256:" + (manifest or {}).get("sha256", "") != artifact_hash:
        return f"artifact_hash does not match sidecar manifest for run {ri}"
    try:
        findings = json.loads(findings_path(project, task, ri).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return f"findings for run {ri} unreadable: {e}"
    # R2 P1: the artifact must be the one this record claims — a run-N file for
    # commit A may not back a record asserting run_index N / commit B.
    if findings.get("run_index") != ri:
        return f"findings run_index {findings.get('run_index')!r} != record run_index {ri}"
    if findings.get("input_commit") != input_commit:
        return "findings input_commit disagrees with record input_commit"
    derived = derive_verdict(findings)
    if derived != record["verdict"]:
        return f"verdict {record['verdict']!r} disagrees with findings (derived {derived!r})"
    return None


# ─── disposition shape validation (spec §1.7 / §3.1 / G4-G8 input) ──────────


def validate_disposition_shape(disposition: dict) -> str | None:
    """Return an error string when ``disposition`` is malformed, else ``None``.

    This validates *shape* only (what the gate needs to evaluate G4-G8 later) —
    it does not verify that referenced commits/artifacts actually exist (that is
    the Phase B gate's job against the live repo / filesystem).
    """
    if not isinstance(disposition, dict):
        return "disposition must be a JSON object"

    for field in ("finding_id", "finding_hash", "original_severity", "disposition"):
        val = disposition.get(field)
        if not isinstance(val, str) or not val.strip():
            return f"disposition missing required field: {field}"

    severity = disposition["original_severity"]
    if severity not in _pc.AUDIT_SEVERITIES:
        return f"original_severity={severity!r} not in {list(_pc.AUDIT_SEVERITIES)}"

    disp = disposition["disposition"]
    if disp not in _pc.DISPOSITION_TYPES:
        return f"disposition={disp!r} not in {list(_pc.DISPOSITION_TYPES)}"

    # Binding hash must be machine-verifiable (codex R1 P1) — it keys the
    # disposition to an original codex finding in Phase B's G3 union check.
    if not _SHA256_REF_RE.match(disposition["finding_hash"]):
        return "finding_hash must be sha256:<64 hex chars>"

    if disp == _pc.DISPOSITION_FIXED:
        fix_commit = disposition.get("fix_commit")
        if not fix_commit:
            return "disposition=fixed requires fix_commit"
        if not _GIT_SHA_RE.match(fix_commit):
            return "fix_commit must be a git SHA (7-40 hex chars)"
    elif disp == _pc.DISPOSITION_REFUTED:
        artifact = disposition.get("independent_reviewer_artifact")
        if not artifact:
            return "disposition=independent_reviewer_refuted requires independent_reviewer_artifact"
        if not _is_safe_relpath(artifact):
            return "independent_reviewer_artifact must be a safe relative path (no abs, no ..)"
        if not disposition.get("reviewer_session_id"):
            return "disposition=independent_reviewer_refuted requires reviewer_session_id"
    elif disp == _pc.DISPOSITION_OWNER_OVERRIDE:
        if not disposition.get("owner_ack_token"):
            return "disposition=owner_override requires owner_ack_token (AI-generated overrides rejected)"
        ref = disposition.get("owner_override_ref")
        if ref is not None and not _is_safe_relpath(ref):
            return "owner_override_ref must be a safe relative path (no abs, no ..)"
    elif disp == _pc.DISPOSITION_DEFERRED:
        if severity not in _pc.DEFERRABLE_SEVERITIES:
            return (
                f"disposition=deferred only allowed for severity in "
                f"{list(_pc.DEFERRABLE_SEVERITIES)}; got {severity}"
            )
        scope_ruling = disposition.get("scope_ruling")
        if not scope_ruling:
            return "disposition=deferred requires scope_ruling"
        if not _is_safe_relpath(scope_ruling):
            return "scope_ruling must be a safe relative path (no abs, no ..)"
    return None


# ─── dispositions store ─────────────────────────────────────────────────────


def load_dispositions(project: str, task: str) -> list[dict]:
    path = dispositions_path(project, task)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def append_disposition(project: str, task: str, disposition: dict) -> list[dict]:
    """Validate + append a disposition to the per-task store (atomic rewrite).

    Raises ``ValueError`` on a malformed shape so a bad disposition never lands
    in the store. Returns the full list after append.
    """
    err = validate_disposition_shape(disposition)
    if err:
        raise ValueError(err)
    # codex R1 P1: load-append-write must be atomic across processes, else two
    # concurrent audit-disposition calls each read the old list and the last
    # writer silently drops the other's disposition.
    lock = _audit_lock_path(project, task)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with atomic.acquire_dir_lock(lock, retries=5, wait_seconds=0.2):
        existing = load_dispositions(project, task)
        existing.append(disposition)
        path = dispositions_path(project, task)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic.atomic_replace(
            path, json.dumps(existing, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        )
    return existing


# ─── Phase B gate: evaluate_audit_gate (G0-G9 — spec §1 / §5) ───────────────
#
# This is the *enforcement* layer. With the audit mandate OFF (the only state
# Phase B ships in) ``retro_gate`` never calls it; turning ``HANDOFF_AUDIT_MANDATE``
# on (Phase D) makes the dump gate run it before clearing a task for handoff.
#
# The gate returns a neutral :class:`AuditGateOutcome` (NOT a retro_gate
# ``GateResult``) so this module need not import ``retro_gate`` — retro_gate maps
# the outcome class to its own exit-code protocol. The subcode strings here are
# the spec §5 contract verbatim.

# The empty tree object name (git's well-known SHA for an empty directory) —
# used as the diff base when a session's oldest commit is the repo root.
_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Owner-ack / bypass-producer constants (Phase D pre-req; design §2.2 / §3.1).
OWNER_ACK_TTL_DAYS = 7  # owner_override exemption validity (owner ruling #4)
BYPASS_FOLLOW_UP_DEADLINE_DAYS = 1  # short debt; next session should re-audit
# The honest threshold for "codex unavailable": how many machine-proven failures
# (exit≠0 / timeout, each with a hashed stderr) the producer requires before it
# will emit a bypass sidecar (design §3.1). The gate enforces the SAME floor
# (R1-P1): a hand-crafted bypass evidence routed straight through
# `handoff dump --retro-evidence` must clear the same ≥3-failure bar the honest
# producer path does, or the threshold is decorative.
MIN_CODEX_FAILURES = 3
# A bypass needs at least this many machine-recorded codex failures (spec §1.3
# "gate 校验次数 ≥ 阈值"). Unified with MIN_CODEX_FAILURES so producer and gate
# agree — the design's safety net (§3.3 "gate _gate_bypass 已校验 N 次") depends
# on the gate checking the real N, not a looser floor.
BYPASS_MIN_FAILURES = MIN_CODEX_FAILURES
OWNER_ACK_SCHEMA_VERSION = "1.0"
BYPASS_OVERRIDE_SCHEMA_VERSION = "1.0"
SUPPORTED_OWNER_ACK_SCHEMA_VERSIONS = ("1.0",)

# docs_only legitimacy (spec §2.2): only these suffixes may be "docs", and
# CLAUDE.md / AGENTS.md / GEMINI.md / anything under prompts/ are force-full
# even though they are .md — they steer the agent, so they need a real audit.
DOC_SUFFIXES = (".md", ".rst", ".txt")
FORCE_FULL_DOC_BASENAMES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")


@dataclass
class AuditGateOutcome:
    """Neutral result of :func:`evaluate_audit_gate`.

    ``klass`` ∈ {``ok``, ``retry``, ``blocked``, ``fatal``, ``bypass``} maps to
    the retro_gate exit-code protocol (§7.1) by the caller. ``subcode`` is the
    spec §5 machine token; ``message`` is the human tail.
    """

    klass: str
    subcode: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.klass == "ok"


_AUDIT_OK = AuditGateOutcome("ok")


def _audit_git(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run git, returning ``(returncode, stdout)``; ``(-1, "")`` if unspawnable.

    Returning the rc lets the gate distinguish a genuinely-empty result (rc 0,
    e.g. an empty diff) from a git failure (rc != 0) — critical for fail-closed
    behaviour: a failed ``git diff`` must never read as "no changes".
    """
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return r.returncode, r.stdout or ""
    except (subprocess.SubprocessError, OSError):
        return -1, ""


def _resolve_commit(workspace: Path, ref: str) -> str | None:
    """Resolve ``ref`` to a full commit SHA, or ``None`` if it isn't a commit."""
    if not isinstance(ref, str) or not _GIT_SHA_RE.match(ref):
        return None
    rc, out = _audit_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], workspace)
    out = out.strip()
    return out if rc == 0 and out else None


def _commit_equals(workspace: Path, a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` resolve to the same commit (handles abbrev SHAs)."""
    ra = _resolve_commit(workspace, a)
    rb = _resolve_commit(workspace, b)
    return ra is not None and ra == rb


def _resolve_audit_ws(block: dict, workspace: Path) -> tuple[Path | None, str | None]:
    """Pick the repo the gate runs git against.

    ``code_repo`` (an *absolute* path to the audited repo) overrides
    ``workspace`` for cross-repo handoff — code audited in repo X, dump launched
    from workspace Y. Absent → ``workspace`` (same-repo, byte-identical behaviour
    to before). Returns ``(audit_ws, error_subcode)``; on any malformed /
    non-existent / non-git ``code_repo`` it fails closed with
    ``codex-audit-code-repo-invalid`` so an attacker can't point the gate at an
    arbitrary clean directory to fake a matching HEAD.

    NOTE: the *retro* freshness check (retro_gate) stays bound to ``workspace`` —
    retro = "did the launching session close out" (workspace), audit = "was the
    code reviewed" (code repo). They are orthogonal and must not be conflated.

    TRUST MODEL (codex R1/R3 / design §1, owner ruling #1 — SAME single-user model
    as owner_ack_token): ``code_repo`` is *self-asserted* by the evidence. There is
    no project→repo registry, so the gate proves "THIS named repo @ its HEAD was
    audited", not "the canonical repo for this project was audited". A caller could
    name a different clean repo whose HEAD matches the audited commit — but that
    repo must actually *contain* that commit (git SHAs are content+history hashes),
    so it is a copy of the audited code, and a fully malicious AI that fabricates
    evidence already had broader forgery paths in this single-user model. The
    available NON-crypto friction (caller declares + gate binds ``code_repo_head``
    to the live HEAD; see evaluate_audit_gate) is enforced; a repo-identity
    allowlist / root-SHA binding is the owner-gated mandate-on hardening (deferred,
    like the owner-held-key owner_ack in design §6), NOT done here.
    """
    raw = block.get("code_repo")
    if raw is None:
        return workspace, None
    if not isinstance(raw, str) or not raw:
        return None, "codex-audit-code-repo-invalid"
    candidate = Path(raw)
    if not candidate.is_absolute() or not candidate.is_dir():
        return None, "codex-audit-code-repo-invalid"
    # Resolve to the canonical realpath ONCE and operate on / return THAT — a
    # symlink that resolves to an allowed repo at check time can't be repointed
    # before later git ops use audit_ws (codex P1-1 TOCTOU). All downstream git
    # runs against the resolved path, not the caller-supplied alias.
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None, "codex-audit-code-repo-invalid"
    rc, _ = _audit_git(["rev-parse", "--git-dir"], resolved)
    if rc != 0:
        return None, "codex-audit-code-repo-invalid"
    # Opt-in repo-identity allowlist (Phase D P1 hardening / codex R1+R3). Key
    # PRESENCE = intent to restrict: when audit_code_repos is configured, a
    # cross-repo code_repo MUST realpath-match a listed path. A configured-but-
    # empty list (all entries mis-written / filtered to nothing) fails CLOSED, not
    # silently degrades to unrestricted (codex P1-2 fail-open). Key absent →
    # unconfigured → no restriction (single-user friction + disclaimer still apply).
    cfg = _config.load()
    if cfg.audit_allowlist_configured:
        allowed = set()
        for a in cfg.audit_code_repos:
            try:
                allowed.add(Path(a).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue
        if resolved not in allowed:
            return None, "codex-audit-code-repo-not-allowed"
    return resolved, None


# Field families that contribute to a finding's stable identity (codex R1-F5 /
# R2-2). Only a TRUE per-finding id may key the identity by itself. A rule /
# check id is NOT unique per finding (one rule fires on many sites), so it folds
# into the location+text identity rather than standing alone — else two distinct
# findings of the same rule in different files would collide onto one hash.
_IDENTITY_UNIQUE_ID_KEYS = ("id", "finding_id", "uuid")
_IDENTITY_RULE_KEYS = ("rule", "rule_id", "check")
_IDENTITY_LOC_KEYS = ("file", "path", "location", "loc")
_IDENTITY_TEXT_KEYS = ("title", "text", "description", "summary", "message")


def _first_str(finding: dict, keys) -> str:
    for k in keys:
        v = finding.get(k)
        if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip():
            return str(v).strip()
    return ""


def _severity(finding: dict) -> str:
    """Normalized severity: ``strip().upper()`` (codex R8-2).

    Centralized so ``"P1 "`` / ``" p1"`` can't evade the ``{P0,P1}`` membership
    check in one place but not another. Returns ``""`` for a missing severity.
    """
    return str(finding.get("severity", "")).strip().upper()


def _normalize_finding_text(finding: dict) -> str:
    """Whitespace-collapsed, lowercased text of a finding's first textual field."""
    raw = _first_str(finding, _IDENTITY_TEXT_KEYS)
    return " ".join(raw.lower().split())


def finding_identity(finding: dict) -> dict:
    """Stable identity core for a codex finding (spec §2.2 union/dedup).

    A true per-finding ``id`` keys the identity by itself, so the SAME finding
    restated across audit rounds dedups even if its wording drifts. Without one,
    the identity is severity + rule + location + line + text together (codex
    R2-2): a ``rule``/``check`` alone never stands as identity, so two distinct
    findings of the same rule in different files / lines do NOT collide onto one
    disposition.
    """
    sev = _severity(finding)
    uid = _first_str(finding, _IDENTITY_UNIQUE_ID_KEYS)
    if uid:
        return {"severity": sev, "id": uid}
    ident = {"severity": sev}
    rule = _first_str(finding, _IDENTITY_RULE_KEYS)
    if rule:
        ident["rule"] = rule
    loc = _first_str(finding, _IDENTITY_LOC_KEYS)
    if loc:
        ident["loc"] = loc
    line = finding.get("line")
    if isinstance(line, int) and not isinstance(line, bool):
        ident["line"] = line
    text = _normalize_finding_text(finding)
    if text:
        ident["text"] = text
    return ident


def has_finding_identity(finding: dict) -> bool:
    """False when a finding has neither a unique id NOR (location or text).

    A rule/check id alone does NOT count (codex R2-2) — it can't distinguish two
    sites of the same rule. Such an unbindable finding is rejected rather than
    letting many distinct findings collide onto a single disposition.
    """
    if _first_str(finding, _IDENTITY_UNIQUE_ID_KEYS):
        return True
    return bool(_first_str(finding, _IDENTITY_LOC_KEYS) or _normalize_finding_text(finding))


def compute_finding_hash(finding: dict) -> str:
    """Stable ``sha256:<64hex>`` identity for a codex finding (spec §2.2 union).

    Hashes :func:`finding_identity` — NOT the raw object — so a finding restated
    across rounds dedups, and a disposition keyed to this hash binds to the
    finding regardless of which round surfaced it. The ``sha256:`` prefix
    matches the disposition ``finding_hash`` format.
    """
    return (
        "sha256:" + hashlib.sha256(_pc.canonical_json_bytes(finding_identity(finding))).hexdigest()
    )


def finding_presence_hash(finding: dict) -> str | None:
    """Severity- AND id-NEUTRAL location/text hash for the G5 "is it gone?" check.

    ``None`` when the finding has no stable presence basis (only an id). A
    "fixed" P0/P1 must be GONE from the last run — not merely downgraded to P2
    (codex R7) NOR reappearing under a fresh ``id`` (codex R8-1). The AI controls
    the ``id``, so id-equality can't prove a fix; presence keys on the stable
    location/rule/text instead, and a blocking finding lacking ANY of those can't
    be fix-verified (the gate fails closed in G5).
    """
    # NOTE: deliberately excludes ``line`` (codex R10). Line numbers shift when
    # unrelated edits land above a finding, so a still-present issue would look
    # "gone" if line were part of the presence key. Presence keys on the stable
    # rule + file-path + text; the binding identity (compute_finding_hash) keeps
    # line for precise disposition matching, but "is it gone?" must not.
    ident: dict = {}
    rule = _first_str(finding, _IDENTITY_RULE_KEYS)
    if rule:
        ident["rule"] = rule
    loc = _first_str(finding, _IDENTITY_LOC_KEYS)
    if loc:
        ident["loc"] = loc
    text = _normalize_finding_text(finding)
    if text:
        ident["text"] = text
    if not ident:
        return None
    return "sha256:" + hashlib.sha256(_pc.canonical_json_bytes(ident)).hexdigest()


def classify_artifact_state(project: str, task: str, run_index: int) -> str:
    """``"ok"`` / ``"missing"`` / ``"tampered"`` for a run's findings artifact.

    Splits :func:`verify_findings_artifact`'s boolean into the two failure
    classes the gate routes differently (spec §3 P2): a *missing* artifact is a
    RETRY (re-run the audit), a *tampered* one (hash mismatch / non-canonical
    rewrite) is FATAL.
    """
    fpath = findings_path(project, task, run_index)
    mpath = manifest_path(project, task, run_index)
    # Absent files are genuinely missing → RETRY (re-run the audit).
    if not fpath.exists() or not mpath.exists():
        return "missing"
    # A PRESENT manifest that won't parse, or carries the wrong algo, is a
    # tamper event — downgrading it to "missing" (RETRY) would let an attacker
    # corrupt the manifest to dodge the FATAL classification (codex R1-F6 P2).
    manifest = read_findings_manifest(project, task, run_index)
    if not manifest or manifest.get("algo") != "sha256":
        return "tampered"
    try:
        raw = fpath.read_bytes()
    except OSError:
        return "missing"
    if hashlib.sha256(raw).hexdigest() != manifest.get("sha256"):
        return "tampered"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return "tampered"
    if _pc.canonical_json_bytes(parsed) != raw:
        return "tampered"
    return "ok"


def _read_run_findings(project: str, task: str, run_index: int) -> dict | None:
    try:
        parsed = json.loads(findings_path(project, task, run_index).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def discover_run_indices(project: str, task: str) -> set[int]:
    """Integer run indices that have a findings artifact persisted on disk.

    The gate cross-checks this against the caller-listed ``audit_runs`` so a
    failing early run can't be omitted from evidence (codex R2-1). A run dir
    counts only when its ``codex-findings.json`` exists — a bare directory does
    not pretend to be a run.
    """
    base = audit_base_dir(project, task)
    if not base.is_dir():
        return set()
    out: set[int] = set()
    for child in base.iterdir():
        if child.is_dir() and child.name.isdigit() and (child / FINDINGS_FILENAME).exists():
            out.add(int(child.name))
    return out


def _is_doc_path(path: str) -> bool:
    """True iff ``path`` may legitimately be part of a docs-only change (spec §2.2)."""
    base = path.rsplit("/", 1)[-1]
    if base in FORCE_FULL_DOC_BASENAMES:
        return False
    if path == "prompts" or path.startswith("prompts/") or "/prompts/" in path:
        return False
    return any(path.endswith(suf) for suf in DOC_SUFFIXES)


def _is_expired(iso: str) -> bool:
    """True when ``iso`` is in the past or unparseable (fail-closed)."""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return datetime.now(dt.tzinfo) > dt


def _audit_base_from_session(payload: dict, workspace: Path) -> str | None:
    """Derive the change base for a docs-only diff from the session commit set.

    The session's oldest commit's parent is the base; if that commit is the
    repo root (no parent), fall back to the empty-tree object. Returns ``None``
    when the snapshot is absent / malformed → caller fails closed (spec §1.2).
    """
    sc = payload.get("session_commits")
    if not isinstance(sc, list) or not sc:
        return None
    if not all(isinstance(c, str) and _GIT_SHA_RE.match(c) for c in sc):
        return None
    oldest = sc[-1]  # session_commits is newest-first
    rc, out = _audit_git(["rev-parse", "--verify", f"{oldest}~1"], workspace)
    if rc == 0 and out.strip():
        return out.strip()
    # oldest is the root commit → diff against the empty tree
    return _EMPTY_TREE_SHA


def _gate_bypass(block: dict) -> AuditGateOutcome:
    """codex_unavailable_bypass: accept ONLY with machine failure proof +
    forced follow-up (spec §1.3). A valid bypass lets the dump proceed (the
    audit debt is owed by the next session); an invalid one is a BYPASS error."""
    attempts = block.get("codex_failure_attempts")
    if not isinstance(attempts, list) or len(attempts) < BYPASS_MIN_FAILURES:
        return AuditGateOutcome(
            "bypass",
            "codex-audit-bypass-no-failure-proof",
            f"bypass needs >= {BYPASS_MIN_FAILURES} recorded codex failure attempts",
        )
    for a in attempts:
        if (
            not isinstance(a, dict)
            or not isinstance(a.get("exit"), int)
            or isinstance(a.get("exit"), bool)
        ):
            return AuditGateOutcome(
                "bypass", "codex-audit-bypass-no-failure-proof", "malformed failure attempt"
            )
        if not _SHA256_REF_RE.match(str(a.get("stderr_hash", ""))):
            return AuditGateOutcome(
                "bypass",
                "codex-audit-bypass-no-failure-proof",
                "failure attempt stderr_hash must be sha256:<64 hex>",
            )
    follow = block.get("follow_up_audit_task_id")
    # Mirror the producer (build_codex_audit_block) and reader
    # (forced_follow_up_task) exactly: isinstance str + fullmatch (R3 P1). The
    # old ``match(str(follow))`` accepted a non-string ``123`` or a trailing-
    # newline slug here, which forced_follow_up_task then rejects — so the gate
    # would pass a bypass whose owed follow-up silently never reaches old_ready.
    if not isinstance(follow, str) or not _pc.TASK_ID_RE.fullmatch(follow):
        return AuditGateOutcome(
            "bypass",
            "codex-audit-bypass-no-failure-proof",
            "bypass needs a follow_up_audit_task_id (next session owes the audit)",
        )
    return _AUDIT_OK


def _gate_empty_diff(block: dict, workspace: Path, head_now: str) -> AuditGateOutcome:
    """empty_diff_attestation: G0 (attested head == HEAD) + machine-recompute the
    diff is actually empty (spec §1.2 / §2.1). Closes the "empty diff 跳审" path."""
    att = block.get("empty_diff_attestation")
    if not isinstance(att, dict):
        return AuditGateOutcome("retry", "codex-audit-required", "empty_diff missing attestation")
    head = att.get("head")
    base = att.get("base")
    if _resolve_commit(workspace, head if isinstance(head, str) else "") is None:
        return AuditGateOutcome(
            "retry", "codex-audit-required", "empty_diff attestation head is not a repo commit"
        )
    # G0: the attested HEAD must still be the live HEAD.
    if not _commit_equals(workspace, head, head_now):
        return AuditGateOutcome(
            "retry",
            "codex-audit-head-moved",
            f"empty_diff attested head {head} != current HEAD {head_now}",
        )
    if _resolve_commit(workspace, base if isinstance(base, str) else "") is None:
        return AuditGateOutcome(
            "retry", "codex-audit-base-missing", f"empty_diff base {base!r} not in repo"
        )
    rc, diff_out = _audit_git(["diff", "--no-color", base, head], workspace)
    if rc != 0:
        return AuditGateOutcome("retry", "codex-audit-base-missing", "git diff base..head failed")
    if diff_out.strip() != "":
        return AuditGateOutcome(
            "retry",
            "codex-audit-required",
            "empty_diff attested but base..head diff is non-empty — full audit required",
        )
    recomputed = "sha256:" + hashlib.sha256(diff_out.encode("utf-8")).hexdigest()
    if att.get("diff_hash") != recomputed:
        return AuditGateOutcome(
            "fatal",
            "codex-audit-tampered",
            "empty_diff diff_hash does not match the recomputed diff",
        )
    return _AUDIT_OK


# Reviewer-artifact verdicts that count as a refutation (spec §1.7). The other
# required fields (independent_run_id / original_finding_hash / artifact_hash /
# reviewer_session_id) are validated explicitly + by type in the refute path.
_REFUTE_VERDICTS = ("refuted", "refute", "rejected", "reject", "not_a_bug", "false_positive")


def _validate_reviewer_refute(
    disposition: dict, fhash: str, session_id, project: str
) -> AuditGateOutcome | None:
    """G6 anti-forgery for ``independent_reviewer_refuted`` (spec §1.7).

    Returns ``None`` when the refutation is genuine, else the gate outcome.
    Beyond "different session id + artifact exists" (the weak Phase-B-first
    check), this parses the reviewer artifact and requires it to (a) carry all
    §1.7 fields, (b) record a refuting verdict, (c) be bound to THIS finding
    hash, and (d) name a reviewer session that both matches the disposition and
    differs from the audited session — so a dummy ``{}`` file no longer passes.
    """
    # R4-1: the independence proof is "reviewer session != audited session". If
    # the audited session_id is absent (hand-crafted evidence that omits it),
    # that comparison is vacuously satisfied — so fail closed when it's missing,
    # else a refute could be accepted without proving any independence at all.
    if not isinstance(session_id, str) or not session_id.strip():
        return AuditGateOutcome(
            "retry",
            "codex-audit-refute-no-reviewer",
            f"finding {fhash} refute rejected: evidence has no session_id to prove independence",
        )
    sid_norm = session_id.strip()
    # R5: a reviewer session id must be a real non-empty string — a whitespace
    # or non-string truthy value (`[ ]`, `{}`) must not pass as "a reviewer".
    reviewer_sid = _nonempty_str(disposition.get("reviewer_session_id"))
    if not reviewer_sid:
        return AuditGateOutcome(
            "retry",
            "codex-audit-refute-no-reviewer",
            f"finding {fhash} refuted without a non-empty reviewer_session_id",
        )
    if reviewer_sid == sid_norm:
        return AuditGateOutcome(
            "blocked",
            "codex-audit-refute-same-session",
            f"finding {fhash} refuted by the same session — independent review required",
        )
    art = disposition.get("independent_reviewer_artifact")
    if not art or not _is_safe_relpath(art):
        return AuditGateOutcome(
            "retry",
            "codex-audit-refute-no-reviewer",
            f"finding {fhash} reviewer artifact path invalid",
        )
    art_path = _project_home(project) / art
    if not art_path.exists():
        return AuditGateOutcome(
            "retry", "codex-audit-refute-no-reviewer", f"finding {fhash} reviewer artifact missing"
        )
    try:
        rev = json.loads(art_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AuditGateOutcome(
            "retry",
            "codex-audit-refute-no-reviewer",
            f"finding {fhash} reviewer artifact unreadable",
        )
    # R6: validate each §1.7 field by TYPE, not bare truthiness — a non-string
    # truthy value (`["x"]`, `{"fake": true}`) must not satisfy an id / hash.
    if not isinstance(rev, dict):
        return AuditGateOutcome(
            "retry",
            "codex-audit-refute-no-reviewer",
            f"finding {fhash} reviewer artifact not an object",
        )
    if not _nonempty_str(rev.get("independent_run_id")):
        return AuditGateOutcome(
            "retry",
            "codex-audit-refute-no-reviewer",
            f"finding {fhash} reviewer artifact independent_run_id must be a non-empty string",
        )
    if not _SHA256_REF_RE.match(str(rev.get("artifact_hash", ""))):
        return AuditGateOutcome(
            "retry",
            "codex-audit-refute-no-reviewer",
            f"finding {fhash} reviewer artifact_hash must be sha256:<64 hex>",
        )
    verdict = rev.get("verdict")
    if not isinstance(verdict, str) or verdict.lower() not in _REFUTE_VERDICTS:
        return AuditGateOutcome(
            "retry",
            "codex-audit-refute-no-reviewer",
            f"finding {fhash} reviewer verdict {verdict!r} is not a refutation",
        )
    if rev.get("original_finding_hash") != fhash:
        return AuditGateOutcome(
            "retry",
            "codex-audit-refute-no-reviewer",
            f"reviewer artifact bound to {rev.get('original_finding_hash')!r}, not finding {fhash}",
        )
    art_sid = _nonempty_str(rev.get("reviewer_session_id"))
    if not art_sid or art_sid != reviewer_sid:
        return AuditGateOutcome(
            "retry",
            "codex-audit-refute-no-reviewer",
            f"finding {fhash} reviewer artifact session disagrees with disposition",
        )
    # The artifact's own recorded reviewer must also differ from the audited
    # session (defends against an artifact that lies in the disposition but
    # records the audited session internally).
    if art_sid == sid_norm:
        return AuditGateOutcome(
            "blocked",
            "codex-audit-refute-same-session",
            f"finding {fhash} reviewer artifact session == audited session",
        )
    return None


def _gate_full(
    block: dict,
    payload: dict,
    workspace: Path,
    project: str,
    task: str,
    head_now: str,
    mode: str,
) -> AuditGateOutcome:
    """full_codex_audit / docs_only_light_audit: the G2-G9 body.

    G9 round cap → G2 artifact integrity (missing=RETRY / tampered=FATAL) →
    G0 last run audited current HEAD → docs_only content-diff legitimacy →
    G3 every P0/P1 (union across rounds) has a disposition → G4-G8 each
    disposition actually resolves its finding.
    """
    runs = block.get("audit_runs")
    disps = block.get("dispositions") or []
    if not isinstance(runs, list) or not runs:
        return AuditGateOutcome("retry", "codex-audit-required", f"{mode} requires audit_runs")
    if not isinstance(disps, list):
        return AuditGateOutcome("retry", "codex-audit-required", "dispositions must be a list")
    # G9: bound the audit→fix→re-audit loop.
    if len(runs) > _pc.MAX_AUDIT_RUNS:
        return AuditGateOutcome(
            "blocked",
            "codex-audit-rounds-exceeded",
            f"{len(runs)} audit runs > MAX_AUDIT_RUNS={_pc.MAX_AUDIT_RUNS}",
        )

    run_indices: list[int] = []
    for rec in runs:
        if (
            not isinstance(rec, dict)
            or not isinstance(rec.get("run_index"), int)
            or isinstance(rec.get("run_index"), bool)
        ):
            return AuditGateOutcome("retry", "codex-audit-missing", "malformed run record")
        ri = rec["run_index"]
        # G2: artifact must exist and be byte-intact.
        state = classify_artifact_state(project, task, ri)
        if state == "missing":
            return AuditGateOutcome(
                "retry", "codex-audit-missing", f"run {ri} findings artifact missing"
            )
        if state == "tampered":
            return AuditGateOutcome(
                "fatal", "codex-audit-tampered", f"run {ri} findings artifact hash mismatch"
            )
        rec_err = validate_run_record(project, task, rec)
        if rec_err:
            return AuditGateOutcome("retry", "codex-audit-missing", f"run {ri}: {rec_err}")
        run_indices.append(ri)

    # R2-1: the union is only as complete as the runs listed. An attacker could
    # run a FAILING audit (run 1), then list ONLY a later clean run (run 2) so
    # the round-1 P0/P1 never enters the union. Defeat omission by (a) requiring
    # the listed runs to be exactly the runs PERSISTED on disk for this task, and
    # (b) requiring them contiguous 1..max so an early run can't be skipped.
    listed = sorted(set(run_indices))
    if len(listed) != len(run_indices):
        return AuditGateOutcome("retry", "codex-audit-missing", "duplicate run_index in audit_runs")
    discovered = sorted(discover_run_indices(project, task))
    if listed != discovered:
        return AuditGateOutcome(
            "retry",
            "codex-audit-findings-unbound",
            f"audit_runs {listed} != on-disk audit runs {discovered}; "
            "every persisted run must be represented (no omitting a failing run)",
        )
    if listed != list(range(1, listed[-1] + 1)):
        return AuditGateOutcome(
            "retry",
            "codex-audit-findings-unbound",
            f"audit run indices {listed} are not contiguous from 1",
        )

    last_ri = max(run_indices)
    last_rec = next(r for r in runs if r["run_index"] == last_ri)
    # G0: the final (clean) re-audit must have been performed against the commit
    # that is about to be handed off. A sibling commit after the last audit run
    # moves HEAD and invalidates it.
    if not _commit_equals(workspace, last_rec["input_commit"], head_now):
        return AuditGateOutcome(
            "retry",
            "codex-audit-head-moved",
            f"last audit run audited {last_rec['input_commit']} != current HEAD {head_now}",
        )

    # docs_only legitimacy: the machine decides the mode, not the AI (spec §2.2).
    if mode == _pc.AUDIT_MODE_DOCS_ONLY:
        base = _audit_base_from_session(payload, workspace)
        if base is None:
            return AuditGateOutcome(
                "retry",
                "codex-audit-base-missing",
                "docs_only requires a derivable session base commit",
            )
        rc, out = _audit_git(["diff", "--name-only", base, head_now], workspace)
        if rc != 0:
            return AuditGateOutcome("retry", "codex-audit-base-missing", "docs_only diff failed")
        for changed in (ln.strip() for ln in out.splitlines() if ln.strip()):
            if not _is_doc_path(changed):
                return AuditGateOutcome(
                    "retry",
                    "codex-audit-required",
                    f"docs_only claimed but non-doc file changed: {changed} — full audit required",
                )

    # G3: union of every P0/P1 finding across ALL rounds (dedup by identity hash).
    union: dict[str, str] = {}
    # fhash → the SET of every severity-neutral presence hash seen for it across
    # rounds. A set (not a scalar) so two distinct findings that share an id
    # (codex R9) both register — G5 then requires ALL of them gone, not just the
    # last one written. ``None`` entries mark a finding with no presence basis.
    presence_by_fhash: dict[str, set[str | None]] = {}
    for ri in run_indices:
        f = _read_run_findings(project, task, ri)
        if f is None:
            return AuditGateOutcome("retry", "codex-audit-missing", f"run {ri} findings unreadable")
        of = f.get("original_findings")
        # Fail closed when original_findings is not a list (codex R6): a dict-
        # shaped value would otherwise iterate as keys, hiding any P0/P1 inside.
        if not isinstance(of, list):
            return AuditGateOutcome(
                "retry", "codex-audit-missing", f"run {ri} original_findings is not a list"
            )
        for finding in of:
            if not isinstance(finding, dict):
                continue
            sev = _severity(finding)
            # codex R8-2: an unrecognized non-empty severity (typo / spoof) must
            # fail closed, not be silently treated as non-blocking.
            if sev and sev not in _pc.AUDIT_SEVERITIES:
                return AuditGateOutcome(
                    "retry",
                    "codex-audit-missing",
                    f"run {ri} has a finding with unrecognized severity {sev!r}",
                )
            if sev in ("P0", "P1"):
                # An identity-less P0/P1 can't be reliably bound to a disposition
                # (every such finding would collide onto one hash) — reject so it
                # can't be silently covered (codex R1-F5).
                if not has_finding_identity(finding):
                    return AuditGateOutcome(
                        "retry",
                        "codex-audit-findings-unbound",
                        f"run {ri} has a {sev} finding with no stable identity "
                        "(needs an id / location / text)",
                    )
                fh = compute_finding_hash(finding)
                union[fh] = sev
                presence_by_fhash.setdefault(fh, set()).add(finding_presence_hash(finding))

    # The last run's presence set is severity-NEUTRAL so a "fixed" P0/P1 that
    # merely reappears downgraded (e.g. P1→P2) is still detected as present.
    last_findings = _read_run_findings(project, task, last_ri) or {}
    last_of = last_findings.get("original_findings")
    last_presence = {
        ph
        for x in (last_of if isinstance(last_of, list) else [])
        if isinstance(x, dict)
        for ph in [finding_presence_hash(x)]
        if ph is not None
    }

    disp_by_hash: dict[str, dict] = {}
    for d in disps:
        if isinstance(d, dict) and isinstance(d.get("finding_hash"), str):
            disp_by_hash[d["finding_hash"]] = d

    refute_count = 0
    for fhash, sev in union.items():
        d = disp_by_hash.get(fhash)
        if d is None:
            return AuditGateOutcome(
                "retry",
                "codex-audit-findings-unbound",
                f"{sev} finding {fhash} has no disposition",
            )
        # Dispatch on the disposition type FIRST, then let each branch validate
        # its OWN required fields with the right exit class. A generic shape
        # check up front would mis-classify (e.g. an AI-fabricated owner_override
        # missing its ack token would surface as a soft RETRY "malformed" instead
        # of the hard BLOCKED the spec mandates). Lesson: shape-vs-verifier
        # separation (prior Phase B G7 regression).
        disp = d.get("disposition")
        if disp not in _pc.DISPOSITION_TYPES:
            return AuditGateOutcome(
                "retry",
                "codex-audit-findings-unbound",
                f"disposition for {fhash} has invalid type {disp!r}",
            )
        # G4: a P0/P1 may never be merely deferred.
        if disp == _pc.DISPOSITION_DEFERRED:
            return AuditGateOutcome(
                "blocked",
                "codex-audit-p0p1-unresolved",
                f"{sev} finding {fhash} was deferred; P0/P1 must be fixed/refuted/overridden",
            )
        # G5: a fix needs a real fix_commit AND must be gone from the last run.
        if disp == _pc.DISPOSITION_FIXED:
            fix_commit = d.get("fix_commit")
            if not fix_commit or not _GIT_SHA_RE.match(str(fix_commit)):
                return AuditGateOutcome(
                    "retry",
                    "codex-audit-findings-unbound",
                    f"finding {fhash} marked fixed without a valid fix_commit",
                )
            presences = presence_by_fhash.get(fhash, set())
            if not presences or None in presences:
                # A contributing finding had no stable location/text basis — the
                # AI-controlled id alone can't prove it gone (codex R8-1). Fail closed.
                return AuditGateOutcome(
                    "retry",
                    "codex-audit-findings-unbound",
                    f"finding {fhash} marked fixed but has no location/text to verify it is gone",
                )
            # EVERY historical location for this identity must be gone from the
            # last run — fixing one site while another (possibly downgraded) site
            # of the same id persists is not a fix (codex R9).
            if presences & last_presence:
                return AuditGateOutcome(
                    "retry",
                    "codex-audit-fix-unverified",
                    f"finding {fhash} marked fixed but still present (any severity) in the last run",
                )
        # G6: a refutation must come from a DIFFERENT session AND be backed by a
        # reviewer artifact whose CONTENT proves the independent review of THIS
        # finding (codex R1-F3 — previously only path-existence was checked).
        elif disp == _pc.DISPOSITION_REFUTED:
            refute_count += 1
            verdict = _validate_reviewer_refute(d, fhash, payload.get("session_id"), project)
            if verdict is not None:
                return verdict
        # G7: an owner override must be backed by a real ON-DISK owner-ack
        # artifact, bound to THIS finding, self-consistent, and unexpired.
        # TRUST MODEL (design §1 / owner ruling #1): anti-tamper + friction, NOT
        # cryptography — an AI with the owner's identity can write a
        # self-consistent ack. This catches silent REUSE (finding_hash binding),
        # indefinite validity (expiry) and trace-less approval (the audit jsonl
        # below); it does NOT stop a malicious forger (owner-held-key, design §6).
        # R5: the disposition token must be a real non-empty string first.
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
            # Binding: the ack must be FOR this finding (catch reuse elsewhere).
            if ack.get("finding_hash") != fhash:
                return AuditGateOutcome(
                    "blocked",
                    "codex-audit-override-invalid",
                    f"finding {fhash} owner-ack binds a different finding "
                    f"{ack.get('finding_hash')!r}",
                )
            # Self-consistency: recompute the binding checksum; the disposition
            # token, the ack token and the recompute must ALL agree.
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
            # Expiry binding (R1-P1): expires_at is NOT covered by the token, so a
            # truly-expired ack could otherwise be revived by hand-editing only
            # expires_at into the future. Pin it to approved_at + TTL (the value
            # write_owner_ack derives) AND require it unexpired.
            exp = ack.get("expires_at")
            if not _nonempty_str(exp):
                return AuditGateOutcome(
                    "blocked",
                    "codex-audit-override-invalid",
                    f"finding {fhash} owner_override ack missing expires_at",
                )
            try:
                exp_dt = _parse_iso_utc(exp)
                approved_dt = _parse_iso_utc(approved_at)
            except ValueError:
                return AuditGateOutcome(
                    "blocked",
                    "codex-audit-override-invalid",
                    f"finding {fhash} owner_override ack has unparseable timestamps",
                )
            if exp_dt != approved_dt + timedelta(days=OWNER_ACK_TTL_DAYS):
                return AuditGateOutcome(
                    "blocked",
                    "codex-audit-override-invalid",
                    f"finding {fhash} owner-ack expires_at != approved_at + "
                    f"{OWNER_ACK_TTL_DAYS}d (tampered window)",
                )
            if _is_expired(exp):
                return AuditGateOutcome(
                    "blocked",
                    "codex-audit-override-invalid",
                    f"finding {fhash} owner_override ack expired at {exp}",
                )
            # Trace the consumption (篡改证据 / design §2.4).
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

    # §2.4: independent refutes are bounded so a finding can't livelock the gate.
    if refute_count > _pc.MAX_INDEP_REVIEW:
        return AuditGateOutcome(
            "blocked",
            "codex-audit-indep-review-exceeded",
            f"{refute_count} independent refutes > MAX_INDEP_REVIEW={_pc.MAX_INDEP_REVIEW}",
        )

    # G8: every recorded deferral (P2/P3) must carry a scope ruling.
    for d in disps:
        if (
            isinstance(d, dict)
            and d.get("disposition") == _pc.DISPOSITION_DEFERRED
            and d.get("original_severity") in _pc.DEFERRABLE_SEVERITIES
            and not _nonempty_str(d.get("scope_ruling"))
        ):
            return AuditGateOutcome(
                "retry",
                "codex-audit-defer-invalid",
                "deferred disposition needs a non-empty scope_ruling",
            )
    return _AUDIT_OK


def evaluate_audit_gate(
    payload: dict, workspace: Path, project: str, task: str
) -> AuditGateOutcome:
    """Run the G0-G9 audit gate over a retro-evidence payload (mandate-ON path).

    Returns an :class:`AuditGateOutcome`; the caller (retro_gate) maps its
    ``klass`` to the exit-code protocol. Assumes the audit mandate is enabled —
    retro_gate guards the flag and only calls this when it is on.
    """
    block = payload.get("codex_audit")
    if not isinstance(block, dict):
        return AuditGateOutcome(
            "retry", "codex-audit-required", "evidence carries no codex_audit block"
        )
    mode = block.get("audit_mode")
    if mode not in _pc.AUDIT_MODES:
        return AuditGateOutcome(
            "retry", "codex-audit-required", f"unknown / missing audit_mode: {mode!r}"
        )

    # Cross-repo anchor: resolve the repo the gate audits ONCE here, then use it
    # for every git op below. ``code_repo`` (when present) is the audited repo;
    # absent → the launching workspace (same-repo, unchanged).
    audit_ws, ws_err = _resolve_audit_ws(block, workspace)
    if ws_err is not None:
        reason = (
            "not on the configured audit_code_repos allowlist"
            if ws_err == "codex-audit-code-repo-not-allowed"
            else "invalid"
        )
        return AuditGateOutcome("retry", ws_err, f"code_repo {reason}: {block.get('code_repo')!r}")

    rc, head_out = _audit_git(["rev-parse", "HEAD"], audit_ws)
    head_now = head_out.strip()
    if rc != 0 or not head_now:
        return AuditGateOutcome("retry", "codex-audit-head-unknown", "git rev-parse HEAD failed")

    # Cross-repo extra binding (codex R1/R2/R3/R4). Only when code_repo is declared:
    if block.get("code_repo") is not None:
        # (R1) docs_only_light_audit derives its diff base from the LAUNCHER's
        # session_commits, which are unrelated to code_repo — so its "only docs
        # changed" ruling would diff the wrong repo. Forbid it cross-repo; a real
        # cross-repo doc change must go through a full audit.
        if mode == _pc.AUDIT_MODE_DOCS_ONLY:
            return AuditGateOutcome(
                "retry",
                "codex-audit-docs-only-cross-repo",
                "docs_only_light_audit not allowed cross-repo: its diff base is the "
                "launcher's session_commits, not code_repo's — use full_codex_audit",
            )
        # (R2/R4) the builder always stamps code_repo_head; require it and bind it
        # to the LIVE code_repo HEAD so a stale / forged / omitted head can't ride
        # along. This makes the recorded head meaningful and is the available
        # non-crypto friction for the wrong-code_repo boundary documented in
        # _resolve_audit_ws (a fully malicious AI is out of the single-user model).
        declared_head = block.get("code_repo_head")
        if not isinstance(declared_head, str) or not _commit_equals(
            audit_ws, declared_head, head_now
        ):
            return AuditGateOutcome(
                "retry",
                "codex-audit-code-repo-head-mismatch",
                f"code_repo_head {declared_head!r} != live code_repo HEAD {head_now}",
            )

    # R3-2: the no-audit-needed modes (bypass / empty_diff) may not be used to
    # sidestep a REAL audit that already ran. If any full-audit run artifact is
    # persisted for this task, the gate forces full_codex_audit so a recorded
    # P0/P1 can't be dodged by switching the declared mode.
    if mode in (_pc.AUDIT_MODE_BYPASS, _pc.AUDIT_MODE_EMPTY_DIFF) and discover_run_indices(
        project, task
    ):
        return AuditGateOutcome(
            "retry",
            "codex-audit-required",
            f"{mode} not allowed: persisted audit runs exist for this task — use full_codex_audit",
        )

    if mode == _pc.AUDIT_MODE_BYPASS:
        return _gate_bypass(block)
    if mode == _pc.AUDIT_MODE_EMPTY_DIFF:
        return _gate_empty_diff(block, audit_ws, head_now)
    return _gate_full(block, payload, audit_ws, project, task, head_now, mode)


# ─── CLI ────────────────────────────────────────────────────────────────────


def _resolve_project_workspace(args) -> tuple[str, Path]:
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    project = args.project or workspace.name
    return project, workspace


def _common_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--task", required=True, help="kebab-case task ID")
    ap.add_argument("--project", default=None, help="project slug; defaults to basename(workspace)")
    ap.add_argument("--workspace", default=None, help="abs path to project root; defaults to cwd")


def main_audit_run(argv: list[str] | None = None) -> int:
    """Register one codex audit run: ingest a findings JSON, write the artifact
    + sidecar manifest, print the run record as JSON (for the AI to collect into
    ``audit-close``)."""
    ap = argparse.ArgumentParser(prog="handoff audit-run")
    _common_args(ap)
    ap.add_argument("--run-index", type=int, required=True, help="1 for the initial audit")
    ap.add_argument("--findings-file", required=True, help="path to codex's findings JSON")
    ap.add_argument(
        "--input-commit",
        default=None,
        help="HEAD this run audited; defaults to the findings file's input_commit",
    )
    ap.add_argument(
        "--code-repo",
        default=None,
        help="abs path to the AUDITED repo when it differs from the launching "
        "workspace; --input-commit then defaults to its HEAD",
    )
    args = ap.parse_args(argv)

    if not _pc.TASK_ID_RE.match(args.task):
        sys.stderr.write(f"ERR-FATAL invalid-task-id: {args.task!r}\n")
        return 1
    if args.run_index < 1:
        sys.stderr.write(f"ERR-FATAL invalid-run-index: {args.run_index} (must be >= 1)\n")
        return 1
    project, _ = _resolve_project_workspace(args)
    if not _pc.TASK_ID_RE.match(project):
        sys.stderr.write(f"ERR-FATAL invalid-project-slug: {project!r}\n")
        return 1

    ffile = Path(args.findings_file)
    if not ffile.exists():
        sys.stderr.write(f"ERR-FATAL findings-file-missing: {ffile}\n")
        return 1
    try:
        findings = json.loads(ffile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"ERR-FATAL findings-file-invalid: {e}\n")
        return 1
    if not isinstance(findings, dict):
        sys.stderr.write("ERR-FATAL findings-file-invalid: must be a JSON object\n")
        return 1
    # codex R6: original_findings must be a list so a dict-shaped value can't hide
    # a P0/P1 from derive_verdict / the gate union. Reject at ingest (fail-closed).
    if not isinstance(findings.get("original_findings"), list):
        sys.stderr.write("ERR-FATAL findings-file-invalid: original_findings must be a list\n")
        return 1

    # Cross-repo: when --code-repo is given and --input-commit is not, the run
    # audited the code repo's HEAD (not the launching workspace's). Resolve it
    # here so the gate's G0 (last run audited current code-repo HEAD) lines up.
    code_repo_head = None
    if args.code_repo and not args.input_commit:
        cr = Path(args.code_repo).resolve()
        rc, out = _audit_git(["rev-parse", "HEAD"], cr)
        out = out.strip()
        if rc != 0 or not out:
            sys.stderr.write(f"ERR-FATAL code-repo-head-unknown: {args.code_repo!r}\n")
            return 1
        code_repo_head = out
    input_commit = args.input_commit or code_repo_head or findings.get("input_commit")
    if not input_commit:
        sys.stderr.write(
            "ERR-FATAL input-commit-missing: pass --input-commit or set it in findings\n"
        )
        return 1

    # R2 P1: serialize artifact writes against a concurrent audit-close snapshot
    # via the per-task audit lock.
    lock = _audit_lock_path(project, args.task)
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        with atomic.acquire_dir_lock(lock, retries=5, wait_seconds=0.2):
            # R3-1 (honest path): audit runs are append-only — refuse to OVERWRITE
            # an existing run index, so a failing run can't be silently replaced
            # with a clean one at the same index. (Deletion-then-recreate by a
            # local writer is out of this CLI's reach; Phase C binds the audit
            # hash into owner-controlled old_ready as the external tamper anchor.)
            if findings_path(project, args.task, args.run_index).exists():
                sys.stderr.write(
                    f"ERR-FATAL run-index-exists: run {args.run_index} already has an artifact; "
                    "audit runs are append-only — use the next index for a re-audit\n"
                )
                return 1
            record = write_findings_artifact(
                project, args.task, args.run_index, findings, input_commit=input_commit
            )
    except atomic.LockAcquisitionError:
        sys.stderr.write(f"ERR-LOCKED audit-lock-held: {lock}\n")
        return 3
    sys.stdout.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


def main_audit_disposition(argv: list[str] | None = None) -> int:
    """Validate + persist one disposition for an original codex finding."""
    ap = argparse.ArgumentParser(prog="handoff audit-disposition")
    _common_args(ap)
    ap.add_argument("--finding-id", required=True)
    ap.add_argument("--finding-hash", required=True)
    ap.add_argument("--original-severity", required=True, choices=list(_pc.AUDIT_SEVERITIES))
    ap.add_argument("--disposition", required=True, choices=list(_pc.DISPOSITION_TYPES))
    ap.add_argument("--fix-commit", default=None)
    ap.add_argument("--independent-reviewer-artifact", default=None)
    ap.add_argument("--reviewer-session-id", default=None)
    ap.add_argument("--owner-ack-token", default=None)
    ap.add_argument("--scope-ruling", default=None)
    ap.add_argument("--material-note", default=None)
    args = ap.parse_args(argv)

    if not _pc.TASK_ID_RE.match(args.task):
        sys.stderr.write(f"ERR-FATAL invalid-task-id: {args.task!r}\n")
        return 1
    project, _ = _resolve_project_workspace(args)
    if not _pc.TASK_ID_RE.match(project):
        sys.stderr.write(f"ERR-FATAL invalid-project-slug: {project!r}\n")
        return 1

    disposition: dict = {
        "finding_id": args.finding_id,
        "finding_hash": args.finding_hash,
        "original_severity": args.original_severity,
        "disposition": args.disposition,
    }
    for key, val in (
        ("fix_commit", args.fix_commit),
        ("independent_reviewer_artifact", args.independent_reviewer_artifact),
        ("reviewer_session_id", args.reviewer_session_id),
        ("owner_ack_token", args.owner_ack_token),
        ("scope_ruling", args.scope_ruling),
        ("material_note", args.material_note),
    ):
        if val is not None:
            disposition[key] = val

    try:
        append_disposition(project, args.task, disposition)
    except ValueError as e:
        sys.stderr.write(f"ERR-FATAL disposition-invalid: {e}\n")
        return 1
    sys.stdout.write(f"OK disposition-recorded: {args.finding_id} → {args.disposition}\n")
    return 0


def main_audit_close(argv: list[str] | None = None) -> int:
    """Single-process: assemble the codex_audit block from registered runs +
    dispositions, fold it into retro evidence, then invoke ``dump`` — all under
    one held dump lock so HEAD can't drift between audit and handoff (R2-P0-6).

    Phase A note (mandate OFF): the dump gate does not yet enforce G0-G9, so a
    close here only *records* the audit block. The lock + single-process
    sequencing is the Phase-B-ready scaffold.
    """
    ap = argparse.ArgumentParser(prog="handoff audit-close")
    _common_args(ap)
    ap.add_argument("--next", required=True, help="next task brief")
    ap.add_argument(
        "--audit-mode",
        required=True,
        choices=list(_pc.AUDIT_MODES),
        help="caller-chosen mode (gate re-decides in Phase B)",
    )
    ap.add_argument(
        "--run-record", action="append", default=[], help="repeatable: a run record JSON"
    )
    ap.add_argument("--attestation-file", default=None, help="empty_diff_attestation JSON file")
    ap.add_argument("--bypass-file", default=None, help="codex_unavailable_bypass JSON file")
    ap.add_argument(
        "--code-repo",
        default=None,
        help="abs path to the AUDITED repo when it differs from the launching "
        "workspace (cross-repo handoff); binds G0 to its HEAD",
    )
    ap.add_argument("--status", default="active", help="dump status (active|done|blocked)")
    ap.add_argument("--tests", default=None, help="forwarded to dump --tests")
    # retro evidence phase status (forwarded to precheck-style build)
    ap.add_argument("--phase0-status", action="append", default=[])
    ap.add_argument("--phase1-status", action="append", default=[])
    args = ap.parse_args(argv)

    if not _pc.TASK_ID_RE.match(args.task):
        sys.stderr.write(f"ERR-FATAL invalid-task-id: {args.task!r}\n")
        return 1
    project, workspace = _resolve_project_workspace(args)
    if not _pc.TASK_ID_RE.match(project):
        sys.stderr.write(f"ERR-FATAL invalid-project-slug: {project!r}\n")
        return 1

    # Parse caller inputs (pure; on-disk state is validated under the lock below).
    try:
        runs = [json.loads(r) for r in args.run_record]
    except json.JSONDecodeError as e:
        sys.stderr.write(f"ERR-FATAL run-record-invalid: {e}\n")
        return 1
    attestation = None
    bypass = None
    if args.attestation_file:
        try:
            attestation = json.loads(Path(args.attestation_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write(f"ERR-FATAL attestation-file-invalid: {e}\n")
            return 1
    if args.bypass_file:
        try:
            bypass = json.loads(Path(args.bypass_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write(f"ERR-FATAL bypass-file-invalid: {e}\n")
            return 1
    p0 = _pc._parse_phase_kv(args.phase0_status)
    p1 = _pc._parse_phase_kv(args.phase1_status)
    reason_err = _pc.check_reason_required(
        p0, _pc.PHASE0_KEYS, "phase0"
    ) or _pc.check_reason_required(p1, _pc.PHASE1_KEYS, "phase1")
    if reason_err:
        sys.stderr.write(reason_err + "\n")
        return 1

    # R2 P1: the entire audit snapshot — validate run records, read dispositions,
    # build the block, write evidence, dump — must run under ONE held critical
    # section so a concurrent audit-run / audit-disposition can't mutate the
    # audited state between validation and handoff. Lock order precheck → dump →
    # audit matches retro_gate; the nested dump→retro_gate re-acquires
    # precheck+dump re-entrantly (process-wide flock registry); audit.lock is not
    # taken by dump, so holding it across dump.main is deadlock-free.
    locks_root = _pc.locks_dir(project)
    precheck_lock = locks_root / "precheck.lock"
    dump_lock = locks_root / "dump.lock"
    audit_lock = _audit_lock_path(project, args.task)
    locks_root.mkdir(parents=True, exist_ok=True)
    try:
        with (
            atomic.acquire_dir_lock(precheck_lock, retries=1, wait_seconds=0.0),
            atomic.acquire_dir_lock(dump_lock, retries=1, wait_seconds=0.0),
            atomic.acquire_dir_lock(audit_lock, retries=5, wait_seconds=0.2),
        ):
            # A run record may only enter evidence if a real findings artifact
            # backs it (paths/commit/hash/verdict cross-checked vs the sidecar).
            if args.audit_mode in (_pc.AUDIT_MODE_FULL, _pc.AUDIT_MODE_DOCS_ONLY):
                for rec in runs:
                    rec_err = validate_run_record(project, args.task, rec)
                    if rec_err:
                        sys.stderr.write(f"ERR-FATAL run-record-invalid: {rec_err}\n")
                        return 1
            dispositions = load_dispositions(project, args.task)
            # Resolve a relative --code-repo to abs so the gate's is_absolute()
            # admission passes; absent → None (same-repo, unchanged).
            code_repo_abs = str(Path(args.code_repo).resolve()) if args.code_repo else None
            try:
                block = build_codex_audit_block(
                    args.audit_mode,
                    audit_runs=runs,
                    dispositions=dispositions,
                    attestation=attestation,
                    bypass=bypass,
                    code_repo=code_repo_abs,
                )
            except ValueError as e:
                sys.stderr.write(f"ERR-FATAL codex-audit-block-invalid: {e}\n")
                return 1

            # Component B: when codex is unavailable, auto-emit the bypass sidecar
            # the Phase C overdue scanner reads (design §3 / owner ruling #2 — no
            # owner click; codex-down is a machine fact). The builder above has
            # already validated the bypass fields; this persists them so the
            # re-audit debt is enforceable. created_at = the audit-close moment.
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

            evidence = _pc.build_evidence(
                task_id=args.task,
                project=project,
                workspace=workspace,
                phase0=p0,
                phase1=p1,
                codex_audit=block,
            )
            out = _pc.precheck_dir(project) / f"{args.task}.retro.evidence.json"
            _pc.write_evidence(evidence, out)

            from handoff_fanout import dump

            dump_argv = [
                "--task",
                args.task,
                "--next",
                args.next,
                "--project",
                project,
                "--workspace",
                str(workspace),
                "--status",
                args.status,
                "--retro-evidence",
                str(out),
            ]
            if args.tests:
                dump_argv += ["--tests", args.tests]
            return dump.main(dump_argv)
    except atomic.LockAcquisitionError:
        sys.stderr.write(f"ERR-LOCKED audit-close-lock-held: {locks_root}\n")
        return 3
