"""S4a — minimal auto-reaction kernel (advisory mode) (design §5 ``tick`` / §8 ②).

This is the slice that turns the S3 **shadow** ("只算不动", :class:`~handoff_fanout.
supervisor.policy.ShadowReplay`) into a **real auto-reaction round**: a worker
delivers → the supervisor *automatically* senses it → ingests → reduces → decides →
advances internal state / surfaces what needs a human — **no more human polling**.
It directly answers the owner's pain ("worker 交付了中枢没反应").

The whole slice sits on the渐进 ``shadow → advisory → enforce`` ladder (design §8 /
§11) at the **advisory** rung:

* **AUTO (executed):** only *internal state advance* — appending the control event
  that moves a node forward in the reduced state (``node_advanced`` / ``node_blocked``
  / ``worker_timeout``). These spawn nothing, merge nothing, and touch nothing
  irreversible; they are pure book-keeping in the single-writer log.
* **SURFACE (NOT executed):** every decision that would **spawn a session**
  (dispatch worker / start audit / spawn fixer), **execute** (run an oracle), **gate
  on a human** (request approval), or be a **multi-step rebase/merge** (revalidate
  stale) is recorded as a recommendation ("中枢建议下一步 = X") + an alert. The owner
  (or S4b's real Dispatcher / S8 enforce) acts on it through the existing two-step
  approval, never this kernel.

The advisory boundary is **structural, not just policy**: this module has *no* spawn
/ merge / subprocess capability at all. It imports only the EventLog (append internal
events), the AckInbox (ingest worker signals), the pure :func:`reduce`, and the pure
:func:`decide`. There is physically no code path here that could spawn a Claude
session or merge an integration branch — so "advisory" cannot regress to "enforce" by
accident. :data:`_SURFACE_KINDS` / :data:`_AUTO_APPLY_KINDS` partition every
:class:`DecisionKind` and a self-check (:func:`assert_advisory_partition_total`)
asserts every spawn/merge/irreversible kind is in the SURFACE set.

Invariants this slice preserves:

* **INV-1 (control plane zero-LLM).** The kernel reads :func:`decide` (a pure
  ``if/else``) — no model is ever consulted for a control decision. The verdict it
  bakes into ``audit_done`` (via the injected ``verdict_for``) is the *machine*
  Verdict (INV-2), computed from raw findings, never an LLM judgement.
* **INV-3 (single writer + pure reducer).** Only the supervisor appends to the log,
  through :class:`EventLog`. ``reduce`` / ``decide`` stay pure: the kernel injects the
  logical clock (``now``) and never reads a wall clock inside them. A re-run of the
  same round is idempotent (every auto-applied event carries a deterministic
  ``dedupe_key``, so a re-delivery is a benign no-op).
* **INV-10 (human observable + rescuable).** Every round yields a *compact*,
  owner-facing :class:`TickResult` (O(1) context — fingerprint + decisions, not the
  whole log) with owner-readable ``reason`` strings and an :class:`Alert` list, so a
  non-technical owner sees what happened and what needs them.

**Explicitly deferred (NOT this slice):** the full Dispatcher (worktree / commit /
binding / merge automation) is S4b; DiskGuard's real enforcement is S5c (a minimal
read-only interface is kept here); security / supply-chain gates are S6; joint Git+DB
rollback is S7; flipping SURFACE decisions to auto-execute is S8 enforce. This kernel
**never** does any of those.
"""

from __future__ import annotations

import dataclasses
import enum
from pathlib import Path
from typing import Any

from .ack_inbox import (
    AckInbox,
    FixerStateFor,
    TranslationDisposition,
    TranslationOutcome,
    VerdictFor,
)
from .event_log import AppendResult, EventLog
from .events import EventType, Provenance
from .payloads import NodeAttempt, NodeReason
from .plan import Plan
from .policy import Decision, DecisionKind, PolicyConfig, decide, would_emit
from .reducer import NodeRuntime, SupervisorState, reduce, state_fingerprint
from .states import NodeState, PlanState
from .verdict import BindingTarget, Verdict


class TickError(RuntimeError):
    """A round could not be driven deterministically (e.g. the auto-apply settle did
    not converge, or produced only deduped events on a path the reducer should have
    advanced — a log inconsistency). Raised fail-closed: the kernel never silently
    half-reacts."""


