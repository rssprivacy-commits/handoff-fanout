"""v5.4 retro-evidence gate.

dump-handoff.py asks this module ``check_retro_gate(...)``; the result drives
the seven-tier exit code protocol from spec §7.1 and triggers any
side-effects mandated by §7.2-§7.7 (counter bump, BLOCKED artifact,
warnings.txt, audit jsonl).

The module never writes the queue/ack file the dump itself produces; that
remains dump.py's job. Here we only touch:

  * ``locks/precheck.lock`` and ``locks/dump.lock``  (§7.3)
  * ``locks/<task>.retro.attempt.lock``              (§7.3)
  * ``ack/<task>.retro.attempt_n.txt``               (§7.2)
  * ``ack/<task>.retro.retry_audit.jsonl``           (§7.2 / §7.3)
  * ``ack/<task>.retro.warnings.txt``                (§7.7)
  * ``queue/<task>.BLOCKED.md``                      (§7.4 / §7.2)

Any new artifact added here must also be documented in §7 of the spec.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from handoff_fanout import atomic
from handoff_fanout import config as _config
from handoff_fanout.handoff_precheck import (
    EVIDENCE_KIND_RETRO,
    MODE_FORENSIC_RETRO,
    MODE_VALID,
    PHASE0_KEYS,
    PHASE1_KEYS,
    PHASE_STATUS_VALID,
    STATUS_REQUIRING_REASON,
    SUPPORTED_EVIDENCE_SCHEMA_VERSIONS,
    build_evidence,
    compute_evidence_hash,
)

# 1-B: stale-class subcodes that an in-process re-align can rescue (the HEAD
# moved due to a sibling commit while this session's own work is intact).
REALIGNABLE_SUBCODES = frozenset({"head-stale-resubmit", "head-stale-fatal"})
REALIGN_MAX_TRIES = 3

# ─── exit codes (§7.1) ──────────────────────────────────────────────────────

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_BLOCKED = 2
EXIT_LOCKED = 3
EXIT_RETRY = 4
EXIT_BYPASS = 6  # exit 5 is intentionally unassigned per §7.1

PREFIX_OK = "OK"
PREFIX_FATAL = "ERR-FATAL"
PREFIX_BLOCKED = "ERR-BLOCKED"
PREFIX_LOCKED = "ERR-LOCKED"
PREFIX_RETRY = "ERR-RETRY"
PREFIX_BYPASS = "ERR-BYPASS"

# ─── defaults from §7.7 ─────────────────────────────────────────────────────

DEFAULT_HEAD_FRESHNESS = {
    "last_commit_max_age_sec": 300,
    "head_at_precheck_drift_tolerance_sec": 30,
    # 1-A: when HEAD still matches the evidence snapshot, evidence freshness is
    # bounded by how old the snapshot is (drift), NOT by how long ago the last
    # commit landed. A session legitimately commits then spends minutes on
    # memory/codex audit before dump; that must not make valid evidence "stale".
    "evidence_max_age_sec": 1800,
    "head_stale_action": "retry",  # retry | block | warn-ok
}
HEAD_STALE_ACTIONS = {"retry", "block", "warn-ok"}

DEFAULT_FOLLOW_UP = {
    "default_deadline_minutes": 30,
    "project_block_on_overdue": True,
    "scan_interval_sec": 60,
}

# attempt_n state machine (§7.2)
ATTEMPT_MAX = 2
PRECHECK_LOCK_STALE_SEC = 300.0
DUMP_LOCK_STALE_SEC = 300.0
ATTEMPT_LOCK_STALE_SEC = 60.0

NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


# ─── result type ────────────────────────────────────────────────────────────


@dataclass
class GateResult:
    """Single source of truth for a gate outcome.

    ``exit_code`` ↔ ``prefix`` mapping comes from §7.1 — never construct one
    without the other. ``subcode`` is the machine-parseable token after the
    prefix (``retro-missing-phase0-memory`` etc.); ``message`` is the
    human-readable tail.
    """

    exit_code: int
    prefix: str
    subcode: str = ""
    message: str = ""

    @property
    def is_ok(self) -> bool:
        return self.exit_code == EXIT_OK

    def emit(self) -> None:
        """Write the stderr prefix line per §7.1 contract."""
        if self.exit_code == EXIT_OK and not self.subcode:
            return
        head = f"{self.prefix}"
        if self.subcode:
            head += f" {self.subcode}"
        if self.message:
            head += f": {self.message}"
        sys.stderr.write(head + "\n")


def _ok() -> GateResult:
    return GateResult(EXIT_OK, PREFIX_OK)


def _retry(subcode: str, msg: str = "") -> GateResult:
    return GateResult(EXIT_RETRY, PREFIX_RETRY, subcode, msg)


def _blocked(subcode: str, msg: str = "") -> GateResult:
    return GateResult(EXIT_BLOCKED, PREFIX_BLOCKED, subcode, msg)


def _locked(subcode: str, msg: str = "") -> GateResult:
    return GateResult(EXIT_LOCKED, PREFIX_LOCKED, subcode, msg)


def _bypass(subcode: str, msg: str = "") -> GateResult:
    return GateResult(EXIT_BYPASS, PREFIX_BYPASS, subcode, msg)


def _fatal(subcode: str, msg: str = "") -> GateResult:
    return GateResult(EXIT_FATAL, PREFIX_FATAL, subcode, msg)


# ─── path helpers ───────────────────────────────────────────────────────────


def _home() -> Path:
    return _config.home_dir()


def _ack_dir(project: str) -> Path:
    return _home() / project / "ack"


def _locks_dir(project: str) -> Path:
    return _home() / project / "locks"


def _queue_dir(project: str) -> Path:
    return _home() / project / "queue"


def _config_path(project: str) -> Path:
    return _home() / project / "handoff.config.json"


def _attempt_path(project: str, task: str) -> Path:
    return _ack_dir(project) / f"{task}.retro.attempt_n.txt"


def _audit_attempt_path(project: str, task: str) -> Path:
    """Counter for audit-gate retries, ISOLATED from the retro attempt counter
    (spec: audit_attempt_n隔离). An audit RETRY must not consume a retro retry
    and vice versa — they are independent failure budgets."""
    return _ack_dir(project) / f"{task}.audit.attempt_n.txt"


def _audit_path(project: str, task: str) -> Path:
    return _ack_dir(project) / f"{task}.retro.retry_audit.jsonl"


def _warnings_path(project: str, task: str) -> Path:
    return _ack_dir(project) / f"{task}.retro.warnings.txt"


def _override_path(project: str, task: str) -> Path:
    return _ack_dir(project) / f"{task}.retro.override.json"


def _blocked_md_path(project: str, task: str) -> Path:
    return _queue_dir(project) / f"{task}.BLOCKED.md"


# ─── small utils ────────────────────────────────────────────────────────────


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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


# ─── config loader (§7.7) ───────────────────────────────────────────────────


def load_project_config(project: str) -> dict:
    """Return merged ``{head_freshness, follow_up}`` dict.

    Missing / malformed file → defaults. Unknown ``head_stale_action`` value
    is coerced to ``retry`` (the safe default) with a warning printed once.
    """
    out = {
        "head_freshness": dict(DEFAULT_HEAD_FRESHNESS),
        "follow_up": dict(DEFAULT_FOLLOW_UP),
    }
    path = _config_path(project)
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"# handoff.config.json malformed ({e}); using defaults\n")
        return out
    for section in ("head_freshness", "follow_up"):
        if isinstance(data.get(section), dict):
            out[section].update(data[section])
    if out["head_freshness"].get("head_stale_action") not in HEAD_STALE_ACTIONS:
        out["head_freshness"]["head_stale_action"] = "retry"
    return out


# ─── audit + counter (§7.2) ─────────────────────────────────────────────────


def _audit_append(project: str, task: str, record: dict) -> None:
    """Append a single JSON line to the per-task retry audit trail."""
    p = _audit_path(project, task)
    p.parent.mkdir(parents=True, exist_ok=True)
    record.setdefault("timestamp", _iso_now())
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line)


def write_mandate_drift_sentinel(
    project: str,
    task: str,
    *,
    workspace: Path | str,
    classification: str,
    retro_mandate: bool,
    audit_mandate: bool,
    mandate_projects: list[str] | None = None,
) -> None:
    """§F#9 silent-downgrade guard (owner ruling: policy B — WARN, never fail-closed).

    A project listed in ``config.json:mandate_projects`` is configured to expect the
    GLOBAL env mandate ON, but the env that activates it is missing — so the governance
    gate would silently fall to the legacy / no-G0-G9 path for a high-blast-radius
    project. ``classification`` is ``"total_missing"`` (BOTH mandates gone on a
    no-evidence dump → silent legacy, detected in ``dump._run_retro_gate``) or
    ``"partial_missing"`` (the AUDIT mandate dropped while the RETRO mandate persisted
    → an evidence-bearing close silently skips the G0-G9 audit, detected at the audit
    gate below).

    Policy B is deliberately NON-fatal (a fail-closed reject would break the
    ``config.py`` documented "unset env mandate to disable enforcement" escape hatch and
    could brick the listed project). We WARN loudly to stderr + leave a durable,
    SELF-OVERWRITING per-task sentinel (no unbounded accumulation), then let the existing
    flow continue. A sentinel write failure must NEVER block the dump (a drift guard must
    not become a new failure mode) — but it emits its own stderr WARN so a missing
    sentinel is itself visible.
    """
    projects = list(mandate_projects) if mandate_projects else []
    where = f" (mandate_projects={projects})" if projects else ""
    print(
        f"⚠️  MANDATE-DRIFT [{classification}]: project={project!r} expects the env "
        f"mandate ON{where} but it is missing "
        f"(HANDOFF_RETRO_MANDATE={retro_mandate}, HANDOFF_AUDIT_MANDATE={audit_mandate}) "
        f"— the governance gate is silently degraded for this project. Restore the env "
        f"mandate (.zshenv / launchctl setenv / auto-continue.plist) or, to intentionally "
        f"disable enforcement, clear mandate_projects in config.json.",
        file=sys.stderr,
    )
    try:
        ack = _ack_dir(project)
        ack.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1",
            "kind": "mandate_drift",
            "classification": classification,
            "project": project,
            "task": task,
            "workspace": str(workspace),
            "retro_mandate": retro_mandate,
            "audit_mandate": audit_mandate,
            "mandate_projects": projects,
            "detected_at": _iso_now(),
        }
        atomic.atomic_replace(ack / f"{task}.mandate_drift.json", json.dumps(payload, indent=2))
    except OSError as e:  # never block the dump on a guard's own write failure
        print(f"⚠️  MANDATE-DRIFT sentinel write failed for {project}/{task}: {e}", file=sys.stderr)


def _read_counter(p: Path) -> tuple[int | None, str]:
    """Return ``(value, raw)`` for an attempt-counter file at ``p``.

    ``value`` is ``None`` when the file is missing or empty (treat as 0).
    Multi-line files have all but the first stripped; non-numeric / >2
    values are surfaced to the caller as ``value=-1`` so they can emit a
    corruption-class BLOCKED result without re-reading. Path-parametric so the
    retro counter and the isolated audit counter share one implementation.
    """
    if not p.exists():
        return None, ""
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return -1, "<read-failed>"
    body = raw.strip()
    if not body:
        with contextlib.suppress(OSError):
            p.unlink()
        return None, raw
    first = body.splitlines()[0].strip()
    if first not in {"0", "1", "2"}:
        return -1, raw
    return int(first), raw


def _write_counter_atomic(p: Path, n: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f"{p.name}.tmp.{os.getpid()}.{time.monotonic_ns()}"
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.write(fd, f"{n}\n".encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, p)


def _quarantine_corrupt(p: Path, raw: str) -> Path:
    """Rename a corrupt counter file out of the way per §7.2.

    Returns the destination path so callers can include it in audit entries.
    """
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        dst = p.parent / f"{p.name}.corrupt-{int(time.time())}"
        dst.write_text(raw, encoding="utf-8")
        return dst
    dst = p.parent / f"{p.name}.corrupt-{int(time.time())}"
    try:
        os.rename(p, dst)
    except OSError:
        dst.write_text(raw, encoding="utf-8")
        with contextlib.suppress(OSError):
            p.unlink()
    return dst


def _read_attempt_n(project: str, task: str) -> tuple[int | None, str]:
    return _read_counter(_attempt_path(project, task))


def _write_attempt_n_atomic(project: str, task: str, n: int) -> None:
    _write_counter_atomic(_attempt_path(project, task), n)


def _quarantine_corrupt_counter(project: str, task: str, raw: str) -> Path:
    return _quarantine_corrupt(_attempt_path(project, task), raw)


def _clear_attempt_on_success(project: str, task: str, evidence_hash: str, sid: str) -> None:
    p = _attempt_path(project, task)
    if p.exists():
        attempt_n_at_success, _ = _read_attempt_n(project, task)
        with contextlib.suppress(OSError):
            p.unlink()
    else:
        attempt_n_at_success = 0
    _audit_append(
        project,
        task,
        {
            "event": "success",
            "attempt_n_at_success": attempt_n_at_success if attempt_n_at_success is not None else 0,
            "evidence_hash": evidence_hash,
            "tab_session_id": sid,
        },
    )


# ─── BLOCKED.md (§7.4) ──────────────────────────────────────────────────────


def _write_blocked_md(
    *,
    project: str,
    task: str,
    subcode: str,
    attempt_n: int,
    evidence_path: Path | None,
    evidence_hash: str | None,
    head: str,
    session_id: str,
    reason: str,
) -> Path:
    """Append-write a BLOCKED.md artifact; idempotent across multiple blocks."""
    target = _blocked_md_path(project, task)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    ev_path = str(evidence_path) if evidence_path else "(none)"
    ev_hash = evidence_hash if evidence_hash else "N/A"
    sep = f"\n## 二次 block {_iso_now()}\n\n" if existed else ""
    body = (
        f"{sep}# BLOCKED — {task}\n\n"
        f"**status**: blocked\n"
        f"**blocked_at**: {_iso_now()}\n"
        f"**blocked_by**: {subcode}\n"
        f"**attempt_n**: {attempt_n}\n"
        f"**evidence_path**: {ev_path}\n"
        f"**evidence_hash**: {ev_hash}\n"
        f"**HEAD_at_block**: {head}\n"
        f"**tab_session_id**: {session_id}\n\n"
        f"## 阻塞原因 (人类可读)\n\n{reason}\n\n"
        "## 主人裁决路径\n\n"
        "1. 手动审 evidence (上方 `evidence_path` 指向的 JSON)\n"
        f"2. 若 AI 应继续 → `rm {target}` + `touch {_override_path(project, task)}`\n"
        f"3. 若 task 应彻底放弃 → `touch {_queue_dir(project) / f'{task}.done'}`\n\n"
        "## audit trail\n\n"
        f"见 `{_audit_path(project, task)}` 完整历史 (含 attempt 0/1/2 三次失败原因).\n"
    )
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(body)
    return target


# ─── warnings sink (§7.7) ───────────────────────────────────────────────────


def _append_warning(project: str, task: str, line: str) -> None:
    p = _warnings_path(project, task)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip() + "\n")


# ─── evidence loading + validation ──────────────────────────────────────────


def _load_evidence(path: Path) -> tuple[dict | None, GateResult | None]:
    """Read + sanity-check an evidence JSON file.

    Returns ``(payload, None)`` on success or ``(None, result)`` when the
    file can't be loaded or fails its self-hash. Schema-version mismatches
    and structural errors are mapped to ``ERR-RETRY`` (fatal-class — won't
    bump the counter) per §7.5.
    """
    if not path.exists():
        return None, _retry("evidence-missing", f"evidence not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        return None, _retry("evidence-corrupt", f"evidence JSON invalid: {e}")
    if not isinstance(payload, dict):
        return None, _retry("evidence-corrupt", "evidence must be a JSON object")
    # Accept any version in the supported set so an in-flight v5.4.1 evidence
    # written by the previous release still passes during the 5.5.0 migration
    # window (fail-open, mandate OFF — spec §2.5 / R2-P1-5). Truly unknown
    # versions remain a fatal-class RETRY.
    if payload.get("schema_version") not in SUPPORTED_EVIDENCE_SCHEMA_VERSIONS:
        return None, _retry(
            "schema-version-unknown",
            f"got {payload.get('schema_version')!r}, "
            f"expected one of {list(SUPPORTED_EVIDENCE_SCHEMA_VERSIONS)}",
        )
    if payload.get("evidence_kind") != EVIDENCE_KIND_RETRO:
        return None, _retry(
            "evidence-kind-mismatch",
            f"expected retro, got {payload.get('evidence_kind')!r}",
        )
    declared = payload.get("evidence_hash")
    if not isinstance(declared, str) or len(declared) != 64:
        return None, _retry("evidence-hash-missing", "evidence_hash field absent or malformed")
    recomputed = compute_evidence_hash(payload)
    if recomputed != declared:
        return None, _retry(
            "evidence-hash-mismatch",
            "canonical hash does not match declared evidence_hash",
        )
    return payload, None


def _validate_phase_status(payload: dict) -> GateResult | None:
    """Check every required phase status is one of ✅/⚠️/❌/skip.

    Missing item, missing ``status`` field, or unknown enum value → fail.
    """
    for section, keys in (("phase0", PHASE0_KEYS), ("phase1", PHASE1_KEYS)):
        items = payload.get(section)
        if not isinstance(items, dict):
            return _retry(
                f"{section}-missing",
                f"{section} section absent or not an object",
            )
        for k in keys:
            entry = items.get(k)
            if not isinstance(entry, dict):
                return _retry(
                    f"{section}-item-missing",
                    f"{section}.{k} missing or not an object",
                )
            status = entry.get("status")
            if status is None:
                return _retry(
                    f"{section}-status-missing",
                    f"{section}.{k}.status missing",
                )
            if status not in PHASE_STATUS_VALID:
                return _retry(
                    f"{section}-status-invalid",
                    f"{section}.{k}.status={status!r} not in {sorted(PHASE_STATUS_VALID)}",
                )
            reason = entry.get("reason")
            if status in STATUS_REQUIRING_REASON and (
                not isinstance(reason, str) or not reason.strip()
            ):
                return _retry(
                    f"{section}-status-missing-reason",
                    f"{section}.{k} status={status} requires a non-empty reason (only ✅ may omit it)",
                )
    return None


# ─── HEAD freshness (§7.7) ──────────────────────────────────────────────────


def _last_commit_age_sec(workspace: Path) -> int:
    iso = _git(["log", "-1", "--format=%cI"], workspace)
    if not iso:
        return -1
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return -1
    return int((datetime.now(ts.tzinfo) - ts).total_seconds())


def _precheck_drift_sec(payload: dict) -> int:
    """Seconds between ``head_at_precheck_timestamp`` and now.

    Returns ``-1`` when the timestamp is missing or unparseable so callers
    can route to the strict failure path.
    """
    ts_raw = payload.get("head_at_precheck_timestamp", "")
    if not isinstance(ts_raw, str) or not ts_raw:
        return -1
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return -1
    return int((datetime.now(ts.tzinfo) - ts).total_seconds())


def _check_head_freshness(
    payload: dict,
    workspace: Path,
    cfg: dict,
    project: str,
    task: str,
) -> tuple[GateResult | None, list[str]]:
    """Three-tier HEAD freshness gate.

    Returns ``(result_or_None, warnings_to_record)``. A ``None`` result means
    the gate passed (the dump can proceed); warnings are still appended
    to ``retro.warnings.txt`` for human audit.

    "Drift" is the wall-clock distance between ``head_at_precheck_timestamp``
    (when precheck snapshotted HEAD) and now — i.e. how stale the evidence
    is regardless of whether more commits have landed since.
    """
    head_now = _git(["rev-parse", "HEAD"], workspace)
    head_evidence = payload.get("head_at_precheck", "")
    last_age = _last_commit_age_sec(workspace)
    drift = _precheck_drift_sec(payload)

    max_age = int(cfg["head_freshness"]["last_commit_max_age_sec"])
    drift_tolerance = int(cfg["head_freshness"]["head_at_precheck_drift_tolerance_sec"])
    evidence_max_age = int(cfg["head_freshness"].get("evidence_max_age_sec", 1800))
    action = cfg["head_freshness"]["head_stale_action"]

    warnings: list[str] = []

    if not head_now:
        return _retry("head-unknown", "git rev-parse HEAD failed"), warnings
    if drift == -1:
        return _retry(
            "head-timestamp-invalid",
            "head_at_precheck_timestamp missing or unparseable",
        ), warnings
    if drift < 0:
        # Future precheck timestamp (clock skew / tampering). A negative drift
        # would otherwise sail through every `drift <= ...` comparison below.
        return _retry(
            "head-timestamp-future",
            f"head_at_precheck_timestamp is {abs(drift)}s in the future",
        ), warnings

    head_matches = head_now == head_evidence

    # 1-A: HEAD unchanged since precheck ⟹ no commit moved it ⟹ the evidence
    # reflects the current repo state. Gate on the evidence snapshot age
    # (drift) rather than last-commit recency, so a session that committed then
    # spent >max_age on memory/audit is not falsely flagged stale.
    if head_matches and drift <= evidence_max_age:
        if last_age > max_age:
            warnings.append(
                f"head-matches-old-commit-ok: {_iso_now()} "
                f"last_commit_age={last_age}s drift={drift}s head={head_now}"
            )
        return None, warnings

    if (not head_matches) and drift <= drift_tolerance:
        warnings.append(
            f"head-drift-within-tolerance: {_iso_now()} drift={drift}s "
            f"head_now={head_now} head_evidence={head_evidence}"
        )
        return None, warnings

    msg = (
        f"head_now={head_now} head_evidence={head_evidence} "
        f"last_commit_age={last_age}s max_age={max_age}s drift={drift}s"
    )
    if action == "warn-ok":
        warnings.append(f"head-stale-warn-ok: {msg}")
        return None, warnings
    if action == "block":
        return _blocked("head-stale-fatal", msg), warnings
    return _retry("head-stale-resubmit", msg), warnings


# ─── bypass + follow-up gates ───────────────────────────────────────────────


def _validate_override(path: Path) -> tuple[dict | None, GateResult | None]:
    if not path.exists():
        return None, _bypass(
            "missing-override",
            f"bypass requires {path} with follow_up_retro_task_id + follow_up_deadline",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, _bypass("override-corrupt", f"{path}: {e}")
    if not isinstance(data, dict):
        return None, _bypass("override-corrupt", f"{path}: not a JSON object")
    follow_task = data.get("follow_up_retro_task_id")
    follow_deadline = data.get("follow_up_deadline")
    if not follow_task or not isinstance(follow_task, str):
        return None, _bypass(
            "missing-follow-up-task",
            "override JSON missing follow_up_retro_task_id",
        )
    # P0: follow_up_retro_task_id is interpolated into a precheck/<task> evidence
    # path by the shell overdue scanner. Restrict to [a-z0-9-] (same charset the
    # shell guard enforces) so a crafted "../foreign" can never resolve an
    # out-of-tree file there.
    if not re.fullmatch(r"[a-z0-9-]+", follow_task):
        return None, _bypass(
            "invalid-follow-up-task",
            f"follow_up_retro_task_id has illegal chars (need [a-z0-9-]): {follow_task!r}",
        )
    if not follow_deadline or not isinstance(follow_deadline, str):
        return None, _bypass(
            "missing-follow-up-deadline",
            "override JSON missing follow_up_deadline",
        )
    try:
        datetime.fromisoformat(follow_deadline.replace("Z", "+00:00"))
    except ValueError:
        return None, _bypass(
            "invalid-follow-up-deadline",
            f"follow_up_deadline not ISO-8601: {follow_deadline!r}",
        )
    return data, None


def _check_follow_up_overdue(project: str, cfg: dict) -> GateResult | None:
    """If any task in this project has an overdue follow-up retro marker, block.

    Spec §7.9: project-scoped block; cross-project dumps are unaffected.
    """
    if not cfg["follow_up"].get("project_block_on_overdue", True):
        return None
    ack = _ack_dir(project)
    if not ack.exists():
        return None
    # Both follow-up debts block the project: the v5.4 retro overdue
    # (*.retro_overdue.txt) and the Phase C codex-audit bypass overdue
    # (*.audit_overdue.txt). The shell overdue scanner writes both via the same
    # machinery (auto-continue.sh scan_overdue_kind); the gate must read both or
    # the codex-audit marker would be write-only (R1 P1). The bypass-override
    # producer IS wired (codex_audit.write_bypass_override → ack/<task>.audit.
    # override.json); this path activates when an audit-close uses
    # codex_unavailable_bypass mode. Currently no such markers exist on disk —
    # the open codex re-audit debts use the separate PUSH gate
    # (audits/bypasses/*.json), which this scanner does not read.
    for pattern in ("*.retro_overdue.txt", "*.audit_overdue.txt"):
        for marker in ack.glob(pattern):
            kind = "codex-audit" if marker.name.endswith(".audit_overdue.txt") else "retro"
            return _bypass(
                "follow-up-overdue",
                f"another task has overdue {kind} follow-up: {marker.name}; resolve before dumping",
            )
    return None


# ─── locks (§7.3) ───────────────────────────────────────────────────────────


@contextlib.contextmanager
def _ordered_locks(project: str, task: str):
    """Acquire precheck.lock → dump.lock in the required §7.3 order.

    The retro.attempt.lock is held briefly per-counter-write inside the
    state-machine code, not over the full critical section.
    """
    lock_root = _locks_dir(project)
    lock_root.mkdir(parents=True, exist_ok=True)
    precheck = lock_root / "precheck.lock"
    dump = lock_root / "dump.lock"
    try:
        with atomic.acquire_dir_lock(
            precheck, stale_seconds=PRECHECK_LOCK_STALE_SEC, retries=1, wait_seconds=0.0
        ):
            try:
                with atomic.acquire_dir_lock(
                    dump, stale_seconds=DUMP_LOCK_STALE_SEC, retries=1, wait_seconds=0.0
                ):
                    yield
            except atomic.LockAcquisitionError as e:
                raise _LockHeld("dump-lock-held", precheck=False, dump=True) from e
    except atomic.LockAcquisitionError as e:
        if isinstance(e.__cause__, _LockHeld):
            raise
        raise _LockHeld("precheck-lock-held", precheck=True, dump=False) from e


class _LockHeld(Exception):
    """Internal signal: a §7.3 lock was held by a sibling tab."""

    def __init__(self, subcode: str, *, precheck: bool, dump: bool):
        super().__init__(subcode)
        self.subcode = subcode
        self.precheck = precheck
        self.dump = dump


@contextlib.contextmanager
def _attempt_lock(project: str, task: str, *, kind: str = "retro"):
    """Short-lived lock around an attempt_n.txt read-modify-write window.

    ``kind`` selects the lock file (``retro`` vs ``audit``) so the two isolated
    counters serialize independently — an audit retry never blocks on the retro
    attempt lock.
    """
    lock_root = _locks_dir(project)
    lock_root.mkdir(parents=True, exist_ok=True)
    lock = lock_root / f"{task}.{kind}.attempt.lock"
    try:
        with atomic.acquire_dir_lock(
            lock, stale_seconds=ATTEMPT_LOCK_STALE_SEC, retries=1, wait_seconds=0.0
        ):
            yield
    except atomic.LockAcquisitionError as e:
        raise _LockHeld(f"{kind}-attempt-lock-held", precheck=False, dump=False) from e


# ─── failure routing (counter bump + BLOCKED) ───────────────────────────────


# Subcodes whose retry would not change the outcome — they bypass the counter.
FATAL_CLASS_SUBCODES = frozenset(
    {
        "evidence-hash-mismatch",
        "evidence-hash-missing",
        "evidence-corrupt",
        "schema-version-unknown",
        "evidence-kind-mismatch",
        "nonce-mismatch",
    }
)


def _is_fatal_class(subcode: str) -> bool:
    return subcode in FATAL_CLASS_SUBCODES


def _handle_validation_failure(
    *,
    project: str,
    task: str,
    failure: GateResult,
    payload: dict | None,
    evidence_path: Path | None,
    session_id: str,
    head: str,
) -> GateResult:
    """Apply the §7.2 counter logic to a soft-retry failure.

    Fatal-class failures (hash mismatch, schema unknown, nonce mismatch)
    short-circuit without touching the counter — retrying would not help,
    so leaving the counter steady avoids penalising legitimate manual fixes.

    Already-terminal failures (``EXIT_BLOCKED``, e.g. ``head-stale-fatal``
    when the project is configured with ``head_stale_action=block``) write
    a BLOCKED.md artifact per §7.4 but bypass the counter entirely — the
    block action is a deliberate hard stop, not a retry budget.
    """
    if failure.exit_code == EXIT_BLOCKED:
        _audit_append(
            project,
            task,
            {
                "event": "blocked-direct",
                "subcode": failure.subcode,
                "tab_session_id": session_id,
            },
        )
        current, _ = _read_attempt_n(project, task)
        attempt_for_log = current if isinstance(current, int) and current >= 0 else 0
        _write_blocked_md(
            project=project,
            task=task,
            subcode=failure.subcode,
            attempt_n=attempt_for_log,
            evidence_path=evidence_path,
            evidence_hash=(payload or {}).get("evidence_hash") if payload else None,
            head=head,
            session_id=session_id,
            reason=failure.message,
        )
        return failure

    if _is_fatal_class(failure.subcode):
        _audit_append(
            project,
            task,
            {
                "event": "fatal-class-failure",
                "subcode": failure.subcode,
                "tab_session_id": session_id,
            },
        )
        return failure

    try:
        with _attempt_lock(project, task):
            current, raw = _read_attempt_n(project, task)
            if current == -1:
                dst = _quarantine_corrupt_counter(project, task, raw)
                _audit_append(
                    project,
                    task,
                    {
                        "event": "counter-corrupt",
                        "subcode": "counter-corrupted",
                        "quarantine": str(dst),
                        "tab_session_id": session_id,
                    },
                )
                _write_blocked_md(
                    project=project,
                    task=task,
                    subcode="counter-corrupted",
                    attempt_n=-1,
                    evidence_path=evidence_path,
                    evidence_hash=(payload or {}).get("evidence_hash") if payload else None,
                    head=head,
                    session_id=session_id,
                    reason=(
                        f"attempt_n.txt 内容损坏 (非 0/1/2), 已 quarantine 到 {dst}; "
                        "请主人确认 task 是否应继续, 然后清除 BLOCKED.md."
                    ),
                )
                return _blocked("counter-corrupted", f"quarantined corrupt counter → {dst}")

            n = current if current is not None else 0
            if n >= ATTEMPT_MAX:
                _audit_append(
                    project,
                    task,
                    {
                        "event": "attempt-exhausted",
                        "subcode": failure.subcode,
                        "attempt_n": n,
                        "tab_session_id": session_id,
                    },
                )
                _write_blocked_md(
                    project=project,
                    task=task,
                    subcode="retro-attempt-exhausted",
                    attempt_n=n,
                    evidence_path=evidence_path,
                    evidence_hash=(payload or {}).get("evidence_hash") if payload else None,
                    head=head,
                    session_id=session_id,
                    reason=(
                        f"attempt_n=2 reached after 3 retries; original failure: "
                        f"{failure.subcode} — {failure.message}"
                    ),
                )
                return _blocked(
                    "retro-attempt-exhausted",
                    f"attempt_n={n} reached after 3 retries (last subcode={failure.subcode})",
                )
            new_n = n + 1
            _write_attempt_n_atomic(project, task, new_n)
            _audit_append(
                project,
                task,
                {
                    "event": "retry-allowed",
                    "subcode": failure.subcode,
                    "attempt_n_after": new_n,
                    "tab_session_id": session_id,
                },
            )
            return _retry(
                failure.subcode,
                f"{failure.message} (attempt_n={new_n}/{ATTEMPT_MAX})",
            )
    except _LockHeld as e:
        _audit_append(
            project,
            task,
            {
                "event": "lock-contention",
                "lock": "attempt.lock",
                "subcode": e.subcode,
                "tab_session_id": session_id,
            },
        )
        return _locked(e.subcode, "another tab is updating attempt counter")


# ─── audit gate failure routing (isolated audit_attempt_n) ──────────────────


def _clear_audit_attempt_on_success(project: str, task: str) -> None:
    """Drop the isolated audit counter once the audit gate passes."""
    p = _audit_attempt_path(project, task)
    if p.exists():
        with contextlib.suppress(OSError):
            p.unlink()


def _handle_audit_failure(
    *,
    project: str,
    task: str,
    outcome,
    payload: dict | None,
    evidence_path: Path | None,
    session_id: str,
    head: str,
) -> GateResult:
    """Map a non-OK :class:`codex_audit.AuditGateOutcome` to a GateResult.

    Mirrors :func:`_handle_validation_failure` but bumps the *isolated* audit
    counter (``ack/<task>.audit.attempt_n.txt``). Class routing:
      * ``fatal``  → ``ERR-FATAL`` (tamper; retry can't help, no counter touch)
      * ``bypass`` → ``ERR-BYPASS`` (codex-unavailable bypass lacked failure proof)
      * ``blocked``→ ``ERR-BLOCKED`` + BLOCKED.md (hard stop, owner decides)
      * ``retry``  → bump audit_attempt_n; at ATTEMPT_MAX → BLOCKED.md
    """
    ev_hash = (payload or {}).get("evidence_hash") if payload else None

    if outcome.klass == "fatal":
        _audit_append(
            project,
            task,
            {"event": "audit-fatal", "subcode": outcome.subcode, "tab_session_id": session_id},
        )
        return _fatal(outcome.subcode, outcome.message)

    if outcome.klass == "bypass":
        _audit_append(
            project,
            task,
            {
                "event": "audit-bypass-rejected",
                "subcode": outcome.subcode,
                "tab_session_id": session_id,
            },
        )
        return _bypass(outcome.subcode, outcome.message)

    if outcome.klass == "blocked":
        _audit_append(
            project,
            task,
            {"event": "audit-blocked", "subcode": outcome.subcode, "tab_session_id": session_id},
        )
        current, _ = _read_counter(_audit_attempt_path(project, task))
        attempt_for_log = current if isinstance(current, int) and current >= 0 else 0
        _write_blocked_md(
            project=project,
            task=task,
            subcode=outcome.subcode,
            attempt_n=attempt_for_log,
            evidence_path=evidence_path,
            evidence_hash=ev_hash,
            head=head,
            session_id=session_id,
            reason=outcome.message,
        )
        return _blocked(outcome.subcode, outcome.message)

    # retry-class → isolated audit counter
    apath = _audit_attempt_path(project, task)
    try:
        with _attempt_lock(project, task, kind="audit"):
            current, raw = _read_counter(apath)
            if current == -1:
                dst = _quarantine_corrupt(apath, raw)
                _audit_append(
                    project,
                    task,
                    {
                        "event": "audit-counter-corrupt",
                        "subcode": "audit-counter-corrupted",
                        "quarantine": str(dst),
                        "tab_session_id": session_id,
                    },
                )
                _write_blocked_md(
                    project=project,
                    task=task,
                    subcode="audit-counter-corrupted",
                    attempt_n=-1,
                    evidence_path=evidence_path,
                    evidence_hash=ev_hash,
                    head=head,
                    session_id=session_id,
                    reason=f"audit.attempt_n.txt 内容损坏, 已 quarantine 到 {dst}.",
                )
                return _blocked("audit-counter-corrupted", f"quarantined corrupt counter → {dst}")
            n = current if current is not None else 0
            if n >= ATTEMPT_MAX:
                _audit_append(
                    project,
                    task,
                    {
                        "event": "audit-attempt-exhausted",
                        "subcode": outcome.subcode,
                        "attempt_n": n,
                        "tab_session_id": session_id,
                    },
                )
                _write_blocked_md(
                    project=project,
                    task=task,
                    subcode="codex-audit-attempt-exhausted",
                    attempt_n=n,
                    evidence_path=evidence_path,
                    evidence_hash=ev_hash,
                    head=head,
                    session_id=session_id,
                    reason=(
                        f"audit gate failed 3 times; last failure: {outcome.subcode} — "
                        f"{outcome.message}"
                    ),
                )
                return _blocked(
                    "codex-audit-attempt-exhausted",
                    f"audit_attempt_n={n} after 3 retries (last={outcome.subcode})",
                )
            new_n = n + 1
            _write_counter_atomic(apath, new_n)
            _audit_append(
                project,
                task,
                {
                    "event": "audit-retry-allowed",
                    "subcode": outcome.subcode,
                    "attempt_n_after": new_n,
                    "tab_session_id": session_id,
                },
            )
            return _retry(
                outcome.subcode,
                f"{outcome.message} (audit_attempt_n={new_n}/{ATTEMPT_MAX})",
            )
    except _LockHeld as e:
        _audit_append(
            project,
            task,
            {
                "event": "lock-contention",
                "lock": "audit.attempt.lock",
                "subcode": e.subcode,
                "tab_session_id": session_id,
            },
        )
        return _locked(e.subcode, "another tab is updating the audit attempt counter")


# ─── 1-B: dump-time re-align ─────────────────────────────────────────────────


_SHA_RE = re.compile(r"[0-9a-f]{7,40}")


def _git_strict(args: list[str], workspace: Path) -> tuple[int, str]:
    """Like ``_git`` but returns ``(returncode, stdout)`` so callers can tell a
    genuinely-empty result (rc 0) from a git failure (rc != 0). Returns
    ``(-1, "")`` if git couldn't even be spawned."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return r.returncode, (r.stdout or "").strip()
    except (subprocess.SubprocessError, OSError):
        return -1, ""


def _is_ancestor(ancestor: str, descendant: str, workspace: Path) -> bool:
    try:
        r = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=str(workspace),
            capture_output=True,
            timeout=5,
            check=False,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _phase_overrides(payload: dict, section: str) -> dict | None:
    val = payload.get(section)
    return val if isinstance(val, dict) else None


def _attempt_realign(
    payload: dict,
    evidence_path: Path,
    workspace: Path,
    project: str,
    task: str,
) -> dict | None:
    """Re-bind stale evidence to the current HEAD when it only drifted because
    a sibling tab committed (1-B). Returns the rewritten payload on success, or
    ``None`` to fall through to the normal retry/attempt path.

    Safe only when ALL hold (fail-closed otherwise):
      * the evidence carries a ``session_commits`` snapshot (else we can't prove
        whose commits moved HEAD);
      * ``git rev-parse HEAD`` works (git healthy);
      * the working tree is clean (this session's work is fully committed, so
        the phase0/phase1 retro claims still hold);
      * every snapshotted session commit is still an ancestor of the new HEAD
        (HEAD moved *only* via siblings — our work was neither rewritten nor
        dropped; this also rejects the ABA reset-to-old-SHA case).

    The whole sequence runs inside the gate's already-held dump.lock; a CAS on
    HEAD before/after the in-process rebuild guards the residual window where a
    sibling commits mid-rebuild (bounded retry with jitter, never bumping
    attempt_n — machine correction, not an AI fix).
    """
    sess = payload.get("session_commits")
    if not isinstance(sess, list) or not sess:
        return None
    # Reject malformed evidence: every entry must look like a git SHA, and the
    # list must be bounded (a self-consistent-hash but crafted payload could
    # otherwise crash _is_ancestor or blow up runtime).
    if len(sess) > 1000 or not all(isinstance(c, str) and _SHA_RE.fullmatch(c) for c in sess):
        return None
    mode = payload.get("mode", "normal")
    if mode == MODE_FORENSIC_RETRO:
        return None  # forensic skips strict checks entirely; nothing to re-align

    old_head = payload.get("head_at_precheck", "")
    if not isinstance(old_head, str) or not _SHA_RE.fullmatch(old_head):
        return None

    for i in range(REALIGN_MAX_TRIES):
        head_now = _git(["rev-parse", "HEAD"], workspace)
        if not head_now:
            return None  # git broken → fail closed
        # Re-align only rescues a *genuine sibling move*: HEAD must have actually
        # advanced past the precheck HEAD. Same-HEAD staleness (drift-only) must
        # NOT be refreshed — that would revive arbitrarily old evidence and mask
        # an ABA reset-to-old-SHA. (codex P0-2)
        if head_now == old_head:
            return None
        if not _is_ancestor(old_head, head_now, workspace):
            return None  # old HEAD not an ancestor → history rewritten / ABA
        # Working tree must be clean — strict, fail-closed on any git error
        # (a plain "" from a failed `git status` must not read as clean). (P1-3)
        rc, status_out = _git_strict(["status", "--porcelain"], workspace)
        if rc != 0 or status_out != "":
            return None
        if not all(_is_ancestor(c, head_now, workspace) for c in sess):
            return None  # our commits not all ancestors → not a pure sibling move
        new_payload = build_evidence(
            task_id=task,
            project=payload.get("project", project),
            workspace=workspace,
            mode=mode,
            nonce=payload.get("nonce"),
            phase0=_phase_overrides(payload, "phase0"),
            phase1=_phase_overrides(payload, "phase1"),
        )
        # The owned commit set is fixed at precheck — preserve the snapshot
        # rather than re-deriving it (a sibling push could otherwise shrink it).
        new_payload["session_commits"] = list(sess)
        new_payload["session_commits_source"] = payload.get("session_commits_source", "")
        # Preserve the original session identity — re-align is a freshness
        # refresh, not a new session. (P1-5)
        for k in ("session_id", "session_id_kind"):
            if k in payload:
                new_payload[k] = payload[k]
        # Preserve the Phase A codex audit block verbatim — re-align refreshes
        # the HEAD binding, it does NOT re-audit, so the recorded findings /
        # dispositions must survive (else a sibling-HEAD move silently erases the
        # audit evidence). When Phase B turns the audit mandate on, the gate's
        # G0 (input_commit == HEAD) will force a re-audit if the refreshed HEAD
        # invalidates these runs; that is Phase B's job, not re-align's.
        if "codex_audit" in payload:
            new_payload["codex_audit"] = payload["codex_audit"]
        # Likewise preserve the retrieval-pull back-reference verbatim — re-align
        # refreshes the HEAD binding, it does NOT re-consume predecessor lessons, so
        # a sibling-HEAD move must not silently erase the recorded back-reference
        # (same rationale as codex_audit above). build_evidence above did not receive
        # it (the gate has no CLI to re-supply it), so copy it from the original.
        if "predecessor_lesson_backref" in payload:
            new_payload["predecessor_lesson_backref"] = payload["predecessor_lesson_backref"]
        # Likewise preserve the component-5 lesson_disposition verbatim — re-align
        # refreshes the HEAD binding only; the honest "did this hop produce a lesson"
        # record must not be erased by a sibling-HEAD move (build_evidence above did
        # not receive it, so copy it from the original — same rationale as backref).
        if "lesson_disposition" in payload:
            new_payload["lesson_disposition"] = payload["lesson_disposition"]
        # The in-process builder must have observed the same HEAD we validated;
        # if not (e.g. its rev-parse failed → "(unknown)"), abort this attempt.
        if new_payload.get("head_at_precheck") != head_now:
            time.sleep(0.2 + 0.1 * i)
            continue
        new_payload["evidence_hash"] = compute_evidence_hash(new_payload)
        head_after = _git(["rev-parse", "HEAD"], workspace)
        if head_after != head_now:
            time.sleep(0.2 + 0.1 * i)  # CAS fail: sibling committed mid-rebuild
            continue
        atomic.atomic_replace(
            evidence_path,
            json.dumps(new_payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        )
        return new_payload
    return None


# ─── public entry point ─────────────────────────────────────────────────────


def check_retro_gate(
    *,
    project: str,
    task: str,
    workspace: Path,
    evidence_path: Path | None,
    bypass_enabled: bool,
    mandate_enabled: bool,
    audit_mandate_enabled: bool = False,
    audit_mandate_expected: bool = False,
    nonce: str | None = None,
    config: dict | None = None,
    session_id: str = "",
) -> GateResult:
    """Run the v5.4 retro evidence gate.

    Returns a :class:`GateResult` whose ``exit_code`` matches §7.1. Callers
    are responsible for emitting the result (``GateResult.emit``) and for
    short-circuiting their own work when ``is_ok`` is false.
    """
    if config is None:
        config = load_project_config(project)
    sid = session_id or "(no-session-id)"

    _ack_dir(project).mkdir(parents=True, exist_ok=True)

    overdue = _check_follow_up_overdue(project, config)
    if overdue is not None:
        _audit_append(
            project,
            task,
            {"event": "follow-up-overdue-block", "tab_session_id": sid},
        )
        return overdue

    if bypass_enabled:
        override_data, override_err = _validate_override(_override_path(project, task))
        if override_err is not None:
            _audit_append(
                project,
                task,
                {
                    "event": "bypass-rejected",
                    "subcode": override_err.subcode,
                    "tab_session_id": sid,
                },
            )
            return override_err
        _audit_append(
            project,
            task,
            {
                "event": "bypass-accepted",
                "follow_up_task": override_data.get("follow_up_retro_task_id"),
                "follow_up_deadline": override_data.get("follow_up_deadline"),
                "tab_session_id": sid,
            },
        )
        return _ok()

    if evidence_path is None:
        if not mandate_enabled and not audit_mandate_enabled:
            return _ok()
        # Audit mandate takes precedence when set (with OR without retro mandate,
        # codex R2-3): route through the isolated audit counter so repeated
        # no-evidence dumps progress 0→1→2→BLOCKED rather than RETRY-looping. The
        # codex_audit block can only ride on a retro-evidence file, so a missing
        # one is an audit failure regardless of the retro mandate.
        if not audit_mandate_enabled:
            return _retry(
                "retro-evidence-missing",
                "no --retro-evidence and no HANDOFF_RETRO_BYPASS; supply evidence file",
            )
        from handoff_fanout import codex_audit

        return _handle_audit_failure(
            project=project,
            task=task,
            outcome=codex_audit.AuditGateOutcome(
                "retry",
                "codex-audit-required",
                "HANDOFF_AUDIT_MANDATE set but no --retro-evidence; supply evidence with codex_audit",
            ),
            payload=None,
            evidence_path=None,
            session_id=sid,
            head=_git(["rev-parse", "HEAD"], workspace),
        )

    try:
        with _ordered_locks(project, task):
            payload, load_err = _load_evidence(evidence_path)
            if load_err is not None:
                return _handle_validation_failure(
                    project=project,
                    task=task,
                    failure=load_err,
                    payload=None,
                    evidence_path=evidence_path,
                    session_id=sid,
                    head=_git(["rev-parse", "HEAD"], workspace),
                )

            if nonce is not None:
                if not NONCE_RE.match(nonce):
                    return _handle_validation_failure(
                        project=project,
                        task=task,
                        failure=_retry(
                            "nonce-invalid-format", "nonce fails ^[A-Za-z0-9_-]{1,128}$"
                        ),
                        payload=payload,
                        evidence_path=evidence_path,
                        session_id=sid,
                        head=payload.get("head_at_precheck", ""),
                    )
                if payload.get("nonce") != nonce:
                    return _handle_validation_failure(
                        project=project,
                        task=task,
                        failure=_retry(
                            "nonce-mismatch",
                            f"caller nonce={nonce!r} payload nonce={payload.get('nonce')!r}",
                        ),
                        payload=payload,
                        evidence_path=evidence_path,
                        session_id=sid,
                        head=payload.get("head_at_precheck", ""),
                    )

            mode = payload.get("mode", "normal")
            forensic = mode == MODE_FORENSIC_RETRO
            if mode not in MODE_VALID:
                return _handle_validation_failure(
                    project=project,
                    task=task,
                    failure=_retry("mode-invalid", f"mode={mode!r} not in {sorted(MODE_VALID)}"),
                    payload=payload,
                    evidence_path=evidence_path,
                    session_id=sid,
                    head=payload.get("head_at_precheck", ""),
                )

            if not forensic:
                phase_err = _validate_phase_status(payload)
                if phase_err is not None:
                    return _handle_validation_failure(
                        project=project,
                        task=task,
                        failure=phase_err,
                        payload=payload,
                        evidence_path=evidence_path,
                        session_id=sid,
                        head=payload.get("head_at_precheck", ""),
                    )

                head_err, warnings = _check_head_freshness(
                    payload, workspace, config, project, task
                )
                for w in warnings:
                    _append_warning(project, task, w)
                if head_err is not None:
                    # 1-B: a stale-class failure may be a pure sibling HEAD move.
                    # Try an in-process re-align (inside this held dump.lock,
                    # CAS-guarded) BEFORE counting it against attempt_n.
                    realigned = None
                    if head_err.subcode in REALIGNABLE_SUBCODES and evidence_path is not None:
                        realigned = _attempt_realign(
                            payload, evidence_path, workspace, project, task
                        )
                    if realigned is not None:
                        head_err2, warnings2 = _check_head_freshness(
                            realigned, workspace, config, project, task
                        )
                        for w in warnings2:
                            _append_warning(project, task, w)
                        if head_err2 is None:
                            _audit_append(
                                project,
                                task,
                                {
                                    "event": "head-realigned",
                                    "old_head": payload.get("head_at_precheck", ""),
                                    "new_head": realigned.get("head_at_precheck", ""),
                                    "tab_session_id": sid,
                                },
                            )
                            payload = realigned  # success path uses realigned hash
                            head_err = None
                        else:
                            head_err = head_err2
                    if head_err is not None:
                        return _handle_validation_failure(
                            project=project,
                            task=task,
                            failure=head_err,
                            payload=payload,
                            evidence_path=evidence_path,
                            session_id=sid,
                            head=payload.get("head_at_precheck", ""),
                        )
            else:
                _audit_append(
                    project,
                    task,
                    {
                        "event": "forensic-retro-bypass-strict-checks",
                        "tab_session_id": sid,
                    },
                )

            # Audit gate (G0-G9) runs whenever its mandate is on — INCLUDING
            # forensic mode (codex R1-F1 P0). forensic_retro only relaxes the
            # *retro* phase-status / freshness checks (a new session can't prove
            # the old session's Phase 0/1); it must NOT become a self-declared
            # field that skips the code audit, or any evidence with
            # mode="forensic_retro" would bypass the gate. Genuine forensic
            # recovery without an audit goes through the owner-approved
            # HANDOFF_RETRO_BYPASS path (short-circuited at the top), which spec
            # §1.1 exempts from G0-G9. It is the last gate before success so G0
            # binds to the HEAD the dump is about to hand off, after re-align.
            if audit_mandate_enabled:
                from handoff_fanout import codex_audit

                # R3-3: hold the per-task audit.lock across the WHOLE evaluation
                # so a concurrent `audit-run` (which writes a new findings
                # artifact under the same lock) can't slip a failing run in
                # between discover_run_indices() and the union check. Lock order
                # is precheck → dump → audit, matching `audit-close`, so nesting
                # it inside the already-held _ordered_locks is deadlock-free.
                audit_lock = _locks_dir(project) / f"{task}.audit.lock"
                try:
                    with atomic.acquire_dir_lock(audit_lock, retries=5, wait_seconds=0.2):
                        audit_outcome = codex_audit.evaluate_audit_gate(
                            payload, workspace, project, task
                        )
                except atomic.LockAcquisitionError:
                    _audit_append(
                        project,
                        task,
                        {
                            "event": "lock-contention",
                            "lock": "audit.lock",
                            "tab_session_id": sid,
                        },
                    )
                    return _locked("audit-lock-held", "another tab is writing audit runs")
                if not audit_outcome.ok:
                    return _handle_audit_failure(
                        project=project,
                        task=task,
                        outcome=audit_outcome,
                        payload=payload,
                        evidence_path=evidence_path,
                        session_id=sid,
                        head=_git(["rev-parse", "HEAD"], workspace),
                    )
                _clear_audit_attempt_on_success(project, task)
            elif audit_mandate_expected:
                # §F#9 silent-downgrade guard — PARTIAL drift: we reached the audit gate
                # with validated evidence, but the AUDIT mandate env dropped while this
                # listed project still expects it → G0-G9 is silently skipped. WARN +
                # durable sentinel, then continue (policy B — non-fatal).
                write_mandate_drift_sentinel(
                    project,
                    task,
                    workspace=workspace,
                    classification="partial_missing",
                    retro_mandate=mandate_enabled,
                    audit_mandate=audit_mandate_enabled,
                )

            if not forensic:
                _clear_attempt_on_success(project, task, payload.get("evidence_hash", ""), sid)
            else:
                _audit_append(
                    project,
                    task,
                    {
                        "event": "forensic-retro-success",
                        "evidence_hash": payload.get("evidence_hash", ""),
                        "tab_session_id": sid,
                    },
                )
            return _ok()
    except _LockHeld as e:
        _audit_append(
            project,
            task,
            {
                "event": "lock-contention",
                "subcode": e.subcode,
                "tab_session_id": sid,
            },
        )
        return _locked(e.subcode, "another tab is processing this task")
