"""handoff-fanout **supervisor** contracts — slice S0 (contract + state-machine freeze).

This subpackage is slice **S0** of the centralized-supervisor orchestration
redesign (authoritative design:
``project-files/handoff/supervisor-orchestration-design.md`` in the ERP repo, §4 +
§5). S0 freezes the data contracts and the state machine so later slices (S1+) do
not each invent incompatible formats. Format drift is the explicit risk S0 kills
(design §12).

**Nothing here is wired into the running handoff engine.** It is pure stdlib
(dataclasses + enums), has no side effects, and depends on nothing else in
``handoff_fanout``. Importing it touches no runtime code path (S0 红线: 只增不改
运行路径). The orchestration *logic* (reducer / dispatcher / verdict computer /
oracle runner / human-machine surface) is deliberately absent — those are S1+.

Quick map (design §4 / §5):

============================  ==========================================
``_base``                     :class:`Contract` base, strict (de)serialization
``events``                    §4.2 :class:`Event` envelope + :class:`EventType`
``plan``                      §4.1 :class:`Plan` / :class:`Node` (static DAG)
``verdict``                   §4.3 :class:`Verdict` (machine, INV-2)
``oracle``                    §4.4 :class:`Oracle` (hermetic acceptance suite)
``actions``                   §4.5 Action / Ack / Approval / DLQEntry / SideEffect
``fixer``                     §4.6 :class:`Fixer` sub-workflow
``states``                    §5 :class:`NodeState` / :class:`PlanState` + transitions
============================  ==========================================
"""

from __future__ import annotations

from ._base import SCHEMA_VERSION, Contract, SchemaError
from .actions import (
    Ack,
    Action,
    Approval,
    DLQEntry,
    JudgeManifest,
    JudgeManifestEntry,
    SideEffect,
    SideEffectKind,
)

# --- slice S2 (Audit + Verdict core) -----------------------------------------
# Additive: S2 imports the frozen S0 verdict contracts above + reuses the live
# ``codex_audit`` *public* finding-identity helpers (never modifying them) to add
# the deterministic verdict producer (C8), the provider-agnostic verifier core +
# adapters, and the ≥3-retry dual-brain runner. It defines NO new wire contract —
# its rich result IS the frozen S0 ``Verdict`` — so ALL_CONTRACTS / S1_CONTRACTS
# are untouched.
from .dual_brain import (
    DEFAULT_ATTEMPTS,
    run_dual_brain,
    run_with_retry,
)
from .event_payloads import (
    EVENT_PAYLOAD_CONTRACT,
    assert_payload_map_total,
    coerce_payload,
    validate_event_payload,
)
from .events import SUPERVISOR_WRITER, Event, EventType, Provenance
from .fixer import Fixer, FixerState, FixerTrigger
from .oracle import (
    CleanupPolicy,
    NetworkPolicy,
    Oracle,
    OracleCriterion,
    OracleRuntime,
    OracleScope,
    OracleType,
    Severity,
)

# --- slice S1 (Oracle execution + Plan draft/lock) ---------------------------
# Additive: S1 imports the frozen S0 contracts above and adds the *runtime logic*
# the S0 __init__ docstring deliberately left out ("oracle runner ... S1+"). It
# does NOT modify any S0 shape or the ALL_CONTRACTS freeze below.
from .oracle_runner import (
    DEFAULT_SANDBOX_MARKER,
    LIVE_DB_DENYLIST,
    CriterionExecutor,
    CriterionResult,
    LiveDbError,
    OracleOutcome,
    OracleRunner,
    OracleRunResult,
    PsqlSandboxDb,
    RawExecution,
    SandboxDb,
    SubprocessExecutor,
    aggregate_outcome,
)
from .payloads import (
    AuditDone,
    ContextPatch,
    ContextPatchOp,
    ContextPatchOpKind,
    FixerDone,
    GlobalPaused,
    GlobalResumed,
    IrreversibleExecuted,
    NodeAttempt,
    NodeReason,
    OracleChecked,
    OwnerOverride,
    PlanAmendment,
    RecoveryTarget,
    RollbackRecord,
    SnapshotTaken,
)
from .plan import MergePolicy, Node, NodeType, Plan, RiskTier, WorktreeMode
from .plan_draft import (
    LockedPlan,
    PlanDraft,
    amend_locked_plan,
    approve_plan,
    canonical_bytes,
    draft_plan,
    is_lock_valid,
    oracle_hash,
    plan_hash,
    verify_lock,
)
from .states import (
    ABORTABLE_NODE_STATES,
    INFORMATIONAL_EVENTS,
    INITIAL_NODE_STATE,
    KNOWN_EVENT_GAPS,
    NODE_STATE_EVENTS,
    NODE_TRANSITIONS,
    PLAN_LEVEL_EVENTS,
    PLAN_TRANSITIONS,
    TERMINAL_NODE_STATES,
    EventGap,
    NodeState,
    PlanState,
    PlanTransition,
    Transition,
    is_terminal,
    outgoing,
    reachable_node_states,
    validate_state_machine_closure,
)
from .verdict import (
    KNOWN_VERDICT_RULES,
    RULE_PREFIX,
    BindingTarget,
    ProviderFindings,
    ProviderStatus,
    Verdict,
    VerdictValue,
)
from .verdict_computer import (
    VERDICT_RULE,
    VerdictComputationError,
    compute_verdict,
    compute_verdict_value,
    deduped_fingerprints,
)
from .verifier_core import (
    AuditAdapter,
    AuditProvider,
    Binding,
    BindingError,
    BrainInvoker,
    ProviderRun,
    RawBrainOutcome,
    codex_adapter,
    gemini_adapter,
    is_bound_to,
    parse_provider_findings,
    resolve_binding,
    verify_findings,
)

