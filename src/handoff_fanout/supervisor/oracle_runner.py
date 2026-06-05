"""S1 — OracleRunner (design §4.4 / §12 C9): run acceptance criteria, grade them.

This is the "立靶子" execution half of slice **S1**. The OracleRunner takes a
frozen :class:`~handoff_fanout.supervisor.oracle.Oracle` and runs the criteria of a
requested *scope* (``affected`` / ``milestone`` / ``final``) inside the oracle's
hermetic runtime, grading each one:

* **GREEN** — the criterion passed.
* **RED** — the criterion ran and *failed* (a real defect → routes to a Fixer, S2+).
* **UNKNOWN** — the criterion could not be *evaluated* (timeout / executor or DB
  error / misconfiguration). This is **never** a defect; it is an infrastructure
  failure that routes to escalation, never an auto-fix — the same GREEN/RED/UNKNOWN
  split the Verdict makes (INV-2; S0-fix P1-5 lesson: ``FixerTrigger`` has only
  ``verdict_RED`` / ``oracle_RED``, *not* ``oracle_UNKNOWN``). A confirmed RED
  dominates a co-occurring UNKNOWN (the RED is actionable now; the UNKNOWN
  re-evaluates after the fix re-runs the oracle).

Reconciliation with the **frozen S0 contract** (this is the crux of "rewrite on the
fixed main(5b4ee5f) contracts"): when the old S1 draft was written the
``oracle_checked`` event payload was still *open*; S0-fix (P0-1) froze it as
:class:`~handoff_fanout.supervisor.payloads.OracleChecked` (``node / scope / passed
/ failed_criteria``). So :class:`OracleRunResult` is the **rich runtime result** the
runner produces (tri-state, per-criterion detail) — NOT a new event payload — and
:meth:`OracleRunResult.to_oracle_checked` projects it onto the frozen S0 payload so
S3's reducer can emit the event without re-inventing a shape. The projection
*refuses* an UNKNOWN run (it is an ``escalated`` event, ``NodeReason`` payload — not
a "checked" result), faithfully surfacing the design intent that an oracle UNKNOWN
escalates rather than spawns a Fixer.

Execution is injected through two ports so the orchestration logic is pure and unit
tests never touch a real subprocess or DB:

* :class:`CriterionExecutor` — how a single criterion is run (default
  :class:`SubprocessExecutor`: shell for cmd/test/invariant, ``psql`` for sql).
* :class:`SandboxDb` — ``drop-recreate-from-template`` cleanup (default
  :class:`PsqlSandboxDb`), which clears schema pollution between runs so a half-
  migrated DB can never deadlock a retry (design §4.4 / §9, Round-2 red line).

🔴 **C′ red line** (design §7 / §11): the runner is hermetic and never touches Live
— :class:`PsqlSandboxDb` *refuses* to drop-recreate a database in
:data:`LIVE_DB_DENYLIST` (or one equal to its own template). Honesty: ``network``
denial is a *soft* control on a single machine (an env sentinel, design §7 "单机非
硬沙箱"); output truncation here is not yet secret-redaction (that is slice S6).

The authoritative design is ``project-files/handoff/supervisor-orchestration-
design.md`` (ERP repo) §4.4 / §12. Emitting :meth:`~OracleRunResult.to_oracle_checked`
into the single-writer event log is the reducer's job (slice S3); S1 only runs the
oracle and produces the result.
"""

from __future__ import annotations

import abc
import dataclasses
import enum
import os
import subprocess

from ._base import Contract, SchemaError
from .oracle import (
    CleanupPolicy,
    Oracle,
    OracleCriterion,
    OracleRuntime,
    OracleScope,
    OracleType,
    Severity,
)
from .payloads import OracleChecked

#: Output captured from a criterion is truncated to this many characters before it
#: is stored in a result (defense against a runaway log; NOT secret redaction —
#: that is slice S6).
_OUTPUT_MAX = 500

