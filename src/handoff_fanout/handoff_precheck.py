"""v5.4 retro-evidence precheck: collect Phase 0 / Phase 1 state into a JSON
artifact that the next dump-handoff invocation gates on.

This module has two surfaces:

  * A library API (used by tests and by the dump gate to compute hashes).
  * A CLI (`handoff-precheck`) that the AI invokes after closing a task to
    emit a `precheck/<task>.retro.evidence.json` file.

Spec source of truth: ``docs/PROTOCOL.md`` Part II §13 (the v5.4 retro-evidence gate).

The Phase 0 / Phase 1 schemas are intentionally lightweight skeletons —
the precheck CLI captures what the workspace can prove (git HEAD, last
commit age, machine fingerprint) and leaves the per-item ✅/⚠️/❌/skip
status to the caller, who supplies it via ``--phase0-status`` /
``--phase1-status`` flags or a ``--phase-status-file`` JSON payload.
The dump-side gate enforces that all 10 items carry a known status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from handoff_fanout import atomic
from handoff_fanout import config as _config

EVIDENCE_SCHEMA_VERSION = "5.5.0"
# The gate accepts any version in this set so an in-flight v5.4.1 evidence file
# (written by the previous release, consumed by the next dump) still passes
# during the migration window — spec §2.5 / R2-P1-5. The builder always emits
# EVIDENCE_SCHEMA_VERSION; the older entries exist only for read compatibility.
SUPPORTED_EVIDENCE_SCHEMA_VERSIONS = (EVIDENCE_SCHEMA_VERSION, "v5.4.1")
EVIDENCE_KIND_RETRO = "retro"
EVIDENCE_KIND_AGGREGATE = "fan_in_aggregate"

# ─── codex audit gate constants (spec §3.5 / §4.4 / §1.7) ───────────────────
# These constants pin the four audit modes, the convergence bounds, and the
# disposition vocabulary that the retro_gate G0-G9 path enforces (mandate ON
# since Phase D, 2026-05-30). (History: Phase A shipped the evidence capability
# with the mandate OFF, recording the block without enforcing it.)

AUDIT_MODE_FULL = "full_codex_audit"
AUDIT_MODE_EMPTY_DIFF = "empty_diff_attestation"
AUDIT_MODE_DOCS_ONLY = "docs_only_light_audit"
AUDIT_MODE_BYPASS = "codex_unavailable_bypass"
AUDIT_MODES = (
    AUDIT_MODE_FULL,
    AUDIT_MODE_EMPTY_DIFF,
    AUDIT_MODE_DOCS_ONLY,
    AUDIT_MODE_BYPASS,
)

# Convergence bounds (spec §4.4): MAX_AUDIT_RUNS caps the audit→fix→re-audit
# loop (initial audit counts as run 1); MAX_INDEP_REVIEW caps independent
# refute reviews so a refuted finding cannot livelock the gate.
MAX_AUDIT_RUNS = 3
MAX_INDEP_REVIEW = 2

DISPOSITION_FIXED = "fixed"
DISPOSITION_REFUTED = "independent_reviewer_refuted"
DISPOSITION_OWNER_OVERRIDE = "owner_override"
DISPOSITION_DEFERRED = "deferred"
DISPOSITION_TYPES = (
    DISPOSITION_FIXED,
    DISPOSITION_REFUTED,
    DISPOSITION_OWNER_OVERRIDE,
    DISPOSITION_DEFERRED,
)

AUDIT_SEVERITIES = ("P0", "P1", "P2", "P3")
# Only P2/P3 may be deferred (recorded, not blocking). P0/P1 must be fixed,
# independently refuted, or owner-overridden (spec §3.1 / G4 / G8).
DEFERRABLE_SEVERITIES = ("P2", "P3")

PHASE0_KEYS = ("memory", "tests", "audit", "commit", "code_review")
PHASE1_KEYS = ("codex", "claude_md", "l2_memory", "tests", "prs")
PHASE_STATUS_VALID = {"✅", "⚠️", "❌", "skip"}

# Any non-✅ status is a claim that an item was partial / skipped / failed.
# Such a claim is meaningless without a reason, so the gate (and the CLI
# that emits evidence) require one. ✅ ("done") needs no explanation.
STATUS_REQUIRING_REASON = {"⚠️", "❌", "skip"}

# ─── retrieval-pull (predecessor_lesson_backref) vocabulary ─────────────────
# The optional structured back-reference a closing session records for each
# predecessor lesson it consumed at session start (component 1 / L1 keystone):
# 前任课 X → 已应用 (applied) / 已被取代 (superseded, name the new lesson) /
# 不相关 (not_relevant, give the reason). A non-"applied" disposition REQUIRES a
# reason — the same "a non-✅ claim is meaningless without a reason" invariant the
# phase status carries. This field is OPTIONAL (warn-mode L1): when omitted the
# evidence payload is byte-for-byte identical to a pre-backref payload.
BACKREF_DISPOSITIONS = ("applied", "superseded", "not_relevant")

# ─── lesson_disposition vocabulary (component 5 / L2 — honest "no new lesson") ─
# The optional single-value record of what this hop did about CAPTURING a lesson
# (distinct from BACKREF_DISPOSITIONS above, which records consuming PREDECESSOR
# lessons): 新课 (new_lesson) / earned 显式「本棒例行·无新课」(no_novel_lesson_attested) /
# 沿用前任课无新增 (carried_forward). The fleet learning canary (built later) uses
# this to EXCLUDE honest lesson-less hops from its lessons-per-handoff denominator,
# so nobody is forced to manufacture cargo-cult lessons just to clear the floor.
# ``no_novel_lesson_attested`` and ``carried_forward`` REQUIRE a reason (an honest
# attestation must say WHY there was nothing novel / what was carried) — ``new_lesson``
# is the unremarkable default and its reason is OPTIONAL. This field is OPTIONAL
# (warn-mode L2): when omitted the evidence payload is byte-for-byte identical to a
# pre-lesson_disposition payload (the same conditional-fold invariant as backref).
LESSON_DISPOSITIONS = ("new_lesson", "no_novel_lesson_attested", "carried_forward")
# The two dispositions whose meaning is an ASSERTION of absence — they must justify it.
LESSON_DISPOSITIONS_REQUIRING_REASON = ("no_novel_lesson_attested", "carried_forward")

# ─── closeout_obligations vocabulary (the third status-vector / warn-mode) ──────
# The optional THIRD status-vector (after PHASE0_KEYS / PHASE1_KEYS): a scope-by-delivery
# closeout contract that turns the soft text rule ⑬「交棒前先复盘」into a machine-checkable
# vector. It separates the two things that rule conflated:
#   * sedimentation_always — lesson + retro-evidence, done on EVERY coordinator handoff (✅).
#   * the rest (audit / doc_mapping / release / sync_pipeline / postmortem) — done BY-DELIVERY,
#     each either an artifact-pass (✅) or an explicit N/A (skip + reason).
# Each item reuses the phase status vocabulary (:data:`PHASE_STATUS_VALID`); ``skip`` is already
# in :data:`STATUS_REQUIRING_REASON`, so an N/A item NATURALLY requires a reason ("N/A + why").
# This vector is OPTIONAL and uses the same CONDITIONAL-FOLD invariant as
# ``predecessor_lesson_backref`` / ``lesson_disposition`` (NOT the always-present merge that
# phase0/phase1 use): when omitted the evidence payload is byte-for-byte identical to a
# pre-closeout payload (DEFAULT-OFF = zero behavior change). Semantics: sedimentation_always =
# every hop (should be ✅); audit = only when there were code changes; doc_mapping = only when
# instructions/architecture/config changed; release = only on user-visible delivery;
# sync_pipeline = only when artifacts changed; postmortem = only when this hop had an
# incident/regression. The dump-side gate is WARN-ONLY (advisory, never blocking) — see
# ``dump._run_closeout_obligations_gate``. Spec: ``docs/PROTOCOL.md`` Part II §13.5.
CLOSEOUT_KEYS = (
    "sedimentation_always",
    "audit",
    "doc_mapping",
    "release",
    "sync_pipeline",
    "postmortem",
)

MODE_NORMAL = "normal"
MODE_FORENSIC_RETRO = "forensic_retro"
MODE_VALID = {MODE_NORMAL, MODE_FORENSIC_RETRO}

TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


# ─── locks ──────────────────────────────────────────────────────────────────


def locks_dir(project: str) -> Path:
    """Project-scoped locks directory; created lazily."""
    return _config.home_dir() / project / "locks"


def precheck_dir(project: str) -> Path:
    """Where retro evidence JSON lives by default."""
    return _config.home_dir() / project / "precheck"


# ─── §7.8 session fingerprint ───────────────────────────────────────────────


def _machine_id() -> str:
    """Stable per-machine identifier.

    macOS: hardware UUID via ``ioreg`` (revised in D-1 probe — the previous
    spec used ``VSCODE_MACHINE_ID`` env var, which Claude Code does not
    expose to subprocess env on darwin).

    Linux: ``/etc/machine-id`` per systemd convention.

    Other / unavailable: literal ``no-machine-id`` (fingerprint still
    unique through workspace + entry-point components).
    """
    if sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for line in r.stdout.splitlines():
                if "IOPlatformUUID" in line:
                    # `    "IOPlatformUUID" = "ABCD-..."`
                    val = line.rsplit("=", 1)[-1].strip().strip('"')
                    if val:
                        return val
        except (subprocess.SubprocessError, OSError):
            pass
    elif sys.platform.startswith("linux"):
        for cand in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                v = Path(cand).read_text(encoding="utf-8").strip()
                if v:
                    return v
            except OSError:
                continue
    return "no-machine-id"


def session_fingerprint() -> str:
    """Deterministic fallback fingerprint when ``CLAUDE_CODE_SESSION_ID`` is absent.

    Compose three stable signals:
      * machine UUID (``ioreg`` on macOS, ``machine-id`` on Linux)
      * canonical workspace path (``os.getcwd()`` resolved + lowercased)
      * Claude Code entry-point env var (CLI vs VSCode shell distinguishes)

    Joined with ASCII unit-separator (``\\x1f``) to prevent partial overlap,
    SHA-256 truncated to 32 hex chars (128 bits — collision < 1e-30).
    """
    parts = [
        _machine_id(),
        str(Path(os.getcwd()).resolve()).lower().replace("\\", "/"),
        os.environ.get("CLAUDE_CODE_ENTRYPOINT", "no-entry"),
    ]
    raw = "\x1f".join(parts).encode("utf-8")
    return "fp-" + hashlib.sha256(raw).hexdigest()[:32]


def resolve_session_id() -> tuple[str, str]:
    """Return ``(session_id, session_id_kind)`` per §7.8.

    Primary key is the ``CLAUDE_CODE_SESSION_ID`` env var (Claude Code emits
    a UUID per session). If absent or empty, fall back to fingerprint.
    """
    raw = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if raw:
        return raw, "claude-uuid"
    return session_fingerprint(), "fallback-fingerprint"


# ─── §7.5 canonical hash ────────────────────────────────────────────────────


def canonical_json_bytes(payload: dict) -> bytes:
    """UTF-8 canonical JSON: sorted keys, no whitespace, no BOM.

    The exact byte layout the dump-side gate will rehash, so any
    re-serialization must produce identical bytes.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_evidence_hash(payload: dict) -> str:
    """SHA-256 of the canonical payload, excluding the ``evidence_hash`` field.

    Implements §7.5: hash is over everything except the field that stores
    the hash itself (otherwise self-reference makes verification impossible).
    """
    p = dict(payload)
    p.pop("evidence_hash", None)
    return hashlib.sha256(canonical_json_bytes(p)).hexdigest()