#: Every concrete S0 wire contract (a :class:`Contract` subclass). Used by the
#: schema-validity test-suite to round-trip-check the whole set, and by S1+ as the
#: authoritative registry of frozen shapes.
ALL_CONTRACTS: tuple[type[Contract], ...] = (
    Provenance,
    Event,
    Node,
    Plan,
    ProviderFindings,
    Verdict,
    OracleRuntime,
    OracleCriterion,
    Oracle,
    SideEffect,
    Action,
    Ack,
    JudgeManifestEntry,
    JudgeManifest,
    Approval,
    DLQEntry,
    Fixer,
    PlanAmendment,
    ContextPatchOp,
    ContextPatch,
    RollbackRecord,
    NodeAttempt,
    NodeReason,
    AuditDone,
    OracleChecked,
    FixerDone,
    IrreversibleExecuted,
    GlobalPaused,
    GlobalResumed,
    OwnerOverride,
    SnapshotTaken,
)

#: Concrete **S1** wire contracts (new in slice S1 — Oracle execution result +
#: Plan draft/lock receipts). Kept separate from :data:`ALL_CONTRACTS` (the S0
#: freeze) so the S0 registry is untouched, but round-trip-checked the same way by
#: the S1 test-suite. ``OracleChecked`` is NOT here — it is a *frozen S0* payload
#: (already in :data:`ALL_CONTRACTS`); S1 *projects* onto it, never redefines it.
S1_CONTRACTS: tuple[type[Contract], ...] = (
    CriterionResult,
    OracleRunResult,
    PlanDraft,
    LockedPlan,
)

__all__ = [
    "SCHEMA_VERSION",
    "Contract",
    "SchemaError",
    "ALL_CONTRACTS",
    # events
    "Event",
    "EventType",
    "Provenance",
    "SUPERVISOR_WRITER",
    # event payloads
    "PlanAmendment",
    "ContextPatch",
    "ContextPatchOp",
    "ContextPatchOpKind",
    "RollbackRecord",
    "NodeAttempt",
    "NodeReason",
    "AuditDone",
    "OracleChecked",
    "FixerDone",
    "IrreversibleExecuted",
    "GlobalPaused",
    "GlobalResumed",
    "OwnerOverride",
    "RecoveryTarget",
    "SnapshotTaken",
    "EVENT_PAYLOAD_CONTRACT",
    "coerce_payload",
    "validate_event_payload",
    "assert_payload_map_total",
    # plan
    "Plan",
    "Node",
    "NodeType",
    "RiskTier",
    "WorktreeMode",
    "MergePolicy",
    # verdict
    "Verdict",
    "VerdictValue",
    "ProviderStatus",
    "ProviderFindings",
    "BindingTarget",
    "RULE_PREFIX",
    "KNOWN_VERDICT_RULES",
    # oracle
    "Oracle",
    "OracleCriterion",
    "OracleRuntime",
    "OracleScope",
    "OracleType",
    "Severity",
    "NetworkPolicy",
    "CleanupPolicy",
    # actions
    "Action",
    "Ack",
    "Approval",
    "DLQEntry",
    "SideEffect",
    "SideEffectKind",
    "JudgeManifest",
    "JudgeManifestEntry",
    # fixer
    "Fixer",
    "FixerState",
    "FixerTrigger",
    # states
    "NodeState",
    "PlanState",
    "Transition",
    "PlanTransition",
    "EventGap",
    "NODE_TRANSITIONS",
    "PLAN_TRANSITIONS",
    "NODE_STATE_EVENTS",
    "PLAN_LEVEL_EVENTS",
    "INFORMATIONAL_EVENTS",
    "KNOWN_EVENT_GAPS",
    "TERMINAL_NODE_STATES",
    "ABORTABLE_NODE_STATES",
    "INITIAL_NODE_STATE",
    "is_terminal",
    "outgoing",
    "reachable_node_states",
    "validate_state_machine_closure",
    # --- slice S1: Oracle execution (oracle_runner) ---
    "OracleRunner",
    "OracleRunResult",
    "CriterionResult",
    "OracleOutcome",
    "aggregate_outcome",
    "CriterionExecutor",
    "SandboxDb",
    "RawExecution",
    "SubprocessExecutor",
    "PsqlSandboxDb",
    "LiveDbError",
    "LIVE_DB_DENYLIST",
    "DEFAULT_SANDBOX_MARKER",
    # --- slice S1: Plan draft / lock (plan_draft) ---
    "PlanDraft",
    "LockedPlan",
    "draft_plan",
    "approve_plan",
    "amend_locked_plan",
    "verify_lock",
    "is_lock_valid",
    "plan_hash",
    "oracle_hash",
    "canonical_bytes",
    "S1_CONTRACTS",
    # --- slice S2: verdict computer (verdict_computer) ---
    "compute_verdict",
    "compute_verdict_value",
    "deduped_fingerprints",
    "VerdictComputationError",
    "VERDICT_RULE",
    # --- slice S2: verifier core + adapters (verifier_core) ---
    "Binding",
    "BindingError",
    "resolve_binding",
    "is_bound_to",
    "ProviderRun",
    "RawBrainOutcome",
    "AuditProvider",
    "AuditAdapter",
    "BrainInvoker",
    "codex_adapter",
    "gemini_adapter",
    "parse_provider_findings",
    "verify_findings",
    # --- slice S2: dual-brain retry runner (dual_brain) ---
    "run_with_retry",
    "run_dual_brain",
    "DEFAULT_ATTEMPTS",
]
