"""req3 — the SAFETY GATE for auto-closing audited worker windows.

🔴🔴 SAFETY-CRITICAL. Closing a window ENDS its session (low reversibility): one
mis-close = the owner loses an un-reviewed delivery. The supreme rule is **宁可漏关，
绝不误关** (rather miss a closeable window than ever close a wrong one). Every predicate
is FAIL-SAFE: any uncertainty (missing/unreadable artifact, git unreachable, unparseable
identity, ANY exception) ⇒ DON'T close.

This module is the pure, importable, fully-testable DECISION layer. The runtime driver
(``~/.claude-handoff/supervisor-monitor/autoclose-audited-workers.py``) does the I/O
(winlist detection + invoking ``coord-close-windows.py``); ALL the dangerous judgement
lives here so it can be unit-tested without a GUI.

A worker window auto-closes ⟺ ``gate_task`` returns ``close_ok`` — i.e. ALL 5 predicates
hold (spec req3 §1):

  1. **worker, NOT coordinator** — the ``queue/<task>.singlepane`` sidecar's ``role``
     is ``worker``. A coordinator window is NEVER auto-closed (the close tool also
     hard-protects 🧭 / sw-coord — defense in depth).
  2. **STRONG audited-to-terminal signal, NON-FORGEABLE (P0-1 + P0-2)** — a
     coordinator-written ``ack/<task>.audit_discharged`` (verdict GREEN) is honored ONLY
     when corroborated by git: ``merge_sha`` is a NON-TRIVIAL real ancestor of the
     integration branch (in-section ``git fetch`` then ``merge-base --is-ancestor``),
     ``merge_sha != spawn_base`` (kills the vacuous-ancestor hole P0-2), the recorded
     ``worktree_head == merge_sha`` (no drift), and the LIVE worktree HEAD == ``merge_sha``
     (the worker-writable signal is EVIDENCE, never authority — mirrors
     ``reclaim.py`` codex-M2/M3). A worktree already reclaimed must carry a
     ``reclaim_done`` (the §6c machinery already proved it merged + removed it).
     ⛔ Worker self-report (``.worker_reported``/``.submitted``/``.done``) is NEVER a
     trigger; bare ``merge-base --is-ancestor`` is NEVER a standalone signal.
  3. **not in-flight (P0-3)** — the newest transcript's last *conversation* entry kind is
     a SETTLED ``assistant_turn`` AND its idle ≥ threshold. ``running_tool`` /
     ``blocked_on_question`` / ``dangling_tool_result`` / ``none`` / unmapped /
     idle < 0 / unreadable ⇒ REFUSE (the probe's ``idle_long`` fail-OPEN is deliberately
     NOT reused: an AI-titled/unmapped window must never read as idle-enough).
  4. **not dirty** — the live worktree has no uncommitted/untracked changes (the close
     tool additionally never force-closes a save-prompt-blocked window).
  5. **fail-safe** — git unreachable / transcript unreadable / identity unparseable /
     ANY exception ⇒ DON'T close.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeGuard

from handoff_fanout import codex_audit
from handoff_fanout import config as _config
from handoff_fanout import reclaim as _reclaim
from handoff_fanout import worktree as _worktree

# The worker idle floor (spec §1.3): a worker default of 30 min, conservative 60 min.
DEFAULT_IDLE_THRESHOLD_SEC = 1800

# The ONLY last-conversation kind that counts as SETTLED (safe to consider idle). Every
# other kind from last_entry_kind.py's taxonomy is treated as not-settled (REFUSE):
#   running_tool / blocked_on_question — actively working / awaiting the owner;
#   dangling_tool_result — suspected stall OR throttle (could resume) → never close;
#   none / other — no conversation entry / unrecognized → fail-safe.
SETTLED_KIND = "assistant_turn"

CLOSE_OK = "ok"

_GIT_SHA_RE = codex_audit._GIT_SHA_RE  # 7-40 lowercase hex
_HEX16_RE = _reclaim._HEX16_RE  # 16 lowercase hex spawn nonce


@dataclass
class GateDecision:
    """The verdict for one worker window. ``close_ok`` is True ONLY when every predicate
    holds; otherwise it is a FAIL-SAFE refusal carrying a machine ``reason`` + ``detail``.
    """

    close_ok: bool
    reason: str  # machine token; ``CLOSE_OK`` when close_ok, else a refusal token
    detail: str = ""
    nonce: str | None = None  # the worker's spawn nonce (close-token binding) when close_ok
    evidence: dict = field(default_factory=dict)


def _refuse(reason: str, detail: str = "") -> GateDecision:
    return GateDecision(close_ok=False, reason=reason, detail=detail)


def _is_sha(v: object) -> TypeGuard[str]:
    return isinstance(v, str) and bool(_GIT_SHA_RE.match(v))


# ─── discovery (for --sweep) ─────────────────────────────────────────────────


def discharged_tasks(cfg: _config.Config, project: str) -> list[str]:
    """Sorted task ids that have an ``ack/<task>.audit_discharged`` signal (candidates
    the gate must still individually clear). Never raises — an unreadable ack dir → []."""
    ack = cfg.ack_dir(project)
    try:
        return sorted(p.name[: -len(".audit_discharged")] for p in ack.glob("*.audit_discharged"))
    except OSError:
        return []


# ─── predicate 1 + 2 (local half): identity + signal shape + non-vacuous ─────


@dataclass
class _LocalOK:
    merge_sha: str
    base_sha: str
    nonce: str


def _corroborate_local(cfg: _config.Config, project: str, task: str) -> GateDecision | _LocalOK:
    """The LOCAL (no-network) half of the signal corroboration: read the signal, prove
    it is well-formed + GREEN + worker-owned + non-vacuous. Returns ``_LocalOK`` (carry
    values for the git half) or a FAIL-SAFE ``GateDecision`` refusal."""
    ack = cfg.ack_dir(project)
    sig = _reclaim._read_json(ack / f"{task}.audit_discharged")
    if not sig:
        # ⛔ No strong signal. Weak self-reports (.worker_reported/.submitted/.done) are
        # NEVER a close trigger — their absence-of-a-real-signal is exactly this refusal.
        return _refuse("no-signal", "ack/<task>.audit_discharged missing/unreadable")
    if sig.get("schema_version") != codex_audit.AUDIT_DISCHARGED_SCHEMA_VERSION:
        return _refuse(
            "signal-schema-unknown",
            f"audit_discharged schema_version {sig.get('schema_version')!r} unsupported (fail-closed)",
        )
    if sig.get("verdict") != "GREEN":  # predicate 2(i)
        return _refuse("verdict-not-green", f"verdict={sig.get('verdict')!r}")
    merge_sha = sig.get("merge_sha")
    if not _is_sha(merge_sha):
        return _refuse("merge-sha-malformed", f"merge_sha={merge_sha!r}")
    wt_head_rec = sig.get("worktree_head")
    if not _is_sha(wt_head_rec):
        return _refuse("worktree-head-malformed", f"worktree_head={wt_head_rec!r}")
    if wt_head_rec != merge_sha:  # P0-1(iv): recorded head must equal the merged commit
        return _refuse(
            "head-drift", f"recorded worktree_head {wt_head_rec[:8]} != merge_sha {merge_sha[:8]}"
        )

    # predicate 1: worker, NOT coordinator — authoritative role from the spawn sidecar.
    sidecar = _reclaim._read_json(cfg.queue_dir(project) / f"{task}.singlepane")
    if not sidecar:
        return _refuse("sidecar-missing", "queue/<task>.singlepane missing — can't confirm worker role")
    if sidecar.get("role") != "worker":
        return _refuse(
            "not-worker",
            f"sidecar role={sidecar.get('role')!r} — NEVER auto-close a non-worker/coordinator window",
        )
    nonce = sidecar.get("spawn_nonce")
    if not isinstance(nonce, str) or not _HEX16_RE.match(nonce):
        return _refuse("nonce-malformed", f"sidecar spawn_nonce={nonce!r}")
    sig_nonce = sig.get("nonce")
    if sig_nonce is not None and sig_nonce != nonce:
        return _refuse("nonce-mismatch", "signal nonce != sidecar spawn_nonce")

    # P0-2 base anchor: the spawn-time ack/<task>.worktree base_sha — written BEFORE the
    # worker ran, INDEPENDENT of the worker-writable signal (mirrors reclaim codex-M3:
    # corroborate the marker against a control-plane/spawn artifact, never trust it alone).
    wtinfo = _reclaim._read_json(ack / f"{task}.worktree")
    base_sha = wtinfo.get("base_sha") if isinstance(wtinfo, dict) else None
    if not _is_sha(base_sha):
        return _refuse(
            "base-anchor-missing",
            "ack/<task>.worktree base_sha missing/malformed — can't prove a non-trivial "
            "advance (P0-2 fail-safe)",
        )
    if merge_sha == base_sha:  # P0-2: empty/just-spawned branch is vacuously an ancestor
        return _refuse(
            "vacuous-no-advance",
            f"merge_sha == spawn base {base_sha[:8]} — branch never advanced (vacuous-ancestor)",
        )
    return _LocalOK(merge_sha=merge_sha, base_sha=base_sha, nonce=nonce)


# ─── predicate 2 (git half) + 4: ancestry + live-head drift + dirty ──────────


def _corroborate_git(cfg: _config.Config, project: str, task: str, local: _LocalOK) -> GateDecision:
    """The NETWORK half: in-section fetch + non-trivial ancestry + the live-worktree
    drift / dirty cross-checks (mirror reclaim ``_evaluate_merged``). Returns the final
    ``close_ok`` decision or a FAIL-SAFE refusal."""
    ack = cfg.ack_dir(project)
    merge_sha, base_sha = local.merge_sha, local.base_sha

    repo: Path | None = cfg.workspace_root / project
    if not _worktree.is_git_repo(repo):
        wt = _worktree.worktree_path(cfg, project, task)
        repo = wt if _worktree.is_git_repo(wt) else None
    if repo is None:
        return _refuse("repo-unresolvable", "neither main repo nor worktree is a git repo (fail-safe)")

    int_branch = _worktree.resolve_integration_branch(repo, cfg, allow_network=False)
    if not int_branch:
        return _refuse("int-branch-unresolvable", "integration branch unresolvable (fail-safe)")
    remote = _reclaim._resolve_canonical_remote(cfg, repo, int_branch)
    if not remote:
        return _refuse("remote-unresolvable", "canonical remote unresolvable (fail-safe)")

    # In-section fetch with an EXPLICIT refspec so the tracking ref we read IS the one this
    # fetch updated (mirror reclaim C1 / codex M1). git unreachable ⇒ predicate 5 fail-safe.
    tracking = f"refs/remotes/{remote}/{int_branch}"
    rc, _, err = _worktree._git(
        ["fetch", remote, f"+refs/heads/{int_branch}:{tracking}"], repo, timeout=30.0
    )
    if rc != 0:
        return _refuse(
            "ref-fetch-failed", f"git fetch failed: {err[:120]} (git unreachable → don't close)"
        )
    canon = _worktree.head_sha_of_ref(repo, tracking)
    if not canon:
        return _refuse("int-head-unresolvable", f"{tracking} unresolvable after fetch")

    # P0-2: merge_sha must DESCEND from the spawn base (a real branch advance) — rejects a
    # forged ``merge_sha`` pointing at some arbitrary OLD commit already on the branch.
    if not _worktree.is_ancestor(repo, base_sha, merge_sha):
        return _refuse(
            "base-not-ancestor-of-merge",
            f"spawn base {base_sha[:8]} is not an ancestor of merge_sha {merge_sha[:8]} "
            "(not a real advance)",
        )
    # P0-1(ii): merge_sha must be a real ancestor of the integration branch (= merged).
    if not _worktree.is_ancestor(repo, merge_sha, canon):
        return _refuse(
            "not-merged",
            f"merge_sha {merge_sha[:8]} not an ancestor of {remote}/{int_branch} — NOT merged",
        )

    # STRONG anti-forge cross-check against terminal REALITY (mirror reclaim codex-M2):
    # the recorded SHA is worker-reported evidence; bind it to the worktree's ACTUAL HEAD.
    wt = _worktree.worktree_path(cfg, project, task)
    if _worktree.is_git_repo(wt):
        live = _worktree.head_sha(wt)
        if live != merge_sha:
            return _refuse(
                "worktree-live-head-drift",
                f"live worktree HEAD {str(live)[:8]} != merge_sha {merge_sha[:8]} "
                "(recorded SHA is not the worktree's real head — forge/drift)",
            )
        # predicate 4: dirty (data-level defense-in-depth; engine-injected links discounted
        # exactly like reclaim's dirty gate). The close tool ALSO blocks on the save prompt.
        if _worktree.is_dirty(wt, ignore=_worktree._link_names(cfg) | {".handoff-abandoned.json"}):
            return _refuse("dirty", "worktree has uncommitted/untracked changes — NEVER close")
    else:
        # Worktree GONE → require the §6c reclaim to have already VERIFIED merged + removed
        # it (the terminal proof). No reclaim_done ⇒ anomalous removal ⇒ fail-safe.
        if not (ack / f"{task}.reclaim_done").exists():
            return _refuse(
                "worktree-gone-no-reclaim-proof",
                "worktree removed but no ack/<task>.reclaim_done — can't confirm terminal (fail-safe)",
            )

    return GateDecision(
        close_ok=True,
        reason=CLOSE_OK,
        nonce=local.nonce,
        evidence={"merge_sha": merge_sha, "base_sha": base_sha, "canonical_int_sha": canon},
    )


# ─── predicate 3: not in-flight (idle + settled kind) ────────────────────────


def _last_conv_kind_and_idle(path: Path, now_epoch: float) -> tuple[str, int]:
    """Last CONVERSATION entry's semantic kind + its idle seconds (mirrors the canonical
    ``supervisor-monitor/last_entry_kind.py`` — vendored here so the gate is self-contained
    + pytest-testable). Skips metadata entries (ai-title/system/…) which pollute file mtime;
    keys idle off the conversation entry's own timestamp. ``-1`` idle when no timestamp."""
    last_conv: dict | None = None
    try:
        with path.open(encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(o, dict) and o.get("type") in ("assistant", "user"):
                    last_conv = o  # skip metadata: only conversation entries count
    except OSError:
        return "none", -1
    if not isinstance(last_conv, dict):
        return "none", -1
    t = last_conv.get("type")
    msg = last_conv.get("message")
    m = msg if isinstance(msg, dict) else {}
    c = m.get("content")
    if t == "user" and isinstance(c, list) and any(
        isinstance(x, dict) and x.get("type") == "tool_result" for x in c
    ):
        kind = "dangling_tool_result"
    elif t == "assistant" and isinstance(c, list) and any(
        isinstance(x, dict) and x.get("type") == "tool_use" for x in c
    ):
        names = [
            (x.get("name") or "")
            for x in c
            if isinstance(x, dict) and x.get("type") == "tool_use"
        ]
        kind = (
            "blocked_on_question"
            if any("askuserquestion" in (n or "").lower() for n in names)
            else "running_tool"
        )
    elif t == "assistant":
        kind = "assistant_turn"
    else:
        kind = "other"
    idle = -1
    ts = last_conv.get("timestamp")
    if ts:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            idle = int(now_epoch - dt.timestamp())
        except (ValueError, TypeError):
            idle = -1
    return kind, idle


def _evaluate_idle(
    cfg: _config.Config,
    project: str,
    task: str,
    idle_threshold_sec: float,
    now_epoch: float,
    projects_root: Path,
) -> GateDecision | None:
    """P0-3: returns a FAIL-SAFE refusal when the window is in-flight / not idle-enough /
    unmapped / unreadable, else ``None`` (the idle predicate passes)."""
    try:
        wt = _worktree.worktree_path(cfg, project, task)
        tdir = projects_root / _reclaim.transcript_project_dir_name(wt)
        if not tdir.is_dir():
            # unmapped / no transcript dir → the probe's idle_long fail-OPENs here; we do NOT.
            return _refuse("transcript-dir-missing", f"no transcript dir {tdir} (unmapped → fail-safe)")
        jsonls = list(tdir.glob("*.jsonl"))
        if not jsonls:
            return _refuse("transcript-absent", "no *.jsonl transcript (fail-safe)")
        newest = max(jsonls, key=lambda p: p.stat().st_mtime)
        kind, conv_idle = _last_conv_kind_and_idle(newest, now_epoch)
    except OSError as e:
        return _refuse("idle-probe-error", f"transcript probe error: {e} (fail-safe)")
    if kind != SETTLED_KIND:
        return _refuse(f"in-flight:{kind}", f"last conversation kind {kind!r} is not settled")
    if conv_idle < 0:
        return _refuse("idle-unknown", "conv_idle unresolvable (no timestamp) — fail-safe")
    if conv_idle < idle_threshold_sec:
        return _refuse("not-idle-enough", f"conv_idle {conv_idle}s < threshold {int(idle_threshold_sec)}s")
    return None


# ─── the gate ────────────────────────────────────────────────────────────────


def gate_task(
    cfg: _config.Config,
    project: str,
    task: str,
    *,
    idle_threshold_sec: float = DEFAULT_IDLE_THRESHOLD_SEC,
    now_epoch: float | None = None,
    projects_root: Path | None = None,
) -> GateDecision:
    """Run the full 5-predicate fail-safe gate for ONE worker window.

    ``close_ok`` is True ⟺ EVERY predicate holds. Order is cheapest-first (local signal
    + identity, then the local idle probe, then the network git corroboration) — but the
    ANSWER is order-independent: any single predicate failing is a refusal. ANY unexpected
    exception is caught and turned into a refusal (predicate 5: never close on uncertainty).
    """
    if now_epoch is None:
        now_epoch = time.time()
    if projects_root is None:
        projects_root = Path.home() / ".claude" / "projects"
    try:
        local = _corroborate_local(cfg, project, task)
        if isinstance(local, GateDecision):  # a local refusal
            return local
        idle_refusal = _evaluate_idle(cfg, project, task, idle_threshold_sec, now_epoch, projects_root)
        if idle_refusal is not None:
            return idle_refusal
        return _corroborate_git(cfg, project, task, local)
    except Exception as e:  # ultimate fail-safe (predicate 5)
        return _refuse("gate-exception", f"unexpected gate error: {type(e).__name__}: {e}")