def compute_retro_evidence_hash(evidence_file: Path) -> str:
    """SHA-256 over the entire file bytes (including ``evidence_hash`` field).

    This is what v4 autoclose stores in ``old_ready.retro_evidence_hash``.
    Distinct from ``evidence_hash`` because it covers serialization too.
    """
    return hashlib.sha256(evidence_file.read_bytes()).hexdigest()


# ─── git baseline ───────────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path) -> str:
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            cwd=str(cwd),
        )
        return (r.stdout or "").strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _last_commit_age_sec(workspace: Path) -> int:
    """Seconds since the last commit's committer timestamp (or ``-1`` if unknown)."""
    iso = _git(["log", "-1", "--format=%cI"], workspace)
    if not iso:
        return -1
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return -1
    return int((datetime.now(ts.tzinfo) - ts).total_seconds())


def session_commits(workspace: Path) -> tuple[list[str], str]:
    """Snapshot the commit SHAs this session is responsible for.

    Returns ``(commits, source)``. Preferred source is everything ahead of the
    upstream (``@{upstream}..HEAD``) — the local commits not yet on origin,
    i.e. this session's unpushed work. When no upstream is configured we fall
    back to the last few commits (``--max-count=5``) and flag the source so the
    re-align consumer can treat it more conservatively.

    Deterministic ``list[str]`` (newest-first, git's natural order) — never a
    set — so it serializes stably into the evidence hash.
    """
    out = _git(["rev-list", "@{upstream}..HEAD"], workspace)
    if out:
        return out.split(), "upstream"
    # Either everything is pushed, or there is no upstream. Distinguish by
    # probing for an upstream ref; absent → conservative fallback.
    has_upstream = _git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], workspace
    )
    if has_upstream:
        return [], "upstream"  # clean: all local work already pushed
    fallback = _git(["rev-list", "--max-count=5", "HEAD"], workspace)
    return (fallback.split() if fallback else []), "fallback"


