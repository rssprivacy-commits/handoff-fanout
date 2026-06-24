"""Step2 契约 A — G3 真沉淀机器证明: memory snapshot baseline + WARN-mode verification.

The retro-evidence checklist's ``memory_updated:✅`` is self-attested (root cause G3) and
the memory surfaces are NOT inside any git repo (实查 F1), so "git diff = 沉淀证明" is
physically impossible — the contract replaces it with a SNAPSHOT-HASH carrier that lives
entirely inside the engine (零新 repo / path-based / 不撞冻结新元工具立法):

  * **baseline write** — every engine path that DISPATCHES a coordinator window
    (``dump --coordinator`` worktree + singlepane, ``spawn --role supervisor_succession``)
    records ``$HANDOFF_HOME/<project>/authority/<task>.memory-baseline.json``: the sha256
    of every ``*.md`` under the project's Claude Code memory dir at dispatch time. The
    baseline time is the ENGINE's dispatch moment, never the session's self-report.
  * **verification** — when that coordinator later relays (``audit-close --coordinator
    --status active --self-task <its own id>``), the engine re-snapshots and compares:
    ≥1 file ADDED or HASH-CHANGED = physical proof something was written ("写过"; the
    owner-ack lesson_path 双保险 proves "写的是 lesson"). File DELETIONS are not proof.
  * **WARN-only this slice** (A.5 observe-then-enforce): a missing/failed proof prints a
    loud ``WARN G3-no-sedimentation`` + audit-log line and NEVER blocks the relay; owner
    reads the observe-period log before ruling on hard enforcement.

Fallback chain (A.4, for a coordinator with no baseline — first-generation 中枢, dx-spawn
legacy dispatch, or a lost file): weak proof via ``memory/*.md max(mtime) >
launched/<self-task>-*.txt`` launch timestamp, logging the concrete hit files + mtimes
(SHOULD#7); no launched artifact either → WARN-pass with an audit-log line (绝不静默).

All functions are best-effort by design: a baseline/verification failure must never brick
a dump/spawn (the relay artifacts may already be published; G3 is an evidence layer, not a
gate — this slice). The audit trail shares ``authority/succession-audit.log`` with the
succession tokens (same authority surface, one forensics stream).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from handoff_fanout.succession_authority import _audit_log, authority_dir

SCHEMA_VERSION = 1

# Verification outcome enums (stable strings — tests + the observe-period log grep them).
VERIFY_OK = "ok"  # snapshot proof: ≥1 file added / hash changed vs the baseline
VERIFY_WEAK_OK = "weak-ok"  # fallback mtime proof (no baseline)
VERIFY_WARN = "warn"  # no proof found — WARN-only this slice (A.5)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def claude_projects_root() -> Path:
    """Root of Claude Code's per-project state dirs (tests monkeypatch this)."""
    return Path.home() / ".claude" / "projects"


def claude_project_slug(workspace: Path | str) -> str:
    """Claude Code's path-flattening slug for a workspace dir (contract A.1).

    The contract's shorthand is ``/`` → ``-``; the real Claude Code flattening also maps
    ``.`` → ``-`` (实证: ``~/.claude-handoff/...`` → ``--claude-handoff-...``), so both
    are mapped here — a dotted workspace path must resolve to the REAL memory dir, not a
    never-existing neighbour. (Interpretation per the contract's stated intent
    "Claude Code 既有路径扁平化"; flagged in the worker report.)
    """
    return str(workspace).replace("/", "-").replace(".", "-")


def memory_dir_for_workspace(workspace: Path | str) -> Path:
    """slug-resolve (warmgap-B codex SHOULD): ``Path.resolve()`` BEFORE flattening, so the
    same project dir written as a relative path / through a symlink / with redundant
    components flattens to ONE slug — different spellings must never split the baseline
    across sibling memory dirs. This is the single chokepoint every caller (write_baseline,
    verify_sedimentation, the A.4 fallback) funnels through."""
    return claude_projects_root() / claude_project_slug(Path(workspace).resolve()) / "memory"


def snapshot_memory_files(memory_dir: Path) -> dict[str, str]:
    """``{relpath: sha256}`` for every ``*.md`` under ``memory_dir`` (recursive — the
    contract's "全量 *.md"; new files anywhere under the dir count as sedimentation).
    A missing dir is an EMPTY snapshot (a first write then shows up as an added file).
    Unreadable files are skipped symmetrically at baseline and verify time."""
    files: dict[str, str] = {}
    if not memory_dir.is_dir():
        return files
    for p in sorted(memory_dir.rglob("*.md")):
        if not p.is_file():
            continue
        try:
            files[str(p.relative_to(memory_dir))] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            continue
    return files


def baseline_path(home: Path, project: str, task: str) -> Path:
    return authority_dir(home, project) / f"{task}.memory-baseline.json"