#: Severities that *gate* a node (mirrors the Verdict: P0/P1 are the redline). Lower
#: severities are recorded but advisory.
_REDLINE = (Severity.P0, Severity.P1)


class OracleOutcome(enum.StrEnum):
    """Grade of a criterion (or an aggregate). UNKNOWN ⟺ could-not-evaluate."""

    GREEN = "GREEN"
    RED = "RED"
    UNKNOWN = "UNKNOWN"


@dataclasses.dataclass(frozen=True)
class RawExecution:
    """The raw result of running one criterion, as produced by a
    :class:`CriterionExecutor`. Immutable so a result can't be mutated after the
    fact. ``error`` set ⟺ the executor could not run the criterion at all (distinct
    from a clean run that exited non-zero)."""

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None


class CriterionExecutor(abc.ABC):
    """Port: run a single criterion in a runtime and return its raw result."""

    @abc.abstractmethod
    def execute(self, criterion: OracleCriterion, runtime: OracleRuntime) -> RawExecution: ...


class SandboxDb(abc.ABC):
    """Port: recreate the sandbox DB from its template (cleanup)."""

    @abc.abstractmethod
    def recreate_from_template(self, db: str, db_template: str) -> None: ...


# --- result contracts (the rich S1 runtime result) --------------------------


@dataclasses.dataclass
class CriterionResult(Contract):
    """The graded outcome of one criterion."""

    id: str
    scope: OracleScope
    type: OracleType
    severity: Severity
    outcome: OracleOutcome
    attempts: int
    expect: str
    actual: str
    detail: str = ""

    def validate(self) -> None:
        if not self.id:
            raise SchemaError("CriterionResult.id required")
        if self.attempts < 1:
            raise SchemaError("CriterionResult.attempts must be >= 1")


def _gating(results: list[CriterionResult]) -> list[CriterionResult]:
    """The subset of ``results`` that actually gates the aggregate outcome: if any
    redline (P0/P1) criterion is present, only redline criteria gate (lower ones are
    advisory); otherwise every criterion gates (so a P2-only scope is not vacuously
    green). Shared by :func:`aggregate_outcome` and the OracleChecked projection so
    the two can never disagree on what "failed"."""
    redline = [r for r in results if r.severity in _REDLINE]
    return redline if redline else results


def aggregate_outcome(results: list[CriterionResult]) -> OracleOutcome:
    """Aggregate per-criterion outcomes into one scope verdict.

    Gates on the highest-severity tier *present* (see :func:`_gating`). A confirmed
    RED dominates an UNKNOWN. An empty scope is vacuously GREEN.
    """
    if not results:
        return OracleOutcome.GREEN
    gating = _gating(results)
    if any(r.outcome is OracleOutcome.RED for r in gating):
        return OracleOutcome.RED
    if any(r.outcome is OracleOutcome.UNKNOWN for r in gating):
        return OracleOutcome.UNKNOWN
    return OracleOutcome.GREEN