# ─── phase status assembly ──────────────────────────────────────────────────


def _empty_phase(keys: tuple[str, ...]) -> dict:
    return {k: {"status": "skip", "reason": "unsupplied"} for k in keys}


def merge_phase_status(
    base: dict,
    overrides: dict | None,
    valid_keys: tuple[str, ...],
) -> dict:
    """Merge user-supplied per-item status onto a skeleton.

    Unknown keys are ignored (the dump gate only checks ``valid_keys``).
    """
    out = dict(base)
    if not overrides:
        return out
    for k, v in overrides.items():
        if k not in valid_keys:
            continue
        if isinstance(v, str):
            out[k] = {"status": v}
        elif isinstance(v, dict):
            out[k] = dict(v)
    return out


# ─── retrieval-pull back-reference validation ───────────────────────────────


def _validate_backref(entries: object) -> list[dict]:
    """Normalize + validate the ``predecessor_lesson_backref`` list.

    Each entry must be a dict with a non-empty ``predecessor_lesson`` (str) and a
    ``disposition`` in :data:`BACKREF_DISPOSITIONS`; ``reason`` (str) is REQUIRED
    and non-empty when the disposition is not ``"applied"`` (for ``"superseded"``
    it should name the new lesson). Raises :class:`ValueError` on any malformed
    entry so garbage can never be folded into the hashed payload. Returns a fresh
    list of dicts normalized to exactly the canonical keys (extra keys dropped).
    """
    if not isinstance(entries, list):
        raise ValueError(
            f"predecessor_lesson_backref must be a list of dicts; got {type(entries).__name__}"
        )
    out: list[dict] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(
                f"predecessor_lesson_backref[{i}] must be a dict; got {type(entry).__name__}"
            )
        lesson = entry.get("predecessor_lesson")
        if not isinstance(lesson, str) or not lesson.strip():
            raise ValueError(
                f"predecessor_lesson_backref[{i}].predecessor_lesson must be a non-empty str"
            )
        disposition = entry.get("disposition")
        if disposition not in BACKREF_DISPOSITIONS:
            raise ValueError(
                f"predecessor_lesson_backref[{i}].disposition must be one of "
                f"{list(BACKREF_DISPOSITIONS)}; got {disposition!r}"
            )
        norm: dict = {
            "predecessor_lesson": lesson.strip(),
            "disposition": disposition,
        }
        reason = entry.get("reason")
        reason_stripped = reason.strip() if isinstance(reason, str) else ""
        if disposition != "applied" and not reason_stripped:
            raise ValueError(
                f"predecessor_lesson_backref[{i}].reason is required (non-empty) when "
                f"disposition={disposition!r} (for 'superseded', name the new lesson)"
            )
        if reason_stripped:
            norm["reason"] = reason_stripped
        out.append(norm)
    return out


