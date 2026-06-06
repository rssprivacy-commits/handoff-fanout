"""S5a — minimal observable + rescuable status board (design §C17/§C18, slice S5a).

This is slice **S5a** of the centralized-supervisor orchestration redesign
(authoritative design: ``project-files/handoff/supervisor-orchestration-design.md``
in the ERP repo, §C17 StatusBoard + §C18 handoff-cli; route: OL-3 大纠偏 — INV-10
"人类可观可救" promoted to the lead invariant, done *before* the heavy Dispatcher/
rollback machinery so the owner can see + intervene as early as possible).

It answers, for a non-technical owner (and a human "supervisor" doing the rounds),
the four questions the bootstrap monitoring protocol
([[lesson-supervisor-monitoring-protocol-2026-06-06]]) had them run ``patrol.sh`` by
hand for:

  1. **What is really running right now?** (运行中)
  2. **Who delivered / who is stuck?** (已交付待审 / 已交付可关 / 卡住需介入)
  3. **Which windows can I close?** (``handoff sessions``)
  4. **How do I stop / pause / resume / approve?** (``handoff stop|pause|resume|approve``)

Data source = **Plan C hybrid** (codex+gemini full-power consensus + owner ruling C):

* **Primary view = the real handoff runtime (B-first / fact source).** It reads the
  on-disk ``~/.claude-handoff/<project>/`` runtime —
  ``queue/<task>.{md,uri,heartbeat,done,529-suspected}`` /
  ``queue/<task>.BLOCKED.md`` /
  ``ack/<task>.{spawned,submitted,worker_reported,failed,old_ready}`` /
  ``worktrees/<task>/`` / the worker's transcript JSONL mtime — and normalizes each
  task into a **business dimension** (see :class:`BusinessState`). This is the human
  replacement for the hand-run ``patrol.sh`` / ``watch.sh``.
* **Side view = a supervisor DAG overlay (only when a task is bound to a run).** It
  projects ``EventLog → reduce → SupervisorState`` (+ the read-only ``decide`` for
  "what the supervisor would do next") onto a bound task. Unbound tasks show no DAG.
* **Thin observation bridge.** It records a ``task_id ↔ run_id/node/...`` binding and
  (explicitly, never automatically inside ``status``) can project a real delivery
  signal onto a supervisor observation event. The bridge is a **one-way projection**
  (real runtime → supervisor events); it never controls the real world.

🔴 **Safety invariants this module is built to honour:**

* **INV-1 (control plane has zero LLM).** Everything here is a deterministic read-only
  projection + pure text rendering. It makes **no** decisions — ``decide`` is called
  read-only purely to *show* the next step; nothing is appended or executed by
  ``status``/``sessions``.
* **INV-3 (single-writer not polluted).** The only writes are: STOP_AUTO sentinels
  (reversible touch/unlink), the bindings file (the board's own state), and — for
  ``approve`` / bridge observation — events appended through the S3
  :class:`~handoff_fanout.supervisor.event_log.EventLog` **single-writer API** (never
  an in-place edit of ``events.jsonl``).
* **INV-10 (human observable + rescuable).** Business-dimension normalization + a dumb
  CLI + a force-sync escape hatch.
* **脑裂 (split-brain) rule (gemini).** When the supervisor overlay disagrees with the
  real runtime, **the real runtime wins** (it is the fact source); the overlay is
  always labelled "监管中枢视图（可能滞后）", and ``force-sync`` detaches a stale
  binding so the real runtime is the sole truth again.

This module is pure stdlib. It is **not** wired into the running handoff engine: the
live ``handoff dump`` / ``worktree`` / ``audit-close`` paths never import it — ``cli.py``
lazy-imports it only for the new ``status`` / ``sessions`` / ``stop`` / ``pause`` /
``resume`` / ``approve`` / ``force-sync`` subcommands (S5a 红线: 只增不改运行路径).
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# --- ANSI (kept trivial — no Rich/Textual dependency, design defer) ----------
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_CYAN = "\033[36m"


def _color(text: str, code: str, *, enabled: bool) -> str:
    return f"{code}{text}{_RESET}" if enabled else text


# =============================================================================
# 1. Layout — locate the real handoff runtime + a worker's transcript dir
# =============================================================================


@dataclasses.dataclass(frozen=True)
class HandoffLayout:
    """Resolves the on-disk locations the board reads (all injectable so tests use a
    temp fixture tree and never touch the real ``~/.claude-handoff`` — C′ red line)."""

    root: Path  # the handoff home (~/.claude-handoff)
    project: str  # e.g. "erp-system"
    transcript_root: Path  # where Claude Code stores per-session JSONL (~/.claude/projects)

    @classmethod
    def resolve(
        cls,
        *,
        project: str,
        root: str | os.PathLike[str] | None = None,
        transcript_root: str | os.PathLike[str] | None = None,
    ) -> HandoffLayout:
        home = Path(os.path.expanduser("~"))
        return cls(
            root=Path(root) if root is not None else home / ".claude-handoff",
            project=project,
            transcript_root=(
                Path(transcript_root)
                if transcript_root is not None
                else home / ".claude" / "projects"
            ),
        )

    # -- per-project dirs ------------------------------------------------------
    @property
    def project_dir(self) -> Path:
        return self.root / self.project

    @property
    def queue_dir(self) -> Path:
        return self.project_dir / "queue"

    @property
    def ack_dir(self) -> Path:
        return self.project_dir / "ack"

    @property
    def worktrees_dir(self) -> Path:
        return self.project_dir / "worktrees"

    @property
    def supervisor_dir(self) -> Path:
        return self.project_dir / "supervisor"

    @property
    def bindings_path(self) -> Path:
        return self.supervisor_dir / "bindings.json"

    # -- STOP_AUTO sentinels (existing convention) -----------------------------
    @property
    def global_stop(self) -> Path:
        return self.root / "STOP_AUTO"

    @property
    def global_done(self) -> Path:
        return self.root / "done"

    @property
    def project_stop(self) -> Path:
        return self.project_dir / "STOP_AUTO"

    # -- transcript dir for one worktree-isolated task -------------------------
    def worktree_path(self, task: str) -> Path:
        return self.worktrees_dir / task

    def transcript_dir(self, task: str) -> Path:
        """The Claude Code project dir for a worktree-isolated session (its ``*.jsonl``
        live here). Claude Code encodes the abs cwd by replacing every ``/`` and ``.``
        with ``-`` (matches the live ``patrol.sh`` ``TDIR`` formula)."""
        encoded = re.sub(r"[/.]", "-", str(self.worktree_path(task).resolve()))
        return self.transcript_root / encoded


# =============================================================================
# 2. Raw scan → TaskSnapshot (the impure I/O layer)
# =============================================================================


@dataclasses.dataclass(frozen=True)
class TaskSnapshot:
    """The raw observed facts for one task — gathered by the scan layer (I/O), then
    fed to the **pure** :func:`classify`. Every field is a plain bool/int/None so the
    classifier is deterministic + trivially testable (construct snapshots directly)."""

    task_id: str
    has_brief: bool = False  # queue/<task>.md
    has_uri: bool = False  # queue/<task>.uri (spawn pending, not yet launched)
    done: bool = False  # queue/<task>.done
    blocked: bool = False  # queue/<task>.BLOCKED.md
    failed: bool = False  # ack/<task>.failed
    suspected_529: bool = False  # queue/<task>.529-suspected
    worker_reported: bool = False  # ack/<task>.worker_reported (explicit delivery)
    spawned: bool = False  # ack/<task>.spawned
    submitted: bool = False  # ack/<task>.submitted
    old_ready: bool = False  # ack/<task>.old_ready (retro evidence ready)
    worktree_present: bool = False  # worktrees/<task>/
    worktree_dirty: bool | None = None  # git WIP in the worktree (None = not checked)
    branch_advanced: bool | None = None  # handoff/<task> pushed past base (None = unknown)
    transcript_idle_s: int | None = None  # now - newest *.jsonl mtime (None = no transcript)
    heartbeat_idle_s: int | None = None  # now - queue/<task>.heartbeat mtime (None = none)
    suspected_529_idle_s: int | None = (
        None  # now - queue/<task>.529-suspected mtime (None = absent)
    )
    bound: bool = False  # has an attached supervisor DAG binding

    def transcript_active(self, *, running_idle_s: int) -> bool:
        """The Claude transcript (newest ``*.jsonl``) was touched within the running
        window (``None`` idle = no transcript = not transcript-active)."""
        return self.transcript_idle_s is not None and self.transcript_idle_s < running_idle_s

    def heartbeat_active(self, *, running_idle_s: int) -> bool:
        """The heartbeat sentinel (``queue/<task>.heartbeat``) was touched within the
        running window. A worker in a **long operation** keeps touching its heartbeat
        every ~60s even while its transcript sits idle >180s; a pure-script / non-Claude
        worker has *only* a heartbeat (no JSONL at all). Either is alive (P1-3 gemini)."""
        return self.heartbeat_idle_s is not None and self.heartbeat_idle_s < running_idle_s

    def is_active(self, *, running_idle_s: int) -> bool:
        """RUNNING liveness = transcript **or** heartbeat fresh (P1-3 gemini 韧性: a
        long-operation worker — transcript idle but heartbeat new — and a pure-script
        worker — heartbeat only — must NOT be misjudged 闲置)."""
        return self.transcript_active(running_idle_s=running_idle_s) or self.heartbeat_active(
            running_idle_s=running_idle_s
        )

    def delivered(self, *, running_idle_s: int) -> bool:
        """The worker did its part: it wrote the explicit ``worker_reported`` sentinel
        (the primary, mandated-by-the-monitoring-protocol signal), OR (the fast fallback
        mirroring ``watch.sh``) its branch advanced past the integration base AND it has
        gone **fully quiet** — both transcript AND heartbeat silent (``not is_active``).
        An *advanced-but-still-active* worker is still RUNNING, not delivered — incl. one
        that only touches its heartbeat mid-long-operation (R2 codex #4 + P1-3 gemini: a
        live heartbeat means the worker may still push more / not be done). Delivery ≠
        closable — the central still owes a review until ``done``. ``running_idle_s`` is
        injected so the pure classifier owns the threshold (the property cannot see
        config)."""
        if self.worker_reported:
            return True
        return bool(self.branch_advanced) and not self.is_active(running_idle_s=running_idle_s)

    def recent_activity_idle_s(self) -> int | None:
        """How long since the **most recent** sign of activity for this task — the smallest
        idle among transcript / heartbeat / the 529-suspected sidecar (each = ``now - mtime``,
        so the smallest is the most recently touched). ``None`` only if the task has no idle
        signal at all. Used by the display-side 久死 (dead-task) noise filter
        (:func:`is_stale_heuristic_blocked`): a task only counts as 陈旧 when *every* footprint
        — including a **freshly-stamped** 529 sidecar — is older than the threshold, so a 529
        the watchdog only just flagged stays in the actionable 卡住 bucket (P1-3 / dual-brain
        consensus: ``min(transcript_idle, heartbeat_idle, suspected_529_idle)``)."""
        candidates = [
            x
            for x in (self.transcript_idle_s, self.heartbeat_idle_s, self.suspected_529_idle_s)
            if x is not None
        ]
        return min(candidates) if candidates else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "has_brief": self.has_brief,
            "has_uri": self.has_uri,
            "done": self.done,
            "blocked": self.blocked,
            "failed": self.failed,
            "suspected_529": self.suspected_529,
            "worker_reported": self.worker_reported,
            "spawned": self.spawned,
            "submitted": self.submitted,
            "old_ready": self.old_ready,
            "worktree_present": self.worktree_present,
            "worktree_dirty": self.worktree_dirty,
            "branch_advanced": self.branch_advanced,
            "transcript_idle_s": self.transcript_idle_s,
            "heartbeat_idle_s": self.heartbeat_idle_s,
            "suspected_529_idle_s": self.suspected_529_idle_s,
            "bound": self.bound,
        }


def _newest_mtime(directory: Path, pattern: str) -> float | None:
    """Newest mtime of ``directory/<pattern>`` files, or ``None`` if none exist. Pure
    I/O, defends against a missing dir (best-effort, never raises on a stat race)."""
    if not directory.is_dir():
        return None
    newest: float | None = None
    for p in directory.glob(pattern):
        try:
            mt = p.stat().st_mtime
        except OSError:  # pragma: no cover - stat race
            continue
        if newest is None or mt > newest:
            newest = mt
    return newest


def _idle_seconds(mtime: float | None, now: float) -> int | None:
    if mtime is None:
        return None
    return max(0, int(now - mtime))


def _is_central(stem: str) -> bool:
    """Whether a discovered stem is the **monitoring central itself** (``supervisor-coord*``),
    not a business task. The central's own heartbeat (``queue/supervisor-coord-3.heartbeat``)
    and ack sidecars (``ack/supervisor-coord-2.*``) live in the same ``queue/`` + ``ack/``
    dirs as real tasks, so every discovery path would otherwise surface the central as a
    phantom IDLE task on the owner board (P1-2 / INV-10「看得懂」: the supervisor must never
    appear as one of the business windows the owner is asked to reason about). Its health is
    shown separately on the dedicated health line (:func:`_central_health`).

    Worker tasks are ``supervisor-s<N>...`` (e.g. ``supervisor-s5a-fix``) and stay; only the
    coordinator's ``supervisor-coord*`` namespace is filtered (matches the design's reserved
    central naming)."""
    return stem.startswith("supervisor-coord")


def discover_task_ids(layout: HandoffLayout) -> list[str]:
    """Every task with a footprint in the runtime: a ``queue/`` file, an ``ack/``
    sidecar, or a ``worktrees/`` dir. Returns sorted, de-duplicated stems — with the
    monitoring central (:func:`_is_central`) filtered out of **all three** discovery
    sources via a single sink filter (P1-2)."""
    ids: set[str] = set()
    if layout.queue_dir.is_dir():
        for p in layout.queue_dir.iterdir():
            name = p.name
            # strip the longest known compound suffix first, else the bare stem
            for suf in (".BLOCKED.md", ".529-suspected", ".heartbeat", ".done", ".uri", ".md"):
                if name.endswith(suf):
                    ids.add(name[: -len(suf)])
                    break
    if layout.ack_dir.is_dir():
        for p in layout.ack_dir.iterdir():
            # ack sidecars are "<task>.<suffix>" / "<task>.<compound.suffix>"; take the
            # stem before the first "." (task ids are kebab-case, no dots).
            ids.add(p.name.split(".", 1)[0])
    if layout.worktrees_dir.is_dir():
        for p in layout.worktrees_dir.iterdir():
            if p.is_dir():
                ids.add(p.name)
    ids.discard("")
    # Single sink filter covers all three sources at once (queue-suffix / ack-split /
    # worktrees-dir) — no three-way drift (P1-2 中枢统一判断).
    return sorted(t for t in ids if not _is_central(t))


def scan_task(
    layout: HandoffLayout,
    task: str,
    *,
    now: float,
    bound_tasks: Iterable[str] = (),
    git_runner: Callable[[list[str], Path], str | None] | None = None,
    integration_ref: str = "origin/main",
) -> TaskSnapshot:
    """Gather the raw facts for one task from the real runtime (I/O).

    ``git_runner`` is injectable so the (best-effort, *local only* — never a network
    call) worktree-dirty / branch-advanced checks can be mocked in tests; passing
    ``None`` skips them (they stay ``None`` = unknown, and the classifier degrades
    gracefully). ``branch_advanced`` is a **local** check (``rev-list --count
    <integration_ref>..HEAD`` against the already-fetched ref — no network); it lets the
    board catch a worker that delivered (pushed commits) but died before writing the
    ``worker_reported`` sentinel (R2 codex #4)."""
    q, a = layout.queue_dir, layout.ack_dir
    worktree = layout.worktree_path(task)
    worktree_present = worktree.is_dir()

    worktree_dirty: bool | None = None
    branch_advanced: bool | None = None
    if git_runner is not None and worktree_present:
        status = git_runner(["status", "--porcelain"], worktree)
        if status is not None:
            worktree_dirty = status.strip() != ""
        # local-only "did this worktree's branch advance past the integration base?"
        # (commits ahead of the already-fetched integration_ref). Network-free; failure
        # (ref absent / git error) leaves branch_advanced = None (unknown, degrades).
        ahead = git_runner(["rev-list", "--count", f"{integration_ref}..HEAD"], worktree)
        if ahead is not None and ahead.strip().isdigit():
            branch_advanced = int(ahead.strip()) > 0

    transcript_idle = _idle_seconds(_newest_mtime(layout.transcript_dir(task), "*.jsonl"), now)
    heartbeat_file = q / f"{task}.heartbeat"
    heartbeat_mtime: float | None = None
    if heartbeat_file.is_file():
        try:
            heartbeat_mtime = heartbeat_file.stat().st_mtime
        except OSError:  # pragma: no cover - stat race
            heartbeat_mtime = None
    heartbeat_idle = _idle_seconds(heartbeat_mtime, now)
    # The .529-suspected sidecar's age (mtime) is the watchdog's heuristic "I flagged a
    # stall at this time" timestamp; the display-side 久死 filter uses it (with transcript /
    # heartbeat) so a freshly-flagged 529 stays actionable while a days-old one drops off the
    # header alarm (single stat → both presence + age, no double-stat).
    suspected_529_file = q / f"{task}.529-suspected"
    suspected_529_mtime: float | None = None
    if suspected_529_file.is_file():
        try:
            suspected_529_mtime = suspected_529_file.stat().st_mtime
        except OSError:  # pragma: no cover - stat race
            suspected_529_mtime = None
    return TaskSnapshot(
        task_id=task,
        has_brief=(q / f"{task}.md").is_file(),
        has_uri=(q / f"{task}.uri").is_file(),
        done=(q / f"{task}.done").is_file(),
        blocked=(q / f"{task}.BLOCKED.md").is_file(),
        failed=(a / f"{task}.failed").is_file(),
        suspected_529=suspected_529_file.is_file(),
        worker_reported=(a / f"{task}.worker_reported").is_file(),
        spawned=(a / f"{task}.spawned").is_file(),
        submitted=(a / f"{task}.submitted").is_file(),
        old_ready=(a / f"{task}.old_ready").is_file(),
        worktree_present=worktree_present,
        worktree_dirty=worktree_dirty,
        branch_advanced=branch_advanced,
        transcript_idle_s=transcript_idle,
        heartbeat_idle_s=heartbeat_idle,
        suspected_529_idle_s=_idle_seconds(suspected_529_mtime, now),
        bound=task in set(bound_tasks),
    )


def scan_all(
    layout: HandoffLayout,
    *,
    now: float,
    bound_tasks: Iterable[str] = (),
    git_runner: Callable[[list[str], Path], str | None] | None = None,
) -> list[TaskSnapshot]:
    bound = set(bound_tasks)
    return [
        scan_task(layout, t, now=now, bound_tasks=bound, git_runner=git_runner)
        for t in discover_task_ids(layout)
    ]


# =============================================================================
# 3. Pure classify → business dimension (gemini "认知撕裂" rule)
# =============================================================================


class BusinessState(enum.StrEnum):
    """The owner-facing business dimensions (gemini 认知撕裂铁律): the board must NOT
    leak "which sentinel file" / "which node state" into the main view — it abstracts
    every real-runtime fact into one of these six buckets a non-technical owner reads.
    The raw signals are the二级 detail."""

    RUNNING = "running"  # 运行中
    BLOCKED = "blocked"  # 卡住需介入
    DELIVERED_AWAITING_REVIEW = "delivered_awaiting_review"  # 已交付待审
    DELIVERED_CLOSABLE = "delivered_closable"  # 已交付可关
    IDLE = "idle"  # 闲置
    DONE = "done"  # 已完成


#: Owner-facing Chinese labels (INV-10 — non-technical owner reads these).
BUSINESS_LABEL: dict[BusinessState, str] = {
    BusinessState.RUNNING: "运行中",
    BusinessState.BLOCKED: "卡住需介入",
    BusinessState.DELIVERED_AWAITING_REVIEW: "已交付待审",
    BusinessState.DELIVERED_CLOSABLE: "已交付可关",
    BusinessState.IDLE: "闲置",
    BusinessState.DONE: "已完成",
}

#: Display order in the board (most owner-urgent first).
BUSINESS_ORDER: tuple[BusinessState, ...] = (
    BusinessState.BLOCKED,
    BusinessState.DELIVERED_AWAITING_REVIEW,
    BusinessState.DELIVERED_CLOSABLE,
    BusinessState.RUNNING,
    BusinessState.IDLE,
    BusinessState.DONE,
)


@dataclasses.dataclass(frozen=True)
class StatusConfig:
    """Deterministic thresholds (no clock inside the pure classifier — the idle
    seconds are pre-computed by the scan layer and injected via the snapshot)."""

    #: A task whose transcript was touched within this many seconds is "running".
    running_idle_s: int = 180
    #: A central heartbeat idler beyond this is flagged unhealthy on the board.
    heartbeat_unhealthy_s: int = 70
    #: A BLOCKED task whose only signal is a heuristic 529 and whose most-recent activity
    #: (transcript / heartbeat / 529-sidecar) is older than this is display-side 陈旧 (久死):
    #: kept visible (INV-10) but moved out of the header 卡住 alarm count. Default 12h
    #: (dual-brain consensus: tolerate the owner's sleep / timezone before archiving a real
    #: stall, while still clearing 50000s+ dead-task noise; never applies to explicit
    #: BLOCKED.md / failed). Configurable so the threshold is not hard-coded.
    stale_idle_s: int = 43200


def classify(
    snap: TaskSnapshot,
    *,
    config: StatusConfig | None = None,
    window_visible: bool | None = None,
) -> BusinessState:
    """Map one raw :class:`TaskSnapshot` to a :class:`BusinessState` — **pure** (no I/O,
    no clock; idle seconds arrive pre-computed in the snapshot).

    Precedence — **explicit / factual signals dominate the heuristic 529 guess**
    (cand-20260606-s5adog1 / dual-brain consensus: a watchdog stall *guess* must never
    outrank a worker's explicit delivery, a real branch advance, or current liveness):

    1. ``done`` → DONE, refined to DELIVERED_CLOSABLE iff a window is still visible
       (``window_visible`` is *injected* by the board layer from osascript — the pure
       core never queries it). A done task whose window is gone is just history (DONE);
       a done task whose window is still open is the actionable "close this" bucket.
    2. ``blocked`` (BLOCKED.md) / ``failed`` → BLOCKED (needs owner). These are **explicit**
       worker-raised signals ("I am stuck" / spawn failed); they beat a delivery claim
       (fail-safe: a task that both reported delivery AND wrote BLOCKED.md still needs a
       human), but not a later ``done`` (the task was unblocked + finished).
    3. ``delivered`` (worker_reported, or branch-advanced + idle) → DELIVERED_AWAITING_
       REVIEW. An explicit delivery beats a stale heuristic 529 (the core noise fix);
       delivery ≠ closable: the central still owes a review/merge until ``done``.
    4. transcript **or** heartbeat active within ``running_idle_s`` → RUNNING (P1-3: a
       long-operation worker keeps its heartbeat fresh while its transcript idles; a
       pure-script worker has only a heartbeat — both are RUNNING, not 闲置). A currently-
       live task with a stale 529 sidecar has **recovered** — current liveness beats the
       old guess, so it is RUNNING, not BLOCKED.
    5. ``529-suspected`` → BLOCKED — the **heuristic** stall guess, the weakest alert: it
       only stands when there is no terminal/explicit/delivery/liveness signal above it (a
       genuinely 529-frozen task has a stale transcript AND heartbeat, so it falls through
       to here). The display-side 久死 filter (:func:`is_stale_heuristic_blocked`) further
       splits a *days-old* 529-only BLOCKED out of the header alarm.
    6. otherwise → IDLE (spawned/queued but quiet — the watcher/Sweeper, not the board,
       decides a stall is a problem; S5a does not auto-escalate)."""
    cfg = config or StatusConfig()
    if snap.done:
        # DELIVERED_CLOSABLE (已交付可关) reuses the SAME strict, conservative closable
        # predicate as ``assess_closable`` (R2 codex #2: the board and ``sessions`` must
        # never disagree — a done-but-dirty / window-unknown task shows DONE, not "可关").
        closable, _ = _closable_reason(snap, window_visible)
        return BusinessState.DELIVERED_CLOSABLE if closable else BusinessState.DONE
    if snap.blocked or snap.failed:
        # Explicit, worker-raised signals only (NOT the heuristic 529) — a self-reported
        # block / spawn failure is strong evidence the owner must act, and beats a delivery
        # claim (fail-safe). The 529 guess is demoted below delivery + liveness (step 5).
        return BusinessState.BLOCKED
    if snap.delivered(running_idle_s=cfg.running_idle_s):
        return BusinessState.DELIVERED_AWAITING_REVIEW
    if snap.is_active(running_idle_s=cfg.running_idle_s):
        return BusinessState.RUNNING
    if snap.suspected_529:
        # Heuristic stall guess — weakest alert. Reached only when nothing above fired:
        # no done, no explicit block/fail, not delivered, not currently active. A real
        # 529 freeze lands here (stale transcript + heartbeat); a recovered/delivered task
        # never does (it exited at step 3/4) — killing the "already-delivered shows 卡住"
        # noise without losing a genuine stall.
        return BusinessState.BLOCKED
    return BusinessState.IDLE


def is_stale_heuristic_blocked(
    snap: TaskSnapshot,
    state: BusinessState,
    *,
    config: StatusConfig | None = None,
) -> bool:
    """Whether a BLOCKED row is a **days-old, heuristic-only** stall that should be moved
    out of the owner's header 卡住 alarm into a dim 陈旧/疑似久死 partition — **pure**, and
    a strictly **display-side read** decision (cand-20260606-s5adog1 part B). It NEVER hides
    the task (INV-10 可观可救 — the row is still rendered, just de-emphasised + uncounted)
    and NEVER writes/deletes a sidecar (pruning the 529 file is ``handoff prune``'s job).

    A row qualifies ONLY when ALL hold (dual-brain consensus):

    * it is :attr:`BusinessState.BLOCKED`;
    * the block is **not** explicit — no ``BLOCKED.md`` and no ``failed``. An explicit
      worker-raised signal is an emergency stop and is **never** archived by age, however
      old (it always stays in the actionable 卡住 bucket);
    * a heuristic ``529-suspected`` sidecar is present (defensive: after the classify
      reorder this is the only remaining route to a non-explicit BLOCKED, but assert it);
    * its most-recent activity (:meth:`TaskSnapshot.recent_activity_idle_s`) is older than
      ``config.stale_idle_s`` — a freshly-flagged 529 (sidecar just stamped) is *not* stale
      and stays actionable.
    """
    if state is not BusinessState.BLOCKED:
        return False
    if snap.blocked or snap.failed:  # explicit signal → never aged out, regardless of age
        return False
    if not snap.suspected_529:  # only a heuristic-529 BLOCKED can be 陈旧
        return False
    cfg = config or StatusConfig()
    idle = snap.recent_activity_idle_s()
    return idle is not None and idle >= cfg.stale_idle_s


# =============================================================================
# 4. Closable assessment — "which sessions can I close?"
# =============================================================================


@dataclasses.dataclass(frozen=True)
class ClosableVerdict:
    """The strict "可关" judgment for one task. ``closable`` is true ONLY when both the
    central's duty toward it is closed AND a window is positively visible (conservative:
    we never tell the owner to close a window we cannot confirm, nor one with WIP)."""

    task_id: str
    closable: bool
    reason: str  # owner-facing (why / why not)


def _closable_reason(snap: TaskSnapshot, window_visible: bool | None) -> tuple[bool, str]:
    """The single closable predicate (shared by :func:`classify` + :func:`assess_closable`
    so they can never disagree — R2 codex #2). Conservative on EVERY uncertainty (not
    done / dirty / dirty-unknown / window unknown / window gone) → NOT closable, so the
    owner is never told to close a window that would lose work (C′ safety)."""
    if not snap.done:
        return False, "中枢未对它写 done（职责未闭环，先审/合并）"
    # R2 codex #3: a present worktree whose dirtiness is True OR unknown (None = the git
    # check failed / was skipped) must NOT pass — only an explicit clean (False) is safe.
    if snap.worktree_present and snap.worktree_dirty is not False:
        if snap.worktree_dirty is True:
            return False, "worktree 有未提交改动（关闭会丢 WIP）"
        return False, "worktree 改动状态未知（git 检查失败 / 跳过，保守不判可关）"
    if window_visible is None:
        return False, "已完成，但窗口可见性未知（保守不判可关）"
    if not window_visible:
        return False, "已完成且无可见窗口（已无窗口可关）"
    return True, "已完成 + 窗口仍开 + 无 WIP → 可安全关闭"


def assess_closable(
    snap: TaskSnapshot,
    *,
    window_visible: bool | None,
) -> ClosableVerdict:
    """Strict closable criteria (task spec): **人眼可见窗口 ∩ 中枢职责对它已闭环**.
    "闲置 + git 安全" ≠ closable — only a task the central has *finished with* (a
    ``done`` signal), with a confirmed-open window and no dirty/unknown WIP, is closable.
    Delegates to :func:`_closable_reason` (shared with ``classify`` — they never drift)."""
    closable, reason = _closable_reason(snap, window_visible)
    return ClosableVerdict(snap.task_id, closable, reason)


# =============================================================================
# 5. osascript window query (impure / macOS / injectable runner)
# =============================================================================


def _default_osascript_runner() -> str | None:
    """Return the front-window + visible-window titles of VS Code (one per line), or
    ``None`` if osascript is unavailable / fails. Best-effort: window→task mapping is
    heuristic (VS Code tab titles are AI-generated summaries, not task ids — see the
    真机验收 lesson), but a worktree-isolated session's window title surfaces the
    workspace folder name, which == the task id, so a substring match is usable."""
    script = (
        'tell application "System Events" to tell (every process whose '
        'name is "Code" or name is "Electron" or name is "Code - Insiders") '
        "to get name of every window"
    )
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - env dependent
        return None
    if out.returncode != 0:  # pragma: no cover - env dependent
        return None
    return out.stdout


def _title_mentions_task(blob: str, task: str) -> bool:
    """Whether a window-title blob mentions ``task`` as a whole kebab-token, not as a
    substring of a longer id (R2 codex #7: ``task-1`` must NOT match ``task-10``). The
    boundary treats ``[A-Za-z0-9_-]`` as token chars (task ids are kebab-case), so the
    match is bounded by anything outside that class (space, ``/``, ``●``, EOL …)."""
    return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(task)}(?![A-Za-z0-9_-])", blob) is not None