@dataclasses.dataclass
class OracleRunResult(Contract):
    """The graded outcome of running one scope of an oracle — the **rich S1 runtime
    result** (NOT the event payload; see :meth:`to_oracle_checked`).

    ``outcome`` must be consistent with :func:`aggregate_outcome` over ``criteria``
    — a result that claims GREEN while a redline criterion is RED is malformed and is
    rejected (fail-closed, mirroring Verdict INV-2)."""

    oracle_version: int
    scope: OracleScope
    outcome: OracleOutcome
    criteria: list[CriterionResult] = dataclasses.field(default_factory=list)
    milestone: str | None = None

    def validate(self) -> None:
        if self.oracle_version < 1:
            raise SchemaError("OracleRunResult.oracle_version must be >= 1")
        # R2 codex P2-3: criterion ids must be unique. The runner can't produce a
        # dup (S0 ``Oracle`` already rejects duplicate criterion ids), but a
        # hand-built result with dupes would make ``gating_failures()`` emit a
        # duplicate id and blow up the OracleChecked projection downstream — fail
        # closed here so the rich result is self-consistent on its own.
        ids = [c.id for c in self.criteria]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise SchemaError(f"OracleRunResult.criteria has duplicate ids: {dupes}")
        off_scope = [c.id for c in self.criteria if c.scope is not self.scope]
        if off_scope:
            raise SchemaError(
                f"OracleRunResult.scope={self.scope.value} but criteria are off-scope: {off_scope}"
            )
        if self.scope is OracleScope.MILESTONE and self.milestone is None:
            raise SchemaError(
                "OracleRunResult.scope=milestone requires a `milestone` id "
                "(otherwise the result is not attributable to a milestone gate)"
            )
        if self.scope is not OracleScope.MILESTONE and self.milestone is not None:
            raise SchemaError(
                f"OracleRunResult.milestone set but scope={self.scope.value} is not milestone"
            )
        expected = aggregate_outcome(self.criteria)
        if self.outcome is not expected:
            raise SchemaError(
                f"OracleRunResult.outcome={self.outcome.value} is inconsistent with its "
                f"criteria (aggregate requires {expected.value})"
            )

    def gating_failures(self) -> list[str]:
        """The ids of the *gating* criteria that came back RED — exactly what the
        frozen ``OracleChecked`` payload calls ``failed_criteria``. Empty iff the run
        is GREEN. (Advisory/non-gating REDs are recorded in ``criteria`` but never
        gate, so they are not listed here — mirroring :func:`aggregate_outcome`.)"""
        return [c.id for c in _gating(self.criteria) if c.outcome is OracleOutcome.RED]

    def to_oracle_checked(self, node: str) -> OracleChecked:
        """Project this rich result onto the **frozen S0** ``oracle_checked`` payload
        (:class:`~handoff_fanout.supervisor.payloads.OracleChecked`) so S3's reducer
        can emit the event without inventing a shape.

        🔴 An UNKNOWN run is **refused**: it is an infrastructure failure that
        escalates (``escalated`` / ``NodeReason``), not a *checked* result —
        ``FixerTrigger`` has no ``oracle_UNKNOWN`` (S0-fix P1-5). Collapsing UNKNOWN
        into ``passed=false`` would let S3 mis-route an infra failure to a Fixer, so
        the boundary is fail-closed here rather than silently lossy.
        """
        if self.outcome is OracleOutcome.UNKNOWN:
            raise SchemaError(
                "cannot project an UNKNOWN oracle run to oracle_checked: an UNKNOWN is "
                "an infra escalation (emit `escalated`/NodeReason), not a checked "
                "result — FixerTrigger has no oracle_UNKNOWN (design §5 / S0-fix P1-5)"
            )
        return OracleChecked(
            node=node,
            scope=self.scope,
            passed=self.outcome is OracleOutcome.GREEN,
            failed_criteria=self.gating_failures(),
        )


# --- the runner --------------------------------------------------------------