def _validate_lesson_disposition(value: object) -> dict:
    """Normalize + validate the ``lesson_disposition`` single value (component 5).

    ``value`` must be a dict with a ``disposition`` in :data:`LESSON_DISPOSITIONS`
    and, for the two absence-asserting dispositions
    (:data:`LESSON_DISPOSITIONS_REQUIRING_REASON`), a non-empty ``reason`` (str) —
    an honest "no new lesson" must say WHY. ``new_lesson`` may carry an optional
    reason. Raises :class:`ValueError` on any malformed input so garbage can never
    be folded into the hashed payload. Returns a fresh dict normalized to exactly
    the canonical keys (``disposition`` + optional ``reason``; extra keys dropped).
    """
    if not isinstance(value, dict):
        raise ValueError(
            f"lesson_disposition must be a dict; got {type(value).__name__}"
        )
    disposition = value.get("disposition")
    if disposition not in LESSON_DISPOSITIONS:
        raise ValueError(
            f"lesson_disposition.disposition must be one of "
            f"{list(LESSON_DISPOSITIONS)}; got {disposition!r}"
        )
    norm: dict = {"disposition": disposition}
    reason = value.get("reason")
    reason_stripped = reason.strip() if isinstance(reason, str) else ""
    if disposition in LESSON_DISPOSITIONS_REQUIRING_REASON and not reason_stripped:
        raise ValueError(
            f"lesson_disposition.reason is required (non-empty) when "
            f"disposition={disposition!r} (attest WHY there was no novel lesson / "
            "what was carried forward)"
        )
    if reason_stripped:
        norm["reason"] = reason_stripped
    return norm