class TickTrigger(enum.StrEnum):
    """Why this round ran — recorded for the owner / metrics (INV-10). The trigger is
    *deterministic* (an explicit signal), never a guessed silence threshold."""

    DELIVERY = "delivery"  # woken by a worker_reported sentinel / a pending inbox signal
    SWEEP = "sweep"  # periodic Sweeper pass (reap in-flight timeouts even with no delivery)
    MANUAL = "manual"  # an explicit owner/driver call


class AdvisoryClass(enum.StrEnum):
    """How the kernel treats a :class:`DecisionKind` in advisory mode."""

    #: Internal state advance only — the kernel appends the backing control event
    #: itself (spawns/merges/irreversibles excluded by construction).
    AUTO_APPLY = "auto_apply"
    #: Spawn a session / execute an oracle / gate on a human / multi-step merge —
    #: recorded as a recommendation + alert, NEVER executed by this kernel.
    SURFACE = "surface"


#: The ONLY decisions the kernel auto-executes (design §8 ② advisory). Each maps to a
#: control event that *only* changes the reduced state (no spawn / merge / side effect):
#: ``advance`` → node_advanced, ``block``/escalate → node_blocked, ``timeout`` (Sweeper)
#: → worker_timeout. This is exactly the "内部状态推进 + 记录决策" the advisory rung allows.
_AUTO_APPLY_KINDS: frozenset[DecisionKind] = frozenset(
    {DecisionKind.ADVANCE, DecisionKind.BLOCK, DecisionKind.TIMEOUT}
)

#: Everything else is surfaced, never executed: DISPATCH / START_AUDIT / SPAWN_FIXER
#: (spawn a session), RUN_ORACLE / FINAL_ORACLE (execute the acceptance oracle),
#: REQUEST_APPROVAL (gate on the owner), REVALIDATE_STALE (S7 multi-step rebase+merge).
_SURFACE_KINDS: frozenset[DecisionKind] = frozenset(DecisionKind) - _AUTO_APPLY_KINDS

#: The control events an AUTO_APPLY decision may append — used by the self-check to
#: prove no auto-applied decision can ever back a spawn/merge event.
_AUTO_APPLY_EVENTS: frozenset[EventType] = frozenset(
    {EventType.NODE_ADVANCED, EventType.NODE_BLOCKED, EventType.WORKER_TIMEOUT}
)


def advisory_class(kind: DecisionKind) -> AdvisoryClass:
    """How the kernel treats ``kind`` (AUTO_APPLY vs SURFACE)."""
    return AdvisoryClass.AUTO_APPLY if kind in _AUTO_APPLY_KINDS else AdvisoryClass.SURFACE


def assert_advisory_partition_total() -> None:
    """Self-check (called by the test-suite, usable as a runtime assertion): the
    advisory partition is **total and disjoint** over :class:`DecisionKind`, AND every
    AUTO_APPLY kind backs an internal-state-only event — so the advisory safety boundary
    is a *structural* fact, not a convention. Raises :class:`TickError` on violation."""
    if _AUTO_APPLY_KINDS & _SURFACE_KINDS:
        raise TickError("advisory partition overlaps (a kind is both AUTO and SURFACE)")
    if set(DecisionKind) != (_AUTO_APPLY_KINDS | _SURFACE_KINDS):
        missing = set(DecisionKind) - (_AUTO_APPLY_KINDS | _SURFACE_KINDS)
        raise TickError(f"advisory partition not total over DecisionKind: missing={missing}")
    # Every auto-applied decision must back ONLY an internal-state event (never a spawn/
    # merge/oracle event). would_emit(kind) for an AUTO kind must be in the allow-set.
    for kind in _AUTO_APPLY_KINDS:
        backing = would_emit(kind)
        if backing not in _AUTO_APPLY_EVENTS:
            raise TickError(
                f"AUTO_APPLY kind {kind.value} backs event {backing} which is not an "
                "internal-state event — advisory safety boundary violated"
            )
    # The spawn/merge/irreversible kinds MUST be surfaced (defense in depth: name them
    # explicitly so a future DecisionKind addition that spawns is caught here, not in prod).
    must_surface = {
        DecisionKind.DISPATCH,
        DecisionKind.START_AUDIT,
        DecisionKind.SPAWN_FIXER,
        DecisionKind.REQUEST_APPROVAL,
        DecisionKind.RUN_ORACLE,
        DecisionKind.FINAL_ORACLE,
        DecisionKind.REVALIDATE_STALE,
    }
    leaked = must_surface & _AUTO_APPLY_KINDS
    if leaked:
        raise TickError(
            f"spawn/merge/irreversible kinds must be SURFACE, but these are AUTO: "
            f"{sorted(k.value for k in leaked)}"
        )