class OracleRunner:
    """Runs an oracle's criteria for a scope and grades them (design §4.4 / C9)."""

    def __init__(
        self,
        oracle: Oracle,
        *,
        expected_oracle_hash: str | None = None,
        executor: CriterionExecutor | None = None,
        sandbox_db: SandboxDb | None = None,
    ) -> None:
        # R2 codex P1-2: enforce the judge contract at the runner *boundary*, not
        # only at S0 construction. A re-validate fails closed if the oracle was
        # mutated after construction or built through a deserialization gap; and an
        # ``expected_oracle_hash`` (the value the owner locked, plan_draft §13) makes
        # INV-5 a real enforcement here rather than a helper nobody calls.
        oracle.validate()
        if expected_oracle_hash is not None:
            from .plan_draft import oracle_hash

            actual = oracle_hash(oracle)
            if actual != expected_oracle_hash:
                raise SchemaError(
                    "OracleRunner: oracle drifted from its approved lock "
                    f"(locked={expected_oracle_hash}, actual={actual}) — refusing to "
                    "run an oracle the owner did not approve (INV-5)"
                )
        self._oracle = oracle
        self._executor = executor or SubprocessExecutor()
        self._sandbox_db = sandbox_db or PsqlSandboxDb()

    def run(self, scope: OracleScope, milestone: str | None = None) -> OracleRunResult:
        """Run every criterion of ``scope`` (filtered to ``milestone`` when given for
        the MILESTONE scope) and return the aggregated result."""
        if scope is OracleScope.MILESTONE and milestone is None:
            raise SchemaError(
                "OracleRunner.run(MILESTONE) requires a `milestone` id — a milestone "
                "gate is attributable to exactly one milestone (design §4.4)"
            )
        selected = [c for c in self._oracle.criteria if self._matches(c, scope, milestone)]
        self._cleanup_if_needed(selected)
        results = [self.run_criterion(c) for c in selected]
        return OracleRunResult(
            oracle_version=self._oracle.oracle_version,
            scope=scope,
            outcome=aggregate_outcome(results),
            criteria=results,
            milestone=milestone if scope is OracleScope.MILESTONE else None,
        )

    def run_criterion(self, criterion: OracleCriterion) -> CriterionResult:
        """Run one criterion, retrying up to ``flaky_retries`` extra times until it
        is GREEN (transient-flake tolerance, opt-in per criterion)."""
        max_attempts = 1 + max(0, criterion.flaky_retries)
        attempts = 0
        outcome = OracleOutcome.UNKNOWN
        actual = ""
        detail = ""
        while attempts < max_attempts:
            attempts += 1
            # R2 gemini P2: a SQL retry must not run on the DB the failed attempt
            # dirtied — that reproduces the §9 schema-pollution deadlock at the
            # micro-retry level. The first attempt rides the run-level cleanup; a
            # retry (attempt>1) recreates first. (Caveat: this resets DB state, so
            # flaky_retries on SQL criteria that *chain* shared state is unsupported
            # — each retry starts from the clean template.)
            if attempts > 1 and criterion.type is OracleType.SQL:
                self._cleanup_if_needed([criterion])
            raw = self._executor.execute(criterion, self._oracle.runtime)
            outcome, actual, detail = _decide(criterion, raw)
            if outcome is OracleOutcome.GREEN:
                break
        return CriterionResult(
            id=criterion.id,
            scope=criterion.scope,
            type=criterion.type,
            severity=criterion.severity,
            outcome=outcome,
            attempts=attempts,
            expect=criterion.expect,
            actual=actual[:_OUTPUT_MAX],
            detail=detail[:_OUTPUT_MAX],
        )

    def _cleanup_if_needed(self, selected: list[OracleCriterion]) -> None:
        runtime = self._oracle.runtime
        has_sql = any(c.type is OracleType.SQL for c in selected)
        if (
            has_sql
            and runtime.cleanup is CleanupPolicy.DROP_RECREATE_FROM_TEMPLATE
            and runtime.db
            and runtime.db_template
        ):
            # Recreate once before the SQL criteria so they share a clean schema
            # state (criteria within a run intentionally share the DB; pollution
            # from a *previous* run is what deadlocks a retry — design §9).
            self._sandbox_db.recreate_from_template(runtime.db, runtime.db_template)

    @staticmethod
    def _matches(c: OracleCriterion, scope: OracleScope, milestone: str | None) -> bool:
        if c.scope is not scope:
            return False
        if scope is OracleScope.MILESTONE and milestone is not None:
            return c.milestone == milestone
        return True