def _validate_closeout(value: object) -> dict:
    """Normalize + validate the ``closeout_obligations`` status-vector (the third vector).

    Mirrors the conditional-fold validators above (:func:`_validate_backref` /
    :func:`_validate_lesson_disposition`): a fail-fast validator whose job is to guarantee
    garbage can NEVER be folded into the hashed payload. ``value`` must be a dict mapping a
    key in :data:`CLOSEOUT_KEYS` to a status entry (a ``{"status": ..., "reason"?: ...}`` dict,
    or a bare status string — both forms are accepted, mirroring :func:`merge_phase_status`).
    Each status must be in :data:`PHASE_STATUS_VALID`; a status in
    :data:`STATUS_REQUIRING_REASON` (⚠️/❌/skip — including the ``skip`` that encodes "N/A")
    REQUIRES a non-empty ``reason``, so an N/A item naturally carries its justification.

    Unknown-key policy: an unknown top-level key (not in :data:`CLOSEOUT_KEYS`) raises
    :class:`ValueError`. This is DELIBERATELY stricter than :func:`merge_phase_status` (which
    silently DROPS unknown phase keys): like the other two conditional-fold validators this is
    a fail-fast guard, the keys ARE an enum, and an unrecognized obligation name is malformed
    input that must not ride into the hashed payload. Extra keys WITHIN an entry dict are
    dropped to the canonical shape (same as :func:`_validate_backref`). Returns a fresh dict
    normalized to exactly ``{key: {"status": ..., "reason"?: ...}}`` (canonical keys only).
    """
    if not isinstance(value, dict):
        raise ValueError(
            f"closeout_obligations must be a dict; got {type(value).__name__}"
        )
    out: dict = {}
    for key, entry in value.items():
        if key not in CLOSEOUT_KEYS:
            raise ValueError(
                f"closeout_obligations key {key!r} must be one of {list(CLOSEOUT_KEYS)}"
            )
        status, reason = _status_and_reason(entry)
        if status not in PHASE_STATUS_VALID:
            raise ValueError(
                f"closeout_obligations[{key!r}].status must be one of "
                f"{sorted(PHASE_STATUS_VALID)}; got {status!r}"
            )
        reason_stripped = reason.strip() if isinstance(reason, str) else ""
        if status in STATUS_REQUIRING_REASON and not reason_stripped:
            raise ValueError(
                f"closeout_obligations[{key!r}].reason is required (non-empty) when "
                f"status={status!r} (only ✅ may omit it; 'skip' encodes an N/A item, so "
                "give the reason it does not apply)"
            )
        norm: dict = {"status": status}
        if reason_stripped:
            norm["reason"] = reason_stripped
        out[key] = norm
    return out


# ─── evidence builder ───────────────────────────────────────────────────────


def build_evidence(
    *,
    task_id: str,
    project: str,
    workspace: Path,
    mode: str = MODE_NORMAL,
    nonce: str | None = None,
    phase0: dict | None = None,
    phase1: dict | None = None,
    codex_audit: dict | None = None,
    predecessor_lesson_backref: list[dict] | None = None,
    lesson_disposition: dict | None = None,
    closeout_obligations: dict | None = None,
) -> dict:
    """Assemble + hash a retro-evidence payload.

    Callers typically supply per-item status via ``phase0`` / ``phase1``;
    when omitted, every item defaults to ``skip`` with reason ``unsupplied``
    which the dump-side gate will reject (so missing input is detected).

    ``codex_audit`` is the optional Phase A audit block (see
    :func:`handoff_fanout.codex_audit.build_codex_audit_block`). When omitted
    the payload is byte-for-byte identical to a pre-5.5.0 retro evidence (minus
    the schema version), so existing flows are unaffected. When present it is
    folded into the hashed payload — tampering with the block invalidates
    ``evidence_hash``.

    ``predecessor_lesson_backref`` (retrieval-pull / L1 keystone) is the optional
    structured back-reference recording which predecessor lessons this closing
    session consumed and what it did with each (see :func:`_validate_backref`).
    Omitted / empty → the payload is byte-for-byte identical to today's (warn-mode
    invariant). When supplied it is validated then folded into the hashed payload
    (so it is bound to the evidence — an independent consumer's score the next
    session can't silently forge).

    ``lesson_disposition`` (component 5 / L2) is the optional single-value record of
    what this hop did about CAPTURING a lesson ({new_lesson, no_novel_lesson_attested,
    carried_forward}; the latter two require a reason — see
    :func:`_validate_lesson_disposition`). It lets the fleet canary exclude honest
    lesson-less hops from its denominator so nobody manufactures cargo-cult lessons.
    Same conditional-fold invariant: omitted → byte-identical; supplied → validated
    then folded into the hashed payload.

    ``closeout_obligations`` (the third status-vector / warn-mode) is the optional
    scope-by-delivery closeout contract ({sedimentation_always, audit, doc_mapping, release,
    sync_pipeline, postmortem}; each ✅ artifact-pass or skip+reason N/A — see
    :func:`_validate_closeout`). SAME conditional-fold invariant as the two above: omitted /
    empty → the payload is byte-for-byte identical to today's (DEFAULT-OFF = zero behavior
    change); supplied → validated then folded into the hashed payload. The dump-side gate that
    reads it is WARN-ONLY (advisory, never blocking).
    """
    if mode not in MODE_VALID:
        raise ValueError(f"mode must be one of {sorted(MODE_VALID)}; got {mode!r}")

    sid, sid_kind = resolve_session_id()
    head = _git(["rev-parse", "HEAD"], workspace) or "(unknown)"
    sess_commits, sess_source = session_commits(workspace)

    payload: dict = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_kind": EVIDENCE_KIND_RETRO,
        "task_id": task_id,
        "project": project,
        "workspace": str(workspace.resolve()),
        "mode": mode,
        "head_at_precheck": head,
        "head_at_precheck_timestamp": _iso_now(),
        "last_commit_age_sec": _last_commit_age_sec(workspace),
        # 1-B: the commit set this session owns, snapshotted at precheck. The
        # dump-side re-align uses it to prove HEAD only moved because of sibling
        # tabs (these commits still ancestors) before re-aligning evidence.
        "session_commits": sess_commits,
        "session_commits_source": sess_source,
        "session_id": sid,
        "session_id_kind": sid_kind,
        "phase0": merge_phase_status(_empty_phase(PHASE0_KEYS), phase0, PHASE0_KEYS),
        "phase1": merge_phase_status(_empty_phase(PHASE1_KEYS), phase1, PHASE1_KEYS),
        "generated_at": _iso_now(),
    }
    if nonce:
        payload["nonce"] = nonce
    if codex_audit is not None:
        payload["codex_audit"] = codex_audit
    # retrieval-pull (L1): fold in ONLY when supplied & non-empty — exactly the
    # conditional-fold pattern above. Absent → byte-identical to today (warn-mode).
    if predecessor_lesson_backref:
        payload["predecessor_lesson_backref"] = _validate_backref(predecessor_lesson_backref)
    # component 5 (L2): fold in ONLY when supplied — same conditional-fold pattern.
    # Absent → byte-identical to today (warn-mode). Present → validated then hashed.
    if lesson_disposition:
        payload["lesson_disposition"] = _validate_lesson_disposition(lesson_disposition)
    # closeout_obligations (third vector): fold in ONLY when supplied — same conditional-fold
    # pattern. Absent / empty → byte-identical to today (DEFAULT-OFF). Present → validated then
    # hashed (bound to the evidence; the warn-mode gate just surfaces it, never blocks).
    if closeout_obligations:
        payload["closeout_obligations"] = _validate_closeout(closeout_obligations)
    payload["evidence_hash"] = compute_evidence_hash(payload)
    return payload