class AlertKind(enum.StrEnum):
    """An owner-facing alert category (INV-10). The owner reads these to know what
    needs them — the signal-rich subset of a round (routine advances are *not* alerted)."""

    ESCALATION = "escalation"  # a node BLOCKED → owner must intervene (DLQ / infra outage)
    TIMEOUT = "timeout"  # an in-flight node was reaped past its budget
    APPROVAL_NEEDED = "approval_needed"  # irreversible node awaits owner approval
    ADVISORY = "advisory"  # 中枢建议下一步 = spawn/merge/oracle (owner / S4b acts, NOT the kernel)
    PLAN_COMPLETE = "plan_complete"  # every node DONE
    PLAN_PAUSED = "plan_paused"  # plan is globally paused (new dispatch stopped)


@dataclasses.dataclass(frozen=True)
class Alert:
    """One owner-facing alert (INV-10). ``message`` is plain-language; ``decision`` (when
    present) is the underlying control intent for the record."""

    kind: AlertKind
    node: str | None
    message: str
    decision: Decision | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "node": self.node,
            "message": self.message,
            "decision": self.decision.to_dict() if self.decision else None,
        }


@dataclasses.dataclass(frozen=True)
class AppliedDecision:
    """An auto-executed internal-state advance: the decision + the control event the
    kernel appended (single-writer). ``deduped`` ⟺ the event already existed (an
    idempotent re-run)."""

    decision: Decision
    event_type: EventType
    seq: int
    deduped: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "event_type": self.event_type.value,
            "seq": self.seq,
            "deduped": self.deduped,
        }


@dataclasses.dataclass(frozen=True)
class Advisory:
    """A surfaced (NOT executed) recommendation: the supervisor *would* do
    ``decision`` (spawn / merge / oracle / approval) in enforce mode, but in advisory
    mode it only recommends it. ``recommended_event`` is the event it would append
    in enforce (``None`` for multi-step S4/S7 actions like FINAL_ORACLE)."""

    decision: Decision
    recommended_event: EventType | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "recommended_event": self.recommended_event.value if self.recommended_event else None,
        }


class PlanStatus(enum.StrEnum):
    """The round's owner-facing plan status (the answer to "where are we?")."""

    RUNNING = "running"
    ALL_DONE = "all_done"
    BLOCKED = "blocked"  # at least one node is BLOCKED (needs owner)
    PAUSED = "paused"


@dataclasses.dataclass(frozen=True)
class TickResult:
    """The compact, owner-facing outcome of one auto-reaction round (INV-10 / O(1)
    context — a fingerprint + decisions, never the whole log)."""

    plan_id: str
    triggered_by: TickTrigger
    now: str
    delivered_nodes: list[str]  # deterministic delivery detection (sentinel ∪ drained)
    ingested: list[TranslationOutcome]  # AckInbox drain outcomes (worker signals → events)
    applied: list[AppliedDecision]  # auto-executed internal-state advances
    advisories: list[Advisory]  # surfaced recommendations (NOT executed)
    alerts: list[Alert]
    plan_status: PlanStatus
    #: All nodes currently BLOCKED (DLQ / infra-outage escalations) — surfaced every round
    #: so a non-technical owner never loses sight of "still needs me", even when ``plan_status``
    #: is PAUSED or ALL_DONE-of-the-rest (R2 codex #2 — persistent visibility, INV-10).
    blocked_nodes: list[str]
    state_fingerprint: str
    last_seq: int
    iterations: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "triggered_by": self.triggered_by.value,
            "now": self.now,
            "delivered_nodes": list(self.delivered_nodes),
            "blocked_nodes": list(self.blocked_nodes),
            "ingested": [
                {
                    "node": o.node,
                    "disposition": o.disposition.value,
                    "event_type": o.event_type.value if o.event_type else None,
                    "reason": o.reason,
                }
                for o in self.ingested
            ],
            "applied": [a.to_dict() for a in self.applied],
            "advisories": [a.to_dict() for a in self.advisories],
            "alerts": [a.to_dict() for a in self.alerts],
            "plan_status": self.plan_status.value,
            "state_fingerprint": self.state_fingerprint,
            "last_seq": self.last_seq,
            "iterations": self.iterations,
        }