def query_visible_tasks(
    task_ids: Iterable[str],
    *,
    runner: Callable[[], str | None] = _default_osascript_runner,
) -> dict[str, bool] | None:
    """For each task, whether a VS Code window whose title mentions the task id (as a
    whole kebab-token — :func:`_title_mentions_task`, not a loose substring) is visible.
    Returns ``None`` (every visibility unknown) when the runner could not read windows
    (osascript unavailable / non-macOS) — the caller degrades to "window unknown" rather
    than falsely claiming a window is gone."""
    titles = runner()
    if titles is None:
        return None
    return {t: _title_mentions_task(titles, t) for t in task_ids}


# =============================================================================
# 6. Bridge — task ↔ supervisor-run binding + read-only overlay + approve
# =============================================================================


@dataclasses.dataclass(frozen=True)
class Binding:
    """A ``task_id ↔ supervisor DAG run`` binding (the thin observation bridge). It maps
    a real runtime task to the supervisor run/node + the artefacts the overlay reads.
    ``detached`` (set by ``force-sync``) hides a stale overlay without losing the record
    (脑裂 escape hatch)."""

    task_id: str
    run_id: str
    node_id: str
    plan_path: str
    events_path: str
    worktree: str | None = None
    branch: str | None = None
    transcript_dir: str | None = None
    detached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Binding:
        fields = {f.name for f in dataclasses.fields(cls)}
        missing = {"task_id", "run_id", "node_id", "plan_path", "events_path"} - d.keys()
        if missing:
            raise ValueError(f"Binding missing required keys: {sorted(missing)}")
        return cls(**{k: v for k, v in d.items() if k in fields})