def write_evidence(payload: dict, output: Path) -> None:
    """Persist evidence via the same atomic + fsync path the dump gate trusts."""
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_with_fsync(
        output,
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )


# ─── CLI ────────────────────────────────────────────────────────────────────


def _parse_phase_kv(values: list[str] | None) -> dict:
    """Parse ``--phaseN-status item=status[:reason]`` pairs into a dict.

    Grammar: ``item=status`` or ``item=status:reason text here``. The status
    is everything between ``=`` and the first ``:``; the reason is everything
    after that first ``:`` (verbatim, stripped — colons inside the reason are
    preserved). Valid statuses (✅/⚠️/❌/skip) never contain a colon, so the
    split is unambiguous.
    """
    out: dict[str, dict] = {}
    if not values:
        return out
    for raw in values:
        if "=" not in raw:
            raise SystemExit(f"❌ --phase status pair must be key=value: {raw!r}")
        k, v = raw.split("=", 1)
        k = k.strip()
        v = v.strip()
        if ":" in v:
            status, reason = v.split(":", 1)
            entry: dict = {"status": status.strip()}
            reason = reason.strip()
            if reason:
                entry["reason"] = reason
        else:
            entry = {"status": v}
        out[k] = entry
    return out


def _status_and_reason(entry: object) -> tuple[str | None, str | None]:
    """Normalize a phase-status entry (str or dict) to ``(status, reason)``."""
    if isinstance(entry, str):
        return entry, None
    if isinstance(entry, dict):
        return entry.get("status"), entry.get("reason")
    return None, None


def check_reason_required(statuses: dict, keys: tuple[str, ...], section: str) -> str | None:
    """Return an error string when a non-✅ status lacks a reason, else ``None``.

    Enforces the v5.4 §7.13 invariant at the CLI surface: any ⚠️/❌/skip
    status must carry a reason so retro evidence cannot be a ceremonial
    checkbox. The dump-side gate re-checks this (defence in depth).
    """
    for k, entry in statuses.items():
        if k not in keys:
            continue
        status, reason = _status_and_reason(entry)
        reason_ok = isinstance(reason, str) and reason.strip()
        if status in STATUS_REQUIRING_REASON and not reason_ok:
            return (
                f"ERR-FATAL reason-required: {section}.{k} status={status!r} requires a "
                f"reason — use --{section}-status {k}={status}:<why it was not ✅>"
            )
    return None


