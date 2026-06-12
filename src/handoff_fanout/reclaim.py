"""§6c worker-worktree window reclaim — contract v4 (frozen 2026-06-10).

Implements the full reclaim chain for worker worktree windows:

  * **C1** ``merged`` is git-derived (no state writer): in-critical-section fetch with
    an EXPLICIT refspec, canonical-remote dynamic resolution (config >
    ``branch.<int>.remote`` > origin — never hardcoded), pinned-SHA dual path
    (merged path ⇐ recorded head SHA only; abandoned path ⇐ the control-plane
    marker's SHA only), and a head-drift/forgery cross-check (recorded SHA must
    equal the worktree's ACTUAL HEAD — worker-writable artifacts are evidence,
    never authority). Squash/rebase integration is out of ancestry's closure and
    must go through an explicit ``handoff worktree abandon`` (never patch-id).
  * **C2** producer = the watchdog tick (``handoff watchdog`` runs every 60s via
    launchd StartInterval + WatchPaths). It NEVER sweeps on its own: a reclaim only
    starts from the coordinator's explicit ``ack/<id>.reclaim_requested`` sentinel
    (JSON ``{run_id, ts}``). Replay-guarded (done markers bind ``run_id``), aged out
    at 24h (``stale-request``), and consumed (``mv → processed/``) on terminal.
  * **C3** the producer no longer PUSHES a close URI (A-poll revision 2026-06-12):
    ``open vscode://…`` could only be delivered to ONE window — VS Code routes it to
    the active/focused window, so a worker window on another desktop never received
    it and the reclaim died ``ack-timeout``. Reversed to PULL: tick N writes the
    ``ack/<task>.reclaim_pending.json`` authorization (it carries
    ``role=worker``, ``reason=reclaim``, ``nonce=<spawn_nonce>``, ``run_id``,
    ``issued_at`` + the configured ``ack_timeout``) and the TARGET window's extension
    polls its OWN pending file, rebuilds the same params, and self-closes — so window
    targeting is intrinsic (each extension only reads its own task's pending), no
    matter where the window lives. The extension still rejects a stale poll with
    ``close-command-expired`` (``now - issued_at > ack_timeout``) BEFORE any side
    effect. The nonce is the auth token (CSPRNG, 64-bit hex16 —
    ``spawn_nonce.new_nonce``'s shape, hard-validated at both ends).

    Window-close split (sw-6c-winclose, method D): ``closeTabs`` only closes editor
    tabs — VS Code has no closeWindow tab API — so the extension's success ack is
    ``close_issued`` (tabs closed + ``workbench.action.closeWindow`` issued), NOT a
    terminal ``done``. ``closeWindow`` kills the extension host, so the extension cannot
    itself observe its window die. The producer therefore OWNS ``done``: on reading a
    ``close_issued`` ack it polls the window's extension-host PID (the worker writes it to
    ``ack/<task>.host_pid.json`` on activate) via ``os.kill(pid, 0)`` and only reclaims
    the worktree once that pid is ESRCH-gone (``_resolve_close_issued`` →
    ``_host_pid_liveness``). PID reuse is fail-closed-safe (a recycled pid reads alive →
    wait → deadline fail-closed, worktree retained); the only residual is a host
    crash/reload landing in the sub-millisecond window between the ack write and
    ``closeWindow`` — negligible, and a reload's re-activate rewrites host_pid with the
    live pid. ``window-close-unconfirmed`` (the +1 enum reason) records any close_issued
    that never confirms; the worktree is retained either way (C7).
  * **C4** ``abandoned`` is authoritative ONLY in the control plane
    (``<project>/abandoned/<task>.json``, written by ``handoff worktree abandon``);
    the in-worktree ``.handoff-abandoned.json`` is an audit copy. A copy without a
    control-plane record (the forge case) or a conflicting copy fails closed.
  * **C5** wave membership truth = the O_EXCL-frozen
    ``<project>/waves/<wave_id>.manifest.json``; same-wave sidecars not in the
    manifest are ignored + audit-marked (attempted late-add); a missing/corrupt
    manifest fails the WHOLE wave closed; a single worker needs no manifest.
  * **C6** the close window is TOCTOU-free: probe → write ``reclaim_pending`` →
    ack/timeout all happen under the project ``.spawn.lock`` (the same lock every
    spawn-intent producer holds), held ACROSS ticks via a cross-tick state machine —
    the watchdog never sleeps in-tick. Tick N writes ``reclaim_pending`` (the
    extension's poll authorization) + renews the lock; tick N+1 consumes the
    extension's self-close ack / enforces the deadline. A crashed holder is reaped by
    the lock TTL; its pending state is then treated stale.
    The live-session probe (transcript mtime) fails CLOSED: any read anomaly ⇒
    unconditionally alive ⇒ never close.
  * **C7** the role×reason whitelist matrix is enforced producer-side here (and
    mirrored extension-side), with rejection BEFORE any side effect, and the
    19-reason ``reclaim_failed.json`` enum below (18 v4 + ``window-close-unconfirmed``).

Scope red line (contract §范围边界): this module is PARALLEL to the existing
succession autoclose (``install/auto-continue.sh try_autoclose``) and mirrors its
discipline (lock-free fast path reads no decision values / one lock-held critical
section / single-exit release / idempotent done markers); it never touches §6
retirement gates, dump/retro, or the succession path.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from handoff_fanout import atomic
from handoff_fanout import config as _config
from handoff_fanout import worktree as _worktree

# ── C7: the unified reclaim_failed reason enum (v4 + sw-6c-winclose — exactly 19) ──
REASON_REF_FETCH_FAILED = "ref-fetch-failed"
REASON_INT_BRANCH_MISSING = "int-branch-missing"
REASON_SHA_UNRESOLVABLE = "sha-unresolvable"
REASON_NOT_MERGED = "not-merged"
REASON_HEAD_DRIFT = "head-drift"
REASON_ABANDON_INVALID = "abandon-invalid"
REASON_ABANDON_SHA_MISMATCH = "abandon-sha-mismatch"
REASON_ABANDON_AUTHORITY_INVALID = "abandon-authority-invalid"
REASON_WAVE_INCOMPLETE = "wave-incomplete"
REASON_MANIFEST_MISSING = "manifest-missing"
REASON_LIVE_SESSION = "live-session"
REASON_PROBE_ERROR = "probe-error"
REASON_DIRTY = "dirty"
REASON_NONCE_MISMATCH = "nonce-mismatch"
REASON_ACK_TIMEOUT = "ack-timeout"
REASON_CLOSE_COMMAND_EXPIRED = "close-command-expired"
REASON_ROLE_REASON_REJECTED = "role-reason-rejected"
REASON_STALE_REQUEST = "stale-request"
# sw-6c-winclose (method D): the extension's ``close_issued`` ack means tabs closed +
# closeWindow issued, NOT that the window is gone. The producer owns the terminal
# ``done``, written only after it confirms the window's extension-host PID left the
# process table. This reason fires when that confirmation never lands — the host is still
# alive at the deadline, or the dead-man token (``host_pid.json``) is missing / nonce-
# mismatched / unreadable. Fail-closed: the worktree is RETAINED (never deleted on a mere
# close intent), so it is safe by construction.
REASON_WINDOW_CLOSE_UNCONFIRMED = "window-close-unconfirmed"

REASONS: tuple[str, ...] = (
    REASON_REF_FETCH_FAILED,
    REASON_INT_BRANCH_MISSING,
    REASON_SHA_UNRESOLVABLE,
    REASON_NOT_MERGED,
    REASON_HEAD_DRIFT,
    REASON_ABANDON_INVALID,
    REASON_ABANDON_SHA_MISMATCH,
    REASON_ABANDON_AUTHORITY_INVALID,
    REASON_WAVE_INCOMPLETE,
    REASON_MANIFEST_MISSING,
    REASON_LIVE_SESSION,
    REASON_PROBE_ERROR,
    REASON_DIRTY,
    REASON_NONCE_MISMATCH,
    REASON_ACK_TIMEOUT,
    REASON_CLOSE_COMMAND_EXPIRED,
    REASON_ROLE_REASON_REJECTED,
    REASON_STALE_REQUEST,
    REASON_WINDOW_CLOSE_UNCONFIRMED,
)

# Reasons the EXTENSION may legitimately put in its ack file. Anything else in an
# ack is mapped to ack-timeout (+ detail) so reclaim_failed.json stays enum-pure.
_EXTENSION_ACK_REASONS = frozenset(
    {
        REASON_DIRTY,
        REASON_CLOSE_COMMAND_EXPIRED,
        REASON_ROLE_REASON_REJECTED,
        REASON_NONCE_MISMATCH,
    }
)

# ── C2/C6 mechanics constants ───────────────────────────────────────────────────
STALE_REQUEST_SECONDS = 24 * 3600  # sentinel older than this is invalidated (one alert)
LOCK_TTL_SECONDS = 120.0  # MUST equal spawn_lock's TTL — it is the SAME lock dir
EXT_ACK_TIMEOUT_CAP = 600.0  # extension-side cap mirrored here for the URI param
BACKOFF_BASE_SECONDS = 60.0  # offline exponential backoff (gemini SHOULD)
BACKOFF_MAX_SECONDS = 3600.0
STUCK_WAVE_SECONDS = 7 * 24 * 3600  # reclaim-report patrol threshold (SHOULD)

_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")

ROLE_WORKER = "worker"
RECLAIM_REASON = "reclaim"  # the worker×reclaim matrix row; written into the pending


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _log(msg: str) -> None:
    print(f"[reclaim] {msg}")


# ─── paths ───────────────────────────────────────────────────────────────────────


def waves_dir(cfg: _config.Config, project: str) -> Path:
    return cfg.home / project / "waves"


def abandoned_dir(cfg: _config.Config, project: str) -> Path:
    return cfg.home / project / "abandoned"


def processed_dir(cfg: _config.Config, project: str) -> Path:
    return cfg.home / project / "processed"


def requested_path(cfg: _config.Config, project: str, request_id: str) -> Path:
    return cfg.ack_dir(project) / f"{request_id}.reclaim_requested"


def pending_path(cfg: _config.Config, project: str, task: str) -> Path:
    return cfg.ack_dir(project) / f"{task}.reclaim_pending.json"


def ack_file_path(cfg: _config.Config, project: str, task: str) -> Path:
    return cfg.ack_dir(project) / f"{task}.reclaim_ack.json"


def host_pid_path(cfg: _config.Config, project: str, task: str) -> Path:
    """The §6c window-close dead-man token (sw-6c-winclose): the worker window's
    extension writes its OWN extension-host PID here on activate; the producer polls
    ``os.kill(pid, 0)`` against it to confirm the window physically closed (its host
    process left the table) before reclaiming the worktree."""
    return cfg.ack_dir(project) / f"{task}.host_pid.json"


def done_path(cfg: _config.Config, project: str, task: str) -> Path:
    return cfg.ack_dir(project) / f"{task}.reclaim_done"


def failed_path(cfg: _config.Config, project: str, task: str) -> Path:
    return cfg.ack_dir(project) / f"{task}.reclaim_failed.json"


def backoff_path(cfg: _config.Config, project: str) -> Path:
    return cfg.ack_dir(project) / ".reclaim_backoff.json"


def _lockdir(cfg: _config.Config, project: str) -> Path:
    return cfg.home / project / ".spawn.lock"  # the ONE project spawn lock (C6)


def _owner_path(cfg: _config.Config, project: str) -> Path:
    # Fencing/owner token (codex SHOULD). A SIBLING of the lock dir, never inside it:
    # an in-lockdir file would make every other holder's stale-break ``rmdir`` fail
    # (bash clean_stale_lock + spawn_lock both rmdir), deadlocking the project after a
    # crashed reclaim hold. The sibling is diagnostic + ownership-binding only; the
    # mutual exclusion itself stays the lock dir's mkdir.
    return cfg.home / project / ".spawn.lock.reclaim-owner.json"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ─── C6: live-session probe (pluggable adapter; fail-closed direction) ───────────


@dataclass
class ProbeResult:
    status: str  # "live" | "dead" | "error"
    detail: str = ""


def transcript_project_dir_name(workspace: Path) -> str:
    """Claude keeps per-project transcripts under ``~/.claude/projects/<munged>``
    where ``<munged>`` is the workspace path with ``/`` and ``.`` replaced by ``-``."""
    return re.sub(r"[/.]", "-", str(workspace))


@dataclass
class TranscriptProbeAdapter:
    """Default live-session probe: any transcript ``*.jsonl`` in the worktree's Claude
    project dir with mtime within ``idle_sec`` ⇒ LIVE. The adapter is deliberately
    swappable (gemini SHOULD): it depends on Claude's local, unversioned directory
    layout, so a layout change must surface as ``probe-error`` (fail-closed, never
    close) rather than silently mis-probing. ``error`` covers: dir missing, no
    transcripts at all, permission errors, ANY exception — all unconditionally
    treated as alive by the caller (C6 gemini MUST: 宁留僵尸窗、绝不误杀).
    """

    idle_sec: float = 600.0
    projects_root: Path = field(default_factory=lambda: Path.home() / ".claude" / "projects")

    def probe(self, workspace: Path) -> ProbeResult:
        try:
            d = self.projects_root / transcript_project_dir_name(workspace)
            if not d.is_dir():
                return ProbeResult("error", f"transcript dir missing: {d}")
            newest: float | None = None
            for f in d.glob("*.jsonl"):
                mt = f.stat().st_mtime
                if newest is None or mt > newest:
                    newest = mt
            if newest is None:
                return ProbeResult("error", "no transcript *.jsonl found")
            if (time.time() - newest) < self.idle_sec:
                return ProbeResult("live", f"transcript active {time.time() - newest:.0f}s ago")
            return ProbeResult("dead", f"newest transcript idle {time.time() - newest:.0f}s")
        except Exception as e:  # ANY anomaly → fail-closed (treated alive)
            return ProbeResult("error", f"probe exception: {e}")


def _default_probe(cfg: _config.Config) -> TranscriptProbeAdapter:
    return TranscriptProbeAdapter(idle_sec=cfg.reclaim_probe_idle_sec)


# ─── C1/C4: eligibility evaluation ────────────────────────────────────────────────


@dataclass
class MemberVerdict:
    ok: bool
    reason: str | None = None  # ∈ REASONS when not ok
    detail: str = ""
    nonce: str | None = None
    evidence: dict = field(default_factory=dict)  # pinned_head_sha / canonical_int_sha / fetched_at
    path: str | None = None  # "merged" | "abandoned" when ok


def _fail(reason: str, detail: str = "", **kw) -> MemberVerdict:
    return MemberVerdict(ok=False, reason=reason, detail=detail, **kw)


def _read_sidecar(cfg: _config.Config, project: str, task: str) -> dict | None:
    return _read_json(cfg.queue_dir(project) / f"{task}.singlepane")


def _pinned_sha(cfg: _config.Config, project: str, task: str) -> tuple[str | None, str]:
    """The worker's RECORDED closing head SHA (merged-path ancestry input, C1 MUST#2).

    Source order: ``ack/<task>.old_ready`` ``commit_hash`` (the engine's retro-gated
    recorder) > ``ack/<task>.head.json`` ``head_sha`` (the ``handoff worktree
    record-head`` evidence channel). NEVER the abandon marker's SHA (dual-path
    separation) and NEVER a live branch read. Missing ⇒ caller fails closed."""
    old_ready = _read_json(cfg.ack_dir(project) / f"{task}.old_ready")
    if old_ready:
        sha = old_ready.get("commit_hash")
        if isinstance(sha, str) and sha and sha != "(unknown)":
            return sha, "old_ready"
    head_rec = _read_json(cfg.ack_dir(project) / f"{task}.head.json")
    if head_rec:
        sha = head_rec.get("head_sha")
        if isinstance(sha, str) and sha:
            return sha, "head.json"
    return None, "none"


def _resolve_canonical_remote(
    cfg: _config.Config, repo: Path, int_branch: str
) -> str | None:
    """C1 gemini M1: config ``canonical_remote`` > ``git config branch.<int>.remote`` >
    ``origin`` — and the winner must actually exist in ``git remote``; else None
    (caller fails closed). Never a hardcoded origin."""
    rc, out, _ = _worktree._git(["remote"], repo)
    remotes = set(out.split()) if rc == 0 else set()
    if cfg.canonical_remote:
        return cfg.canonical_remote if cfg.canonical_remote in remotes else None
    rc, out, _ = _worktree._git(["config", f"branch.{int_branch}.remote"], repo)
    if rc == 0 and out and out in remotes:
        return out
    return "origin" if "origin" in remotes else None


def _abandon_records(
    cfg: _config.Config, project: str, task: str, wt: Path
) -> tuple[dict | None, dict | None, bool]:
    """Returns ``(control_record, worktree_copy, copy_unreadable)``."""
    control = _read_json(abandoned_dir(cfg, project) / f"{task}.json")
    copy_path = wt / ".handoff-abandoned.json"
    copy: dict | None = None
    copy_unreadable = False
    if copy_path.exists():
        copy = _read_json(copy_path)
        if copy is None:
            copy_unreadable = True
    return control, copy, copy_unreadable


_ABANDON_REQUIRED_KEYS = ("reason", "ts", "actor", "task", "branch_head_sha")


def _evaluate_abandoned(
    cfg: _config.Config, project: str, task: str, wt: Path
) -> MemberVerdict | None:
    """C4. Returns a verdict when abandon artifacts exist (valid → eligible; anomalous
    → fail-closed — a forged/conflicting marker must surface, never fall through to
    the merged path). Returns ``None`` when no abandon artifacts exist at all."""
    control, copy, copy_unreadable = _abandon_records(cfg, project, task, wt)
    if control is None and copy is None and not copy_unreadable:
        return None  # not an abandon candidate at all
    if control is None:
        # The forge case (codex M3): a worker-writable in-tree marker with NO
        # control-plane record never grants eligibility.
        return _fail(
            REASON_ABANDON_AUTHORITY_INVALID,
            "worktree abandon copy exists but no control-plane record "
            f"({abandoned_dir(cfg, project) / (task + '.json')})",
        )
    missing = [k for k in _ABANDON_REQUIRED_KEYS if not control.get(k)]
    if missing or control.get("task") != task:
        return _fail(
            REASON_ABANDON_INVALID,
            f"control-plane record schema invalid (missing={missing}, task={control.get('task')!r})",
        )
    if copy_unreadable:
        return _fail(REASON_ABANDON_AUTHORITY_INVALID, "worktree audit copy unreadable")
    if copy is not None and copy.get("branch_head_sha") != control.get("branch_head_sha"):
        return _fail(
            REASON_ABANDON_AUTHORITY_INVALID,
            "worktree audit copy conflicts with control-plane record (branch_head_sha)",
        )
    if not wt.exists():
        return _fail(REASON_SHA_UNRESOLVABLE, f"worktree gone: {wt}")
    actual = _worktree.head_sha(wt)
    if not actual:
        return _fail(REASON_SHA_UNRESOLVABLE, "worktree HEAD unresolvable")
    if actual != control["branch_head_sha"]:
        return _fail(
            REASON_ABANDON_SHA_MISMATCH,
            f"abandon recorded {str(control['branch_head_sha'])[:8]} but worktree HEAD "
            f"is {actual[:8]}",
        )
    return MemberVerdict(
        ok=True,
        path="abandoned",
        evidence={"pinned_head_sha": actual, "abandon_reason": control.get("reason")},
    )


def _evaluate_merged(
    cfg: _config.Config, project: str, task: str, repo: Path, wt: Path
) -> MemberVerdict:
    """C1 merged path: explicit-refspec fetch + canonical remote + pinned-SHA ancestry
    + head-drift cross-check. ALL failure modes fail closed with an enum reason."""
    pinned, src = _pinned_sha(cfg, project, task)
    if pinned is None:
        return _fail(
            REASON_SHA_UNRESOLVABLE,
            "no recorded closing head SHA (ack/<task>.old_ready commit_hash or "
            "ack/<task>.head.json — record one via `handoff worktree record-head`)",
        )
    int_branch = _worktree.resolve_integration_branch(repo, cfg, allow_network=False)
    if not int_branch:
        return _fail(REASON_INT_BRANCH_MISSING, "integration branch unresolvable")
    remote = _resolve_canonical_remote(cfg, repo, int_branch)
    if not remote:
        return _fail(
            REASON_REF_FETCH_FAILED,
            "canonical remote unresolvable (config canonical_remote > "
            f"branch.{int_branch}.remote > origin all failed)",
        )
    # MUST#1 + v4 codex M1: in-critical-section fetch with an EXPLICIT refspec so the
    # remote-tracking ref we read below is THE ref this fetch updated (same source).
    tracking_ref = f"refs/remotes/{remote}/{int_branch}"
    rc, _out, err = _worktree._git(
        ["fetch", remote, f"+refs/heads/{int_branch}:{tracking_ref}"], repo, timeout=30.0
    )
    fetched_at = _now_iso()
    if rc != 0:
        low = err.lower()
        if "couldn't find remote ref" in low or "no such ref" in low:
            return _fail(REASON_INT_BRANCH_MISSING, f"remote branch missing: {err[:120]}")
        return _fail(REASON_REF_FETCH_FAILED, f"fetch failed: {err[:120]}")
    canonical_int_sha = _worktree.head_sha_of_ref(repo, tracking_ref)
    if not canonical_int_sha:
        return _fail(REASON_INT_BRANCH_MISSING, f"{tracking_ref} unresolvable after fetch")
    if not wt.exists():
        return _fail(REASON_SHA_UNRESOLVABLE, f"worktree gone: {wt}")
    actual = _worktree.head_sha(wt)
    if not actual:
        return _fail(REASON_SHA_UNRESOLVABLE, "worktree HEAD unresolvable")
    # v4 codex M2: the recorded SHA is worker-reported EVIDENCE. Cross-validate it
    # against the worktree's actual HEAD; drift (stale report / post-ready commits /
    # forged value) fails closed.
    if pinned != actual:
        return _fail(
            REASON_HEAD_DRIFT,
            f"recorded {pinned[:8]} ({src}) != worktree HEAD {actual[:8]}",
        )
    if not _worktree.is_ancestor(repo, pinned, canonical_int_sha):
        return _fail(
            REASON_NOT_MERGED,
            f"{pinned[:8]} not an ancestor of {remote}/{int_branch} "
            "(squash/rebase integration must go through `handoff worktree abandon`)",
            evidence={
                "pinned_head_sha": pinned,
                "canonical_int_sha": canonical_int_sha,
                "fetched_at": fetched_at,
            },
        )
    return MemberVerdict(
        ok=True,
        path="merged",
        evidence={
            "pinned_head_sha": pinned,
            "canonical_int_sha": canonical_int_sha,
            "fetched_at": fetched_at,
        },
    )


def _evaluate_member(
    cfg: _config.Config,
    project: str,
    task: str,
    expected_nonce: str | None,
    probe: TranscriptProbeAdapter | None,
) -> MemberVerdict:
    """Full per-member gate chain (C7 worker×reclaim row). Order: identity (sidecar /
    nonce / isolation) → eligibility (C4 abandoned | C1 merged) → dirty → live probe.
    Rejection always precedes any side effect (the caller only fires the URI on ok)."""
    sidecar = _read_sidecar(cfg, project, task)
    if sidecar is None:
        return _fail(REASON_NONCE_MISMATCH, "sidecar queue/<task>.singlepane missing/unreadable")
    nonce = sidecar.get("spawn_nonce")
    if not isinstance(nonce, str) or not _HEX16_RE.match(nonce):
        # C3 gemini M2: the nonce is the auth token — a malformed one is never fired.
        return _fail(REASON_NONCE_MISMATCH, f"sidecar spawn_nonce malformed: {nonce!r}")
    if expected_nonce is not None and nonce != expected_nonce:
        return _fail(
            REASON_NONCE_MISMATCH,
            "sidecar spawn_nonce does not match the frozen wave manifest member",
        )
    if sidecar.get("role") != ROLE_WORKER or sidecar.get("isolation") != "worktree":
        # §6c covers worker WORKTREE windows only (the role×reason matrix's worker
        # row); anything else is out of this gate's jurisdiction → matrix reject.
        return _fail(
            REASON_ROLE_REASON_REJECTED,
            f"not a worker worktree dispatch (role={sidecar.get('role')!r}, "
            f"isolation={sidecar.get('isolation')!r})",
        )

    wt = _worktree.worktree_path(cfg, project, task)
    repo = cfg.workspace_root / project
    if not repo.is_dir():
        repo = wt  # any checkout of the shared repo works for ref/ancestry queries

    abandoned_verdict = _evaluate_abandoned(cfg, project, task, wt)
    if abandoned_verdict is not None:
        verdict = abandoned_verdict
    else:
        verdict = _evaluate_merged(cfg, project, task, repo, wt)
    if not verdict.ok:
        verdict.nonce = nonce
        return verdict

    # dirty gate (covers untracked via is_dirty's porcelain scan; engine links discounted).
    # The abandon AUDIT COPY is a CLI-created engine artifact, not user WIP — discount it
    # (untracked-only, same redline as .vscode/.handoff.code-workspace: a TRACKED change
    # to it still reads dirty).
    if _worktree.is_dirty(wt, ignore=_worktree._link_names(cfg) | {".handoff-abandoned.json"}):
        return _fail(
            REASON_DIRTY, "worktree has uncommitted/untracked changes", nonce=nonce
        )

    # C6 live-session probe — fail-closed: error ⇒ unconditionally alive.
    if not cfg.reclaim_probe_disabled:
        adapter = probe if probe is not None else _default_probe(cfg)
        pr = adapter.probe(wt)
        if pr.status == "live":
            return _fail(REASON_LIVE_SESSION, pr.detail, nonce=nonce)
        if pr.status != "dead":
            return _fail(REASON_PROBE_ERROR, pr.detail, nonce=nonce)

    verdict.nonce = nonce
    return verdict


# ─── C5: wave manifest ────────────────────────────────────────────────────────────


def load_manifest(cfg: _config.Config, project: str, wave_id: str) -> list[dict] | None:
    """Validated manifest members, or None (missing/corrupt ⇒ whole wave fails closed)."""
    data = _read_json(waves_dir(cfg, project) / f"{wave_id}.manifest.json")
    if not data or data.get("wave_id") != wave_id:
        return None
    members = data.get("members")
    if not isinstance(members, list) or not members:
        return None
    out: list[dict] = []
    for m in members:
        if not isinstance(m, dict):
            return None
        tid, nonce = m.get("task_id"), m.get("spawn_nonce")
        if not (isinstance(tid, str) and _SLUG_RE.match(tid)):
            return None
        if not (isinstance(nonce, str) and _HEX16_RE.match(nonce)):
            return None
        out.append({"task_id": tid, "spawn_nonce": nonce})
    return out


def _audit_late_adds(
    cfg: _config.Config, project: str, wave_id: str, members: list[dict], run_id: str
) -> None:
    """C5: a sidecar claiming this wave_id but absent from the frozen manifest is
    IGNORED for membership and surfaced as an attempted late-add (visible, never
    effective). Idempotent: full-state overwrite keyed by run_id."""
    member_ids = {m["task_id"] for m in members}
    late: list[str] = []
    queue = cfg.queue_dir(project)
    if queue.is_dir():
        for sc in sorted(queue.glob("*.singlepane")):
            if sc.stem in member_ids:
                continue
            data = _read_json(sc)
            if data and data.get("wave_id") == wave_id:
                late.append(sc.stem)
    if late:
        atomic.atomic_replace(
            cfg.ack_dir(project) / f"{wave_id}.reclaim_lateadd.json",
            json.dumps(
                {"wave_id": wave_id, "run_id": run_id, "attempted_late_add": late,
                 "ts": _now_iso()},
            )
            + "\n",
        )
        _log(f"{project}/{wave_id}: attempted late-add ignored: {', '.join(late)}")


# ─── markers / sentinel consumption ──────────────────────────────────────────────


def _write_failed(
    cfg: _config.Config,
    project: str,
    task: str,
    *,
    reason: str,
    run_id: str,
    detail: str = "",
    terminal: bool = True,
) -> None:
    assert reason in REASONS, reason
    atomic.atomic_replace(
        failed_path(cfg, project, task),
        json.dumps(
            {
                "task": task,
                "reason": reason,
                "run_id": run_id,
                "detail": detail,
                "terminal": terminal,
                "ts": _now_iso(),
            }
        )
        + "\n",
    )
    _log(f"{project}/{task}: reclaim_failed reason={reason} run={run_id} {detail}")


def _write_done(
    cfg: _config.Config, project: str, task: str, *, run_id: str, payload: dict
) -> None:
    atomic.atomic_replace(
        done_path(cfg, project, task),
        json.dumps({"task": task, "run_id": run_id, "ts": _now_iso(), **payload}) + "\n",
    )
    _log(f"{project}/{task}: reclaim_done run={run_id}")


def _consume_sentinel(cfg: _config.Config, project: str, request_id: str, run_id: str) -> None:
    """v3 sentinel-consumption semantics: terminal states mv the requested sentinel to
    ``processed/`` so a stale/failed request never re-alerts every tick."""
    src = requested_path(cfg, project, request_id)
    if not src.exists():
        return
    dst_dir = processed_dir(cfg, project)
    dst_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.replace(src, dst_dir / f"{src.name}.{run_id or 'invalid'}")


# ─── cross-tick lock (C6: held probe→URI→ack/timeout; renewed, TTL-reaped) ───────


def _acquire_lock(cfg: _config.Config, project: str, run_id: str) -> bool:
    """Non-blocking mkdir acquire of the project spawn lock (same dir/TTL as
    ``spawn_lock.project_spawn_lock`` and the bash try_autoclose). Stale (>TTL)
    locks are broken once. On success an OWNER sibling records {run_id} (fencing)."""
    lock = _lockdir(cfg, project)
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            lock.mkdir()
            atomic.atomic_replace(
                _owner_path(cfg, project),
                json.dumps({"run_id": run_id, "acquired_at": _now_iso(),
                            "renewed_epoch": time.time()})
                + "\n",
            )
            return True
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue  # released between mkdir and stat → retry once
            if age <= LOCK_TTL_SECONDS:
                return False  # genuinely held → skip this tick (mirror autoclose SKIP)
            with contextlib.suppress(OSError):
                lock.rmdir()
    return False


def _renew_lock(cfg: _config.Config, project: str, run_id: str) -> None:
    lock = _lockdir(cfg, project)
    with contextlib.suppress(OSError):
        os.utime(lock, None)
    atomic.atomic_replace(
        _owner_path(cfg, project),
        json.dumps({"run_id": run_id, "renewed_at": _now_iso(),
                    "renewed_epoch": time.time()})
        + "\n",
    )


def _release_lock(cfg: _config.Config, project: str) -> None:
    with contextlib.suppress(OSError):
        _lockdir(cfg, project).rmdir()
    with contextlib.suppress(OSError):
        _owner_path(cfg, project).unlink()


def _we_own_lock(cfg: _config.Config, project: str, run_id: str) -> bool:
    """Best-effort cross-tick ownership check (fencing). We own iff the lock dir
    exists AND the owner sibling carries OUR run_id AND our last renewal is within
    the TTL (a renewal gap > TTL means the lock was reapable — whoever holds it now
    may be a rival, so we must NOT touch it)."""
    if not _lockdir(cfg, project).is_dir():
        return False
    owner = _read_json(_owner_path(cfg, project))
    if not owner or owner.get("run_id") != run_id:
        return False
    renewed = owner.get("renewed_epoch")
    if not isinstance(renewed, (int, float)):
        return False
    return (time.time() - renewed) <= LOCK_TTL_SECONDS


# ─── offline backoff (gemini SHOULD) ─────────────────────────────────────────────


def _backoff_active(cfg: _config.Config, project: str) -> bool:
    data = _read_json(backoff_path(cfg, project))
    if not data:
        return False
    nxt = data.get("next_allowed_epoch")
    return isinstance(nxt, (int, float)) and time.time() < nxt


def _bump_backoff(cfg: _config.Config, project: str) -> None:
    data = _read_json(backoff_path(cfg, project)) or {}
    consecutive = int(data.get("consecutive", 0)) + 1
    delay = min(BACKOFF_BASE_SECONDS * (2 ** (consecutive - 1)), BACKOFF_MAX_SECONDS)
    atomic.atomic_replace(
        backoff_path(cfg, project),
        json.dumps(
            {
                "consecutive": consecutive,
                "next_allowed_epoch": time.time() + delay,
                "ts": _now_iso(),
            }
        )
        + "\n",
    )
    _log(f"{project}: ref-fetch backoff ×{consecutive} ({delay:.0f}s)")


def _reset_backoff(cfg: _config.Config, project: str) -> None:
    with contextlib.suppress(OSError):
        backoff_path(cfg, project).unlink()


# ─── member set / terminal bookkeeping ───────────────────────────────────────────


def _resolve_members(
    cfg: _config.Config, project: str, request_id: str
) -> tuple[list[dict] | None, bool, str]:
    """Returns ``(members, is_wave, detail)``. ``None`` members ⇒ fail the request
    closed (manifest-missing). A single worker (sidecar exists, no manifest) is its
    own one-member set with no expected-nonce pinning beyond its sidecar (C5)."""
    manifest_file = waves_dir(cfg, project) / f"{request_id}.manifest.json"
    if manifest_file.exists():
        members = load_manifest(cfg, project, request_id)
        if members is None:
            return None, True, f"manifest unreadable/invalid: {manifest_file}"
        return members, True, ""
    if (cfg.queue_dir(project) / f"{request_id}.singlepane").exists():
        return [{"task_id": request_id, "spawn_nonce": None}], False, ""
    return (
        None,
        True,
        "no wave manifest and no member sidecar — member set unknown (C5 fail-closed)",
    )


def _member_terminal(cfg: _config.Config, project: str, task: str, run_id: str) -> str | None:
    """``"done"`` (any run — the window is gone), ``"failed"`` (THIS run, terminal),
    or None (still open for this run)."""
    if done_path(cfg, project, task).exists():
        return "done"
    failed = _read_json(failed_path(cfg, project, task))
    if failed and failed.get("run_id") == run_id and failed.get("terminal", True):
        return "failed"
    return None


def _finalize_request_if_terminal(
    cfg: _config.Config, project: str, request_id: str, run_id: str, members: list[dict],
    is_wave: bool,
) -> bool:
    """When every member is terminal: write the wave summary (all done → wave done
    marker; any failed → ``wave-incomplete`` summary with the per-member map — the
    per-member semantics are the codex SHOULD: closes already happened member-wise),
    consume the sentinel, and return True."""
    states: dict[str, str] = {}
    for m in members:
        st = _member_terminal(cfg, project, m["task_id"], run_id)
        if st is None:
            return False
        states[m["task_id"]] = st
    if is_wave:
        if all(s == "done" for s in states.values()):
            _write_done(cfg, project, request_id, run_id=run_id, payload={"members": states})
        else:
            detail_map = {}
            for t, s in states.items():
                if s == "failed":
                    rec = _read_json(failed_path(cfg, project, t)) or {}
                    detail_map[t] = rec.get("reason", "failed")
                else:
                    detail_map[t] = "done"
            _write_failed(
                cfg,
                project,
                request_id,
                reason=REASON_WAVE_INCOMPLETE,
                run_id=run_id,
                detail=json.dumps(detail_map, sort_keys=True),
            )
    _consume_sentinel(cfg, project, request_id, run_id)
    return True


# ─── post-close resource reclaim (C4: head re-verified into the ack) ────────────


def _reclaim_worktree_resources(
    cfg: _config.Config, project: str, task: str, verdict_path: str, evidence: dict
) -> dict:
    """After the window is CONFIRMED closed (its extension-host PID left the process
    table — method D dead-man switch, not a mere ``close_issued`` intent), reclaim the
    worktree + branch.

    merged path → the fail-safe ``remove_worktree`` (clean+published only).
    abandoned path → force-remove, but ONLY after re-verifying the head SHA still
    matches the abandon record (C4: 复核进 ack). Any failure retains the worktree
    and is recorded in the done marker — the window close itself stands."""
    wt = _worktree.worktree_path(cfg, project, task)
    branch = _worktree.branch_name(cfg, task)
    repo = cfg.workspace_root / project
    if not repo.is_dir():
        return {"worktree_removed": False, "remove_detail": "main repo missing; retained"}
    if not wt.exists():
        return {"worktree_removed": False, "remove_detail": "worktree already gone"}
    if verdict_path == "abandoned":
        actual = _worktree.head_sha(wt)
        if actual != evidence.get("pinned_head_sha"):
            return {
                "worktree_removed": False,
                "remove_detail": f"head moved since eligibility ({actual!r}); retained",
            }
        _worktree._git(["worktree", "remove", "--force", str(wt)], repo)
        _worktree._git(["worktree", "prune"], repo)
        _worktree._git(["branch", "-D", branch], repo)
        removed = not wt.exists()
        return {
            "worktree_removed": removed,
            "remove_detail": "force-removed (abandoned)" if removed else "force-remove failed",
            "removed_head_sha": actual,
        }
    int_branch = _worktree.resolve_integration_branch(repo, cfg, allow_network=False) or ""
    removed, reason = _worktree.remove_worktree(
        repo, wt, branch, int_branch, _worktree._link_names(cfg)
    )
    return {"worktree_removed": removed, "remove_detail": reason}


# ─── method D: PID dead-man verification of the window close ─────────────────────


def _host_pid_liveness(host: dict | None, expected_nonce: str | None) -> str:
    """Probe the worker window's extension-host PID (sw-6c-winclose method D). Returns:

      * ``"dead"``    — ``os.kill(pid, 0)`` raised ESRCH: NO process holds this pid, so
                        the window's host is gone ⇒ the window is confirmed closed.
      * ``"alive"``   — the process exists (signal sent, or EPERM = exists but owned by
                        another uid) ⇒ keep waiting.
      * ``"unknown"`` — token missing / unreadable pid / nonce-mismatch / any other kill
                        error ⇒ fail-closed (NEVER reclaim).

    PID-reuse safety: a recycled pid returns ``"alive"`` (false-ALIVE) → the caller waits
    to the deadline then fail-closes with the worktree RETAINED (no data loss). ``ESRCH``
    fires ONLY when nothing holds the pid = the window is truly gone, the correct signal.
    The nonce binds the token to THIS spawn: a leftover ``host_pid.json`` from a previous
    spawn of the same task carries a different nonce → ``"unknown"`` (never trusted)."""
    if not isinstance(host, dict):
        return "unknown"
    pid = host.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown"
    if expected_nonce is not None and host.get("nonce") != expected_nonce:
        return "unknown"
    try:
        os.kill(pid, 0)
        return "alive"
    except ProcessLookupError:
        return "dead"  # ESRCH — the host process is gone → window confirmed closed
    except PermissionError:
        return "alive"  # EPERM — the process EXISTS (other uid) → still alive
    except OSError:
        return "unknown"  # any other kill error → fail-closed


def _terminate_pending_state(
    cfg: _config.Config, project: str, task: str, request_id: str, run_id: str, pf: Path
) -> None:
    """Common terminal cleanup for a resolved pending: drop the extension ack + the
    pending state file, finalize the (possibly multi-member) request, release the lock."""
    with contextlib.suppress(OSError):
        ack_file_path(cfg, project, task).unlink()
    with contextlib.suppress(OSError):
        pf.unlink()
    _maybe_finalize_after_member(cfg, project, request_id, run_id)
    _release_lock(cfg, project)


def _resolve_close_issued(
    cfg: _config.Config,
    project: str,
    task: str,
    *,
    run_id: str,
    request_id: str,
    pending: dict,
    pf: Path,
) -> bool:
    """Method D terminal arbitration after a ``close_issued`` ack. The extension closed
    the tabs + issued ``workbench.action.closeWindow`` — but closeWindow kills the
    extension host, so the extension cannot itself confirm the window died. The producer
    owns the terminal ``done``: independently verify the window's host PID physically left
    the process table before reclaiming the worktree (C7: never delete on a close intent).

    Returns True iff the run reached a TERMINAL state THIS tick (``done`` or fail-closed —
    markers written, ack+pending cleaned, request finalized, lock released). Returns False
    iff the window's host is still alive AND within the deadline (the caller renews the
    lock and keeps the cross-tick pending so the next tick re-checks)."""
    host = _read_json(host_pid_path(cfg, project, task))
    liveness = _host_pid_liveness(host, pending.get("nonce"))

    if liveness == "dead":
        removal = _reclaim_worktree_resources(
            cfg, project, task, pending.get("path", "merged"), pending.get("evidence", {})
        )
        _write_done(
            cfg,
            project,
            task,
            run_id=run_id,
            payload={
                **pending.get("evidence", {}),
                **removal,
                "nonce": pending.get("nonce"),
                "window_close_confirmed": "host-pid-gone",
            },
        )
        _terminate_pending_state(cfg, project, task, request_id, run_id, pf)
        return True

    if liveness == "unknown":
        _write_failed(
            cfg,
            project,
            task,
            reason=REASON_WINDOW_CLOSE_UNCONFIRMED,
            run_id=run_id,
            detail="close_issued but host_pid token missing / nonce-mismatch / unreadable "
            "— cannot confirm the window closed; worktree retained",
        )
        _terminate_pending_state(cfg, project, task, request_id, run_id, pf)
        return True

    # alive → bounded by the same ack-window deadline as the close_issued wait.
    deadline = pending.get("deadline_epoch")
    if not isinstance(deadline, (int, float)) or time.time() > deadline:
        _write_failed(
            cfg,
            project,
            task,
            reason=REASON_WINDOW_CLOSE_UNCONFIRMED,
            run_id=run_id,
            detail="close_issued but the window's host process is still alive at the "
            "deadline — window not confirmed closed; worktree retained",
        )
        _terminate_pending_state(cfg, project, task, request_id, run_id, pf)
        return True

    return False  # still alive, within the deadline → caller renews + re-polls next tick


# ─── pending resolution (tick N+1+) ──────────────────────────────────────────────


def _resolve_pending(cfg: _config.Config, project: str, pf: Path) -> bool:
    """Process one ``reclaim_pending`` state file. Returns True iff still pending
    (lock held + renewed; the caller must NOT start new request processing)."""
    pending = _read_json(pf)
    if not pending or not isinstance(pending.get("run_id"), str):
        _log(f"{project}: corrupt pending {pf.name} — dropped (lock left to TTL)")
        with contextlib.suppress(OSError):
            pf.unlink()
        return False
    run_id = pending["run_id"]
    task = pending.get("task") or pf.name.removesuffix(".reclaim_pending.json")
    request_id = pending.get("request_id") or task

    if not _we_own_lock(cfg, project, run_id):
        # TTL reaped the lock (crashed hold) or a rival owns it: the close window is
        # gone — treat the pending state as stale (contract C6) and never touch the
        # lock another holder may own now.
        _write_failed(
            cfg,
            project,
            task,
            reason=REASON_ACK_TIMEOUT,
            run_id=run_id,
            detail="lock lost across ticks (TTL reaped / rival holder) — pending treated stale",
        )
        with contextlib.suppress(OSError):
            pf.unlink()
        return False

    ack = _read_json(ack_file_path(cfg, project, task))
    if ack and ack.get("run_id") == run_id:
        result = ack.get("result")
        if result == "close_issued":
            # Method D: the ack is a close INTENT, not a terminal done. Enter the PID
            # dead-man phase — only confirm-then-reclaim once the window's host is gone.
            # If still alive within the deadline, keep the ack+pending and re-poll.
            if _resolve_close_issued(
                cfg, project, task, run_id=run_id, request_id=request_id,
                pending=pending, pf=pf,
            ):
                return False  # terminal (done / fail-closed) handled inside
            _renew_lock(cfg, project, run_id)  # host still alive, within deadline → hold
            return True
        # Any non-close_issued ack is an extension FAILURE ack (the extension never writes
        # a terminal done — the producer owns it). Map to the enum + record terminal.
        reason = ack.get("reason")
        detail = str(ack.get("detail", ""))
        if reason not in _EXTENSION_ACK_REASONS:
            detail = f"extension ack reason {reason!r} not in enum; {detail}".strip("; ")
            reason = REASON_ACK_TIMEOUT
        _write_failed(cfg, project, task, reason=reason, run_id=run_id, detail=detail)
        _terminate_pending_state(cfg, project, task, request_id, run_id, pf)
        return False

    deadline = pending.get("deadline_epoch")
    if not isinstance(deadline, (int, float)) or time.time() > deadline:
        _write_failed(
            cfg,
            project,
            task,
            reason=REASON_ACK_TIMEOUT,
            run_id=run_id,
            detail="no extension ack before deadline — NOT assuming the window closed",
        )
        with contextlib.suppress(OSError):
            pf.unlink()
        _maybe_finalize_after_member(cfg, project, request_id, run_id)
        _release_lock(cfg, project)
        return False

    _renew_lock(cfg, project, run_id)  # still within the ack window → hold across ticks
    return True


def _maybe_finalize_after_member(
    cfg: _config.Config, project: str, request_id: str, run_id: str
) -> None:
    members, is_wave, _detail = _resolve_members(cfg, project, request_id)
    if members is None:
        # Single-task request whose sidecar got cleaned between ticks: the member's own
        # terminal marker already stands; just consume the sentinel.
        _consume_sentinel(cfg, project, request_id, run_id)
        return
    _finalize_request_if_terminal(cfg, project, request_id, run_id, members, is_wave)


# ─── request processing (tick N) ─────────────────────────────────────────────────


def _process_request(
    cfg: _config.Config,
    project: str,
    sentinel: Path,
    probe: TranscriptProbeAdapter | None,
) -> None:
    request_id = sentinel.stem
    data = _read_json(sentinel)
    run_id = data.get("run_id") if data else None
    ts = data.get("ts") if data else None
    if not (isinstance(run_id, str) and _HEX16_RE.match(run_id) and isinstance(ts, str)):
        _write_failed(
            cfg,
            project,
            request_id,
            reason=REASON_STALE_REQUEST,
            run_id=run_id if isinstance(run_id, str) else "invalid",
            detail="malformed requested sentinel (need JSON {run_id: hex16, ts: ISO})",
        )
        _consume_sentinel(cfg, project, request_id, run_id if isinstance(run_id, str) else "")
        return

    # C2: sentinel age-out (one-time alert via consumption — never re-alerts).
    try:
        age = time.time() - sentinel.stat().st_mtime
    except OSError:
        return
    if age > STALE_REQUEST_SECONDS:
        _write_failed(
            cfg,
            project,
            request_id,
            reason=REASON_STALE_REQUEST,
            run_id=run_id,
            detail=f"requested sentinel older than 24h ({age:.0f}s)",
        )
        _consume_sentinel(cfg, project, request_id, run_id)
        return

    if _backoff_active(cfg, project):
        return  # offline backoff window — no fetch storm, sentinel stays

    if not _acquire_lock(cfg, project, run_id):
        _log(f"{project}/{request_id}: spawn lock held — skip this tick")
        return

    hold = False
    try:
        if not sentinel.exists():  # consumed by a rival tick between scan and lock
            return
        # Replay guard (codex Q2): the same run_id already reached done → no-op.
        done = _read_json(done_path(cfg, project, request_id))
        if done and done.get("run_id") == run_id:
            _consume_sentinel(cfg, project, request_id, run_id)
            return

        members, is_wave, detail = _resolve_members(cfg, project, request_id)
        if members is None:
            _write_failed(
                cfg,
                project,
                request_id,
                reason=REASON_MANIFEST_MISSING,
                run_id=run_id,
                detail=detail,
            )
            _consume_sentinel(cfg, project, request_id, run_id)
            return
        if is_wave:
            _audit_late_adds(cfg, project, request_id, members, run_id)

        if _finalize_request_if_terminal(cfg, project, request_id, run_id, members, is_wave):
            return

        # One member per tick: deterministic, bounded, and the single pending state
        # matches the single project lock.
        member = next(
            m
            for m in members
            if _member_terminal(cfg, project, m["task_id"], run_id) is None
        )
        task = member["task_id"]
        verdict = _evaluate_member(cfg, project, task, member.get("spawn_nonce"), probe)

        if not verdict.ok:
            if verdict.reason == REASON_REF_FETCH_FAILED:
                # Transient/offline: record the reason (visible) but NON-terminal —
                # the sentinel stays and the exponential backoff dampens retries.
                _write_failed(
                    cfg,
                    project,
                    task,
                    reason=REASON_REF_FETCH_FAILED,
                    run_id=run_id,
                    detail=verdict.detail,
                    terminal=False,
                )
                _bump_backoff(cfg, project)
                return
            _write_failed(
                cfg,
                project,
                task,
                reason=verdict.reason or REASON_ROLE_REASON_REJECTED,
                run_id=run_id,
                detail=verdict.detail,
            )
            _finalize_request_if_terminal(cfg, project, request_id, run_id, members, is_wave)
            return

        _reset_backoff(cfg, project)
        # All gates passed under the lock → write the close AUTHORIZATION (pending) and
        # transition to the cross-tick pending state WITHOUT releasing (C6 TOCTOU
        # closure). No sleep, NO push: the producer no longer ``open vscode://…`` (an
        # untargetable URI — A-poll revision 2026-06-12). The TARGET window's extension
        # polls this very file, rebuilds the same params, and self-closes — so window
        # targeting is intrinsic regardless of which desktop the window is on. The
        # pending therefore carries the full close-param set the extension reconstructs:
        # role/reason (the C7 worker×reclaim row), nonce (C3 auth, self-targeting
        # against the window title), run_id + issued_at + ack_timeout (C3 freshness,
        # reused via the extension's effectiveAckTimeoutMs — a poll past issued_at +
        # ack_timeout is rejected close-command-expired, so a stale pending can never
        # close a window a new spawn now occupies).
        with contextlib.suppress(OSError):
            ack_file_path(cfg, project, task).unlink()  # clear any stale ack first
        issued_at = _now_iso()
        ack_timeout = min(cfg.reclaim_ack_timeout, EXT_ACK_TIMEOUT_CAP)
        atomic.atomic_replace(
            pending_path(cfg, project, task),
            json.dumps(
                {
                    "run_id": run_id,
                    "request_id": request_id,
                    "task": task,
                    "project": project,
                    "role": ROLE_WORKER,
                    "reason": RECLAIM_REASON,
                    "nonce": verdict.nonce,
                    "ack_timeout": ack_timeout,
                    "path": verdict.path,
                    "issued_at": issued_at,
                    "deadline_epoch": time.time() + ack_timeout,
                    "evidence": verdict.evidence,
                }
            )
            + "\n",
        )
        _renew_lock(cfg, project, run_id)
        hold = True  # ← the ONE exit that keeps the lock (cross-tick state machine)
        _log(f"{project}/{task}: reclaim_pending written run={run_id}; awaiting poll ack/deadline")
    finally:
        if not hold:
            _release_lock(cfg, project)


# ─── tick entry (called from watchdog.main every ~60s) ───────────────────────────


def tick(
    cfg: _config.Config | None = None, probe: TranscriptProbeAdapter | None = None
) -> int:
    """One watchdog reclaim pass over all projects. Returns the number of projects
    with reclaim activity. NEVER sleeps; never sweeps without a requested sentinel."""
    cfg = cfg or _config.load()
    active = 0
    if not cfg.home.exists():
        return 0
    for proj_dir in sorted(cfg.home.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name in {"locks", "_recovery"}:
            continue
        project = proj_dir.name
        if (proj_dir / "STOP_AUTO").exists():
            continue
        ack = cfg.ack_dir(project)
        if not ack.is_dir():
            continue
        try:
            pendings = sorted(ack.glob("*.reclaim_pending.json"))
            still_pending = False
            for pf in pendings:
                if _resolve_pending(cfg, project, pf):
                    still_pending = True
                active += 1
            if still_pending:
                continue  # the lock is held by our pending close — no new work
            requests = sorted(ack.glob("*.reclaim_requested"))
            if not requests:
                continue
            _process_request(cfg, project, requests[0], probe)
            active += 1
        except Exception as e:  # one project's failure must not starve the others
            print(f"[reclaim] {project} error: {e}", file=sys.stderr)
    return active


# ─── CLI: handoff worktree {abandon,record-head,wave-freeze,reclaim-request,reclaim-report}


def _actor() -> str:
    role = os.environ.get("HANDOFF_SESSION_ROLE")
    user = os.environ.get("USER", "unknown")
    return f"{user}/{role}" if role else user


def cli_abandon(argv: list[str] | None = None) -> int:
    """``handoff worktree abandon <task> --project P --reason "..."`` — the ONLY way a
    worktree becomes ``abandoned`` (C4). Writes the AUTHORITATIVE record to the
    control plane (``<project>/abandoned/<task>.json``) + an in-worktree audit copy.
    Never deletes anything; it only grants reclaim eligibility."""
    ap = argparse.ArgumentParser(prog="handoff worktree abandon")
    ap.add_argument("task")
    ap.add_argument("--project", required=True)
    ap.add_argument("--reason", required=True)
    args = ap.parse_args(argv)
    cfg = _config.load()
    if not _SLUG_RE.match(args.project) or not _SLUG_RE.match(args.task):
        print("❌ project/task must be kebab-case slugs", file=sys.stderr)
        return 2
    if not args.reason.strip():
        print("❌ --reason must be non-empty", file=sys.stderr)
        return 2
    wt = _worktree.worktree_path(cfg, args.project, args.task)
    if not wt.exists():
        print(f"❌ worktree not found: {wt} — nothing to abandon", file=sys.stderr)
        return 2
    head = _worktree.head_sha(wt)
    if not head:
        print(f"❌ cannot resolve worktree HEAD in {wt}", file=sys.stderr)
        return 2
    out = abandoned_dir(cfg, args.project) / f"{args.task}.json"
    if out.exists():
        print(f"❌ already abandoned: {out} (records are immutable)", file=sys.stderr)
        return 2
    sidecar = _read_sidecar(cfg, args.project, args.task) or {}
    record = {
        "task": args.task,
        "reason": args.reason.strip(),
        "ts": _now_iso(),
        "actor": _actor(),
        "branch_head_sha": head,
        "worktree_path": str(wt),
        "wave_id": sidecar.get("wave_id"),
        "spawn_nonce": sidecar.get("spawn_nonce"),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(record, indent=2) + "\n"
    # O_EXCL first-writer-wins (mirrors the manifest discipline).
    if not atomic.atomic_create(out):
        print(f"❌ already abandoned: {out}", file=sys.stderr)
        return 2
    atomic.write_with_fsync(out, content)
    with contextlib.suppress(OSError):  # audit copy — best-effort, NON-authoritative
        (wt / ".handoff-abandoned.json").write_text(content, encoding="utf-8")
    print(f"✅ abandoned {args.project}/{args.task} @ {head[:8]} → {out}")
    return 0


def cli_record_head(argv: list[str] | None = None) -> int:
    """``handoff worktree record-head <task> --project P`` — record the worker's
    closing head SHA as ``ack/<task>.head.json`` (the C1 merged-path pinned-SHA
    evidence channel; cross-validated against the actual worktree HEAD at reclaim)."""
    ap = argparse.ArgumentParser(prog="handoff worktree record-head")
    ap.add_argument("task")
    ap.add_argument("--project", required=True)
    args = ap.parse_args(argv)
    cfg = _config.load()
    if not _SLUG_RE.match(args.project) or not _SLUG_RE.match(args.task):
        print("❌ project/task must be kebab-case slugs", file=sys.stderr)
        return 2
    wt = _worktree.worktree_path(cfg, args.project, args.task)
    head = _worktree.head_sha(wt) if wt.exists() else None
    if not head:
        print(f"❌ cannot resolve worktree HEAD ({wt})", file=sys.stderr)
        return 2
    ack = cfg.ack_dir(args.project)
    ack.mkdir(parents=True, exist_ok=True)
    atomic.atomic_replace(
        ack / f"{args.task}.head.json",
        json.dumps({"task": args.task, "head_sha": head, "recorded_at": _now_iso(),
                    "actor": _actor()})
        + "\n",
    )
    print(f"✅ recorded {args.project}/{args.task} head {head[:8]}")
    return 0


def cli_wave_freeze(argv: list[str] | None = None) -> int:
    """``handoff worktree wave-freeze --project P --wave-id W --members a,b,c`` —
    atomically (O_EXCL) freeze the wave membership manifest from the members'
    already-published sidecars (C5). Re-dispatch = a NEW wave id, never an edit."""
    ap = argparse.ArgumentParser(prog="handoff worktree wave-freeze")
    ap.add_argument("--project", required=True)
    ap.add_argument("--wave-id", required=True, dest="wave_id")
    ap.add_argument("--members", required=True, help="comma-separated task ids")
    args = ap.parse_args(argv)
    cfg = _config.load()
    if not _SLUG_RE.match(args.project) or not _SLUG_RE.match(args.wave_id):
        print("❌ project/wave-id must be kebab-case slugs", file=sys.stderr)
        return 2
    member_ids = [m.strip() for m in args.members.split(",") if m.strip()]
    if not member_ids:
        print("❌ --members must list at least one task id", file=sys.stderr)
        return 2
    members: list[dict] = []
    for tid in member_ids:
        if not _SLUG_RE.match(tid):
            print(f"❌ member must be a kebab-case slug: {tid!r}", file=sys.stderr)
            return 2
        sidecar = _read_sidecar(cfg, args.project, tid)
        nonce = sidecar.get("spawn_nonce") if sidecar else None
        if not (isinstance(nonce, str) and _HEX16_RE.match(nonce)):
            print(
                f"❌ member {tid}: no valid spawn_nonce sidecar — dispatch it first "
                "(the manifest freezes dispatch-time identity)",
                file=sys.stderr,
            )
            return 2
        members.append({"task_id": tid, "spawn_nonce": nonce})
    out = waves_dir(cfg, args.project) / f"{args.wave_id}.manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not atomic.atomic_create(out):  # O_EXCL: a frozen wave is immutable
        print(f"❌ wave already frozen: {out} (补派 = a NEW wave id)", file=sys.stderr)
        return 2
    atomic.write_with_fsync(
        out,
        json.dumps(
            {"wave_id": args.wave_id, "frozen_ts": _now_iso(), "members": members}, indent=2
        )
        + "\n",
    )
    print(f"✅ wave {args.wave_id} frozen with {len(members)} member(s) → {out}")
    return 0


def cli_reclaim_request(argv: list[str] | None = None) -> int:
    """``handoff worktree reclaim-request <task|wave-id> --project P`` — the
    coordinator's explicit intent sentinel (C2). The watchdog acts ONLY on this."""
    ap = argparse.ArgumentParser(prog="handoff worktree reclaim-request")
    ap.add_argument("request_id", help="task id, or a frozen wave id")
    ap.add_argument("--project", required=True)
    args = ap.parse_args(argv)
    cfg = _config.load()
    if not _SLUG_RE.match(args.project) or not _SLUG_RE.match(args.request_id):
        print("❌ project/request-id must be kebab-case slugs", file=sys.stderr)
        return 2
    out = requested_path(cfg, args.project, args.request_id)
    if out.exists():
        print(f"❌ a reclaim request is already pending: {out}", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    run_id = secrets.token_hex(8)
    atomic.atomic_replace(out, json.dumps({"run_id": run_id, "ts": _now_iso()}) + "\n")
    print(f"✅ reclaim requested: {args.project}/{args.request_id} run={run_id}")
    return 0


def cli_reclaim_report(argv: list[str] | None = None) -> int:
    """``handoff worktree reclaim-report [--project P]`` — READ-ONLY zombie-window
    patrol (gemini SHOULD): lists reclaim candidates + their current blocking gate
    (no lock, no fetch, no URI — purely informational), plus stuck waves (>7d with
    non-terminal members → ``reclaim-blocked``)."""
    ap = argparse.ArgumentParser(prog="handoff worktree reclaim-report")
    ap.add_argument("--project", default=None)
    args = ap.parse_args(argv)
    cfg = _config.load()
    projects = (
        [args.project]
        if args.project
        else sorted(
            p.name
            for p in (cfg.home.iterdir() if cfg.home.exists() else [])
            if p.is_dir() and p.name not in {"locks", "_recovery"}
        )
    )
    any_line = False
    for project in projects:
        queue = cfg.queue_dir(project)
        ack = cfg.ack_dir(project)
        if queue.is_dir():
            for sc in sorted(queue.glob("*.singlepane")):
                data = _read_json(sc)
                if not data or data.get("isolation") != "worktree" or data.get("role") != "worker":
                    continue
                task = sc.stem
                if done_path(cfg, project, task).exists():
                    continue
                wt = _worktree.worktree_path(cfg, project, task)
                if not wt.exists():
                    continue
                blockers: list[str] = []
                failed = _read_json(failed_path(cfg, project, task))
                if failed:
                    blockers.append(f"last-fail={failed.get('reason')}")
                pinned, _src = _pinned_sha(cfg, project, task)
                if pinned is None:
                    blockers.append("no recorded head SHA")
                control = _read_json(abandoned_dir(cfg, project) / f"{task}.json")
                if control:
                    blockers.append("abandoned(control-plane)")
                if _worktree.is_dirty(wt, ignore=_worktree._link_names(cfg)):
                    blockers.append("dirty")
                if not requested_path(cfg, project, task).exists():
                    blockers.append("no reclaim_requested sentinel")
                print(f"  {project}/{task}  {wt}  [{'; '.join(blockers) or 'ready to request'}]")
                any_line = True
        wdir = waves_dir(cfg, project)
        if wdir.is_dir():
            for mf in sorted(wdir.glob("*.manifest.json")):
                wave_id = mf.name.removesuffix(".manifest.json")
                if done_path(cfg, project, wave_id).exists():
                    continue
                try:
                    age = time.time() - mf.stat().st_mtime
                except OSError:
                    continue
                members = load_manifest(cfg, project, wave_id) or []
                open_members = [
                    m["task_id"]
                    for m in members
                    if not done_path(cfg, project, m["task_id"]).exists()
                ]
                if age > STUCK_WAVE_SECONDS and open_members:
                    print(
                        f"  {project}/{wave_id}  reclaim-blocked: wave frozen "
                        f"{age / 86400:.1f}d ago, open members: {', '.join(open_members)}"
                    )
                    any_line = True
        if ack.is_dir():
            for sent in sorted(ack.glob("*.reclaim_requested")):
                print(f"  {project}/{sent.stem}  requested (awaiting watchdog tick)")
                any_line = True
    if not any_line:
        print("[reclaim-report] no reclaim candidates / requests / stuck waves.")
    return 0


# Re-exported for shell completeness checks (`handoff worktree --help` discovery).
__all__ = [
    "REASONS",
    "tick",
    "cli_abandon",
    "cli_record_head",
    "cli_wave_freeze",
    "cli_reclaim_request",
    "cli_reclaim_report",
    "TranscriptProbeAdapter",
    "transcript_project_dir_name",
]
