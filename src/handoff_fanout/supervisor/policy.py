"""S3 — Policy (C4 / SupervisorTurn) + shadow watcher (design §3 C4 / §5 / §8).

INV-1 is this module's命脉: the control plane is **zero-LLM**. :func:`decide` is a
**pure, deterministic ``if/else``** function of ``(plan, state, now, config)`` — no
model is ever consulted (an LLM only ever produces a *proposal* in the data plane,
never a control decision). It reads the verdict / oracle outcomes that already live
*in the reduced state* (baked into ``audit_done`` / ``oracle_checked`` events by the
supervisor at ingest time, INV-2/INV-3) and emits :class:`Decision` intents — it does
**not** call S1 Oracle or S2 Verdict live, and it does **not** mutate anything.

``now`` is **injected** (the watcher passes the turn's logical time, derived from
event ``ts`` on replay) so the policy stays time-free and deterministic — INV-3 even
though the Sweeper reasons about timeouts.

**Shadow mode (design §8 ①, the key S3 safety boundary):** the policy is *report-only*.
:class:`ShadowReplay` replays a recorded event sequence and, at each prefix, reduces
to state + computes the decisions the supervisor *would* make — producing a
deterministic ``(state_fingerprint, decisions)`` sequence. It **never spawns a worker,
never appends a control event, never touches an integration branch.** The real
Dispatcher (which turns a :class:`Decision` into a spawn/commit/merge) is S4 — this
module only *decides + records*. The decisions are mapped to the control events they
*would* emit (:func:`would_emit`) purely for offline comparison ("比对中枢决策 vs 历史").

State names are taken **strictly** from S0 (``states.NodeState`` / ``PlanState``); no
state or transition is invented here.

Faithful-deviation note (documented, not silent): §5's pseudocode picks a *single*
``next_actionable`` node per tick. :func:`decide` instead returns the decisions for
**all** independently-actionable nodes in the tick, sorted by ``node_id`` for
determinism. The design's whole purpose is a *parallel* DAG (§2 / §6 "并行 worktree");
serialising one node per tick would defeat that. Decisions target distinct nodes, so
they are conflict-free. The single-node pseudocode is illustrative; the parallel form
is a strict superset reached over the same edges.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any

from .actions import Approval
from .events import EventType
from .fixer import FixerState, FixerTrigger
from .oracle import OracleScope
from .plan import Node, Plan
from .reducer import NodeRuntime, SupervisorState, is_stale, reduce
from .states import NodeState, PlanState


@dataclasses.dataclass(frozen=True)
class PolicyConfig:
    """Deterministic knobs for the policy turn (no clock/fs inside — all injected)."""

    #: Seconds an in-flight worker/audit may run before the Sweeper times it out. The
    #: comparison uses lexicographic ISO-8601 ``ts`` math via :func:`_elapsed_seconds`.
    timeout_s: int = 1800
    #: Max dispatch attempts before a timed-out node escalates (BLOCK) instead of retry.
    max_dispatch_attempts: int = 2
    #: Whether to emit a FINAL_ORACLE decision once every node is DONE.
    run_final_oracle: bool = True


class DecisionKind(enum.StrEnum):
    """A control-plane intent. Each maps (via :func:`would_emit`) to the event it
    *would* append in enforce mode — in shadow it is only recorded."""

    DISPATCH = "dispatch"  # → node_dispatched (spawn worker)
    REQUEST_APPROVAL = "request_approval"  # → approval_requested (irreversible node)
    START_AUDIT = "start_audit"  # → audit_started (spawn audit subagent)
    RUN_ORACLE = "run_oracle"  # → oracle_checked (run the isolated acceptance oracle)
    ADVANCE = "advance"  # → node_advanced (verdict GREEN & oracle GREEN)
    SPAWN_FIXER = "spawn_fixer"  # → fixer_spawned (verdict RED or oracle RED)
    BLOCK = "block"  # → node_blocked (verdict UNKNOWN / fix cap / retry cap → escalate)
    TIMEOUT = "timeout"  # → worker_timeout (Sweeper)
    REVALIDATE_STALE = "revalidate_stale"  # DONE stale → async rebase+revalidate (Fixer, S7)
    FINAL_ORACLE = "final_oracle"  # all nodes DONE → run whole-plan acceptance oracle


#: DecisionKind → the EventType it would append in enforce mode (None = no single
#: backing event, e.g. FINAL_ORACLE / REVALIDATE_STALE are S4/S7 multi-step actions).
_WOULD_EMIT: dict[DecisionKind, EventType | None] = {
    DecisionKind.DISPATCH: EventType.NODE_DISPATCHED,
    DecisionKind.REQUEST_APPROVAL: EventType.APPROVAL_REQUESTED,
    DecisionKind.START_AUDIT: EventType.AUDIT_STARTED,
    DecisionKind.RUN_ORACLE: EventType.ORACLE_CHECKED,
    DecisionKind.ADVANCE: EventType.NODE_ADVANCED,
    DecisionKind.SPAWN_FIXER: EventType.FIXER_SPAWNED,
    DecisionKind.BLOCK: EventType.NODE_BLOCKED,
    DecisionKind.TIMEOUT: EventType.WORKER_TIMEOUT,
    DecisionKind.REVALIDATE_STALE: None,
    DecisionKind.FINAL_ORACLE: None,
}


def would_emit(kind: DecisionKind) -> EventType | None:
    """The control event a decision would append in enforce mode (offline比对 only)."""
    return _WOULD_EMIT[kind]


@dataclasses.dataclass(frozen=True)
class Decision:
    """One control-plane intent the supervisor would act on (in shadow: only recorded).
    Frozen + deterministically serialisable so a decision sequence is reproducible."""

    kind: DecisionKind
    node: str | None = None
    attempt: int | None = None
    scope: OracleScope | None = None
    trigger: FixerTrigger | None = None
    reason: str = ""  # owner-facing (INV-10)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "node": self.node,
            "attempt": self.attempt,
            "scope": self.scope.value if self.scope else None,
            "trigger": self.trigger.value if self.trigger else None,
            "reason": self.reason,
        }


def _elapsed_seconds(since_ts: str, now: str) -> float | None:
    """Seconds between two ISO-8601 instants, or ``None`` if either is unparseable.
    Pure (no clock read) — both instants are injected."""
    import datetime

    try:
        a = datetime.datetime.fromisoformat(since_ts)
        b = datetime.datetime.fromisoformat(now)
    except ValueError:
        return None
    return (b - a).total_seconds()


# --- the pure decision function (INV-1 zero-LLM) -----------------------------


def decide(
    plan: Plan,
    state: SupervisorState,
    *,
    now: str,
    config: PolicyConfig | None = None,
) -> list[Decision]:
    """The deterministic supervisor turn (design §5 ``tick`` decision half).

    Pure: depends only on ``(plan, state, now, config)``; reads no clock/fs/LLM
    (INV-1/INV-3). Returns every independently-actionable decision this tick (see the
    module faithful-deviation note), sorted by node_id. Does NOT emit/execute —
    ``ShadowReplay`` records the result (design §8 ① shadow report-only)."""
    cfg = config or PolicyConfig()
    nodes_by_id = {n.node_id: n for n in plan.nodes}
    decisions: list[Decision] = []

    # 1. Sweeper: time out any in-flight (DISPATCHED/AUDITING) node past the budget.
    #    Runs even while GLOBAL_PAUSED (§5: pause stops *new* dispatch, still reaps
    #    in-flight timeouts).
    for nid in sorted(state.nodes):
        node = state.nodes[nid]
        if node.status in (NodeState.DISPATCHED, NodeState.AUDITING) and _timed_out(node, now, cfg):
            decisions.append(
                Decision(
                    kind=DecisionKind.TIMEOUT,
                    node=nid,
                    attempt=node.attempt,
                    reason=f"in-flight {node.status.value} exceeded {cfg.timeout_s}s budget",
                )
            )

    # 2. While paused, make no new forward decisions (§5: "if s.global_paused: return").
    if state.plan_state is PlanState.GLOBAL_PAUSED:
        return decisions

    # 3. Forward decisions for every actionable node (parallel DAG — see module note).
    timed_out_now = {d.node for d in decisions if d.kind is DecisionKind.TIMEOUT}
    for nid in sorted(state.nodes):
        if nid in timed_out_now:
            continue  # already decided this tick (the timeout)
        node = state.nodes[nid]
        spec = nodes_by_id.get(nid)
        if spec is None:  # pragma: no cover - reducer rejects unknown nodes upstream
            continue
        decision = _decide_node(plan, state, spec, node, cfg, now)
        if decision is not None:
            decisions.append(decision)

    # 4. Whole-plan final oracle once every node is DONE & no per-node action is pending.
    if cfg.run_final_oracle and not _forward_decisions(decisions) and _all_done(state):
        decisions.append(
            Decision(kind=DecisionKind.FINAL_ORACLE, reason="all nodes DONE — run final oracle")
        )
    return decisions


def _timed_out(node: NodeRuntime, now: str, cfg: PolicyConfig) -> bool:
    if node.inflight_since_ts is None:
        return False
    elapsed = _elapsed_seconds(node.inflight_since_ts, now)
    return elapsed is not None and elapsed > cfg.timeout_s


def _approval_valid(approval: Approval | None, now: str) -> bool:
    """Whether an approval may gate dispatch of an irreversible node: it must be PRESENT
    and not past its hard ``expires_at`` (§6.4 anti-replay — an expired approval is no
    consent, like a stale one). Fail-closed: an unparseable expiry/now, or the exact expiry
    instant (``now >= expires_at``), counts as NOT valid — an irreversible side effect must
    never run on an approval whose freshness cannot be positively verified. ``now`` is the
    injected logical clock (INV-3), so this stays time-free/deterministic."""
    if approval is None:
        return False
    elapsed = _elapsed_seconds(approval.expires_at, now)  # (now - expires_at) seconds
    return elapsed is not None and elapsed < 0


def _forward_decisions(decisions: list[Decision]) -> list[Decision]:
    return [d for d in decisions if d.kind is not DecisionKind.TIMEOUT]


def _all_done(state: SupervisorState) -> bool:
    return bool(state.nodes) and all(n.status is NodeState.DONE for n in state.nodes.values())


def _deps_done(spec: Node, state: SupervisorState) -> bool:
    return all(
        (dep := state.nodes.get(d)) is not None and dep.status is NodeState.DONE for d in spec.deps
    )


def _decide_node(
    plan: Plan,
    state: SupervisorState,
    spec: Node,
    node: NodeRuntime,
    cfg: PolicyConfig,
    now: str,
) -> Decision | None:
    """The single-node decision (§5 dispatch branch). Returns ``None`` when the node is
    waiting (deps unmet / work in flight / awaiting owner) — i.e. not actionable now."""
    status = node.status

    if status is NodeState.PENDING:
        if not _deps_done(spec, state):
            return None  # waiting on upstream deps
        # P1-4: an irreversible node dispatches only behind a VALID approval — none, or an
        # EXPIRED one, must re-request fresh consent (never dispatch on stale approval).
        if not spec.reversible and not _approval_valid(node.approval, now):
            return Decision(
                kind=DecisionKind.REQUEST_APPROVAL,
                node=node.node_id,
                reason="irreversible node requires fresh owner approval before execution "
                "(absent or expired — §6.4 anti-replay)",
            )
        return Decision(
            kind=DecisionKind.DISPATCH,
            node=node.node_id,
            attempt=node.attempt + 1,
            reason="deps satisfied — dispatch worker",
        )

    if status is NodeState.AWAIT_APPROVAL:
        # P1-4: an expired approval is no consent — fail-closed by waiting for a fresh one
        # (the gate previously dispatched the irreversible execution on ANY non-None
        # approval, ignoring its hard expires_at → an expired approval still ran §6.4 work).
        if not _approval_valid(node.approval, now):
            return None  # waiting on (fresh) owner approval
        return Decision(
            kind=DecisionKind.DISPATCH,
            node=node.node_id,
            attempt=node.attempt + 1,
            reason="approval granted — dispatch the (audited) irreversible execution",
        )

    if status is NodeState.AUDITING:
        if not node.audit_started:
            return Decision(
                kind=DecisionKind.START_AUDIT,
                node=node.node_id,
                attempt=node.attempt,
                reason="worker done — start the read-only dual-brain audit",
            )
        return None  # audit in flight (Sweeper handles timeout)

    if status is NodeState.EVALUATING:
        return _decide_evaluating(spec, node)

    if status is NodeState.BLOCKED_BY_FIX:
        return _decide_blocked_by_fix(spec, node, now, cfg)

    if status is NodeState.TIMED_OUT:
        if node.attempt < cfg.max_dispatch_attempts:
            return Decision(
                kind=DecisionKind.DISPATCH,
                node=node.node_id,
                attempt=node.attempt + 1,
                reason=f"retry after timeout (attempt {node.attempt + 1}/{cfg.max_dispatch_attempts})",
            )
        return Decision(
            kind=DecisionKind.BLOCK,
            node=node.node_id,
            reason=f"timed out and retries exhausted ({cfg.max_dispatch_attempts}) — escalate",
        )

    if status is NodeState.DONE:
        if is_stale(state, plan, node.node_id):
            return Decision(
                kind=DecisionKind.REVALIDATE_STALE,
                node=node.node_id,
                reason="upstream regressed/re-passed at new provenance — async revalidate (§5)",
            )
        return None  # settled & current

    # BLOCKED / CANCELLED — terminal-ish; await owner override / abort (no auto action).
    return None


def _decide_evaluating(spec: Node, node: NodeRuntime) -> Decision | None:
    """EVALUATING: verdict is known (from audit_done). Reconciliation #4: UNKNOWN →
    BLOCK (infra, escalate — never a Fixer); RED → Fixer (gated on the fix budget);
    GREEN → gate on the oracle. A node with ``max_fix_attempts==0`` is never auto-fixed
    — a defect blocks it for the owner instead of spawning a forbidden Fixer."""
    verdict = node.verdict
    if verdict is None:  # pragma: no cover - EVALUATING always has a verdict (audit_done)
        return None
    from .verdict import VerdictValue

    if verdict.verdict is VerdictValue.UNKNOWN:
        return Decision(
            kind=DecisionKind.BLOCK,
            node=node.node_id,
            reason="verdict UNKNOWN (dual-brain degraded/unavailable) — escalate, not auto-fix",
        )
    if verdict.verdict is VerdictValue.RED:
        return _spawn_fixer_or_block(
            spec, node, FixerTrigger.VERDICT_RED, "verdict RED (real defect)"
        )
    # GREEN verdict — the milestone oracle must also pass before advancing.
    if node.oracle is None:
        return Decision(
            kind=DecisionKind.RUN_ORACLE,
            node=node.node_id,
            scope=OracleScope.MILESTONE,
            reason="verdict GREEN — run the milestone acceptance oracle",
        )
    if node.oracle.passed:
        return Decision(
            kind=DecisionKind.ADVANCE,
            node=node.node_id,
            attempt=node.attempt,
            reason="verdict GREEN & oracle GREEN — advance",
        )
    return _spawn_fixer_or_block(
        spec,
        node,
        FixerTrigger.ORACLE_RED,
        f"oracle RED ({', '.join(node.oracle.failed_criteria)})",
    )


def _decide_blocked_by_fix(
    spec: Node, node: NodeRuntime, now: str, cfg: PolicyConfig
) -> Decision | None:
    """BLOCKED_BY_FIX: a Fixer is attached. Advance on a post-fix oracle GREEN; retry the
    Fixer under cap; otherwise BLOCK→DLQ (§5). Also reclaims a hung in-flight Fixer
    (P1-2)."""
    fixer = node.active_fixer
    if fixer is None:  # pragma: no cover - BLOCKED_BY_FIX always has an active fixer
        return None
    if fixer.state in (FixerState.DISPATCHED, FixerState.AUDITING):
        # P1-2: reclaim a hung in-flight Fixer. The literal Sweeper (decide() step 1) only
        # reaps DISPATCHED/AUDITING *node* states, so a Fixer that hangs (or whose worker
        # dies) left this node BLOCKED_BY_FIX forever — the whole pipeline永久死锁. A Fixer
        # past the in-flight budget (node.inflight_since_ts == the fixer_spawned ts) is
        # treated as a FAILED Fixer and routed through the SAME budget rule: retry under the
        # fix cap, else BLOCK→DLQ — both existing S0 edges, so no frozen BLOCKED_BY_FIX→
        # TIMED_OUT state is invented. (Gated by GLOBAL_PAUSED via decide() step 2: a pause
        # legitimately stops spawning a replacement Fixer, unlike the node-Sweeper which only
        # marks a passive TIMED_OUT — reclaiming a Fixer means new/escalating work.)
        if _timed_out(node, now, cfg):
            # Trigger reuse is deliberate: a distinct TIMEOUT/INTERNAL_ERROR FixerTrigger
            # would change the S0-frozen ``FixerTrigger`` enum (states.py reconciliation #4:
            # only VERDICT_RED / ORACLE_RED), out of scope for this片. The owner-facing
            # ``reason`` already names "exceeded budget" so the cause is not lost; a finer
            # trigger taxonomy for metrics (timeout vs real oracle-RED) is a future S0
            # amendment (报中枢), not a silent enum change here.
            return _spawn_fixer_or_block(
                spec, node, FixerTrigger.ORACLE_RED, f"fixer exceeded {cfg.timeout_s}s budget"
            )
        return None  # fixer genuinely in flight, within budget
    if fixer.state is FixerState.DONE:
        # Fixer succeeded → re-run the affected→milestone oracle before advancing.
        if node.oracle is None:
            return Decision(
                kind=DecisionKind.RUN_ORACLE,
                node=node.node_id,
                scope=OracleScope.MILESTONE,
                reason="fixer done — re-run affected→milestone oracle",
            )
        if node.oracle.passed:
            return Decision(
                kind=DecisionKind.ADVANCE,
                node=node.node_id,
                attempt=node.attempt,
                reason="fixer done & oracle GREEN — advance",
            )
        # oracle still RED after the fix → retry under cap, else DLQ.
        return _spawn_fixer_or_block(
            spec, node, FixerTrigger.ORACLE_RED, "oracle still RED after fix"
        )
    # FixerState.FAILED → retry under cap, else DLQ.
    return _spawn_fixer_or_block(spec, node, FixerTrigger.ORACLE_RED, "fixer failed")


def _spawn_fixer_or_block(
    spec: Node, node: NodeRuntime, trigger: FixerTrigger, why: str
) -> Decision:
    """Spawn a Fixer iff the node still has fix budget (``fix_attempts < max_fix_attempts``),
    else BLOCK→DLQ. Shared by EVALUATING (first defect, ``fix_attempts==0`` so a
    ``max_fix_attempts==0`` node blocks instead of spawning a forbidden Fixer) and
    BLOCKED_BY_FIX (a failed/oracle-RED retry under the same cap) — one budget rule, no
    drift between the first spawn and the retries (§5)."""
    if node.fix_attempts < spec.max_fix_attempts:
        return Decision(
            kind=DecisionKind.SPAWN_FIXER,
            node=node.node_id,
            trigger=trigger,
            reason=f"{why} — spawn Fixer ({node.fix_attempts}/{spec.max_fix_attempts})",
        )
    return Decision(
        kind=DecisionKind.BLOCK,
        node=node.node_id,
        reason=f"{why} and no fix budget (max_fix_attempts={spec.max_fix_attempts}) — DLQ + escalate",
    )


# --- shadow watcher (design §8 ① report-only) --------------------------------


@dataclasses.dataclass(frozen=True)
class ShadowStep:
    """One replay step: after applying ``prefix_len`` events, the reduced state's
    fingerprint and the decisions the supervisor *would* make (recorded, not executed)."""

    prefix_len: int
    now: str
    state_fingerprint: str
    decisions: list[Decision]

    def to_dict(self) -> dict[str, Any]:
        return {
            "prefix_len": self.prefix_len,
            "now": self.now,
            "state_fingerprint": self.state_fingerprint,
            "decisions": [d.to_dict() for d in self.decisions],
        }


def _default_now(prefix: list, fallback: str) -> str:
    """Injected logical clock for a prefix: the ts of the last applied event (so the
    Sweeper reasons about the log's own time, never the wall clock — INV-3)."""
    return prefix[-1].ts if prefix else fallback


class ShadowReplay:
    """Replays a recorded event sequence and produces the deterministic
    ``(state_fingerprint, decisions)`` trace the supervisor *would* drive — design §8
    ① Shadow. **Report-only**: nothing is spawned, appended, or merged.
    """

    def __init__(self, plan: Plan, config: PolicyConfig | None = None) -> None:
        self.plan = plan
        self.config = config or PolicyConfig()

    def run(self, events: list, *, genesis_now: str = "1970-01-01T00:00:00") -> list[ShadowStep]:
        """Replay ``events`` prefix-by-prefix. For each prefix (len 0..N) reduce → state
        → decide, recording the step. Deterministic: same input ⇒ identical trace
        (INV-1/INV-3)."""
        from .reducer import state_fingerprint

        steps: list[ShadowStep] = []
        for k in range(len(events) + 1):
            prefix = events[:k]
            now = _default_now(prefix, genesis_now)
            state = reduce(self.plan, prefix)
            decisions = decide(self.plan, state, now=now, config=self.config)
            steps.append(
                ShadowStep(
                    prefix_len=k,
                    now=now,
                    state_fingerprint=state_fingerprint(state),
                    decisions=decisions,
                )
            )
        return steps


@dataclasses.dataclass(frozen=True)
class ShadowMismatch:
    """A recorded control event that the policy did NOT predict at the prior prefix —
    surfaced by :func:`compare_to_history` for offline shadow比对 (design §8 ①)."""

    prefix_len: int
    recorded_event_type: EventType
    node: str | None
    predicted_control_events: list[str]


#: Control events the *policy* (not a worker) decides. A recorded event of one of these
#: types should have been among the policy's predicted decisions at the prior prefix.
#: Worker/audit/oracle-sourced events (worker_done / audit_done / oracle_checked / …)
#: are inputs, not policy decisions, so they are excluded from the比对.
_POLICY_DRIVEN_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.NODE_DISPATCHED,
        EventType.APPROVAL_REQUESTED,
        EventType.AUDIT_STARTED,
        EventType.NODE_ADVANCED,
        EventType.FIXER_SPAWNED,
        EventType.NODE_BLOCKED,
        EventType.WORKER_TIMEOUT,
    }
)