# --- explicit worker-delivery sentinel (deterministic trigger, design §8 ②) --------


class SentinelWatch:
    """The explicit per-node ``<node>.worker_reported`` delivery sentinel — the same
    mechanism the bootstrap human-supervisor protocol validated (a worker's *absolute
    last step* is to touch its sentinel so the supervisor reacts秒级, not by guessing a
    silence threshold). Here it is a **deterministic delivery signal**, not a
    correctness source (the AckInbox is): a present sentinel means "node X reported".

    Consuming a sentinel (after the round processed it) moves it to ``consumed/`` so it
    is not re-counted — idempotent, never deleted-and-lost.
    """

    SUFFIX = ".worker_reported"

    def __init__(self, sentinel_dir: str | Path) -> None:
        self.dir = Path(sentinel_dir)
        self.consumed_dir = self.dir / "consumed"

    def deposit(self, node: str) -> Path:
        """A worker marks its node delivered (its absolute last step). Idempotent."""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{node}{self.SUFFIX}"
        path.touch()
        return path

    def reported_nodes(self) -> set[str]:
        """The nodes with a present (un-consumed) sentinel — deterministic delivery."""
        if not self.dir.exists():
            return set()
        return {
            p.name[: -len(self.SUFFIX)]
            for p in self.dir.iterdir()
            if p.is_file() and p.name.endswith(self.SUFFIX)
        }

    def consume(self, node: str) -> None:
        """Acknowledge a sentinel was processed: move it to ``consumed/`` (idempotent —
        a missing sentinel is a no-op, never an error).

        Honest note (R2 codex #3): the sentinel name is fixed per node, so a worker that
        re-touches its sentinel *between* the round's snapshot and this consume could have
        its second wake consumed. This is **wake-only** loss, never a correctness loss: the
        AckInbox signal for that re-delivery is the source of truth and keeps
        :meth:`DeliveryDetector.poll` ``pending`` until it is drained, so a tick still runs.
        A nonce/attempt-stamped sentinel would close the wake race; it is deferred (the inbox
        backstop makes it non-urgent for the advisory kernel)."""
        path = self.dir / f"{node}{self.SUFFIX}"
        if not path.exists():
            return
        self.consumed_dir.mkdir(parents=True, exist_ok=True)
        path.replace(self.consumed_dir / path.name)


@dataclasses.dataclass(frozen=True)
class DeliverySignal:
    """Whether (and what) delivered — the deterministic trigger an external driver
    uses to decide WHEN to run a DELIVERY tick (vs a periodic SWEEP). Never a guessed
    silence threshold."""

    pending: bool
    reported_nodes: list[str]  # from worker_reported sentinels
    inbox_signal_count: int  # pending AckInbox signals

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending": self.pending,
            "reported_nodes": list(self.reported_nodes),
            "inbox_signal_count": self.inbox_signal_count,
        }


class DeliveryDetector:
    """Deterministic delivery detection over the explicit ``worker_reported`` sentinels
    + the AckInbox's pending signals (design §8 ②). An external driver (the automated
    successor of the bootstrap ``watch.sh``) calls :meth:`poll` to decide whether to run
    a tick — it never guesses a silence/idle threshold (the failure the bootstrap
    protocol fixed: "等静默 8 分钟才认定交付")."""

    def __init__(self, inbox: AckInbox, sentinel_watch: SentinelWatch | None = None) -> None:
        self.inbox = inbox
        self.sentinel_watch = sentinel_watch

    def poll(self) -> DeliverySignal:
        reported = sorted(self.sentinel_watch.reported_nodes()) if self.sentinel_watch else []
        count = len(self._inbox_signal_files())
        return DeliverySignal(
            pending=bool(reported) or count > 0,
            reported_nodes=reported,
            inbox_signal_count=count,
        )

    def _inbox_signal_files(self) -> list[Path]:
        # Read-only count of pending signals (mirrors AckInbox._signal_files without
        # draining — detection must never mutate the inbox).
        if not self.inbox.dir.exists():
            return []
        return [p for p in self.inbox.dir.iterdir() if p.is_file() and p.suffix == ".json"]