def _load_phase_file(path: Path | None) -> dict:
    if not path:
        return {}
    if not path.exists():
        raise SystemExit(f"❌ --phase-status-file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"❌ --phase-status-file invalid JSON: {e}") from e


def parse_backref_kv(values: list[str] | None) -> list[dict]:
    """Parse ``--predecessor-lesson-backref lesson=disposition[:reason]`` pairs.

    Grammar mirrors :func:`_parse_phase_kv`: ``lesson=disposition`` or
    ``lesson=disposition:reason text here`` (the reason is everything after the
    first ``:``, verbatim). Returns a list of raw dicts (NOT yet validated — the
    builder's :func:`_validate_backref` is the single validation point). Raises
    :class:`SystemExit` for a missing ``=`` (a CLI grammar error), so the caller
    surfaces a clean message, not a traceback.
    """
    out: list[dict] = []
    if not values:
        return out
    for raw in values:
        if "=" not in raw:
            raise SystemExit(
                f"❌ --predecessor-lesson-backref must be lesson=disposition[:reason]: {raw!r}"
            )
        lesson, rest = raw.split("=", 1)
        lesson = lesson.strip()
        rest = rest.strip()
        if ":" in rest:
            disposition, reason = rest.split(":", 1)
            entry: dict = {"predecessor_lesson": lesson, "disposition": disposition.strip()}
            reason = reason.strip()
            if reason:
                entry["reason"] = reason
        else:
            entry = {"predecessor_lesson": lesson, "disposition": rest}
        out.append(entry)
    return out


def _load_backref_file(path: Path | None) -> list[dict] | None:
    """Load a JSON-array backref file. ``None`` when no path; raises SystemExit on
    a missing file or non-array / invalid JSON (clean CLI error, not a traceback).
    The contents are NOT validated here — the builder validates."""
    if not path:
        return None
    if not path.exists():
        raise SystemExit(f"❌ --predecessor-lesson-backref-file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"❌ --predecessor-lesson-backref-file invalid JSON: {e}") from e
    if not isinstance(data, list):
        raise SystemExit("❌ --predecessor-lesson-backref-file must be a JSON array of objects")
    return data


def parse_lesson_disposition(value: str | None) -> dict | None:
    """Parse a single ``--lesson-disposition <enum>[:reason]`` value (component 5).

    Grammar reuses the ``:``-split from :func:`_parse_phase_kv`: ``<enum>`` or
    ``<enum>:reason text here`` (the reason is everything after the first ``:``,
    verbatim). The enum values never contain a ``:`` so the split is unambiguous.
    Returns ``None`` when no value was given (so it stays an omitted optional →
    byte-identical evidence), else a raw dict ``{"disposition": ..., "reason"?: ...}``
    (NOT yet validated — the builder's :func:`_validate_lesson_disposition` is the
    single validation point).
    """
    if value is None:
        return None
    raw = value.strip()
    if ":" in raw:
        disposition, reason = raw.split(":", 1)
        entry: dict = {"disposition": disposition.strip()}
        reason = reason.strip()
        if reason:
            entry["reason"] = reason
    else:
        entry = {"disposition": raw}
    return entry


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="handoff-precheck",
        description=("v5.4 retro-evidence precheck: emit retro.evidence.json for the dump gate."),
    )
    ap.add_argument("--task", required=True, help="kebab-case task ID")
    ap.add_argument("--project", default=None, help="project slug; defaults to basename(workspace)")
    ap.add_argument("--workspace", default=None, help="abs path to project root; defaults to cwd")
    ap.add_argument(
        "--mode",
        default=MODE_NORMAL,
        choices=sorted(MODE_VALID),
        help="normal: enforce phase status; forensic_retro: gate is lenient",
    )
    ap.add_argument("--nonce", default=None, help="optional per-task nonce (carried into hash)")
    ap.add_argument(
        "--output",
        default=None,
        help="destination JSON path; defaults to $HANDOFF_HOME/<project>/precheck/<task>.retro.evidence.json",
    )
    ap.add_argument(
        "--phase0-status",
        action="append",
        default=[],
        help="repeatable; item=status[:reason]. ✅ needs no reason; ⚠️/❌/skip "
        "require one, e.g. --phase0-status audit=⚠️:codex pending",
    )
    ap.add_argument(
        "--phase1-status",
        action="append",
        default=[],
        help="repeatable; item=status[:reason]. ⚠️/❌/skip require a reason, "
        "e.g. --phase1-status codex=skip:not a code change",
    )
    ap.add_argument(
        "--phase-status-file",
        default=None,
        help="JSON file with {phase0:{item:status}, phase1:{item:status}}",
    )
    ap.add_argument(
        "--predecessor-lesson-backref",
        action="append",
        default=[],
        dest="predecessor_lesson_backref",
        help="retrieval-pull (warn-mode L1): repeatable; "
        "lesson=disposition[:reason] where disposition ∈ "
        f"{list(BACKREF_DISPOSITIONS)} (reason required for non-'applied'), e.g. "
        "--predecessor-lesson-backref lesson-old=superseded:lesson-new replaces it",
    )
    ap.add_argument(
        "--predecessor-lesson-backref-file",
        default=None,
        dest="predecessor_lesson_backref_file",
        help="path to a JSON array of backref objects; when given it REPLACES the "
        "--predecessor-lesson-backref flags (file wins)",
    )
    ap.add_argument(
        "--lesson-disposition",
        default=None,
        dest="lesson_disposition",
        help="component 5 (warn-mode L2): single value <enum>[:reason] where enum ∈ "
        f"{list(LESSON_DISPOSITIONS)} (reason required for "
        f"{list(LESSON_DISPOSITIONS_REQUIRING_REASON)}), e.g. "
        "--lesson-disposition no_novel_lesson_attested:routine feature, nothing novel",
    )
    ap.add_argument(
        "--closeout-status",
        action="append",
        default=[],
        dest="closeout_status",
        help="closeout_obligations (warn-mode, third vector): repeatable; key=status[:reason] "
        f"where key ∈ {list(CLOSEOUT_KEYS)} and status ∈ {sorted(PHASE_STATUS_VALID)}. ✅ needs "
        "no reason; ⚠️/❌/skip require one ('skip' encodes an N/A item → give why it does not "
        "apply), e.g. --closeout-status release=skip:no user-visible change this hop",
    )
    ap.add_argument(
        "--no-lock",
        action="store_true",
        help="skip precheck.lock acquisition (advanced; for forensic batch only)",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not TASK_ID_RE.match(args.task):
        sys.stderr.write(f"ERR-FATAL invalid-task-id: {args.task!r}\n")
        return 1

    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    if not workspace.exists():
        sys.stderr.write(f"ERR-FATAL workspace-missing: {workspace}\n")
        return 1
    project = args.project or workspace.name
    if not TASK_ID_RE.match(project):
        sys.stderr.write(f"ERR-FATAL invalid-project-slug: {project!r}\n")
        return 1

    file_overrides = _load_phase_file(
        Path(args.phase_status_file) if args.phase_status_file else None
    )
    p0 = dict(file_overrides.get("phase0", {}))
    p1 = dict(file_overrides.get("phase1", {}))
    p0.update(_parse_phase_kv(args.phase0_status))
    p1.update(_parse_phase_kv(args.phase1_status))

    reason_err = check_reason_required(p0, PHASE0_KEYS, "phase0") or check_reason_required(
        p1, PHASE1_KEYS, "phase1"
    )
    if reason_err:
        sys.stderr.write(reason_err + "\n")
        return 1

    # retrieval-pull (L1): the file REPLACES the flags (file wins) — warn if both.
    backref_file = _load_backref_file(
        Path(args.predecessor_lesson_backref_file)
        if args.predecessor_lesson_backref_file
        else None
    )
    if backref_file is not None:
        if args.predecessor_lesson_backref:
            sys.stderr.write(
                "WARN backref-file-wins: both --predecessor-lesson-backref and "
                "--predecessor-lesson-backref-file given; the file replaces the flags\n"
            )
        backref_raw: list[dict] | None = backref_file
    else:
        backref_raw = parse_backref_kv(args.predecessor_lesson_backref) or None
    # Validate now so a malformed input is a clean nonzero exit (not a traceback)
    # BEFORE we acquire the lock or write anything.
    if backref_raw:
        try:
            _validate_backref(backref_raw)
        except ValueError as e:
            sys.stderr.write(f"ERR-FATAL backref-invalid: {e}\n")
            return 1

    # component 5 (L2): parse + validate now so a malformed value is a clean nonzero
    # exit BEFORE the lock / any artifact is written (mirrors the backref pre-check).
    lesson_disp_raw = parse_lesson_disposition(args.lesson_disposition)
    if lesson_disp_raw:
        try:
            _validate_lesson_disposition(lesson_disp_raw)
        except ValueError as e:
            sys.stderr.write(f"ERR-FATAL lesson-disposition-invalid: {e}\n")
            return 1

    # closeout_obligations (third vector / warn-mode): parse with the SAME key=status[:reason]
    # grammar the phase flags use, then validate now so a malformed value is a clean nonzero
    # exit BEFORE the lock / any artifact is written (mirrors the backref / lesson pre-check).
    closeout_raw = _parse_phase_kv(args.closeout_status) or None
    if closeout_raw:
        try:
            _validate_closeout(closeout_raw)
        except ValueError as e:
            sys.stderr.write(f"ERR-FATAL closeout-status-invalid: {e}\n")
            return 1

    output = (
        Path(args.output)
        if args.output
        else precheck_dir(project) / f"{args.task}.retro.evidence.json"
    )

    def _do_build_and_write() -> int:
        payload = build_evidence(
            task_id=args.task,
            project=project,
            workspace=workspace,
            mode=args.mode,
            nonce=args.nonce,
            phase0=p0,
            phase1=p1,
            predecessor_lesson_backref=backref_raw,
            lesson_disposition=lesson_disp_raw,
            closeout_obligations=closeout_raw,
        )
        write_evidence(payload, output)
        sys.stdout.write(f"OK precheck-written: {output}\n")
        return 0

    if args.no_lock:
        return _do_build_and_write()

    lock_path = locks_dir(project) / "precheck.lock"
    try:
        with atomic.acquire_dir_lock(lock_path, stale_seconds=300, retries=1, wait_seconds=0.0):
            return _do_build_and_write()
    except atomic.LockAcquisitionError:
        sys.stderr.write(f"ERR-LOCKED precheck-lock-held: {lock_path}\n")
        return 3


if __name__ == "__main__":
    sys.exit(main())