class BindingStore:
    """Persists task↔run bindings in ``supervisor/bindings.json`` (the board's own
    state — NOT the event log). Atomic writes; tolerant of a missing/empty file."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _load_raw(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def all(self) -> dict[str, Binding]:
        out: dict[str, Binding] = {}
        for task, d in self._load_raw().items():
            try:
                out[task] = Binding.from_dict({"task_id": task, **d})
            except (ValueError, TypeError):
                continue  # skip a corrupt entry, never crash the board
        return out

    def get(self, task_id: str) -> Binding | None:
        return self.all().get(task_id)

    def active_bound_tasks(self) -> list[str]:
        """Tasks with an *attached* (non-detached) binding — these get a DAG overlay."""
        return sorted(t for t, b in self.all().items() if not b.detached)

    def _write_locked(self, bindings: dict[str, Binding]) -> None:
        """Atomic write under the flock already held by the caller. Unique tmp name
        (pid-tagged) so two writers never clobber a shared ``.tmp`` (R2 codex #8)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            t: {k: v for k, v in b.to_dict().items() if k != "task_id"}
            for t, b in sorted(bindings.items())
        }
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def put(self, binding: Binding) -> None:
        # R2 codex #8: read-modify-write under the engine's cross-process flock so two
        # concurrent bind/force-sync calls cannot lose each other's update.
        from .atomic import acquire_dir_lock

        with acquire_dir_lock(self.lock_path):
            bindings = self.all()
            bindings[binding.task_id] = binding
            self._write_locked(bindings)

    def set_detached(self, task_id: str, detached: bool) -> Binding:
        from .atomic import acquire_dir_lock

        with acquire_dir_lock(self.lock_path):
            bindings = self.all()
            b = bindings.get(task_id)
            if b is None:
                raise KeyError(f"no binding for task {task_id!r}")
            updated = dataclasses.replace(b, detached=detached)
            bindings[task_id] = updated
            self._write_locked(bindings)
            return updated