# --- the kernel ---------------------------------------------------------------


def _provenance_from_verdict(verdict: Verdict | None) -> Provenance | None:
    """Reconstruct the build provenance a node passed at, from its machine Verdict's
    anti-replay binding (``bound_to`` + ``binding_target``). This is the deterministic
    code-state the node was audited GREEN against, so an auto-advanced DONE node carries
    a faithful ``built_provenance`` and downstream staleness (reducer ``is_stale``)
    still catches an upstream that re-passed at a *different* commit.

    Honest limitation (deferred to S4b): on the post-fix ``BLOCKED_BY_FIX → DONE`` path
    the node's verdict is still the pre-fix one (the frozen reducer does not re-bind a
    verdict after a Fixer), so this provenance is best-effort there. The real Dispatcher
    (S4b) supplies the merge provenance; S4a records the milestone-pass binding it can
    deterministically derive."""
    if verdict is None:
        return None
    if verdict.binding_target is BindingTarget.TREE_OID:
        return Provenance(tree_oid=verdict.bound_to)
    if verdict.binding_target is BindingTarget.STAGED_DIFF_HASH:
        return Provenance(staged_diff_hash=verdict.bound_to)
    if verdict.binding_target is BindingTarget.HEAD:
        return Provenance(commit=verdict.bound_to)
    return None  # pragma: no cover - BindingTarget is exhaustive


