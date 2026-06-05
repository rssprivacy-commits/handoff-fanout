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

🔴 **C′ red line** (design §7 / §11): the runner is hermetic and must never touch
Live. The soft-isolation layer (s1-fix hardening): every subprocess (shell / sql /
``dropdb`` / ``createdb``) runs under a **sanitized allowlist env** with no host
``PG*`` / ``DATABASE_URL`` / cloud creds and a sandbox ``HOME``
(:func:`_sanitized_base_env`); :class:`PsqlSandboxDb` *refuses* a drop target that is
not a plain identifier (:data:`_SAFE_DB_NAME` — no ``--host=`` / ``service=`` / flag
injection), is on :data:`LIVE_DB_DENYLIST`, lacks the sandbox marker, or equals its
template, and passes names after a ``--`` argv terminator. Honesty (design §7 "单机非
硬沙箱"): this is the *soft* layer — it stops a *misconfigured / poisoned env* from
reaching Live, but ``network=deny`` is an advisory sentinel (no enforced egress
block) and a same-user ``shell=True`` spec can still read absolute paths / the
network. Hard isolation (OS user / container / seccomp) is the §7 防恶意层, **not
promised this slice**. Output truncation here is not yet secret-redaction (slice S6).

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
import re
import subprocess
import tempfile
from collections.abc import Mapping

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

#: Reserved criterion id for the synthetic UNKNOWN produced when sandbox DB cleanup
#: fails (infra failure / C′ guard refusal → escalate, never a defect — s1-fix P1).
#: The dunder marks it runner-internal, distinct from any owner-authored id.
_CLEANUP_FAILURE_ID = "__sandbox_cleanup__"

#: Severities that *gate* a node (mirrors the Verdict: P0/P1 are the redline). Lower
#: severities are recorded but advisory.
_REDLINE = (Severity.P0, Severity.P1)


# --- C′ red line: sanitized subprocess env + strict sandbox-db naming ---------
# (design §7 two-layer honesty: this is the *soft* default layer — env hygiene +
# arg-injection-proof db ops + a fail-closed name guard. It shrinks the blast radius
# of a poisoned oracle/runtime on a single same-user machine; it is NOT a hard
# sandbox against a determined local attacker — that is the §7 防恶意层 / OS-level
# isolation (independent user, read-only mount, container), explicitly *not promised*
# this slice. The honesty is load-bearing: do not read "sanitized env + name guard"
# as "cannot touch Live under any circumstance".)

#: The ONLY host env vars any oracle subprocess (shell / sql / dropdb / createdb)
#: inherits. A *closed* allowlist — nothing else from ``os.environ`` ever reaches a
#: subprocess, so a worker-poisoned runtime / ``shell=True`` spec cannot read the
#: supervisor's ``PG*`` / ``DATABASE_URL`` / ``AWS_*`` / cloud tokens and punch
#: through the soft sandbox to Live data or creds (C′; R2 consensus gemini P0 + codex
#: P2-5, extended by the s1-fix audit to cover ``dropdb``/``createdb`` too, which
#: previously inherited the full host env). ``HOME`` is intentionally absent: it is
#: set explicitly per call site to a sandbox path so a ``shell=True`` spec / libpq
#: cannot reach ``~/.aws`` / ``~/.ssh`` / ``~/.pgpass`` / ``~/.pg_service.conf`` under
#: the real home (s1-fix codex P0-3 / gemini P1 blast-radius shrink).
_HOST_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TMPDIR", "TZ", "SHELL")


def _sanitized_base_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The sanitized env shared by *every* oracle subprocess (C′).

    Built from the closed :data:`_HOST_ENV_ALLOWLIST` (never the full
    ``os.environ``), then overlaid with the supervisor-provided sandbox knobs
    (``extra`` — e.g. a sandbox-only ``PGHOST`` / ``PGUSER`` / ``PGPASSWORD`` /
    ``PGPASSFILE``). Those knobs come from the *trusted construction site* (whoever
    builds the runner / executor / sandbox), **never** from the worker-controllable
    oracle artefact — so a poisoned ``oracle.json`` / ``runtime`` cannot redirect a
    subprocess to a Live server. ``HOME`` is deliberately absent (set per call site
    to a sandbox path, never the real home)."""
    env = {k: os.environ[k] for k in _HOST_ENV_ALLOWLIST if k in os.environ}
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


#: A ``dropdb`` / ``createdb`` target (or template) must match this — a *plain*
#: database identifier: a leading letter / digit / underscore (never ``-`` / ``=`` /
#: a flag) followed only by ``[A-Za-z0-9_.:-]``. This refuses option injection
#: (``--host=sandbox``, ``-h``), libpq conninfo forms (``service=...`` /
#: ``host=... port=...``), and any ``=``- or whitespace-bearing string *before* the
#: ``--`` argv terminator even applies — so a forged ``runtime.db`` can never be
#: parsed as an option and drop the wrong DB (s1-fix: codex P0/#2 + gemini P0). ``:``
#: stays allowed (the design's own ``sandbox:erp_test`` label); URI / path shapes are
#: caught separately by :data:`_CONNSTRING_CHARS` for a clearer message.
_SAFE_DB_NAME = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.:-]*\Z")


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
        the MILESTONE scope) and return the aggregated result.

        A failure to *prepare* the run (sandbox DB cleanup raising — an infra failure,
        or the C′ guard refusing a Live DB) is **never** a defect: it yields an
        UNKNOWN run that escalates to a human, exactly like a per-criterion
        could-not-evaluate (s1-fix codex/gemini P1; design §4.4 / §9). It is caught,
        not re-raised, so a dirty / unreachable / mis-pointed sandbox DB cannot crash
        the pure-script supervisor turn (§13).
        """
        if scope is OracleScope.MILESTONE and milestone is None:
            raise SchemaError(
                "OracleRunner.run(MILESTONE) requires a `milestone` id — a milestone "
                "gate is attributable to exactly one milestone (design §4.4)"
            )
        selected = [c for c in self._oracle.criteria if self._matches(c, scope, milestone)]
        try:
            self._cleanup_if_needed(selected)
        except (LiveDbError, subprocess.SubprocessError, OSError) as exc:
            return self._cleanup_failure_result(scope, milestone, exc)
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
            # R2 gemini P2 + s1-fix gemini P1: a retry must not run on the DB the
            # failed attempt dirtied — that reproduces the §9 schema-pollution
            # deadlock at the micro-retry level. The first attempt rides the run-level
            # cleanup; a retry (attempt>1) recreates first. The rebuild is gated on
            # the runtime being DB-bearing (inside ``_cleanup_if_needed``), NOT on the
            # criterion type: a ``test`` / ``cmd`` is run as a shell command and can
            # equally hit the DB (an integration test, a migration), so gating on
            # ``OracleType.SQL`` left those retries running on a polluted DB. (Caveat:
            # each retry restarts from the clean template, so flaky_retries on criteria
            # that *chain* shared DB state is unsupported.) A cleanup that itself fails
            # is an infra failure → UNKNOWN, never RED: don't retry onto a DB we could
            # not reset (s1-fix P1; same escalate-not-autofix rule as the run level).
            if attempts > 1:
                try:
                    self._cleanup_if_needed([criterion])
                except (LiveDbError, subprocess.SubprocessError, OSError) as exc:
                    outcome = OracleOutcome.UNKNOWN
                    actual = ""
                    detail = f"sandbox DB cleanup before retry {attempts} failed: {exc}"
                    break
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
        """Recreate the sandbox DB from its template before running ``selected`` when
        the runtime is DB-bearing with drop-recreate cleanup.

        s1-fix gemini/codex P1: the trigger is a **DB-bearing runtime**
        (``cleanup=drop-recreate`` + ``db`` + ``db_template``), NOT "a SQL criterion
        is present". A ``test`` / ``cmd`` criterion is run as a shell command and can
        equally hit the DB (an integration test, a migration), so gating the recreate
        on ``OracleType.SQL`` left those paths running on a DB a previous run polluted
        and deadlocking a retry (§9 red line). An empty selection touches nothing, so
        it skips the recreate — no point dropping a DB no criterion will use (and it
        keeps a no-criteria scope from doing surprise DB I/O)."""
        if not selected:
            return
        runtime = self._oracle.runtime
        if (
            runtime.cleanup is CleanupPolicy.DROP_RECREATE_FROM_TEMPLATE
            and runtime.db
            and runtime.db_template
        ):
            # Recreate once before the criteria so they share a clean schema state
            # (criteria within a run intentionally share the DB; pollution from a
            # *previous* run is what deadlocks a retry — design §9).
            self._sandbox_db.recreate_from_template(runtime.db, runtime.db_template)

    def _cleanup_failure_result(
        self, scope: OracleScope, milestone: str | None, exc: BaseException
    ) -> OracleRunResult:
        """Project an infra failure to *prepare* a run (sandbox cleanup raised) as a
        whole-run **UNKNOWN** (s1-fix P1; never a defect — design §4.4 / §9).

        Modelled as a single P0 synthetic criterion (id :data:`_CLEANUP_FAILURE_ID`)
        so the rich result is self-consistent — ``aggregate_outcome`` derives UNKNOWN,
        and :meth:`OracleRunResult.to_oracle_checked` *refuses* it (an UNKNOWN
        escalates / ``NodeReason``, it does not spawn a Fixer; ``FixerTrigger`` has no
        ``oracle_UNKNOWN``). The ``detail`` names the cause — including a C′
        ``LiveDbError`` guard refusal — so the escalation surfaces *why* loudly to the
        owner instead of being swallowed."""
        crit = CriterionResult(
            id=_CLEANUP_FAILURE_ID,
            scope=scope,
            type=OracleType.SQL,
            severity=Severity.P0,
            outcome=OracleOutcome.UNKNOWN,
            attempts=1,
            expect="sandbox DB recreated from template (clean state)",
            actual="sandbox DB cleanup failed — run could not be prepared",
            detail=f"{type(exc).__name__}: {exc}"[:_OUTPUT_MAX],
        )
        return OracleRunResult(
            oracle_version=self._oracle.oracle_version,
            scope=scope,
            outcome=OracleOutcome.UNKNOWN,
            criteria=[crit],
            milestone=milestone if scope is OracleScope.MILESTONE else None,
        )

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

    The criterion ``spec`` is run with ``shell=True``. That is safe *only* in the
    soft-isolation sense of design §7: the oracle is an owner-approved, write-
    protected artefact (the §7 threat is a worker poisoning the *environment*, not
    the oracle spec). Crucially the env is built from a **closed allowlist**
    (:data:`_HOST_ENV_ALLOWLIST` via :func:`_sanitized_base_env`), never inherited
    from the host — so the spec can't read ``DATABASE_URL`` / ``PG*`` / cloud creds
    and punch through to Live (R2 gemini P0 + codex P2-5 / C′). ``HOME`` is pinned to
    the sandbox ``cwd`` so ``~/.aws`` / ``~/.ssh`` / ``~/.pgpass`` resolve into the
    disposable worktree, not the real home (s1-fix codex P0-3 blast-radius shrink).

    🔴 §7 honesty (do not over-read): this is the *soft* layer. On a single same-user
    machine a ``shell=True`` spec still has the user's uid — it can reach the network
    (``HANDOFF_ORACLE_NETWORK=deny`` is an advisory env sentinel, not an enforced
    egress block) and any file the user can read by absolute path. Hard isolation
    (network namespace / seccomp / container / independent OS user) is the §7 防恶意层
    and is **not promised this slice**. The env allowlist + sandbox ``HOME`` shrink the
    blast radius of a *poisoned environment*; they are not a jail.

    Sandbox connection knobs (a sandbox-only ``PGHOST`` / ``PGUSER`` / ``PGPASSWORD``
    / ``PGPASSFILE`` for the ``psql`` path) are passed at construction via
    ``sandbox_env`` — i.e. by the *trusted supervisor*, never carried on the worker-
    controllable oracle runtime — and are the only ``PG*`` an oracle subprocess sees.
    """

    def __init__(self, *, sandbox_env: Mapping[str, str] | None = None) -> None:
        # Supervisor-provided sandbox knobs (e.g. sandbox-only PGHOST/PGUSER/
        # PGPASSWORD). Trusted construction-site input — NOT from the worker-
        # controllable oracle artefact — so a poisoned oracle.json can never redirect
        # psql to a Live server. Empty by default: with no explicit PG*, libpq uses
        # local defaults (unix socket / current user), the fail-closed sandbox posture.
        self._sandbox_env = dict(sandbox_env or {})

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
        # s1-fix codex #3 defense-in-depth: ``psql -d <db>`` accepts a libpq *conninfo*
        # as well as a plain name, so a runtime.db like ``service=prod`` / ``host=live
        # dbname=erp`` would connect somewhere other than the sandbox. runtime.db is
        # owner-locked (INV-5, re-checked at the runner boundary), but reject a
        # non-plain target anyway → could-not-evaluate (UNKNOWN), never a silent Live
        # connect. ``-w`` forbids an interactive password prompt (a hermetic oracle
        # must never block on stdin; with PG* sanitized out, a prompt would otherwise
        # hang until timeout).
        if not _SAFE_DB_NAME.match(runtime.db):
            return RawExecution(
                error=f"refusing a non-plain runtime.db for psql -d (conninfo / option "
                f"injection): {runtime.db!r}"
            )
        cmd = ["psql", "-w", "-d", runtime.db, "-tAX", "-c", criterion.spec]
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

    def _build_env(self, runtime: OracleRuntime) -> dict[str, str]:
        # Closed host allowlist + the trusted sandbox knobs (never the host's PG* /
        # cloud creds — C′, see _sanitized_base_env / _HOST_ENV_ALLOWLIST).
        env = _sanitized_base_env(self._sandbox_env)
        # HOME → the sandbox worktree, NOT the host home: a shell=True spec resolving
        # ``~/.aws`` / ``~/.ssh`` / ``~/.pgpass`` / ``~/.pg_service.conf`` lands inside
        # the disposable worktree, not the user's real credential files (s1-fix codex
        # P0-3). §7 honesty: soft on a single same-user machine — the spec can still
        # read those files by absolute path; this only closes the ``~`` shortcut.
        env["HOME"] = runtime.cwd
        # Soft network control (design §7: single machine is not a hard sandbox — this
        # is an advisory sentinel a cooperating spec may read, not an enforced block).
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

    Layers, fail-closed (R2 + s1-fix hardening, both brains):

    * **strict name shape** — ``db`` and ``db_template`` must be *plain* identifiers
      (:data:`_SAFE_DB_NAME`): a leading letter/digit/underscore then only
      ``[A-Za-z0-9_.:-]``. This refuses ``--host=sandbox`` / ``service=prod`` /
      leading-``-`` / ``=``-bearing strings that would otherwise be parsed by
      dropdb/createdb as an *option* or a libpq *conninfo* and hit a DB the guard
      never checked (s1-fix codex #2 + gemini P0).
    * **positive allowlist** — the drop *target* (``db``) must contain
      ``sandbox_marker`` (normalized, default ``"sandbox"``). An un-marked name is
      refused, so the guard is project-agnostic instead of relying on an ERP-specific
      denylist that silently no-ops elsewhere.
    * **denylist + normalization** — defense-in-depth: known live names (normalized:
      stripped + casefolded, so ``"ERP"`` / ``" erp "`` are caught) and
      connection-string-shaped inputs are refused for both db and template.
    * **arg-injection-proof exec** — ``dropdb``/``createdb`` get the name *after* a
      ``--`` argv terminator and a **sanitized env** (no host ``PG*`` / cloud creds —
      the previous code passed no ``env=`` and inherited them all). The sandbox PG*
      is injected at construction (``env=``) by the trusted supervisor, never the
      host (s1-fix codex #2 + gemini P1).

    The marker requirement is on the *drop target* only; the template is read by
    ``createdb`` (never dropped), so it just has to pass the name + denylist checks.
    🔴 Honesty (design §7): this is a *soft*, single-machine guard against mistakes /
    drift / a poisoned env, **not** a hard sandbox against a determined same-user
    local attacker (that is the §7 防恶意层 — OS user isolation / container — not
    promised this slice). Do not read these layers as "cannot ever touch Live".
    """

    def __init__(
        self,
        *,
        denylist: frozenset[str] = LIVE_DB_DENYLIST,
        extra_denied: tuple[str, ...] = (),
        sandbox_marker: str = DEFAULT_SANDBOX_MARKER,
        env: Mapping[str, str] | None = None,
    ):
        self._denied = {self._normalize(d) for d in (set(denylist) | set(extra_denied))}
        self._marker = self._normalize(sandbox_marker)
        if not self._marker:
            raise LiveDbError("PsqlSandboxDb sandbox_marker must be non-empty (fail-closed)")
        # dropdb/createdb must NOT inherit the host's PG* / cloud creds. s1-fix codex
        # #2 + gemini P1: the previous code passed no ``env=`` at all, so a host
        # ``PGHOST`` / ``PGPASSWORD`` / ``DATABASE_URL`` pointing at Live made these
        # drop/create on the *Live* server — destroying a real DB on a mere
        # misconfiguration, no attacker needed. Same sanitized env as the executor;
        # any sandbox-only PG* is injected explicitly here by the trusted construction
        # site (it must agree with the executor's ``sandbox_env`` so SQL criteria and
        # cleanup hit the same sandbox server).
        self._env = _sanitized_base_env(env)
        # HOME → a tmp dir (never the real home) so libpq cannot read the real
        # ``~/.pgpass`` / ``~/.pg_service.conf`` and auto-redirect dropdb/createdb. §7
        # honesty: soft — with HOME *unset* libpq falls back to getpwuid → the real
        # home, so pinning it is the hardening; a sandbox pgpass is supplied via an
        # explicit ``PGPASSFILE`` in ``env``, never ambiently.
        self._env.setdefault("HOME", tempfile.gettempdir())

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().casefold()

    def recreate_from_template(self, db: str, db_template: str) -> None:
        self._guard(db, db_template)
        # ``--`` terminates option parsing so a name that somehow passed the guard can
        # never be read as a flag (defense in depth behind the strict name guard —
        # s1-fix gemini P0 ``--host=`` injection). ``env=self._env`` keeps host Live
        # creds out (codex #2). ``check=True`` raises on failure; the caller
        # (:meth:`OracleRunner.run`) turns that into an UNKNOWN escalation (s1-fix P1),
        # never a crash.
        subprocess.run(
            ["dropdb", "--if-exists", "--", db],
            check=True,
            capture_output=True,
            text=True,
            env=self._env,
        )
        subprocess.run(
            ["createdb", "--template", db_template, "--", db],
            check=True,
            capture_output=True,
            text=True,
            env=self._env,
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
            # s1-fix codex #2 + gemini P0: a name that is not a *plain* identifier can
            # be parsed by dropdb/createdb as an option or a libpq conninfo, dropping a
            # DB other than the one the guard checked (e.g. ``--host=sandbox`` →
            # ``--host`` + default DB; ``service=prod`` → a Live conninfo). The marker
            # check would pass (the string still *contains* "sandbox"), so the strict
            # shape check is the real defense — the ``--`` terminator is belt-and-
            # suspenders on top. Refuses a leading ``-``, any ``=``, and anything
            # outside ``[A-Za-z0-9_.:-]`` after the first char.
            if not _SAFE_DB_NAME.match(raw):
                raise LiveDbError(
                    f"refusing a {label} that is not a plain database name "
                    f"(option / conninfo injection — a leading '-', an '=', or "
                    f"'--host='/'service=' shapes): {raw!r}"
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