def _decide(criterion: OracleCriterion, raw: RawExecution) -> tuple[OracleOutcome, str, str]:
    """Map a raw execution to (outcome, actual, detail) by criterion type.

    Could-not-evaluate (error / timeout / no exit code / misconfigured expect) is
    always UNKNOWN, never RED.
    """
    if raw.error is not None:
        return OracleOutcome.UNKNOWN, "", f"executor error: {raw.error}"
    if raw.timed_out:
        return OracleOutcome.UNKNOWN, "", "timed out"

    if criterion.type is OracleType.SQL:
        actual = raw.stdout.strip()
        passed = actual == criterion.expect.strip()
        return (
            OracleOutcome.GREEN if passed else OracleOutcome.RED,
            actual,
            f"sql result, expected {criterion.expect.strip()!r}",
        )

    # exit-code based types
    if raw.exit_code is None:
        return OracleOutcome.UNKNOWN, "", "no exit code from executor"
    code = raw.exit_code
    actual = str(code)

    # R2 gemini P1: a negative exit code means the process was killed by a signal
    # (subprocess returns -signum) — OOM-killer / SIGKILL / a node restart truncating
    # the run. That is an infrastructure failure, NOT a defect: grading it RED would
    # route an OOM to an LLM Fixer that hallucinates fixes against a memory error
    # (INV-2 / S0-fix P1-5 anti-storm). Could-not-evaluate ⟹ UNKNOWN, never RED.
    if code < 0:
        return OracleOutcome.UNKNOWN, actual, f"process killed by signal {-code}"

    if criterion.type is OracleType.CMD:
        try:
            want = int(criterion.expect.strip())
        except ValueError:
            return (
                OracleOutcome.UNKNOWN,
                actual,
                f"misconfigured oracle: expect={criterion.expect!r} is not an exit code",
            )
        passed = code == want
        return (
            OracleOutcome.GREEN if passed else OracleOutcome.RED,
            actual,
            f"expected exit {want}",
        )

    if criterion.type is OracleType.TEST:
        # pytest exit codes: 0 pass, 1 failures, else infra (2 interrupted, 3
        # internal, 4 usage, 5 no tests) — could not confirm → UNKNOWN.
        if code == 0:
            return OracleOutcome.GREEN, actual, "tests passed"
        if code == 1:
            return OracleOutcome.RED, actual, "test failures"
        return OracleOutcome.UNKNOWN, actual, f"pytest exit {code} (could not run tests)"

    # INVARIANT (the closed enum leaves nothing else)
    passed = code == 0
    return (OracleOutcome.GREEN if passed else OracleOutcome.RED, actual, f"invariant exit {code}")


# --- default executor + sandbox DB (real; never run against Live) ------------


