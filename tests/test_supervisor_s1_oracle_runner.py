"""S1 — OracleRunner tests (design §4.4 / §12 C9).

The OracleRunner runs an oracle's acceptance criteria in a hermetic runtime
(network denied, frozen clock, seeded, ``cleanup=drop-recreate-from-template``) and
grades each criterion GREEN / RED / UNKNOWN. The four criterion *types*
(cmd/sql/invariant/test) × three *scopes* (affected/milestone/final) are the matrix
S1 must cover. UNKNOWN ⟺ the criterion could not be *evaluated* (timeout / executor
or DB error) — never a defect (a defect is RED) — mirroring the Verdict INV-2
split (R2 P0 UNKNOWN-routing lesson).

Execution is injected (``CriterionExecutor`` / ``SandboxDb`` ports) so unit tests
mock subprocess + DB; the default ``SubprocessExecutor`` / ``PsqlSandboxDb`` are
exercised with real-but-harmless commands (``true`` / ``false`` / ``sleep``) and the
C′ live-DB guard is tested without touching any real database.

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s1_oracle_runner.py
"""

from __future__ import annotations

import dataclasses

import pytest

from handoff_fanout.supervisor import (
    CleanupPolicy,
    Oracle,
    OracleCriterion,
    OracleRuntime,
    OracleScope,
    OracleType,
    SchemaError,
    Severity,
)
from handoff_fanout.supervisor.oracle_runner import (
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
    aggregate_outcome,
)

# --- test doubles ------------------------------------------------------------


class FakeExecutor(CriterionExecutor):
    """Returns a scripted :class:`RawExecution` per criterion id. A list value is
    consumed one-per-attempt (to script flaky retries). Records the runtime it was
    handed so the runtime→executor contract can be asserted."""

    def __init__(self, by_id: dict[str, RawExecution | list[RawExecution]]):
        self._by_id = {k: list(v) if isinstance(v, list) else [v] for k, v in by_id.items()}
        self.calls: list[tuple[str, OracleRuntime]] = []

    def execute(self, criterion: OracleCriterion, runtime: OracleRuntime) -> RawExecution:
        self.calls.append((criterion.id, runtime))
        queue = self._by_id[criterion.id]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class FakeSandboxDb(SandboxDb):
    def __init__(self) -> None:
        self.recreated: list[tuple[str, str]] = []

    def recreate_from_template(self, db: str, db_template: str) -> None:
        self.recreated.append((db, db_template))


def _runtime(**kw) -> OracleRuntime:
    base = {"cwd": "/tmp/wt", "db": "sandbox:erp_test", "db_template": "erp_baseline"}
    base.update(kw)
    return OracleRuntime(**base)


def _oracle(criteria: list[OracleCriterion], runtime: OracleRuntime | None = None) -> Oracle:
    return Oracle(
        schema_version=1,
        oracle_version=3,
        runtime=runtime or _runtime(),
        criteria=criteria,
    )


def _crit(
    cid: str,
    type: OracleType,
    *,
    scope: OracleScope = OracleScope.AFFECTED,
    expect: str = "0",
    severity: Severity = Severity.P0,
    spec: str = "do-thing",
    milestone: str | None = None,
    flaky_retries: int = 0,
) -> OracleCriterion:
    return OracleCriterion(
        id=cid,
        scope=scope,
        type=type,
        spec=spec,
        expect=expect,
        severity=severity,
        milestone=milestone,
        flaky_retries=flaky_retries,
    )


# --- per-type outcome mapping ------------------------------------------------


def test_cmd_passes_when_exit_matches_expected_code() -> None:
    ex = FakeExecutor({"o1": RawExecution(exit_code=0)})
    res = OracleRunner(_oracle([_crit("o1", OracleType.CMD, expect="0")]), executor=ex).run(
        OracleScope.AFFECTED
    )
    assert res.criteria[0].outcome is OracleOutcome.GREEN


def test_cmd_fails_when_exit_does_not_match() -> None:
    ex = FakeExecutor({"o1": RawExecution(exit_code=1)})
    res = OracleRunner(_oracle([_crit("o1", OracleType.CMD, expect="0")]), executor=ex).run(
        OracleScope.AFFECTED
    )
    assert res.criteria[0].outcome is OracleOutcome.RED


