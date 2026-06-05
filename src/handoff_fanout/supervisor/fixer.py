"""S0 — FixerSubworkflow contract (design §4.6 / R2 fix #3).

A Fixer is a first-class repair sub-workflow. It exists so that "the DAG is static
but repairs still happen" does not turn into a dynamic DAG through a back door
(INV-9). A Fixer runs in its own isolated worktree off the parent node's base,
goes through the same audit + oracle gates, and on DONE makes the parent re-run
its affected→milestone oracle. Fixer events (``fixer_spawned`` / ``fixer_done``)
go into the event log, so the reducer rebuilds Fixer state deterministically
rather than constructing it in memory.

This module defines the Fixer *shape* + its small lifecycle enum. The reducer that
drives it is slice S3; the full Fixer loop closes in S7.
"""

from __future__ import annotations

import dataclasses
import enum

from ._base import Contract, SchemaError
from .plan import MergePolicy, WorktreeMode


class FixerState(enum.StrEnum):
    """Fixer lifecycle (design §4.6 ``state``). Distinct from a node's
    :class:`~handoff_fanout.supervisor.states.NodeState`: a Fixer is a sub-workflow
    of a node that sits in ``BLOCKED_BY_FIX``."""

    DISPATCHED = "DISPATCHED"
    AUDITING = "AUDITING"
    DONE = "DONE"
    FAILED = "FAILED"


class FixerTrigger(enum.StrEnum):
    """Why a Fixer was spawned (design §4.6 ``trigger``)."""

    VERDICT_RED = "verdict_RED"
    ORACLE_RED = "oracle_RED"


@dataclasses.dataclass
class Fixer(Contract):
    """A repair sub-workflow attached to a parent node (design §4.6)."""

    fixer_id: str
    parent_node: str
    attempt: int
    trigger: FixerTrigger
    base_ref: str
    file_ownership: list[str] = dataclasses.field(default_factory=list)
    worktree: WorktreeMode = WorktreeMode.ISOLATED
    max_attempts: int = 2
    merge_policy: MergePolicy = MergePolicy.REBASE_THEN_FF
    state: FixerState = FixerState.DISPATCHED

    def validate(self) -> None:
        if not self.fixer_id:
            raise SchemaError("Fixer.fixer_id required")
        if not self.parent_node:
            raise SchemaError("Fixer.parent_node required")
        if not self.base_ref:
            raise SchemaError("Fixer.base_ref required")
        if self.attempt < 1:
            raise SchemaError("Fixer.attempt must be >= 1")
        if self.max_attempts < 1:
            raise SchemaError("Fixer.max_attempts must be >= 1")
        if self.attempt > self.max_attempts:
            raise SchemaError(
                f"Fixer.attempt ({self.attempt}) exceeds max_attempts ({self.max_attempts})"
            )