@dataclasses.dataclass(frozen=True)
class OverlayNode:
    """One node's owner-facing projection in the DAG overlay."""

    node_id: str
    status: str  # NodeState value
    attempt: int
    reason: str | None  # last_reason (block / cancel / escalate)
    next_step: str | None  # the read-only decide() intent for this node, if any


@dataclasses.dataclass(frozen=True)
class SupervisorOverlay:
    """The read-only projection of a bound supervisor DAG run (脑裂: labelled
    "可能滞后", real runtime wins). Built by ``reduce`` (state) + the **read-only**
    ``decide`` (next steps) — nothing is appended or executed (INV-1)."""

    task_id: str
    run_id: str
    plan_id: str
    plan_status: str  # PlanStatus value
    bound_node: OverlayNode | None
    blocked_nodes: list[str]
    next_steps: list[str]  # owner-facing decide() intents (read-only)
    last_seq: int
    error: str | None = None  # set if the run artefacts could not be read

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "plan_status": self.plan_status,
            "bound_node": dataclasses.asdict(self.bound_node) if self.bound_node else None,
            "blocked_nodes": list(self.blocked_nodes),
            "next_steps": list(self.next_steps),
            "last_seq": self.last_seq,
            "error": self.error,
        }


def _plan_status(state: Any) -> str:
    """Derive an owner-facing plan status from a reduced state (mirrors S4a PlanStatus
    without running a tick — pure read)."""
    from handoff_fanout.supervisor.states import NodeState, PlanState

    if state.plan_state is PlanState.GLOBAL_PAUSED:
        return "paused"
    statuses = [n.status for n in state.nodes.values()]
    if statuses and all(s is NodeState.DONE for s in statuses):
        return "all_done"
    if any(s is NodeState.BLOCKED for s in statuses):
        return "blocked"
    return "running"