def test_cmd_honours_a_nonzero_expected_code() -> None:
    ex = FakeExecutor({"o1": RawExecution(exit_code=3)})
    res = OracleRunner(_oracle([_crit("o1", OracleType.CMD, expect="3")]), executor=ex).run(
        OracleScope.AFFECTED
    )
    assert res.criteria[0].outcome is OracleOutcome.GREEN


def test_test_type_green_on_pytest_pass() -> None:
    ex = FakeExecutor({"o1": RawExecution(exit_code=0)})
    res = OracleRunner(_oracle([_crit("o1", OracleType.TEST, expect="pass")]), executor=ex).run(
        OracleScope.AFFECTED
    )
    assert res.criteria[0].outcome is OracleOutcome.GREEN


def test_test_type_red_on_pytest_failures() -> None:
    ex = FakeExecutor({"o1": RawExecution(exit_code=1)})
    res = OracleRunner(_oracle([_crit("o1", OracleType.TEST, expect="pass")]), executor=ex).run(
        OracleScope.AFFECTED
    )
    assert res.criteria[0].outcome is OracleOutcome.RED


def test_test_type_unknown_on_no_tests_collected() -> None:
    # pytest exit 5 = no tests collected → cannot confirm, infra-level UNKNOWN
    ex = FakeExecutor({"o1": RawExecution(exit_code=5)})
    res = OracleRunner(_oracle([_crit("o1", OracleType.TEST, expect="pass")]), executor=ex).run(
        OracleScope.AFFECTED
    )
    assert res.criteria[0].outcome is OracleOutcome.UNKNOWN


def test_invariant_green_on_zero_exit() -> None:
    ex = FakeExecutor({"o1": RawExecution(exit_code=0)})
    res = OracleRunner(
        _oracle([_crit("o1", OracleType.INVARIANT, expect="hold")]), executor=ex
    ).run(OracleScope.AFFECTED)
    assert res.criteria[0].outcome is OracleOutcome.GREEN


def test_invariant_red_on_nonzero_exit() -> None:
    ex = FakeExecutor({"o1": RawExecution(exit_code=2)})
    res = OracleRunner(
        _oracle([_crit("o1", OracleType.INVARIANT, expect="hold")]), executor=ex
    ).run(OracleScope.AFFECTED)
    assert res.criteria[0].outcome is OracleOutcome.RED


def test_sql_green_when_result_matches_expect() -> None:
    crit = _crit("o1", OracleType.SQL, spec="SELECT sum(debit)-sum(credit) FROM je", expect="0")
    ex = FakeExecutor({"o1": RawExecution(exit_code=0, stdout="0\n")})
    res = OracleRunner(_oracle([crit]), executor=ex, sandbox_db=FakeSandboxDb()).run(
        OracleScope.AFFECTED
    )
    assert res.criteria[0].outcome is OracleOutcome.GREEN


def test_sql_red_when_result_differs() -> None:
    crit = _crit("o1", OracleType.SQL, spec="SELECT ...", expect="0")
    ex = FakeExecutor({"o1": RawExecution(exit_code=0, stdout="500\n")})
    res = OracleRunner(_oracle([crit]), executor=ex, sandbox_db=FakeSandboxDb()).run(
        OracleScope.AFFECTED
    )
    assert res.criteria[0].outcome is OracleOutcome.RED


# --- UNKNOWN ⟺ could not evaluate (never a defect) ---------------------------


def test_timeout_is_unknown_not_red() -> None:
    ex = FakeExecutor({"o1": RawExecution(timed_out=True)})
    res = OracleRunner(_oracle([_crit("o1", OracleType.CMD)]), executor=ex).run(
        OracleScope.AFFECTED
    )
    assert res.criteria[0].outcome is OracleOutcome.UNKNOWN


def test_executor_error_is_unknown() -> None:
    ex = FakeExecutor({"o1": RawExecution(error="command not found")})
    res = OracleRunner(_oracle([_crit("o1", OracleType.CMD)]), executor=ex).run(
        OracleScope.AFFECTED
    )
    assert res.criteria[0].outcome is OracleOutcome.UNKNOWN


def test_cmd_with_non_integer_expect_is_unknown_misconfig() -> None:
    ex = FakeExecutor({"o1": RawExecution(exit_code=0)})
    res = OracleRunner(
        _oracle([_crit("o1", OracleType.CMD, expect="totally-not-a-code")]), executor=ex
    ).run(OracleScope.AFFECTED)
    assert res.criteria[0].outcome is OracleOutcome.UNKNOWN