class SupervisorTick:
    """The minimal auto-reaction kernel (S4a). One :meth:`run` is one deterministic
    round: ingest worker signals → settle internal state automatically → surface what
    needs a human. **Advisory**: it auto-executes only internal-state advances and never
    spawns / merges / does anything irreversible (structurally — see the module
    docstring).

    Wiring: the ``verdict_for`` / ``fixer_state_for`` callbacks (INV-2: the supervisor
    computes the machine verdict from raw findings) are injected, matching the S3
    AckInbox seam — in the live engine (S4b) they run the S2 VerdictComputer over the
    audit's findings; here they are injected so the kernel is exercised without coupling
    to S2 execution. An audit/fixer signal with no callback is gracefully quarantined by
    the AckInbox (the kernel never crashes the round)."""

    def __init__(
        self,
        plan: Plan,
        log: EventLog,
        inbox: AckInbox,
        *,
        config: PolicyConfig | None = None,
        sentinel_watch: SentinelWatch | None = None,
        verdict_for: VerdictFor | None = None,
        fixer_state_for: FixerStateFor | None = None,
    ) -> None:
        self.plan = plan
        self.log = log
        self.inbox = inbox
        self.config = config or PolicyConfig()
        self.sentinel_watch = sentinel_watch
        self.verdict_for = verdict_for
        self.fixer_state_for = fixer_state_for
        # Structural safety self-check at construction: the advisory partition must be
        # total and every auto-applied decision must back an internal-state-only event.
        assert_advisory_partition_total()

    def run(self, *, now: str, triggered_by: TickTrigger = TickTrigger.MANUAL) -> TickResult:
        """Drive one auto-reaction round. ``now`` is the injected logical clock (the
        supervisor reads the wall clock once and passes it in — ``reduce``/``decide``
        stay pure, INV-3). Deterministic: same (log, inbox, sentinels, now, callbacks)
        ⇒ same result + same appended events."""
        # 0. Deterministic delivery detection (sentinels present BEFORE this round).
        reported = self.sentinel_watch.reported_nodes() if self.sentinel_watch else set()

        # 1. Ingest: translate every pending worker/audit/fixer signal into its event
        #    (single-writer — AckInbox.drain appends through the EventLog, INV-3).
        ingested = self.inbox.drain(
            self.log,
            self.plan,
            ts=now,
            verdict_for=self.verdict_for,
            fixer_state_for=self.fixer_state_for,
        )
        drained = {
            o.node
            for o in ingested
            if o.node
            and o.disposition in (TranslationDisposition.APPENDED, TranslationDisposition.DEDUPED)
        }
        delivered_nodes = sorted(reported | drained)

        # 2. Settle internal state automatically (advisory AUTO), collect SURFACE decisions.
        applied, advisories, iterations = self._settle(now)

        # 3. Final compact state + owner-facing alerts + plan status (INV-10).
        final_state = reduce(self.plan, self.log.read_all())
        alerts = self._build_alerts(applied, advisories, final_state)
        status = self._plan_status(final_state)

        # 4. Consume the sentinels this round handled (idempotent — they have served
        #    their wake purpose; the AckInbox remains the correctness source).
        if self.sentinel_watch is not None:
            for n in reported:
                self.sentinel_watch.consume(n)

        return TickResult(
            plan_id=self.plan.plan_id,
            triggered_by=triggered_by,
            now=now,
            delivered_nodes=delivered_nodes,
            ingested=ingested,
            applied=applied,
            advisories=advisories,
            alerts=alerts,
            plan_status=status,
            blocked_nodes=_blocked_nodes(final_state),
            state_fingerprint=state_fingerprint(final_state),
            last_seq=final_state.last_seq,
            iterations=iterations,
        )

    # --- internal-state settle (advisory AUTO) -------------------------------

    def _settle(self, now: str) -> tuple[list[AppliedDecision], list[Advisory], int]:
        """Repeatedly reduce → decide → auto-apply the internal-state decisions until no
        AUTO decision remains, then return the surfaced (SURFACE) decisions of the final
        settled state. Bounded: each auto-apply moves a node monotonically toward a
        more-terminal state, so a tick converges; a genuinely non-converging loop (a state
        machine bug) trips the iteration bound → :class:`TickError` (fail-closed).

        A *no-progress* round (every auto event deduped to a no-op) is NOT a crash (R2
        gemini #5): a deduped auto event means the log already holds exactly the event the
        policy wants, i.e. the desired state is already reached — so the round ends
        gracefully and surfaces the settled state's SURFACE decisions. With the
        occurrence-unique dedupe keys (:meth:`_apply_internal`) this is practically
        unreachable, but treating idempotent convergence as a fatal error (rather than as
        "already done") would turn a benign re-run / race into a stuck plan."""
        applied: list[AppliedDecision] = []
        max_iterations = len(self.plan.nodes) * 12 + 16  # generous monotone-progress bound
        iterations = 0
        while True:
            iterations += 1
            if iterations > max_iterations:
                raise TickError(
                    f"auto-apply did not converge after {max_iterations} iterations "
                    "(fail-closed — internal-state settle should be monotone)"
                )
            state = reduce(self.plan, self.log.read_all())
            decisions = decide(self.plan, state, now=now, config=self.config)
            auto = [d for d in decisions if advisory_class(d.kind) is AdvisoryClass.AUTO_APPLY]
            surface = [d for d in decisions if advisory_class(d.kind) is AdvisoryClass.SURFACE]
            if not auto:
                return applied, _to_advisories(surface), iterations
            progressed = False
            for decision in auto:
                ad = self._apply_internal(decision, state, now)
                applied.append(ad)
                if not ad.deduped:
                    progressed = True
            if not progressed:
                # Every auto event already existed (idempotent) — the state already matches
                # what the policy wants. End settle gracefully (NOT a crash) and surface the
                # already-settled state's SURFACE decisions.
                return applied, _to_advisories(surface), iterations

    def _apply_internal(
        self, decision: Decision, state: SupervisorState, now: str
    ) -> AppliedDecision:
        """Append the one internal-state control event a decision backs (single-writer).
        ONLY ``advance`` / ``block`` / ``timeout`` reach here (asserted) — none spawns,
        merges, or has a side effect.

        Dedupe keys are **occurrence-unique** (R2 gemini #5 root cause): a node may legally
        re-enter the same auto event at the SAME attempt within one plan (e.g. an owner
        force-run override keeps the dispatch attempt, so a second UNKNOWN verdict produces
        a second ``node_blocked`` at that attempt; a stale DONE node re-advances at the same
        attempt). Keying only on ``(node, attempt)`` would collide a distinct LATER
        occurrence with the earlier event → a silent dedupe no-op (stuck node) or a
        DedupeCollisionError. Including the decision-time log tail (``state.last_seq``)
        makes each occurrence's key unique while staying deterministic on replay (the same
        log prefix reduces to the same ``last_seq``)."""
        node_id = decision.node
        assert node_id is not None  # AUTO decisions always target a node
        node = state.nodes[node_id]
        occ = state.last_seq  # the log tail this decision was computed against (occurrence id)
        if decision.kind is DecisionKind.ADVANCE:
            attempt = _attempt(decision, node)
            return self._append_auto(
                decision,
                type=EventType.NODE_ADVANCED,
                payload=NodeAttempt(node=node_id, attempt=attempt),
                dedupe_key=f"node_advanced:{node_id}:{attempt}:{occ}",
                now=now,
                attempt_id=str(attempt),
                provenance=_provenance_from_verdict(node.verdict),
            )
        if decision.kind is DecisionKind.BLOCK:
            return self._append_auto(
                decision,
                type=EventType.NODE_BLOCKED,
                payload=NodeReason(node=node_id, reason=decision.reason or "blocked — escalate"),
                dedupe_key=f"node_blocked:{node_id}:{node.attempt}:{occ}",
                now=now,
            )
        if decision.kind is DecisionKind.TIMEOUT:
            attempt = _attempt(decision, node)
            return self._append_auto(
                decision,
                type=EventType.WORKER_TIMEOUT,
                payload=NodeAttempt(node=node_id, attempt=attempt),
                dedupe_key=f"worker_timeout:{node_id}:{attempt}:{occ}",
                now=now,
                attempt_id=str(attempt),
            )
        raise TickError(  # pragma: no cover - guarded by advisory_class()
            f"_apply_internal called with non-auto decision {decision.kind.value}"
        )

    def _append_auto(
        self,
        decision: Decision,
        *,
        type: EventType,
        payload: Any,
        dedupe_key: str,
        now: str,
        attempt_id: str | None = None,
        provenance: Provenance | None = None,
    ) -> AppliedDecision:
        result: AppendResult = self.log.append_event(
            type=type,
            payload=payload,
            dedupe_key=dedupe_key,
            ts=now,
            attempt_id=attempt_id,
            provenance=provenance,
        )
        return AppliedDecision(
            decision=decision,
            event_type=result.event.type,
            seq=result.event.seq,
            deduped=result.deduped,
        )

    # --- owner-facing surfacing (INV-10) -------------------------------------

    def _build_alerts(
        self,
        applied: list[AppliedDecision],
        advisories: list[Advisory],
        final_state: SupervisorState,
    ) -> list[Alert]:
        """Derive the signal-rich owner alerts. Routine advances are NOT alerted (noise);
        blocks/timeouts/approvals/spawn-recs and plan-level state are.

        These are **level-triggered** per round: a node that sits PENDING/AUDITING re-surfaces
        the same ADVISORY recommendation each tick until a human/S4b acts (the kernel is
        stateless across ticks, by INV-3, so it cannot edge-trigger on its own). De-duplicating
        unchanged advisories for the owner-facing CLI/board (so an idle SWEEP loop does not
        repeat one recommendation N times — R2 gemini #3) is the **S5a status-board's** job: it
        holds the prior round's state and diffs against it. Keeping the kernel stateless here is
        deliberate — adding cross-tick memory would break the pure-replay guarantee."""
        alerts: list[Alert] = []
        for ad in applied:
            d = ad.decision
            if d.kind is DecisionKind.BLOCK:
                alerts.append(
                    Alert(
                        kind=AlertKind.ESCALATION,
                        node=d.node,
                        message=f"node {d.node} BLOCKED — needs owner: {d.reason}",
                        decision=d,
                    )
                )
            elif d.kind is DecisionKind.TIMEOUT:
                # R2 gemini #4 (honesty / INV-10): advisory mode marks the node TIMED_OUT in
                # the state machine but does NOT kill the worker's OS process/container — the
                # message must not imply the resource was reclaimed (a false sense of safety
                # that hides a still-burning process / billing leak). The real process kill is
                # S4b/S5 (Dispatcher/Watchdog).
                alerts.append(
                    Alert(
                        kind=AlertKind.TIMEOUT,
                        node=d.node,
                        message=f"node {d.node} marked TIMED_OUT: {d.reason}. ⚠️ advisory mode "
                        "does NOT kill the worker process — manual/S4b kill is needed to stop "
                        "it consuming resources",
                        decision=d,
                    )
                )
        for adv in advisories:
            d = adv.decision
            if d.kind is DecisionKind.REQUEST_APPROVAL:
                alerts.append(
                    Alert(
                        kind=AlertKind.APPROVAL_NEEDED,
                        node=d.node,
                        message=f"node {d.node} needs owner approval before the irreversible "
                        f"step: {d.reason}",
                        decision=d,
                    )
                )
            else:
                # DISPATCH / START_AUDIT / SPAWN_FIXER / RUN_ORACLE / FINAL_ORACLE /
                # REVALIDATE_STALE — the supervisor RECOMMENDS, the owner/S4b acts.
                target = f"node {d.node}" if d.node else "plan"
                alerts.append(
                    Alert(
                        kind=AlertKind.ADVISORY,
                        node=d.node,
                        message=f"中枢建议下一步 = {d.kind.value} ({target}): {d.reason}",
                        decision=d,
                    )
                )
        if final_state.plan_state is PlanState.GLOBAL_PAUSED:
            alerts.append(
                Alert(
                    kind=AlertKind.PLAN_PAUSED,
                    node=None,
                    message="plan is globally paused — no new dispatch (in-flight timeouts "
                    "are still reaped)",
                )
            )
        elif _all_done(final_state):
            alerts.append(
                Alert(
                    kind=AlertKind.PLAN_COMPLETE,
                    node=None,
                    message="all nodes DONE — plan complete (recommend the final acceptance oracle)",
                )
            )
        return alerts

    @staticmethod
    def _plan_status(state: SupervisorState) -> PlanStatus:
        if state.plan_state is PlanState.GLOBAL_PAUSED:
            return PlanStatus.PAUSED
        if _all_done(state):
            return PlanStatus.ALL_DONE
        if any(n.status is NodeState.BLOCKED for n in state.nodes.values()):
            return PlanStatus.BLOCKED
        return PlanStatus.RUNNING