def write_baseline(*, home: Path, project: str, coordinator_task: str, workspace: Path) -> None:
    """Record the dispatch-time memory snapshot for a coordinator window.

    ``coordinator_task`` (SHOULD#2 naming) = the task id of the coordinator being
    DISPATCHED (it is "self" from the new window's perspective — semantically disjoint
    from the token's ``successor_task``, even though dump/spawn pass the same value).
    ``workspace`` = the real project tree the window opens against (its slug locates the
    project memory dir).

    0600 + ``O_EXCL`` so a half-written file can never exist (issue_token 既有模式); a
    pre-existing same-task baseline is KEPT (first dispatch wins — a retry re-dispatch
    must not absorb sedimentation that happened in between). Best-effort: any failure
    WARNs + audit-logs but never raises into the dump/spawn path.
    """
    try:
        mem_dir = memory_dir_for_workspace(workspace)
        payload = {
            "schema": SCHEMA_VERSION,
            "project": project,  # SHOULD#3: verification asserts this against the relay
            "coordinator_task": coordinator_task,
            "created_at": _now_iso(),
            "memory_dir": str(mem_dir),
            "files": snapshot_memory_files(mem_dir),
        }
        d = authority_dir(home, project)
        d.mkdir(parents=True, exist_ok=True)
        path = baseline_path(home, project, coordinator_task)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            _audit_log(
                home,
                project,
                "G3-BASELINE-KEPT",
                f"task={coordinator_task} (existing baseline kept — first dispatch wins)",
            )
            return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2) + "\n")
        except OSError:
            path.unlink(missing_ok=True)  # never leave a half-written baseline behind
            raise
        _audit_log(
            home,
            project,
            "G3-BASELINE-WRITTEN",
            f"task={coordinator_task} files={len(payload['files'])} memory_dir={mem_dir}",
        )
    except OSError as e:
        sys.stderr.write(
            f"[memory-baseline] WARN G3-baseline-write-failed: {e} — coordinator "
            f"{project}/{coordinator_task} dispatches WITHOUT a sedimentation baseline "
            "(its relay will take the A.4 fallback chain)\n"
        )
        _audit_log(home, project, "G3-BASELINE-WRITE-FAILED", f"task={coordinator_task} err={e}")


def _load_baseline(home: Path, project: str, self_task: str) -> dict | None:
    """Parse + validate the baseline for ``self_task``; None when missing/invalid
    (the caller falls back). Invalid is logged — never silently treated as absent."""
    path = baseline_path(home, project, self_task)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        _audit_log(home, project, "G3-BASELINE-INVALID", f"task={self_task} err={e}")
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
        _audit_log(home, project, "G3-BASELINE-INVALID", f"task={self_task} err=bad-shape")
        return None
    if payload.get("project") != project:
        # SHOULD#3 cross-project对位断言: a baseline issued for another project must
        # never be compared against this relay (cross-project 交棒 takes the fallback).
        _audit_log(
            home,
            project,
            "G3-BASELINE-PROJECT-MISMATCH",
            f"task={self_task} baseline_project={payload.get('project')!r}",
        )
        return None
    return payload


def _fallback_weak_proof(
    *, home: Path, project: str, self_task: str, workspace: Path
) -> tuple[str, str]:
    """A.4: no baseline → weak mtime proof against the launch timestamp; logs the
    concrete hit files + mtimes (SHOULD#7) to cut observe-period triage cost."""
    launched = sorted((home / project / "launched").glob(f"{self_task}-*.txt"))
    if not launched:
        detail = (
            f"no baseline AND no launched/{self_task}-*.txt — cannot prove or disprove "
            "sedimentation; WARN-pass (A.4 绝不静默)"
        )
        _audit_log(home, project, "G3-FALLBACK-NO-EVIDENCE", f"task={self_task}")
        return VERIFY_WARN, detail
    launch_ts = max(p.stat().st_mtime for p in launched)
    mem_dir = memory_dir_for_workspace(workspace)
    hits: list[str] = []
    if mem_dir.is_dir():
        for p in sorted(mem_dir.rglob("*.md")):
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime > launch_ts:
                hits.append(
                    f"{p.relative_to(mem_dir)} "
                    f"(mtime={datetime.fromtimestamp(mtime, UTC).isoformat(timespec='seconds')})"
                )
    if hits:
        detail = (
            f"weak mtime proof (no baseline): {len(hits)} file(s) newer than launch: "
            + ", ".join(hits)
        )
        _audit_log(
            home, project, "G3-FALLBACK-WEAK-PASS", f"task={self_task} hits={'; '.join(hits)}"
        )
        return VERIFY_WEAK_OK, detail
    detail = (
        f"no baseline and no memory/*.md newer than the launch timestamp under {mem_dir} "
        "— no sedimentation evidence (weak check)"
    )
    _audit_log(home, project, "G3-NO-SEDIMENTATION", f"task={self_task} mode=fallback-weak")
    return VERIFY_WARN, detail


def verify_sedimentation(
    *, home: Path, project: str, self_task: str, workspace: Path
) -> tuple[str, str]:
    """Compare the coordinator's OWN dispatch baseline against the current memory
    snapshot. Returns ``(status, detail)`` with status in {VERIFY_OK, VERIFY_WEAK_OK,
    VERIFY_WARN} — the caller prints; nothing here ever blocks (A.5 WARN mode)."""
    baseline = _load_baseline(home, project, self_task)
    if baseline is None:
        return _fallback_weak_proof(
            home=home, project=project, self_task=self_task, workspace=workspace
        )
    mem_dir = Path(baseline.get("memory_dir") or memory_dir_for_workspace(workspace))
    current = snapshot_memory_files(mem_dir)
    old = baseline["files"]
    added = sorted(set(current) - set(old))
    changed = sorted(k for k in set(current) & set(old) if current[k] != old[k])
    if added or changed:
        detail = (
            f"sedimentation proven vs baseline ({baseline.get('created_at')}): "
            f"added={added or '[]'} changed={changed or '[]'}"
        )
        _audit_log(
            home,
            project,
            "G3-SEDIMENTATION-OK",
            f"task={self_task} added={len(added)} changed={len(changed)}",
        )
        return VERIFY_OK, detail
    detail = (
        f"NO memory sedimentation since dispatch ({baseline.get('created_at')}): every "
        f"*.md under {mem_dir} is byte-identical to the baseline ({len(old)} files; "
        "deletions don't count). 交棒前必复盘沉淀 — write the lesson, then relay"
    )
    _audit_log(home, project, "G3-NO-SEDIMENTATION", f"task={self_task} mode=baseline")
    return VERIFY_WARN, detail