def load_overlay(binding: Binding, *, now: str) -> SupervisorOverlay:
    """Project a bound supervisor run into a read-only overlay: ``reduce(plan, events)``
    then the **read-only** ``decide`` for "what the supervisor would do next". Appends
    nothing (INV-1). A missing/corrupt artefact degrades to an ``error`` overlay rather
    than crashing the board (脑裂: the board still shows the real runtime)."""
    from handoff_fanout.supervisor.event_log import EventLog
    from handoff_fanout.supervisor.plan import Plan
    from handoff_fanout.supervisor.policy import decide
    from handoff_fanout.supervisor.reducer import reduce
    from handoff_fanout.supervisor.states import NodeState

    # R2 codex #6 + P1-4 gemini: the WHOLE projection — read + reduce + decide +
    # _plan_status + node access — is fail-safe. ANY exception (a missing/corrupt/
    # version-incompatible artefact; a structural TypeError/AttributeError from a shape
    # mismatch; OR a RuntimeError/IndexError/AssertionError thrown from deep inside
    # reduce/decide) degrades to an ``error`` overlay; it NEVER crashes ``handoff status``.
    # 脑裂铁律: the projection (a *side* view) blowing up must never take down the real-
    # runtime main view (the fact source) — the owner must keep 可观可救 even with a
    # broken overlay, so the catch here is deliberately total (``except Exception``).
    try:
        plan = Plan.from_dict(json.loads(Path(binding.plan_path).read_text(encoding="utf-8")))
        log = EventLog(binding.events_path, plan.plan_id)
        state = reduce(plan, log.read_all())
        decisions = decide(plan, state, now=now)
        next_by_node: dict[str, str] = {}
        free_steps: list[str] = []
        for d in decisions:
            text = f"{d.kind.value}" + (f"（{d.reason}）" if d.reason else "")
            if d.node:
                next_by_node.setdefault(d.node, text)
            else:
                free_steps.append(text)
        nr = state.nodes.get(binding.node_id)
        bound_node = (
            OverlayNode(
                node_id=binding.node_id,
                status=nr.status.value,
                attempt=nr.attempt,
                reason=nr.last_reason,
                next_step=next_by_node.get(binding.node_id),
            )
            if nr is not None
            else None
        )
        blocked = sorted(nid for nid, n in state.nodes.items() if n.status is NodeState.BLOCKED)
        next_steps = [next_by_node[n] for n in sorted(next_by_node)] + free_steps
        return SupervisorOverlay(
            task_id=binding.task_id,
            run_id=binding.run_id,
            plan_id=state.plan_id,
            plan_status=_plan_status(state),
            bound_node=bound_node,
            blocked_nodes=blocked,
            next_steps=next_steps,
            last_seq=state.last_seq,
        )
    except Exception as exc:
        return SupervisorOverlay(
            task_id=binding.task_id,
            run_id=binding.run_id,
            plan_id="?",
            plan_status="unknown",
            bound_node=None,
            blocked_nodes=[],
            next_steps=[],
            last_seq=-1,
            error=f"无法投影监管中枢 run 产物（{type(exc).__name__}: {exc}）",
        )


class ApproveError(RuntimeError):
    """Raised when ``approve`` is asked to do something it must refuse (unbound task,
    node not AWAIT_APPROVAL) — never fakes a button (task red line)."""


def approve_node(
    binding: Binding,
    *,
    grantor: str,
    granted_at: str,
    expires_at: str,
    reason: str = "",
) -> dict[str, Any]:
    """Append an ``approval_granted`` event for a **bound** run's AWAIT_APPROVAL node.

    Reuses the frozen S0 :class:`~handoff_fanout.supervisor.actions.Approval` contract +
    **auto-computes** ``bound_hash`` from the node's pre-approval evidence (anti-replay: a
    later rollback / re-gate bumps attempt/reason → a different hash → the approval cannot
    be silently replayed); never hand-writes JSON. Appends through the EventLog
    single-writer API (INV-3). Refuses if the node is not AWAIT_APPROVAL (no fake
    approval). The owner running this CLI command *is* the consent act (it is
    owner-invoked, not an autonomous decision)."""
    import hashlib

    from handoff_fanout.supervisor.actions import Approval
    from handoff_fanout.supervisor.event_log import DedupeCollisionError, EventLog
    from handoff_fanout.supervisor.events import EventType
    from handoff_fanout.supervisor.plan import Plan
    from handoff_fanout.supervisor.reducer import reduce
    from handoff_fanout.supervisor.states import NodeState

    plan = Plan.from_dict(json.loads(Path(binding.plan_path).read_text(encoding="utf-8")))
    log = EventLog(binding.events_path, plan.plan_id)
    state = reduce(plan, log.read_all())
    node = state.nodes.get(binding.node_id)
    if node is None:
        raise ApproveError(f"节点 {binding.node_id!r} 不在 run {binding.run_id!r} 中")
    if node.status is not NodeState.AWAIT_APPROVAL:
        raise ApproveError(
            f"节点 {binding.node_id!r} 当前是 {node.status.value}，不是 AWAIT_APPROVAL —"
            " 只能 approve 等待审批的不可逆节点（不造假按钮）"
        )
    # bound_hash binds to the node's *pre-approval evidence* (id + attempt + the reason
    # it is awaiting approval), NOT the whole-plan fingerprint: a pre-dispatch node has
    # no diff to bind to, and over-binding to the full state would invalidate the
    # approval on any unrelated node change. It is stable across the approval append
    # itself (anti-replay: a later rollback / re-gate bumps attempt/reason → mismatch).
    bound_hash = hashlib.sha256(
        f"{plan.plan_id}\x00{binding.node_id}\x00{node.attempt}\x00{node.last_reason or ''}".encode()
    ).hexdigest()
    # Semantic idempotency (INV-4): a node that already carries an approval is a no-op —
    # re-running ``approve`` must not append a second approval_granted (log bloat) nor
    # crash on a dedupe collision (the append would change the state fingerprint).
    if node.approval is not None:
        return {
            "appended": False,
            "deduped": True,
            "already_approved": True,
            "node": binding.node_id,
            "bound_hash": node.approval.bound_hash,
            "seq": state.last_seq,
        }
    approval = Approval(
        node=binding.node_id,
        grantor=grantor,
        granted_at=granted_at,
        expires_at=expires_at,
        bound_hash=bound_hash,
        conditions=[reason] if reason else [],
    )
    try:
        result = log.append_event(
            type=EventType.APPROVAL_GRANTED,
            payload=approval,
            dedupe_key=f"approval:{binding.node_id}:{node.attempt}",
            ts=granted_at,
        )
    except DedupeCollisionError:
        # R2 codex #5: a concurrent approve (or a key already on disk our pre-read missed)
        # could land here. Re-reduce: if the node is now approved, treat it as an
        # idempotent no-op (the other writer won); else surface a clear refusal rather
        # than a raw crash (the CLI only catches ApproveError).
        fresh = reduce(plan, log.read_all()).nodes.get(binding.node_id)
        if fresh is not None and fresh.approval is not None:
            return {
                "appended": False,
                "deduped": True,
                "already_approved": True,
                "node": binding.node_id,
                "bound_hash": fresh.approval.bound_hash,
                "seq": -1,
            }
        raise ApproveError(
            f"节点 {binding.node_id!r} 的 approval 写入冲突（并发 approve / 已存在异体）—"
            " 请重跑 handoff approve 确认状态（不静默吞）"
        ) from None
    return {
        "appended": result.appended,
        "deduped": result.deduped,
        "already_approved": False,
        "node": binding.node_id,
        "bound_hash": bound_hash,
        "seq": result.event.seq,
    }