def _to_advisories(surface: list[Decision]) -> list[Advisory]:
    """Wrap the settled state's SURFACE decisions as advisories (the event each WOULD
    emit in enforce, for the owner record)."""
    return [Advisory(decision=d, recommended_event=would_emit(d.kind)) for d in surface]


def _attempt(decision: Decision, node: NodeRuntime) -> int:
    """The attempt a node-lifecycle event must carry (NodeAttempt requires >= 1). Prefer
    the decision's explicit attempt (ADVANCE/TIMEOUT set it); fall back to the node's
    current attempt.

    R2 codex #4 (fail-closed): a node that produced an advance/timeout/block was dispatched,
    so its attempt is >= 1 by the state machine. If it is somehow < 1 here, a frozen
    invariant has been violated — raise :class:`TickError` rather than silently coercing it
    to a plausible-looking ``1`` (which would mask the corruption and forge a NodeAttempt)."""
    attempt = decision.attempt if decision.attempt is not None else node.attempt
    if attempt < 1:
        raise TickError(
            f"node {node.node_id!r} yielded an auto decision with attempt {attempt} (< 1) — "
            "a frozen state-machine invariant is broken (a node that advances/times-out/blocks "
            "must have been dispatched at attempt >= 1) — fail-closed"
        )
    return attempt


def _all_done(state: SupervisorState) -> bool:
    return bool(state.nodes) and all(n.status is NodeState.DONE for n in state.nodes.values())


def _blocked_nodes(state: SupervisorState) -> list[str]:
    return sorted(nid for nid, n in state.nodes.items() if n.status is NodeState.BLOCKED)


__all__ = [
    "TickError",
    "TickTrigger",
    "AdvisoryClass",
    "advisory_class",
    "assert_advisory_partition_total",
    "AlertKind",
    "Alert",
    "AppliedDecision",
    "Advisory",
    "PlanStatus",
    "TickResult",
    "SentinelWatch",
    "DeliverySignal",
    "DeliveryDetector",
    "SupervisorTick",
]
