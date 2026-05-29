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
import sys
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


def derive_verdict(findings: dict) -> str:
    """``"pass"`` iff no original finding is P0/P1, else ``"fail"`` (spec §3.1).

    The verdict is *derived*, never trusted from the AI: a run is clean only
    when codex surfaced no blocking-severity finding.
    """
    blocking = {"P0", "P1"}
    for f in findings.get("original_findings", []) or []:
        if isinstance(f, dict) and f.get("severity") in blocking:
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


def build_codex_audit_block(
    audit_mode: str,
    *,
    audit_runs: list[dict] | None = None,
    dispositions: list[dict] | None = None,
    attestation: dict | None = None,
    bypass: dict | None = None,
) -> dict:
    """Assemble the mode-specific ``codex_audit`` block embedded in evidence.

    Each of the four modes has its own schema (R2-P1-1); this builder validates
    that the caller supplied the pieces that mode requires and shapes the block
    accordingly. It does NOT decide the mode (that is the gate's machine
    ruling via ``git diff`` in Phase B) — the caller passes the chosen mode.
    """
    if audit_mode not in _pc.AUDIT_MODES:
        raise ValueError(f"audit_mode must be one of {list(_pc.AUDIT_MODES)}; got {audit_mode!r}")

    if audit_mode in (_pc.AUDIT_MODE_FULL, _pc.AUDIT_MODE_DOCS_ONLY):
        if not audit_runs:
            raise ValueError(f"{audit_mode} requires a non-empty audit_runs list")
        return {
            "audit_mode": audit_mode,
            "audit_runs": list(audit_runs),
            "dispositions": list(dispositions or []),
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
        }

    # AUDIT_MODE_BYPASS
    if not bypass or not bypass.get("codex_failure_attempts"):
        raise ValueError(
            "codex_unavailable_bypass requires bypass.codex_failure_attempts (machine "
            "proof of >=N codex failures)"
        )
    follow = bypass.get("follow_up_audit_task_id")
    if not follow or not _pc.TASK_ID_RE.match(follow):
        raise ValueError(
            "codex_unavailable_bypass requires follow_up_audit_task_id as a slug [a-z0-9-]"
        )
    # R2 P1: the failure proof must be machine-verifiable, not free-form — each
    # attempt needs an exit code, a hashed stderr, and a timestamp.
    attempts = bypass["codex_failure_attempts"]
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("codex_failure_attempts must be a non-empty list")
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

    input_commit = args.input_commit or findings.get("input_commit")
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
            try:
                block = build_codex_audit_block(
                    args.audit_mode,
                    audit_runs=runs,
                    dispositions=dispositions,
                    attestation=attestation,
                    bypass=bypass,
                )
            except ValueError as e:
                sys.stderr.write(f"ERR-FATAL codex-audit-block-invalid: {e}\n")
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