# =============================================================================
# 7. STOP_AUTO control (reversible sentinels — existing convention)
# =============================================================================


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def set_pause(layout: HandoffLayout, *, scope: str) -> Path:
    """Write a STOP_AUTO sentinel (reversible). ``scope`` ∈ {"project", "global"}."""
    target = layout.global_stop if scope == "global" else layout.project_stop
    _touch(target)
    return target


def clear_pause(layout: HandoffLayout, *, scope: str) -> tuple[Path, bool]:
    """Remove a STOP_AUTO sentinel. Returns (path, existed)."""
    target = layout.global_stop if scope == "global" else layout.project_stop
    existed = target.exists()
    target.unlink(missing_ok=True)
    return target, existed


# =============================================================================
# 8. Render — pure ANSI text builders
# =============================================================================


@dataclasses.dataclass(frozen=True)
class BoardRow:
    snap: TaskSnapshot
    state: BusinessState
    window_visible: bool | None


def _detail_chips(snap: TaskSnapshot) -> str:
    """Compact二级 technical detail (kept OUT of the business label). Owner ignores it;
    a human supervisor reads it."""
    chips: list[str] = []
    if snap.transcript_idle_s is not None:
        chips.append(f"idle {snap.transcript_idle_s}s")
    if snap.worker_reported:
        chips.append("worker_reported")
    if snap.blocked:
        chips.append("BLOCKED.md")
    if snap.suspected_529:
        chips.append("529?")
    if snap.failed:
        chips.append("spawn-failed")
    if snap.worktree_dirty:
        chips.append("dirty-wt")
    if snap.bound:
        chips.append("DAG")
    return " · ".join(chips)


def render_status(
    rows: list[BoardRow],
    *,
    now_iso: str,
    project: str,
    overlays: list[SupervisorOverlay] | None = None,
    central_heartbeat_idle_s: int | None,
    watcher_alive: bool | None,
    config: StatusConfig | None = None,
    paused: bool = False,
    color: bool = True,
) -> str:
    """The human-readable overview board. Business-dimension first; the technical
    二级 detail trails dimly. Optional DAG overlay appended for bound tasks."""
    cfg = config or StatusConfig()
    c = color
    lines: list[str] = []
    lines.append(_color(f"📋 handoff 状态总览 · {project} · {now_iso}", _BOLD, enabled=c))
    if paused:
        lines.append(_color("   ⏸  自动接续已暂停（STOP_AUTO 在位）", _YELLOW, enabled=c))

    # health line
    hb = (
        "无"
        if central_heartbeat_idle_s is None
        else (
            f"{central_heartbeat_idle_s}s"
            + ("  ⚠️失活" if central_heartbeat_idle_s > cfg.heartbeat_unhealthy_s else "  ✓")
        )
    )
    wa = "未知" if watcher_alive is None else ("ALIVE ✓" if watcher_alive else "DEAD ⚠️")
    lines.append(_color(f"   健康: 中枢 heartbeat={hb} · watcher={wa}", _DIM, enabled=c))

    by_state: dict[BusinessState, list[BoardRow]] = {s: [] for s in BUSINESS_ORDER}
    for r in rows:
        by_state[r.state].append(r)

    state_color = {
        BusinessState.BLOCKED: _RED,
        BusinessState.DELIVERED_AWAITING_REVIEW: _YELLOW,
        BusinessState.DELIVERED_CLOSABLE: _GREEN,
        BusinessState.RUNNING: _CYAN,
        BusinessState.IDLE: _DIM,
        BusinessState.DONE: _DIM,
    }
    # Display-side 久死 split (cand-20260606-s5adog1 part B): a days-old, heuristic-529-only
    # BLOCKED row is moved OUT of the header 卡住 alarm into a dim 陈旧 partition — kept
    # visible (INV-10), never pruned. The header 卡住 count = only 近期可行动 BLOCKED rows.
    stale_blocked = [
        r
        for r in by_state[BusinessState.BLOCKED]
        if is_stale_heuristic_blocked(r.snap, r.state, config=cfg)
    ]
    actionable_blocked = [
        r
        for r in by_state[BusinessState.BLOCKED]
        if not is_stale_heuristic_blocked(r.snap, r.state, config=cfg)
    ]

    closable_n = len(by_state[BusinessState.DELIVERED_CLOSABLE])
    blocked_n = len(actionable_blocked)
    review_n = len(by_state[BusinessState.DELIVERED_AWAITING_REVIEW])
    summary = (
        f"   摘要: 🔴卡住 {blocked_n} · 🟡待审 {review_n} · 🟢可关 {closable_n} · "
        f"运行 {len(by_state[BusinessState.RUNNING])} · "
        f"闲置 {len(by_state[BusinessState.IDLE])} · "
        f"完成 {len(by_state[BusinessState.DONE])}"
    )
    if stale_blocked:
        # surfaced, never silently dropped (禁止静默降级): the owner sees the stale count.
        summary += f" · 🗄陈旧 {len(stale_blocked)}"
    lines.append(summary)
    lines.append("")

    for st in BUSINESS_ORDER:
        # the BLOCKED group renders only its 近期可行动 rows; 陈旧 rows go to their own dim
        # partition below (the other states are unaffected).
        group = actionable_blocked if st is BusinessState.BLOCKED else by_state[st]
        if not group:
            continue
        head = _color(f"{BUSINESS_LABEL[st]}（{len(group)}）", state_color[st], enabled=c)
        lines.append(head)
        for r in sorted(group, key=lambda x: x.snap.task_id):
            chips = _detail_chips(r.snap)
            suffix = _color(f"   {chips}", _DIM, enabled=c) if chips else ""
            lines.append(f"  • {r.snap.task_id}{suffix}")
        lines.append("")

    if stale_blocked:
        lines.append(_color(f"陈旧/疑似久死（{len(stale_blocked)}）", _DIM, enabled=c))
        lines.append(
            _color(
                f"  （仅陈旧启发式 529、idle ≥ {cfg.stale_idle_s}s 且无显式求救 — 已移出头部"
                "卡住计数，仍可观/可救/可收尾；清理走 handoff prune）",
                _DIM,
                enabled=c,
            )
        )
        for r in sorted(stale_blocked, key=lambda x: x.snap.task_id):
            chips = _detail_chips(r.snap)
            suffix = _color(f"   {chips}", _DIM, enabled=c) if chips else ""
            lines.append(_color(f"  • {r.snap.task_id}{suffix}", _DIM, enabled=c))
        lines.append("")

    if overlays:
        live = [o for o in overlays]
        if live:
            lines.append(
                _color(
                    "── 监管中枢视图（DAG overlay · 可能滞后，真实运行时为准）──", _BLUE, enabled=c
                )
            )
            for o in live:
                lines.extend(_render_overlay_block(o, color=c))
            lines.append("")

    if not rows:
        lines.append(_color("  （无在跑/最近 task）", _DIM, enabled=c))
    return "\n".join(lines).rstrip() + "\n"


