"""S0 — Plan / static DAG contract (design §4.1 / INV-9).

The Plan is the **static** DAG of work nodes. Once created it is never mutated in
place at runtime — a change is a ``plan_amended`` event carrying a diff + reason +
approver + hash (INV-9: 主 DAG 静态). Repair work does NOT amend the plan; it
spawns a :class:`~handoff_fanout.supervisor.fixer.Fixer` (a first-class schema, so
the DAG stays static instead of growing dynamic nodes through a back door).

This module defines the shape + structural validity (unique ids, deps resolve, no
cycle). It does NOT schedule or dispatch nodes (Dispatcher = slice S4).
"""

from __future__ import annotations

import dataclasses
import enum

from ._base import SCHEMA_VERSION, Contract, SchemaError
from .actions import SideEffect


class RiskTier(enum.StrEnum):
    L = "L"
    M = "M"
    H = "H"


class WorktreeMode(enum.StrEnum):
    ISOLATED = "isolated"
    SHARED = "shared"


class MergePolicy(enum.StrEnum):
    REBASE_THEN_FF = "rebase-then-ff"
    ESCALATE_ON_CONFLICT = "escalate-on-conflict"


class NodeType(enum.StrEnum):
    """Kind of plan node. Only ``work`` exists today (design §4.1 ``type:"work"``);
    extending the set is a contract amendment, never an ad-hoc string."""

    WORK = "work"


@dataclasses.dataclass
class Node(Contract):
    """One DAG node (design §4.1 ``nodes[]``)."""

    node_id: str
    brief: str
    base_ref: str
    type: NodeType = NodeType.WORK
    deps: list[str] = dataclasses.field(default_factory=list)
    worktree: WorktreeMode = WorktreeMode.ISOLATED
    merge_policy: MergePolicy = MergePolicy.REBASE_THEN_FF
    file_ownership: list[str] = dataclasses.field(default_factory=list)
    milestone_criteria: list[str] = dataclasses.field(default_factory=list)
    reversible: bool = True
    side_effects: list[SideEffect] = dataclasses.field(default_factory=list)
    max_fix_attempts: int = 2
    risk_tier: RiskTier = RiskTier.M

    def validate(self) -> None:
        if not self.node_id:
            raise SchemaError("Node.node_id required")
        if not self.brief:
            raise SchemaError("Node.brief required")
        if not self.base_ref:
            raise SchemaError("Node.base_ref required")
        if self.node_id in self.deps:
            raise SchemaError(f"Node {self.node_id!r} depends on itself")
        if self.max_fix_attempts < 0:
            raise SchemaError("Node.max_fix_attempts must be >= 0")
        dupe_deps = sorted({d for d in self.deps if self.deps.count(d) > 1})
        if dupe_deps:
            raise SchemaError(f"Node {self.node_id!r} has duplicate deps: {dupe_deps}")


@dataclasses.dataclass
class Plan(Contract):
    """The static DAG (design §4.1). Structural invariants enforced at
    construction: unique node ids, every dep resolves, and the graph is acyclic
    (it is a DAG by definition — INV-9)."""

    schema_version: int
    plan_id: str
    objective: str
    acceptance_oracle_ref: str
    nodes: list[Node] = dataclasses.field(default_factory=list)

    def validate(self) -> None:
        if not 1 <= self.schema_version <= SCHEMA_VERSION:
            raise SchemaError(
                f"Plan.schema_version must be in 1..{SCHEMA_VERSION} (fail-closed), "
                f"got {self.schema_version}"
            )
        if not self.plan_id:
            raise SchemaError("Plan.plan_id required")
        if not self.objective:
            raise SchemaError("Plan.objective required")
        if not self.acceptance_oracle_ref:
            raise SchemaError("Plan.acceptance_oracle_ref required")

        ids = [n.node_id for n in self.nodes]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise SchemaError(f"Plan has duplicate node ids: {dupes}")

        id_set = set(ids)
        for n in self.nodes:
            missing = [d for d in n.deps if d not in id_set]
            if missing:
                raise SchemaError(
                    f"Node {n.node_id!r} deps reference unknown nodes: {sorted(missing)}"
                )

        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        """DFS cycle detection (INV-9: the main DAG must be acyclic)."""
        graph = {n.node_id: list(n.deps) for n in self.nodes}
        WHITE, GREY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in graph}

        def visit(nid: str, stack: list[str]) -> None:
            color[nid] = GREY
            stack.append(nid)
            for dep in graph[nid]:
                if color[dep] == GREY:
                    cycle = stack[stack.index(dep) :] + [dep]
                    raise SchemaError(f"Plan DAG has a cycle: {' -> '.join(cycle)}")
                if color[dep] == WHITE:
                    visit(dep, stack)
            stack.pop()
            color[nid] = BLACK

        for nid in graph:
            if color[nid] == WHITE:
                visit(nid, [])
