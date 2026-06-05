"""S0 — Oracle contract (design §4.4).

The Oracle is the machine-checkable acceptance suite a node is graded against. It
runs in an **isolated runtime** (sandbox DB, network denied, frozen clock, seeded)
with ``cleanup = drop-recreate-from-template`` so schema pollution can never
deadlock a retry (design §4.4 / Round-2 fix #2). Oracle + fixtures are versioned;
a business-rule change is an oracle *amendment* event plus regression, never a
silent edit.

This module only defines the Oracle *shape*. Running it (the OracleRunner, C9) is
slice S1.
"""

from __future__ import annotations

import dataclasses
import enum

from ._base import SCHEMA_VERSION, Contract, SchemaError


class OracleScope(enum.StrEnum):
    """How wide a criterion is checked (design §4.4 ``scope``)."""

    AFFECTED = "affected"  # only what this node touched
    MILESTONE = "milestone"  # an intermediate milestone gate
    FINAL = "final"  # whole-plan acceptance


class OracleType(enum.StrEnum):
    """How a criterion is evaluated (design §4.4 ``type``)."""

    CMD = "cmd"
    SQL = "sql"
    INVARIANT = "invariant"
    TEST = "test"


class Severity(enum.StrEnum):
    """Criterion severity. P0/P1 are the redline severities that block."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class NetworkPolicy(enum.StrEnum):
    DENY = "deny"
    ALLOW = "allow"


class CleanupPolicy(enum.StrEnum):
    DROP_RECREATE_FROM_TEMPLATE = "drop-recreate-from-template"
    NONE = "none"


@dataclasses.dataclass
class OracleRuntime(Contract):
    """The hermetic environment the oracle runs in (design §4.4 ``runtime``).

    ``network`` defaults to DENY and ``cleanup`` to drop-recreate-from-template:
    the oracle is hermetic and never touches Live (design §C′). A criterion that
    legitimately needs the network is a documented amendment, not a default.
    """

    cwd: str
    venv: str | None = None
    db: str | None = None
    db_template: str | None = None
    fixtures: str | None = None
    fixture_version: int = 1
    timeout_s: int = 120
    network: NetworkPolicy = NetworkPolicy.DENY
    time_freeze: str | None = None
    seed: int | None = None
    cleanup: CleanupPolicy = CleanupPolicy.DROP_RECREATE_FROM_TEMPLATE

    def validate(self) -> None:
        if not self.cwd:
            raise SchemaError("OracleRuntime.cwd required")
        if self.timeout_s <= 0:
            raise SchemaError("OracleRuntime.timeout_s must be > 0")
        if self.fixture_version < 1:
            raise SchemaError("OracleRuntime.fixture_version must be >= 1")
        # Round-2 red line: drop-recreate-from-template is the only thing that
        # clears schema pollution between retries (design §4.4 / §9), and it needs
        # both a live db and the template to recreate it from.
        if self.cleanup is CleanupPolicy.DROP_RECREATE_FROM_TEMPLATE and (
            not self.db or not self.db_template
        ):
            raise SchemaError(
                "OracleRuntime.cleanup=drop-recreate-from-template requires both "
                "`db` and `db_template` (design §4.4 schema-pollution red line)"
            )


@dataclasses.dataclass
class OracleCriterion(Contract):
    """One acceptance check (design §4.4 ``criteria[]``)."""

    id: str
    scope: OracleScope
    type: OracleType
    spec: str
    expect: str
    severity: Severity
    milestone: str | None = None
    flaky_retries: int = 0

    def validate(self) -> None:
        if not self.id:
            raise SchemaError("OracleCriterion.id required")
        if not self.spec:
            raise SchemaError("OracleCriterion.spec required")
        if not self.expect:
            raise SchemaError("OracleCriterion.expect required")
        if self.flaky_retries < 0:
            raise SchemaError("OracleCriterion.flaky_retries must be >= 0")
        if self.scope is OracleScope.MILESTONE and not self.milestone:
            raise SchemaError(
                "OracleCriterion.scope=milestone requires a `milestone` id "
                "(otherwise oracle layering is undecidable)"
            )


@dataclasses.dataclass
class Oracle(Contract):
    """A versioned acceptance suite (design §4.4)."""

    schema_version: int
    oracle_version: int
    runtime: OracleRuntime
    criteria: list[OracleCriterion] = dataclasses.field(default_factory=list)

    def validate(self) -> None:
        if not 1 <= self.schema_version <= SCHEMA_VERSION:
            raise SchemaError(
                f"Oracle.schema_version must be in 1..{SCHEMA_VERSION} (fail-closed), "
                f"got {self.schema_version}"
            )
        if self.oracle_version < 1:
            raise SchemaError("Oracle.oracle_version must be >= 1")
        ids = [c.id for c in self.criteria]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise SchemaError(f"Oracle.criteria has duplicate ids: {dupes}")
        # A SQL criterion touches the sandbox DB, so the runtime MUST clear schema
        # pollution between retries (design §4.4 red line / R2 codex C-P1-8).
        if any(c.type is OracleType.SQL for c in self.criteria) and (
            self.runtime.cleanup is not CleanupPolicy.DROP_RECREATE_FROM_TEMPLATE
        ):
            raise SchemaError(
                "Oracle has a SQL criterion but runtime.cleanup is not "
                "drop-recreate-from-template (schema-pollution red line)"
            )