def _render_overlay_block(o: SupervisorOverlay, *, color: bool) -> list[str]:
    c = color
    out = [f"  ▸ {o.task_id} → run {o.run_id} (plan {o.plan_id}, seq {o.last_seq})"]
    if o.error:
        out.append(_color(f"      ⚠️ {o.error}", _RED, enabled=c))
        return out
    out.append(f"      plan_status={o.plan_status}")
    if o.bound_node:
        n = o.bound_node
        line = f"      节点 {n.node_id}: {n.status} (attempt {n.attempt})"
        if n.reason:
            line += f" — {n.reason}"
        out.append(line)
        if n.next_step:
            out.append(_color(f"      下一步(建议): {n.next_step}", _DIM, enabled=c))
    if o.blocked_nodes:
        out.append(_color(f"      卡住节点: {', '.join(o.blocked_nodes)}", _RED, enabled=c))
    return out


def render_sessions(
    verdicts: list[ClosableVerdict],
    *,
    project: str,
    window_query_ok: bool,
    color: bool = True,
) -> str:
    """The "哪些会话可关" answer (strict: 可见窗口 ∩ 中枢职责闭环)."""
    c = color
    lines = [_color(f"🪟 可关会话评估 · {project}", _BOLD, enabled=c)]
    if not window_query_ok:
        lines.append(
            _color(
                "   ⚠️ 无法读取可见窗口（osascript 不可用 / 非 macOS）— 保守不判定可关",
                _YELLOW,
                enabled=c,
            )
        )
    closable = [v for v in verdicts if v.closable]
    not_closable = [v for v in verdicts if not v.closable]
    lines.append("")
    lines.append(_color(f"✅ 可安全关闭（{len(closable)}）", _GREEN, enabled=c))
    if closable:
        for v in sorted(closable, key=lambda x: x.task_id):
            lines.append(f"  • {v.task_id}  — {v.reason}")
    else:
        lines.append(_color("  （无）", _DIM, enabled=c))
    lines.append("")
    lines.append(_color(f"⏳ 暂不可关（{len(not_closable)}）", _DIM, enabled=c))
    for v in sorted(not_closable, key=lambda x: x.task_id):
        lines.append(_color(f"  • {v.task_id}  — {v.reason}", _DIM, enabled=c))
    return "\n".join(lines).rstrip() + "\n"


# =============================================================================
# 9. CLI glue (lazy-imported by cli.py; INV-3 — clock read only here)
# =============================================================================


def _git_runner(args: list[str], cwd: Path) -> str | None:
    """Local-only git (never a network op): run ``git <args>`` in ``cwd``; ``None`` on
    failure. Used for the best-effort worktree-dirty / branch-advanced checks.

    🔴 **P1-1 / C′「只读真实运行时」红线**: the board is a *safety-patrol entry point* —
    it must leave **zero write side-effect** on a worker's live worktree. A bare ``git
    status`` can refresh + write back the index and create ``.git/index.lock`` (a write +
    a lock-race on the worker's active tree). So every status-probe git call here is made
    strictly read-only with BOTH defences (双保险): ``--no-optional-locks`` (top-level flag
    — never take the optional index lock) AND ``GIT_OPTIONAL_LOCKS=0`` in the env (the
    same instruction via environment, covering any git op that consults the env)."""
    try:
        out = subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - env dependent
        return None
    return out.stdout if out.returncode == 0 else None


