"""v5.4 retro-evidence precheck: collect Phase 0 / Phase 1 state into a JSON
artifact that the next dump-handoff invocation gates on.

This module has two surfaces:

  * A library API (used by tests and by the dump gate to compute hashes).
  * A CLI (`handoff-precheck`) that the AI invokes after closing a task to
    emit a `precheck/<task>.retro.evidence.json` file.

Spec source of truth: ``v5.4-retro-mandate-draft.md §7.5 / §7.6 / §7.8``.

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
# Phase A introduces the *evidence capability* only (mandate OFF): these
# constants pin the four audit modes, the convergence bounds, and the
# disposition vocabulary that the Phase B retro_gate (G0-G9) will enforce.

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