def compare_to_history(steps: list[ShadowStep], events: list) -> list[ShadowMismatch]:
    """Compare the shadow trace against the recorded log (design §8 ① "比对中枢决策 vs
    历史"). For each recorded *policy-driven* control event at index ``k``, check that the
    policy at prefix ``k`` predicted a decision that would emit that event type for that
    node. Mismatches are returned (empty ⇒ the policy reproduces the recorded control
    flow). Worker/audit/oracle-sourced events are not policy decisions and are skipped.

    Fidelity (R2 codex #6, acknowledged): this is a **coarse** type+node alignment check
    — it does not yet compare ``attempt`` / ``trigger`` / ``scope`` (so a predicted
    dispatch of the same node at a different attempt would not be flagged). That finer
    decision-equivalence比对 is a shadow-calibration enhancement; the S3 deliverable is the
    *deterministic decision sequence*, which this check is an offline aid to, not a gate."""
    mismatches: list[ShadowMismatch] = []
    for k, event in enumerate(events):
        if event.type not in _POLICY_DRIVEN_EVENTS:
            continue
        node = _event_node(event)
        predicted = [
            would_emit(d.kind)
            for d in steps[k].decisions
            if (would_emit(d.kind) is event.type and (d.node == node or node is None))
        ]
        if not predicted:
            mismatches.append(
                ShadowMismatch(
                    prefix_len=k,
                    recorded_event_type=event.type,
                    node=node,
                    predicted_control_events=sorted(
                        {
                            e.value
                            for d in steps[k].decisions
                            if (e := would_emit(d.kind)) is not None
                        }
                    ),
                )
            )
    return mismatches


def _event_node(event) -> str | None:
    """Best-effort node id from a control event's payload (for比对 only)."""
    payload = event.payload
    if isinstance(payload, dict):
        return payload.get("node") or payload.get("parent_node") or payload.get("to_node")
    return None


__all__ = [
    "PolicyConfig",
    "DecisionKind",
    "Decision",
    "decide",
    "would_emit",
    "ShadowStep",
    "ShadowReplay",
    "ShadowMismatch",
    "compare_to_history",
]