def _iso_now(epoch: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(epoch).replace(microsecond=0).isoformat()


def _gather_rows(
    layout: HandoffLayout,
    *,
    now: float,
    store: BindingStore,
    config: StatusConfig,
    no_windows: bool,
) -> tuple[list[BoardRow], dict[str, bool] | None]:
    bound = store.active_bound_tasks()
    snaps = scan_all(layout, now=now, bound_tasks=bound, git_runner=_git_runner)
    visibility: dict[str, bool] | None = None
    if not no_windows:
        visibility = query_visible_tasks([s.task_id for s in snaps])
    rows = []
    for s in snaps:
        wv = visibility.get(s.task_id) if visibility is not None else None
        rows.append(
            BoardRow(snap=s, state=classify(s, config=config, window_visible=wv), window_visible=wv)
        )
    return rows, visibility


def _central_health(layout: HandoffLayout, *, now: float) -> tuple[int | None, bool | None]:
    """Central heartbeat idle (newest supervisor-coord*.heartbeat) + watcher liveness
    (best-effort pgrep; ``None`` if it cannot be determined)."""
    hb = _idle_seconds(_newest_mtime(layout.queue_dir, "supervisor-coord*.heartbeat"), now)
    watcher_alive: bool | None = None
    try:
        out = subprocess.run(
            ["pgrep", "-f", "supervisor-monitor/watch.sh"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        watcher_alive = out.returncode == 0 and out.stdout.strip() != ""
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - env dependent
        watcher_alive = None
    return hb, watcher_alive


def _cmd_status(args: argparse.Namespace) -> int:
    layout = HandoffLayout.resolve(
        project=args.project, root=args.root, transcript_root=args.transcript_root
    )
    config = StatusConfig()
    now = time.time()
    store = BindingStore(layout.bindings_path)
    rows, _ = _gather_rows(layout, now=now, store=store, config=config, no_windows=args.no_windows)
    overlays = [
        load_overlay(b, now=_iso_now(now))
        for t in store.active_bound_tasks()
        if (b := store.get(t)) is not None
    ]
    hb, watcher = _central_health(layout, now=now)
    paused = layout.project_stop.exists() or layout.global_stop.exists()
    if args.json:
        payload = {
            "project": args.project,
            "now": _iso_now(now),
            "paused": paused,
            "rows": [
                {
                    **r.snap.to_dict(),
                    "business_state": r.state.value,
                    "window_visible": r.window_visible,
                    # display-side 久死 marker (part B): True = days-old heuristic-529-only
                    # BLOCKED, excluded from the header 卡住 alarm but still listed. A machine
                    # consumer counts actionable 卡住 as business_state=="blocked" and !stale.
                    "stale": is_stale_heuristic_blocked(r.snap, r.state, config=config),
                }
                for r in rows
            ],
            "overlays": [o.to_dict() for o in overlays],
            "central_heartbeat_idle_s": hb,
            "watcher_alive": watcher,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(
        render_status(
            rows,
            now_iso=_iso_now(now),
            project=args.project,
            overlays=overlays,
            central_heartbeat_idle_s=hb,
            watcher_alive=watcher,
            config=config,
            paused=paused,
            color=not args.no_color and sys.stdout.isatty(),
        ),
        end="",
    )
    return 0


def _cmd_sessions(args: argparse.Namespace) -> int:
    layout = HandoffLayout.resolve(
        project=args.project, root=args.root, transcript_root=args.transcript_root
    )
    now = time.time()
    store = BindingStore(layout.bindings_path)
    snaps = scan_all(
        layout, now=now, bound_tasks=store.active_bound_tasks(), git_runner=_git_runner
    )
    visibility = None if args.no_windows else query_visible_tasks([s.task_id for s in snaps])
    verdicts = [
        assess_closable(s, window_visible=(visibility.get(s.task_id) if visibility else None))
        for s in snaps
    ]
    if args.json:
        print(
            json.dumps(
                {
                    "project": args.project,
                    "window_query_ok": visibility is not None,
                    "sessions": [dataclasses.asdict(v) for v in verdicts],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(
        render_sessions(
            verdicts,
            project=args.project,
            window_query_ok=visibility is not None,
            color=not args.no_color and sys.stdout.isatty(),
        ),
        end="",
    )
    return 0


def _cmd_pause(args: argparse.Namespace) -> int:
    layout = HandoffLayout.resolve(project=args.project, root=args.root)
    scope = "global" if args.global_ else "project"
    target = set_pause(layout, scope=scope)
    print(f"✅ 已暂停自动接续（{scope}）: 写入 {target}")
    print(f"   恢复: handoff resume{' --global' if args.global_ else ''}")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    layout = HandoffLayout.resolve(project=args.project, root=args.root)
    scope = "global" if args.global_ else "project"
    target, existed = clear_pause(layout, scope=scope)
    if existed:
        print(f"✅ 已放行自动接续（{scope}）: 删除 {target}")
    else:
        print(f"ℹ️ 未发现 STOP_AUTO（{scope}）: {target} 不存在，已是放行态")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    # R2 codex #1: ``stop`` writes ONLY a STOP_AUTO sentinel (the brief scopes
    # stop/pause/resume to STOP_AUTO). It is a reversible alias of ``pause``; the
    # drastic permanent global ``done`` sentinel is deliberately NOT an S5a command —
    # the owner uses the existing `touch ~/.claude-handoff/done` quick command for that.
    layout = HandoffLayout.resolve(project=args.project, root=args.root)
    scope = "global" if args.global_ else "project"
    target = set_pause(layout, scope=scope)
    print(f"⏸  已停止自动接续（{scope}）: 写入 {target}（= pause / 可 resume 放行）")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    layout = HandoffLayout.resolve(project=args.project, root=args.root)
    store = BindingStore(layout.bindings_path)
    binding = store.get(args.target)
    if binding is None or binding.detached:
        print(
            f"⛔ 当前 task {args.target!r} 不是已绑定的 supervisor DAG 节点，无法 approve"
            "（不造假按钮）。先 bind 一个 supervisor run 才能 approve。",
            file=sys.stderr,
        )
        return 2
    now = time.time()
    granted = _iso_now(now)
    expires = _iso_now(now + args.expires_days * 86400)
    try:
        result = approve_node(
            binding,
            grantor=args.grantor,
            granted_at=granted,
            expires_at=expires,
            reason=args.reason or "",
        )
    except ApproveError as exc:
        print(f"⛔ {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # P2-6: a missing/corrupt plan.json or events.jsonl, a shape mismatch, or any
        # reduce/decide failure deep inside ``approve_node`` (the same class P1-4 hardens
        # for the overlay) must become a clear owner-facing refusal (exit 2), NOT a raw
        # Python traceback. The owner's rescue command degrades, it never dies — the
        # binding may point at a broken/missing artefact, which is operator-fixable.
        print(
            f"⛔ 无法读取/投影该 run 的 plan/events 产物（{type(exc).__name__}: {exc}）—"
            " 绑定可能指向损坏或缺失的文件，请检查 bind 路径或重新 bind（不露 traceback）",
            file=sys.stderr,
        )
        return 2
    if result["deduped"]:
        print(f"ℹ️ approval 已存在（幂等去重）: 节点 {result['node']} seq {result['seq']}")
    else:
        print(
            f"✅ 已为节点 {result['node']} 写 approval_granted 事件 (seq {result['seq']})\n"
            f"   bound_hash={result['bound_hash'][:16]}… grantor={args.grantor} expires={expires}"
        )
    return 0


def _cmd_bind(args: argparse.Namespace) -> int:
    """The bridge's first job (绑定): record a ``task_id ↔ supervisor run/node`` mapping
    so the overlay / approve / force-sync can reach the run. Pure metadata write (the
    board's own bindings.json), not an event-log or real-world write."""
    layout = HandoffLayout.resolve(project=args.project, root=args.root)
    store = BindingStore(layout.bindings_path)
    binding = Binding(
        task_id=args.target,
        run_id=args.run_id,
        node_id=args.node_id,
        plan_path=args.plan_path,
        events_path=args.events_path,
        worktree=args.worktree,
        branch=args.branch,
        transcript_dir=args.transcript_dir,
    )
    store.put(binding)
    print(
        f"✅ 已绑定 task {args.target} → run {args.run_id} / node {args.node_id}\n"
        f"   plan={args.plan_path}\n   events={args.events_path}\n"
        f"   （单向投影：真实运行时 → supervisor 事件；overlay 标「可能滞后」）"
    )
    return 0


def _cmd_force_sync(args: argparse.Namespace) -> int:
    layout = HandoffLayout.resolve(project=args.project, root=args.root)
    store = BindingStore(layout.bindings_path)
    try:
        updated = store.set_detached(args.target, detached=not args.reattach)
    except KeyError:
        print(f"⛔ 没有 task {args.target!r} 的 supervisor 绑定", file=sys.stderr)
        return 2
    if updated.detached:
        print(
            f"✅ force-sync: 已 detach run {updated.run_id} 的 overlay（task {args.target}）。\n"
            "   监管中枢视图已隐藏，真实运行时为唯一真相（脑裂逃生口）。\n"
            f"   重新挂回: handoff force-sync {args.target} --reattach"
        )
    else:
        print(f"✅ force-sync: 已重新挂回 run {updated.run_id} 的 overlay（task {args.target}）。")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handoff status_board",
        description="S5a 最小可观可救：人话状态看板 + 傻瓜 CLI (status/sessions/stop/pause/resume/approve/force-sync)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _common(p: argparse.ArgumentParser, *, windows: bool = False) -> None:
        p.add_argument(
            "--project", default="erp-system", help="handoff project (default erp-system)"
        )
        p.add_argument("--root", default=None, help="handoff home (default ~/.claude-handoff)")
        if windows:
            p.add_argument("--transcript-root", default=None, help="Claude Code projects dir")
            p.add_argument("--no-windows", action="store_true", help="skip osascript window query")
            p.add_argument("--no-color", action="store_true")
            p.add_argument("--json", action="store_true")

    p_status = sub.add_parser(
        "status", help="人话总览看板（真实运行时业务维度 + 可关 + 健康 + DAG overlay）"
    )
    _common(p_status, windows=True)
    p_status.set_defaults(func=_cmd_status)

    p_sessions = sub.add_parser("sessions", help="哪些会话可关（严格：可见窗口 ∩ 中枢职责闭环）")
    _common(p_sessions, windows=True)
    p_sessions.set_defaults(func=_cmd_sessions)

    p_pause = sub.add_parser("pause", help="暂停自动接续（写 STOP_AUTO / reversible）")
    _common(p_pause)
    p_pause.add_argument("--global", dest="global_", action="store_true", help="全局而非仅本项目")
    p_pause.set_defaults(func=_cmd_pause)

    p_resume = sub.add_parser("resume", help="放行自动接续（删 STOP_AUTO）")
    _common(p_resume)
    p_resume.add_argument("--global", dest="global_", action="store_true")
    p_resume.set_defaults(func=_cmd_resume)

    p_stop = sub.add_parser(
        "stop", help="停止自动接续（写 STOP_AUTO / = pause 可逆 / 项目或 --global）"
    )
    _common(p_stop)
    p_stop.add_argument("--global", dest="global_", action="store_true")
    p_stop.set_defaults(func=_cmd_stop)

    p_approve = sub.add_parser(
        "approve", help="批准已绑定 run 的 AWAIT_APPROVAL 节点（仅绑定 / 不造假）"
    )
    _common(p_approve)
    p_approve.add_argument("target", help="task id（须已绑定 supervisor run）")
    p_approve.add_argument("--grantor", default=os.environ.get("USER", "owner"))
    p_approve.add_argument("--reason", default=None)
    p_approve.add_argument("--expires-days", type=int, default=7)
    p_approve.set_defaults(func=_cmd_approve)

    p_bind = sub.add_parser(
        "bind", help="桥：绑定 task ↔ supervisor run/node（overlay/approve 的入口）"
    )
    _common(p_bind)
    p_bind.add_argument("target", help="task id")
    p_bind.add_argument("--run-id", required=True)
    p_bind.add_argument("--node-id", required=True)
    p_bind.add_argument("--plan-path", required=True, help="该 run 的 plan.json 路径")
    p_bind.add_argument("--events-path", required=True, help="该 run 的 events.jsonl 路径")
    p_bind.add_argument("--worktree", default=None)
    p_bind.add_argument("--branch", default=None)
    p_bind.add_argument("--transcript-dir", default=None)
    p_bind.set_defaults(func=_cmd_bind)

    p_force = sub.add_parser(
        "force-sync", help="脑裂逃生口：detach 某 run 的 overlay（真实运行时为准）"
    )
    _common(p_force)
    p_force.add_argument("target", help="task id（已绑定）")
    p_force.add_argument("--reattach", action="store_true", help="重新挂回（撤销 detach）")
    p_force.set_defaults(func=_cmd_force_sync)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