# --- flaky retries -----------------------------------------------------------


def test_flaky_retry_recovers_to_green() -> None:
    ex = FakeExecutor({"o1": [RawExecution(exit_code=1), RawExecution(exit_code=0)]})
    res = OracleRunner(
        _oracle([_crit("o1", OracleType.CMD, expect="0", flaky_retries=1)]), executor=ex
    ).run(OracleScope.AFFECTED)
    assert res.criteria[0].outcome is OracleOutcome.GREEN
    assert res.criteria[0].attempts == 2


def test_flaky_retry_exhausted_stays_red() -> None:
    ex = FakeExecutor({"o1": [RawExecution(exit_code=1), RawExecution(exit_code=1)]})
    res = OracleRunner(
        _oracle([_crit("o1", OracleType.CMD, expect="0", flaky_retries=1)]), executor=ex
    ).run(OracleScope.AFFECTED)
    assert res.criteria[0].outcome is OracleOutcome.RED
    assert res.criteria[0].attempts == 2


def test_no_retry_runs_exactly_once() -> None:
    ex = FakeExecutor({"o1": RawExecution(exit_code=1)})
    OracleRunner(_oracle([_crit("o1", OracleType.CMD, flaky_retries=0)]), executor=ex).run(
        OracleScope.AFFECTED
    )
    assert len(ex.calls) == 1


# --- scope + milestone filtering ---------------------------------------------


def test_run_only_executes_criteria_of_the_requested_scope() -> None:
    crits = [
        _crit("a", OracleType.CMD, scope=OracleScope.AFFECTED),
        _crit("f", OracleType.CMD, scope=OracleScope.FINAL),
    ]
    ex = FakeExecutor({"a": RawExecution(exit_code=0), "f": RawExecution(exit_code=0)})
    res = OracleRunner(_oracle(crits), executor=ex).run(OracleScope.AFFECTED)
    assert [c.id for c in res.criteria] == ["a"]
    assert [cid for cid, _ in ex.calls] == ["a"]


def test_milestone_scope_filters_by_milestone_id() -> None:
    crits = [
        _crit("m1", OracleType.CMD, scope=OracleScope.MILESTONE, milestone="after-n2"),
        _crit("m2", OracleType.CMD, scope=OracleScope.MILESTONE, milestone="after-n3"),
    ]
    ex = FakeExecutor({"m1": RawExecution(exit_code=0), "m2": RawExecution(exit_code=0)})
    res = OracleRunner(_oracle(crits), executor=ex).run(OracleScope.MILESTONE, milestone="after-n2")
    assert [c.id for c in res.criteria] == ["m1"]
    assert res.milestone == "after-n2"


def test_empty_scope_is_vacuously_green() -> None:
    ex = FakeExecutor({})
    res = OracleRunner(
        _oracle([_crit("f", OracleType.CMD, scope=OracleScope.FINAL)]), executor=ex
    ).run(OracleScope.AFFECTED)
    assert res.criteria == []
    assert res.outcome is OracleOutcome.GREEN


# --- aggregation (gate on the highest-severity tier present) -----------------


def test_aggregate_red_when_a_redline_criterion_is_red() -> None:
    crits = [
        _crit("a", OracleType.CMD, severity=Severity.P0),
        _crit("b", OracleType.CMD, severity=Severity.P1),
    ]
    ex = FakeExecutor({"a": RawExecution(exit_code=1), "b": RawExecution(exit_code=0)})
    res = OracleRunner(_oracle(crits), executor=ex).run(OracleScope.AFFECTED)
    assert res.outcome is OracleOutcome.RED


def test_aggregate_unknown_when_redline_unknown_and_no_red() -> None:
    crits = [
        _crit("a", OracleType.CMD, severity=Severity.P0),
        _crit("b", OracleType.CMD, severity=Severity.P0),
    ]
    ex = FakeExecutor({"a": RawExecution(exit_code=0), "b": RawExecution(timed_out=True)})
    res = OracleRunner(_oracle(crits), executor=ex).run(OracleScope.AFFECTED)
    assert res.outcome is OracleOutcome.UNKNOWN


