"""Shared builders for the S3 (EventLog/AckInbox/Reducer/Policy) test-suite.

Not a test module (no ``test_`` prefix → not collected). Provides a fluent
:class:`Seq` event-sequence builder + verdict/oracle/fixer fixtures so each test reads
as the orchestration story it exercises, not envelope boilerplate. Times are explicit
ISO-8601 strings (injected, never the wall clock — mirrors INV-3).
"""

from __future__ import annotations

from handoff_fanout.supervisor.actions import Ack
from handoff_fanout.supervisor.event_log import build_event
from handoff_fanout.supervisor.events import Event, EventType, Provenance
from handoff_fanout.supervisor.fixer import Fixer, FixerState, FixerTrigger
from handoff_fanout.supervisor.oracle import OracleScope
from handoff_fanout.supervisor.payloads import (
    AuditDone,
    ContextPatch,
    ContextPatchOp,
    ContextPatchOpKind,
    FixerDone,
    GlobalPaused,
    GlobalResumed,
    NodeAttempt,
    NodeReason,
    OracleChecked,
    OwnerOverride,
    RecoveryTarget,
    RollbackRecord,
    SnapshotTaken,
)
from handoff_fanout.supervisor.plan import Node, Plan
from handoff_fanout.supervisor.verdict import (
    BindingTarget,
    ProviderFindings,
    ProviderStatus,
    Verdict,
    VerdictValue,
)

TS = "2026-06-06T10:00:00"


def make_plan(plan_id: str = "p1", *, nodes: list[Node] | None = None) -> Plan:
    if nodes is None:
        nodes = [Node(node_id="n1", brief="do n1", base_ref="main")]
    return Plan(
        schema_version=1,
        plan_id=plan_id,
        objective="obj",
        acceptance_oracle_ref="oracle.json",
        nodes=nodes,
    )


def node(node_id: str, *, deps: list[str] | None = None, reversible: bool = True, **kw) -> Node:
    return Node(
        node_id=node_id,
        brief=f"do {node_id}",
        base_ref="main",
        deps=deps or [],
        reversible=reversible,
        **kw,
    )


# --- verdict fixtures --------------------------------------------------------


def green_verdict(bound: str = "tree1") -> Verdict:
    ok = ProviderFindings(status=ProviderStatus.OK)
    return Verdict(
        verdict=VerdictValue.GREEN,
        by="rule:any-p0p1",
        codex=ok,
        gemini=ok,
        bound_to=bound,
        findings_ref="findings/n.json",
        binding_target=BindingTarget.TREE_OID,
    )


def red_verdict(bound: str = "tree1") -> Verdict:
    codex = ProviderFindings(status=ProviderStatus.OK, p0=1, fingerprints=["fp-1"])
    gemini = ProviderFindings(status=ProviderStatus.OK)
    return Verdict(
        verdict=VerdictValue.RED,
        by="rule:any-p0p1",
        codex=codex,
        gemini=gemini,
        bound_to=bound,
        findings_ref="findings/n.json",
        binding_target=BindingTarget.TREE_OID,
        deduped_fingerprints=["fp-1"],
    )


def unknown_verdict(bound: str = "tree1") -> Verdict:
    return Verdict(
        verdict=VerdictValue.UNKNOWN,
        by="rule:any-p0p1",
        codex=ProviderFindings(status=ProviderStatus.UNAVAILABLE),
        gemini=ProviderFindings(status=ProviderStatus.OK),
        bound_to=bound,
        findings_ref="findings/n.json",
        binding_target=BindingTarget.TREE_OID,
        degraded=True,
    )