class SubprocessExecutor(CriterionExecutor):
    """Default executor: runs cmd/test/invariant as a shell command and sql through
    ``psql``, inside the oracle's hermetic runtime (cwd / venv / timeout / seeded /
    network-deny env sentinel).

    The criterion ``spec`` is run with ``shell=True``. That is safe because the
    oracle is an owner-approved, write-protected artefact (the §7 threat is a worker
    poisoning the *environment*, not the oracle spec itself). Crucially the env is
    built from an **allowlist** (:data:`_ENV_ALLOWLIST`), never inherited from the
    host — so the oracle spec can't read ``DATABASE_URL`` / ``PG*`` / cloud creds and
    punch through the soft sandbox (R2 consensus gemini P0 + codex P2-5 / C′).
    """

    def execute(self, criterion: OracleCriterion, runtime: OracleRuntime) -> RawExecution:
        if criterion.type is OracleType.SQL:
            return self._run_sql(criterion, runtime)
        return self._run_shell(criterion, runtime)

    def _run_shell(self, criterion: OracleCriterion, runtime: OracleRuntime) -> RawExecution:
        try:
            proc = subprocess.run(  # noqa: S602 (owner-approved oracle spec, not user input)
                criterion.spec,
                shell=True,
                cwd=runtime.cwd,
                env=self._build_env(runtime),
                capture_output=True,
                text=True,
                timeout=runtime.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return RawExecution(timed_out=True)
        except OSError as exc:
            return RawExecution(error=str(exc))
        return RawExecution(
            exit_code=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or ""
        )

    def _run_sql(self, criterion: OracleCriterion, runtime: OracleRuntime) -> RawExecution:
        if not runtime.db:
            return RawExecution(error="SQL criterion but runtime.db is unset")
        cmd = ["psql", "-d", runtime.db, "-tAX", "-c", criterion.spec]
        try:
            proc = subprocess.run(
                cmd,
                cwd=runtime.cwd,
                env=self._build_env(runtime),
                capture_output=True,
                text=True,
                timeout=runtime.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return RawExecution(timed_out=True)
        except OSError as exc:
            return RawExecution(error=str(exc))
        if proc.returncode != 0:
            return RawExecution(
                error=f"psql exit {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
            )
        return RawExecution(exit_code=0, stdout=proc.stdout or "", stderr=proc.stderr or "")

    #: The ONLY host env vars an oracle inherits. R2 consensus (gemini P0 + codex
    #: P2-5): inheriting the supervisor's full ``os.environ`` would hand a
    #: ``shell=True`` oracle spec ``DATABASE_URL`` / ``PG*`` / ``AWS_*`` / cloud
    #: tokens — punching straight through the soft sandbox to Live data / creds (the
    #: C′ red line). So the env is built from an allowlist, never inherited: the
    #: oracle gets a clean shell + only its own ``HANDOFF_ORACLE_*`` knobs. Anything
    #: a sandbox legitimately needs (e.g. a sandbox-only ``PGHOST``/``PGUSER``) must
    #: be set explicitly on the runtime, never leaked from the host.
    _ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "SHELL")

    @classmethod
    def _build_env(cls, runtime: OracleRuntime) -> dict[str, str]:
        env = {k: os.environ[k] for k in cls._ENV_ALLOWLIST if k in os.environ}
        # Soft network control (design §7: single machine is not a hard sandbox).
        env["HANDOFF_ORACLE_NETWORK"] = runtime.network.value
        env["HANDOFF_ORACLE_FIXTURE_VERSION"] = str(runtime.fixture_version)
        if runtime.seed is not None:
            env["HANDOFF_ORACLE_SEED"] = str(runtime.seed)
            env["PYTHONHASHSEED"] = str(runtime.seed)
        if runtime.time_freeze:
            env["HANDOFF_ORACLE_TIME_FREEZE"] = runtime.time_freeze
        if runtime.db:
            env["HANDOFF_ORACLE_DB"] = runtime.db
        if runtime.fixtures:
            env["HANDOFF_ORACLE_FIXTURES"] = runtime.fixtures
        if runtime.venv:
            env["VIRTUAL_ENV"] = runtime.venv
            env["PATH"] = f"{runtime.venv}/bin:" + env.get("PATH", "")
        return env


#: Databases the sandbox runner must NEVER drop-recreate (C′ red line) — kept as
#: defense-in-depth *behind* the positive sandbox-marker allowlist below. ``erp`` is
#: the live ERP DB (``.env`` ``POSTGRES_DB=erp``); the rest are real/system DBs an
#: oracle could be mis-pointed at (ERP memory: erp_real / erp_dogfood live stacks).
LIVE_DB_DENYLIST = frozenset(
    {"erp", "erp_real", "erp_system", "erp_dogfood", "postgres", "template0", "template1"}
)

#: The drop target's normalized name MUST contain this marker — a positive allowlist
#: (R2 consensus: codex P1-1 + gemini P1). A bare denylist is fragile (case /
#: whitespace / connection-string bypass) and ERP-specific (it would silently no-op
#: for any other project's live DB names, e.g. ``dharmaxis``). Requiring a sandbox
#: marker is fail-closed by default: an un-marked name is refused, whatever it is.
DEFAULT_SANDBOX_MARKER = "sandbox"

#: Characters that make a "db name" actually a connection string / path — refused so
#: ``postgres://host/erp`` or ``host:5432/erp`` can't smuggle a live DB past the
#: exact-name denylist (R2 codex P1-1). ``:`` is intentionally allowed: the design's
#: own example db is ``sandbox:erp_test`` (a scheme-style sandbox label), but ``://``
#: (a real URI) is rejected.
_CONNSTRING_CHARS = ("/", "\\", "@", "?", "#", " ", "\t", "\n", "\r")


class LiveDbError(RuntimeError):
    """Raised when a drop-recreate would hit a Live (non-sandbox) database (C′)."""


class PsqlSandboxDb(SandboxDb):
    """Default sandbox DB cleanup via ``dropdb``/``createdb --template`` — guarded so
    it can never touch a Live database (C′ red line, design §7 / §11).

    Two layers, fail-closed (R2 hardening, both brains):

    * **positive allowlist** — the drop *target* (``db``) must contain
      ``sandbox_marker`` (normalized, default ``"sandbox"``). An un-marked name is
      refused, so the guard is project-agnostic instead of relying on an ERP-specific
      denylist that silently no-ops elsewhere.
    * **denylist + normalization** — defense-in-depth: known live names (normalized:
      stripped + casefolded, so ``"ERP"`` / ``" erp "`` are caught) and
      connection-string-shaped inputs are refused for both db and template.

    The marker requirement is on the *drop target* only; the template is read by
    ``createdb`` (never dropped), so it just has to pass the denylist + char checks.
    Honesty (design §7): this is a soft, single-machine guard against mistakes/drift,
    not a hard sandbox against a determined local attacker.
    """

    def __init__(
        self,
        *,
        denylist: frozenset[str] = LIVE_DB_DENYLIST,
        extra_denied: tuple[str, ...] = (),
        sandbox_marker: str = DEFAULT_SANDBOX_MARKER,
    ):
        self._denied = {self._normalize(d) for d in (set(denylist) | set(extra_denied))}
        self._marker = self._normalize(sandbox_marker)
        if not self._marker:
            raise LiveDbError("PsqlSandboxDb sandbox_marker must be non-empty (fail-closed)")

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().casefold()

    def recreate_from_template(self, db: str, db_template: str) -> None:
        self._guard(db, db_template)
        subprocess.run(["dropdb", "--if-exists", db], check=True, capture_output=True, text=True)
        subprocess.run(
            ["createdb", db, "--template", db_template], check=True, capture_output=True, text=True
        )

    def _guard(self, db: str, db_template: str) -> None:
        nd, nt = self._normalize(db), self._normalize(db_template)
        # Connection-string / path / whitespace inputs are refused for both — checked
        # on the RAW value (not the normalized one): the original string is what
        # ``dropdb`` actually receives, so validating a stripped/normalized variant
        # would let a name the guard never saw (e.g. " sandbox_db ") be executed.
        for raw, norm, label in ((db, nd, "db"), (db_template, nt, "db_template")):
            if not norm:
                raise LiveDbError(f"refusing an empty/blank {label}: {raw!r}")
            if "://" in raw or any(c in raw for c in _CONNSTRING_CHARS):
                raise LiveDbError(
                    f"refusing a {label} that looks like a connection string / path "
                    f"(unsafe chars): {raw!r}"
                )
        # Denylist (normalized) — defense-in-depth behind the allowlist.
        if nd in self._denied:
            raise LiveDbError(
                f"refusing to drop-recreate a Live database: {db!r} "
                "(C′ red line — the oracle is hermetic and never touches Live)"
            )
        if nt in self._denied:
            raise LiveDbError(f"refusing to use a Live database as a template: {db_template!r}")
        # Positive allowlist — the drop target MUST be marked as a sandbox.
        if self._marker not in nd:
            raise LiveDbError(
                f"refusing to drop-recreate {db!r}: a sandbox DB name must contain "
                f"{self._marker!r} (allowlist, not just a denylist — C′ red line)"
            )
        if nd == nt:
            raise LiveDbError(
                f"refusing to recreate {db!r} from itself (db must differ from db_template)"
            )