def test_aggregate_red_dominates_unknown() -> None:
    # A confirmed defect (RED) is actionable now; the UNKNOWN re-evaluates after a
    # fix re-runs the oracle. RED dominates (deliberately unlike Verdict).
    crits = [
        _crit("a", OracleType.CMD, severity=Severity.P0),
        _crit("b", OracleType.CMD, severity=Severity.P0),
    ]
    ex = FakeExecutor({"a": RawExecution(exit_code=1), "b": RawExecution(timed_out=True)})
    res = OracleRunner(_oracle(crits), executor=ex).run(OracleScope.AFFECTED)
    assert res.outcome is OracleOutcome.RED


def test_lower_severity_failure_does_not_gate_when_redline_green() -> None:
    crits = [
        _crit("p0", OracleType.CMD, severity=Severity.P0),
        _crit("p2", OracleType.CMD, severity=Severity.P2),
    ]
    ex = FakeExecutor({"p0": RawExecution(exit_code=0), "p2": RawExecution(exit_code=1)})
    res = OracleRunner(_oracle(crits), executor=ex).run(OracleScope.AFFECTED)
    assert res.outcome is OracleOutcome.GREEN
    # ... but the P2 failure is still recorded for visibility.
    p2 = next(c for c in res.criteria if c.id == "p2")
    assert p2.outcome is OracleOutcome.RED


def test_aggregate_outcome_helper_matches_runner() -> None:
    results = [
        CriterionResult(
            id="x",
            scope=OracleScope.AFFECTED,
            type=OracleType.CMD,
            severity=Severity.P0,
            outcome=OracleOutcome.RED,
            attempts=1,
            expect="0",
            actual="1",
        )
    ]
    assert aggregate_outcome(results) is OracleOutcome.RED


# --- drop-recreate-from-template cleanup (治 schema 污染死锁) -----------------


def test_recreates_sandbox_db_before_running_sql() -> None:
    db = FakeSandboxDb()
    crit = _crit("o1", OracleType.SQL, spec="SELECT 1", expect="1")
    ex = FakeExecutor({"o1": RawExecution(exit_code=0, stdout="1")})
    OracleRunner(_oracle([crit]), executor=ex, sandbox_db=db).run(OracleScope.AFFECTED)
    assert db.recreated == [("sandbox:erp_test", "erp_baseline")]


def test_no_db_recreate_when_no_sql_criteria() -> None:
    db = FakeSandboxDb()
    crit = _crit("o1", OracleType.CMD, expect="0")
    ex = FakeExecutor({"o1": RawExecution(exit_code=0)})
    OracleRunner(_oracle([crit]), executor=ex, sandbox_db=db).run(OracleScope.AFFECTED)
    assert db.recreated == []


def test_db_recreate_happens_once_before_all_sql() -> None:
    db = FakeSandboxDb()
    crits = [
        _crit("s1", OracleType.SQL, spec="SELECT 1", expect="1"),
        _crit("s2", OracleType.SQL, spec="SELECT 2", expect="2"),
    ]
    ex = FakeExecutor(
        {"s1": RawExecution(exit_code=0, stdout="1"), "s2": RawExecution(exit_code=0, stdout="2")}
    )
    OracleRunner(_oracle(crits), executor=ex, sandbox_db=db).run(OracleScope.AFFECTED)
    assert len(db.recreated) == 1


def test_cleanup_none_skips_recreate() -> None:
    db = FakeSandboxDb()
    rt = _runtime(cleanup=CleanupPolicy.NONE)
    # an all-cmd oracle so OracleRuntime/Oracle validation allows cleanup=none
    crit = _crit("o1", OracleType.CMD, expect="0")
    ex = FakeExecutor({"o1": RawExecution(exit_code=0)})
    OracleRunner(_oracle([crit], runtime=rt), executor=ex, sandbox_db=db).run(OracleScope.AFFECTED)
    assert db.recreated == []


# --- C′ red line: never drop-recreate a Live database ------------------------


def test_live_db_denylist_contains_the_real_erp_db() -> None:
    assert "erp" in LIVE_DB_DENYLIST


def test_psql_sandbox_refuses_to_recreate_a_live_db() -> None:
    with pytest.raises(LiveDbError):
        PsqlSandboxDb().recreate_from_template("erp", "erp_baseline")


def test_psql_sandbox_refuses_when_db_equals_template() -> None:
    with pytest.raises(LiveDbError):
        PsqlSandboxDb().recreate_from_template("sandbox_x", "sandbox_x")


def test_psql_sandbox_refuses_template_that_is_a_live_db() -> None:
    with pytest.raises(LiveDbError):
        PsqlSandboxDb().recreate_from_template("sandbox_x", "postgres")