class Seq:
    """A growing, well-formed event sequence (the supervisor's single-writer output).

    Each method appends the next envelope (auto seq / expected_prev_seq / dedupe_key) so
    a test reads as the lifecycle it drives. ``ts`` defaults to :data:`TS` but is
    per-call overridable (the Sweeper/INV-3 tests inject distinct times)."""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.events: list[Event] = []

    def _add(
        self,
        type: EventType,
        payload,
        dedupe: str,
        *,
        ts: str = TS,
        provenance: Provenance | None = None,
    ) -> Seq:
        prev = self.events[-1].seq if self.events else -1
        self.events.append(
            build_event(
                plan_id=self.plan.plan_id,
                prev_seq=prev,
                type=type,
                payload=payload,
                dedupe_key=dedupe,
                ts=ts,
                provenance=provenance,
            )
        )
        return self

    def plan_created(self) -> Seq:
        return self._add(EventType.PLAN_CREATED, self.plan, "plan_created")

    def dispatch(self, n: str, attempt: int = 1, *, ts: str = TS) -> Seq:
        return self._add(
            EventType.NODE_DISPATCHED,
            NodeAttempt(node=n, attempt=attempt),
            f"dispatch:{n}:{attempt}",
            ts=ts,
        )

    def worker_done(self, n: str, attempt: int = 1, *, ts: str = TS) -> Seq:
        ack = Ack(node=n, run_id=f"run-{n}-{attempt}", attempt=attempt, tree_oid="tree1")
        return self._add(EventType.WORKER_DONE, ack, f"worker_done:{n}:{attempt}", ts=ts)

    def audit_started(self, n: str, attempt: int = 1, *, ts: str = TS) -> Seq:
        return self._add(
            EventType.AUDIT_STARTED,
            NodeAttempt(node=n, attempt=attempt),
            f"audit_started:{n}:{attempt}",
            ts=ts,
        )

    def audit_done(self, n: str, verdict: Verdict, attempt: int = 1, *, ts: str = TS) -> Seq:
        return self._add(
            EventType.AUDIT_DONE,
            AuditDone(node=n, attempt=attempt, verdict=verdict),
            f"audit_done:{n}:{attempt}",
            ts=ts,
        )

    def oracle_checked(
        self,
        n: str,
        passed: bool,
        *,
        scope: OracleScope = OracleScope.MILESTONE,
        failed: list[str] | None = None,
    ) -> Seq:
        payload = OracleChecked(
            node=n, scope=scope, passed=passed, failed_criteria=[] if passed else (failed or ["o1"])
        )
        return self._add(
            EventType.ORACLE_CHECKED,
            payload,
            f"oracle_checked:{n}:{scope.value}:{len(self.events)}",
        )

    def node_advanced(
        self, n: str, attempt: int = 1, *, provenance: Provenance | None = None
    ) -> Seq:
        return self._add(
            EventType.NODE_ADVANCED,
            NodeAttempt(node=n, attempt=attempt),
            f"advanced:{n}:{attempt}",
            provenance=provenance,
        )

    def fixer_spawned(
        self,
        n: str,
        fixer_id: str,
        *,
        trigger: FixerTrigger = FixerTrigger.VERDICT_RED,
        attempt: int = 1,
    ) -> Seq:
        fixer = Fixer(
            fixer_id=fixer_id, parent_node=n, attempt=attempt, trigger=trigger, base_ref="main"
        )
        return self._add(EventType.FIXER_SPAWNED, fixer, f"fixer_spawned:{fixer_id}")

    def fixer_done(self, n: str, fixer_id: str, state: FixerState, *, attempt: int = 1) -> Seq:
        payload = FixerDone(fixer_id=fixer_id, parent_node=n, attempt=attempt, state=state)
        return self._add(EventType.FIXER_DONE, payload, f"fixer_done:{fixer_id}")

    def node_blocked(self, n: str, reason: str = "blocked") -> Seq:
        return self._add(
            EventType.NODE_BLOCKED,
            NodeReason(node=n, reason=reason),
            f"blocked:{n}:{len(self.events)}",
        )

    def context_patched(self, ops: list[ContextPatchOp]) -> Seq:
        return self._add(
            EventType.CONTEXT_PATCHED, ContextPatch(patches=ops), f"ctx:{len(self.events)}"
        )

    def global_paused(self, reason: str = "owner", actor: str = "owner") -> Seq:
        return self._add(
            EventType.GLOBAL_PAUSED,
            GlobalPaused(reason=reason, actor=actor),
            f"paused:{len(self.events)}",
        )

    def global_resumed(self, actor: str = "owner") -> Seq:
        return self._add(
            EventType.GLOBAL_RESUMED, GlobalResumed(actor=actor), f"resumed:{len(self.events)}"
        )

    def owner_override(self, n: str, target: RecoveryTarget, *, actor: str = "owner") -> Seq:
        payload = OwnerOverride(
            node=n, target_state=target, actor=actor, reason="rescue", bound_hash="h1"
        )
        return self._add(EventType.OWNER_OVERRIDE, payload, f"override:{n}:{len(self.events)}")

    def rolled_back(self, n: str, commit: str = "deadbeef") -> Seq:
        return self._add(
            EventType.ROLLED_BACK,
            RollbackRecord(to_node=n, to_commit=commit),
            f"rollback:{n}:{len(self.events)}",
        )

    def snapshot_taken(self, through_seq: int, state_hash: str) -> Seq:
        return self._add(
            EventType.SNAPSHOT_TAKEN,
            SnapshotTaken(through_seq=through_seq, state_hash=state_hash),
            f"snapshot:{len(self.events)}",
        )


# Re-exports tests commonly need.
__all__ = [
    "TS",
    "Seq",
    "make_plan",
    "node",
    "green_verdict",
    "red_verdict",
    "unknown_verdict",
    "Ack",
    "ContextPatchOp",
    "ContextPatchOpKind",
    "FixerState",
    "FixerTrigger",
    "OracleScope",
    "RecoveryTarget",
    "Provenance",
    "build_event",
    "EventType",
    "NodeAttempt",
]