# --- runtime → executor contract (settings actually flow through) ------------


def test_runner_hands_the_runtime_to_the_executor() -> None:
    rt = _runtime(seed=42)  # network omitted → schema default DENY
    ex = FakeExecutor({"o1": RawExecution(exit_code=0)})
    OracleRunner(_oracle([_crit("o1", OracleType.CMD)], runtime=rt), executor=ex).run(
        OracleScope.AFFECTED
    )
    _, seen_runtime = ex.calls[0]
    assert seen_runtime.seed == 42
    assert seen_runtime.network.value == "deny"


# --- result contracts round-trip ---------------------------------------------


def test_run_result_round_trips() -> None:
    ex = FakeExecutor({"o1": RawExecution(exit_code=0)})
    res = OracleRunner(_oracle([_crit("o1", OracleType.CMD)]), executor=ex).run(
        OracleScope.AFFECTED
    )
    assert OracleRunResult.from_dict(res.to_dict()) == res


def test_run_result_rejects_inconsistent_outcome() -> None:
    # A result that claims GREEN while a redline criterion is RED is malformed
    # (fail-closed, mirroring Verdict INV-2).
    red = CriterionResult(
        id="x",
        scope=OracleScope.AFFECTED,
        type=OracleType.CMD,
        severity=Severity.P0,
        outcome=OracleOutcome.RED,
        attempts=1,
        expect="0",
        actual="1",
    )
    with pytest.raises(SchemaError):
        OracleRunResult(
            oracle_version=1,
            scope=OracleScope.AFFECTED,
            outcome=OracleOutcome.GREEN,
            criteria=[red],
        )


# --- default SubprocessExecutor (real, harmless commands) --------------------


def test_subprocess_executor_runs_a_real_passing_command(tmp_path) -> None:
    rt = _runtime(cwd=str(tmp_path), db=None, db_template=None, cleanup=CleanupPolicy.NONE)
    crit = _crit("o1", OracleType.CMD, spec="true", expect="0")
    res = OracleRunner(_oracle([crit], runtime=rt)).run(OracleScope.AFFECTED)
    assert res.criteria[0].outcome is OracleOutcome.GREEN


def test_subprocess_executor_runs_a_real_failing_command(tmp_path) -> None:
    rt = _runtime(cwd=str(tmp_path), db=None, db_template=None, cleanup=CleanupPolicy.NONE)
    crit = _crit("o1", OracleType.CMD, spec="false", expect="0")
    res = OracleRunner(_oracle([crit], runtime=rt)).run(OracleScope.AFFECTED)
    assert res.criteria[0].outcome is OracleOutcome.RED


def test_subprocess_executor_enforces_timeout(tmp_path) -> None:
    rt = _runtime(
        cwd=str(tmp_path), db=None, db_template=None, cleanup=CleanupPolicy.NONE, timeout_s=1
    )
    crit = _crit("o1", OracleType.CMD, spec="sleep 3", expect="0")
    res = OracleRunner(_oracle([crit], runtime=rt)).run(OracleScope.AFFECTED)
    assert res.criteria[0].outcome is OracleOutcome.UNKNOWN


def test_subprocess_executor_applies_network_deny_env(tmp_path) -> None:
    rt = _runtime(cwd=str(tmp_path), db=None, db_template=None, cleanup=CleanupPolicy.NONE)
    crit = _crit("o1", OracleType.CMD, spec='test "$HANDOFF_ORACLE_NETWORK" = deny', expect="0")
    res = OracleRunner(_oracle([crit], runtime=rt)).run(OracleScope.AFFECTED)
    assert res.criteria[0].outcome is OracleOutcome.GREEN


def test_subprocess_executor_applies_seed_env(tmp_path) -> None:
    rt = _runtime(cwd=str(tmp_path), db=None, db_template=None, cleanup=CleanupPolicy.NONE, seed=42)
    crit = _crit("o1", OracleType.CMD, spec='test "$HANDOFF_ORACLE_SEED" = 42', expect="0")
    res = OracleRunner(_oracle([crit], runtime=rt)).run(OracleScope.AFFECTED)
    assert res.criteria[0].outcome is OracleOutcome.GREEN


def test_raw_execution_is_immutable() -> None:
    raw = RawExecution(exit_code=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        raw.exit_code = 1  # type: ignore[misc]
