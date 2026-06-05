"""S1 OracleRunner tests (design §4.4 / §12 C9).

Covers the four criterion types (cmd / sql / invariant / test) × three scopes
(affected / milestone / final), the GREEN/RED/UNKNOWN grading split (UNKNOWN ⟺
could-not-evaluate, never a defect), flaky-retry tolerance, the aggregate gating
rules, the OracleRunResult ⇄ frozen-S0 ``OracleChecked`` projection (incl. the
UNKNOWN-refusal), and the C′ red line (the sandbox DB never touches Live).

Unit tests inject fake executors/sandbox-DBs so they never spawn a real subprocess
or touch a DB; a small set exercises the real :class:`SubprocessExecutor` against
harmless shell built-ins (``true`` / ``false`` / a bad cwd) — never Live.

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s1_oracle_runner.py
"""

from __future__ import annotations

import json

import pytest

from handoff_fanout import supervisor as sup
from handoff_fanout.supervisor import SchemaError
from handoff_fanout.supervisor.oracle_runner import _decide

# --- helpers -----------------------------------------------------------------


def _runtime(**kw):
    """A hermetic cmd-only runtime (no DB → cleanup=none is the only valid choice
    since drop-recreate requires a db+template)."""
    base = dict(cwd="/tmp/wt", cleanup=sup.CleanupPolicy.NONE)
    base.update(kw)
    return sup.OracleRuntime(**base)


def _sql_runtime(**kw):
    """A DB-bearing runtime — must drop-recreate-from-template (schema-pollution red
    line) so it carries both db + db_template."""
    base = dict(
        cwd="/tmp/wt",
        db="sandbox:erp_test",
        db_template="erp_baseline",
        cleanup=sup.CleanupPolicy.DROP_RECREATE_FROM_TEMPLATE,
    )
    base.update(kw)
    return sup.OracleRuntime(**base)


def _crit(
    cid,
    *,
    scope=sup.OracleScope.AFFECTED,
    ctype=sup.OracleType.CMD,
    sev=sup.Severity.P0,
    spec="true",
    expect="0",
    milestone=None,
    flaky=0,
):
    return sup.OracleCriterion(
        id=cid,
        scope=scope,
        type=ctype,
        spec=spec,
        expect=expect,
        severity=sev,
        milestone=milestone,
        flaky_retries=flaky,
    )


def _oracle(criteria, *, runtime=None, version=2):
    return sup.Oracle(
        schema_version=1,
        oracle_version=version,
        runtime=runtime or _runtime(),
        criteria=criteria,
    )


class _ScriptedExecutor(sup.CriterionExecutor):
    """Returns a pre-programmed RawExecution per criterion id (or by attempt for
    flaky tests). ``by_id`` maps criterion id → RawExecution | list[RawExecution]
    (the list is consumed one per attempt)."""

    def __init__(self, by_id):
        self._by_id = {k: (list(v) if isinstance(v, list) else v) for k, v in by_id.items()}

    def execute(self, criterion, runtime):
        v = self._by_id[criterion.id]
        if isinstance(v, list):
            return v.pop(0)
        return v


class _RecordingSandbox(sup.SandboxDb):
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def recreate_from_template(self, db, db_template):
        self.calls.append((db, db_template))


# --- decision logic per type -------------------------------------------------


def test_cmd_green_red_and_misconfigured_expect():
    runner = sup.OracleRunner(_oracle([]), executor=_ScriptedExecutor({}), sandbox_db=None)
    # exit matches expect → GREEN
    g = _decide(_crit("c", ctype=sup.OracleType.CMD, expect="0"), sup.RawExecution(exit_code=0))
    assert g[0] is sup.OracleOutcome.GREEN
    # exit mismatch → RED
    r = _decide(_crit("c", ctype=sup.OracleType.CMD, expect="0"), sup.RawExecution(exit_code=3))
    assert r[0] is sup.OracleOutcome.RED
    # expect is not an int → misconfigured oracle → UNKNOWN (not RED)
    u = _decide(_crit("c", ctype=sup.OracleType.CMD, expect="green"), sup.RawExecution(exit_code=0))
    assert u[0] is sup.OracleOutcome.UNKNOWN
    assert runner is not None  # constructed without error


def test_sql_green_and_red_string_match():
    g = _decide(
        _crit("s", ctype=sup.OracleType.SQL, spec="select 0", expect="0"),
        sup.RawExecution(stdout="0\n"),
    )
    assert g[0] is sup.OracleOutcome.GREEN
    r = _decide(
        _crit("s", ctype=sup.OracleType.SQL, spec="select 0", expect="0"),
        sup.RawExecution(stdout="42\n"),
    )
    assert r[0] is sup.OracleOutcome.RED


def test_test_type_pytest_exit_codes():
    # 0 pass → GREEN, 1 fail → RED, anything else (infra) → UNKNOWN
    assert (
        _decide(_crit("t", ctype=sup.OracleType.TEST), sup.RawExecution(exit_code=0))[0]
        is sup.OracleOutcome.GREEN
    )
    assert (
        _decide(_crit("t", ctype=sup.OracleType.TEST), sup.RawExecution(exit_code=1))[0]
        is sup.OracleOutcome.RED
    )
    assert (
        _decide(_crit("t", ctype=sup.OracleType.TEST), sup.RawExecution(exit_code=5))[0]
        is sup.OracleOutcome.UNKNOWN
    )


def test_invariant_type_exit0_is_green():
    assert (
        _decide(_crit("i", ctype=sup.OracleType.INVARIANT), sup.RawExecution(exit_code=0))[0]
        is sup.OracleOutcome.GREEN
    )
    assert (
        _decide(_crit("i", ctype=sup.OracleType.INVARIANT), sup.RawExecution(exit_code=1))[0]
        is sup.OracleOutcome.RED
    )


def test_could_not_evaluate_is_always_unknown_never_red():
    c = _crit("c", ctype=sup.OracleType.CMD, expect="0")
    assert _decide(c, sup.RawExecution(error="boom"))[0] is sup.OracleOutcome.UNKNOWN
    assert _decide(c, sup.RawExecution(timed_out=True))[0] is sup.OracleOutcome.UNKNOWN
    assert _decide(c, sup.RawExecution(exit_code=None))[0] is sup.OracleOutcome.UNKNOWN


# --- aggregate gating --------------------------------------------------------


def _result(cid, outcome, *, sev=sup.Severity.P0, scope=sup.OracleScope.AFFECTED):
    return sup.CriterionResult(
        id=cid,
        scope=scope,
        type=sup.OracleType.CMD,
        severity=sev,
        outcome=outcome,
        attempts=1,
        expect="0",
        actual="0",
    )


def test_aggregate_empty_is_vacuously_green():
    assert sup.aggregate_outcome([]) is sup.OracleOutcome.GREEN


def test_aggregate_red_dominates_unknown():
    out = sup.aggregate_outcome(
        [
            _result("a", sup.OracleOutcome.RED),
            _result("b", sup.OracleOutcome.UNKNOWN),
        ]
    )
    assert out is sup.OracleOutcome.RED


def test_aggregate_redline_gates_advisory_does_not():
    # P0 green, P2 red → only the redline (P0) gates → GREEN overall
    out = sup.aggregate_outcome(
        [
            _result("a", sup.OracleOutcome.GREEN, sev=sup.Severity.P0),
            _result("b", sup.OracleOutcome.RED, sev=sup.Severity.P2),
        ]
    )
    assert out is sup.OracleOutcome.GREEN


def test_aggregate_p2_only_scope_is_not_vacuously_green():
    # no redline present → the P2s gate, so a failing P2-only scope is RED
    out = sup.aggregate_outcome([_result("b", sup.OracleOutcome.RED, sev=sup.Severity.P2)])
    assert out is sup.OracleOutcome.RED


# --- the runner end-to-end (scoped) ------------------------------------------


def test_run_affected_all_green():
    crits = [_crit("o1"), _crit("o2", spec="also-true")]
    runner = sup.OracleRunner(
        _oracle(crits),
        executor=_ScriptedExecutor(
            {"o1": sup.RawExecution(exit_code=0), "o2": sup.RawExecution(exit_code=0)}
        ),
        sandbox_db=None,
    )
    res = runner.run(sup.OracleScope.AFFECTED)
    assert res.outcome is sup.OracleOutcome.GREEN
    assert [c.id for c in res.criteria] == ["o1", "o2"]


def test_run_milestone_filters_by_milestone_id():
    crits = [
        _crit("m1", scope=sup.OracleScope.MILESTONE, milestone="after-n2"),
        _crit("m2", scope=sup.OracleScope.MILESTONE, milestone="after-n3"),
    ]
    runner = sup.OracleRunner(
        _oracle(crits),
        executor=_ScriptedExecutor(
            {"m1": sup.RawExecution(exit_code=0), "m2": sup.RawExecution(exit_code=1)}
        ),
        sandbox_db=None,
    )
    res = runner.run(sup.OracleScope.MILESTONE, milestone="after-n2")
    assert [c.id for c in res.criteria] == ["m1"]
    assert res.outcome is sup.OracleOutcome.GREEN
    assert res.milestone == "after-n2"


def test_run_milestone_without_id_is_rejected():
    runner = sup.OracleRunner(_oracle([]), executor=_ScriptedExecutor({}), sandbox_db=None)
    with pytest.raises(SchemaError, match="requires a `milestone`"):
        runner.run(sup.OracleScope.MILESTONE)


def test_run_final_scope_red():
    crits = [_crit("f1", scope=sup.OracleScope.FINAL)]
    runner = sup.OracleRunner(
        _oracle(crits),
        executor=_ScriptedExecutor({"f1": sup.RawExecution(exit_code=9)}),
        sandbox_db=None,
    )
    res = runner.run(sup.OracleScope.FINAL)
    assert res.outcome is sup.OracleOutcome.RED


def test_flaky_retries_recover_to_green():
    # first attempt RED, second GREEN → flaky_retries=1 lets it pass on attempt 2
    crit = _crit("flk", flaky=1, expect="0")
    runner = sup.OracleRunner(
        _oracle([crit]),
        executor=_ScriptedExecutor(
            {"flk": [sup.RawExecution(exit_code=1), sup.RawExecution(exit_code=0)]}
        ),
        sandbox_db=None,
    )
    res = runner.run(sup.OracleScope.AFFECTED)
    assert res.outcome is sup.OracleOutcome.GREEN
    assert res.criteria[0].attempts == 2


def test_sql_run_triggers_sandbox_cleanup_once():
    crit = _crit("sq", ctype=sup.OracleType.SQL, spec="select 0", expect="0")
    sandbox = _RecordingSandbox()
    runner = sup.OracleRunner(
        _oracle([crit], runtime=_sql_runtime()),
        executor=_ScriptedExecutor({"sq": sup.RawExecution(stdout="0")}),
        sandbox_db=sandbox,
    )
    res = runner.run(sup.OracleScope.AFFECTED)
    assert res.outcome is sup.OracleOutcome.GREEN
    assert sandbox.calls == [("sandbox:erp_test", "erp_baseline")]


def test_cmd_only_run_does_not_clean_db():
    sandbox = _RecordingSandbox()
    runner = sup.OracleRunner(
        _oracle([_crit("o1")]),
        executor=_ScriptedExecutor({"o1": sup.RawExecution(exit_code=0)}),
        sandbox_db=sandbox,
    )
    runner.run(sup.OracleScope.AFFECTED)
    assert sandbox.calls == []  # no SQL → no cleanup


# --- OracleRunResult validation + projection to frozen OracleChecked ---------


def test_run_result_rejects_outcome_inconsistent_with_criteria():
    with pytest.raises(SchemaError, match="inconsistent with its"):
        sup.OracleRunResult(
            oracle_version=1,
            scope=sup.OracleScope.AFFECTED,
            outcome=sup.OracleOutcome.GREEN,  # lying: a P0 criterion is RED
            criteria=[_result("a", sup.OracleOutcome.RED)],
        )


def test_run_result_rejects_off_scope_criteria():
    with pytest.raises(SchemaError, match="off-scope"):
        sup.OracleRunResult(
            oracle_version=1,
            scope=sup.OracleScope.FINAL,
            outcome=sup.OracleOutcome.GREEN,
            criteria=[_result("a", sup.OracleOutcome.GREEN, scope=sup.OracleScope.AFFECTED)],
        )


def test_projection_green_passes_with_no_failed_criteria():
    res = sup.OracleRunResult(
        oracle_version=1,
        scope=sup.OracleScope.AFFECTED,
        outcome=sup.OracleOutcome.GREEN,
        criteria=[_result("a", sup.OracleOutcome.GREEN)],
    )
    oc = res.to_oracle_checked("n1")
    assert isinstance(oc, sup.OracleChecked)
    assert oc.passed is True
    assert oc.failed_criteria == []
    assert oc.node == "n1" and oc.scope is sup.OracleScope.AFFECTED


def test_projection_red_names_gating_failures_only():
    res = sup.OracleRunResult(
        oracle_version=1,
        scope=sup.OracleScope.AFFECTED,
        outcome=sup.OracleOutcome.RED,
        criteria=[
            _result("p0fail", sup.OracleOutcome.RED, sev=sup.Severity.P0),
            _result("p2fail", sup.OracleOutcome.RED, sev=sup.Severity.P2),  # advisory, not gating
        ],
    )
    oc = res.to_oracle_checked("n2")
    assert oc.passed is False
    assert oc.failed_criteria == ["p0fail"]  # the advisory P2 is NOT a gating failure


def test_projection_refuses_unknown():
    res = sup.OracleRunResult(
        oracle_version=1,
        scope=sup.OracleScope.AFFECTED,
        outcome=sup.OracleOutcome.UNKNOWN,
        criteria=[_result("a", sup.OracleOutcome.UNKNOWN)],
    )
    with pytest.raises(SchemaError, match="UNKNOWN.*escalat"):
        res.to_oracle_checked("n3")


def test_run_result_round_trips():
    res = sup.OracleRunResult(
        oracle_version=2,
        scope=sup.OracleScope.MILESTONE,
        milestone="after-n2",
        outcome=sup.OracleOutcome.GREEN,
        criteria=[_result("a", sup.OracleOutcome.GREEN, scope=sup.OracleScope.MILESTONE)],
    )
    again = sup.OracleRunResult.from_dict(json.loads(json.dumps(res.to_dict())))
    assert again == res


# --- C′ red line: sandbox DB never touches Live ------------------------------


def test_sandbox_refuses_live_db():
    db = sup.PsqlSandboxDb()
    with pytest.raises(sup.LiveDbError, match="never touches Live"):
        db.recreate_from_template("erp", "erp_baseline")


def test_sandbox_refuses_live_template():
    db = sup.PsqlSandboxDb()
    with pytest.raises(sup.LiveDbError, match="Live database as a template"):
        db.recreate_from_template("sandbox_x", "erp_real")


def test_sandbox_refuses_self_template():
    db = sup.PsqlSandboxDb()
    with pytest.raises(sup.LiveDbError, match="from itself"):
        db.recreate_from_template("sandbox_x", "sandbox_x")


def test_sandbox_extra_denied_db():
    db = sup.PsqlSandboxDb(extra_denied=("my_precious",))
    with pytest.raises(sup.LiveDbError):
        db.recreate_from_template("my_precious", "tmpl")


# --- real SubprocessExecutor against harmless built-ins (never Live) ---------


def test_real_executor_cmd_green_and_red(tmp_path):
    ex = sup.SubprocessExecutor()
    rt = _runtime(cwd=str(tmp_path))
    g = ex.execute(_crit("c", spec="true", expect="0"), rt)
    assert g.exit_code == 0
    r = ex.execute(_crit("c", spec="false", expect="0"), rt)
    assert r.exit_code == 1


def test_real_executor_bad_cwd_is_error_then_unknown(tmp_path):
    ex = sup.SubprocessExecutor()
    rt = _runtime(cwd=str(tmp_path / "does-not-exist"))
    raw = ex.execute(_crit("c", spec="true", expect="0"), rt)
    assert raw.error is not None
    assert _decide(_crit("c", spec="true", expect="0"), raw)[0] is sup.OracleOutcome.UNKNOWN


def test_real_executor_sql_without_db_is_error():
    ex = sup.SubprocessExecutor()
    # a SQL criterion run against a runtime with no db → executor error (not RED)
    raw = ex.execute(
        _crit("s", ctype=sup.OracleType.SQL, spec="select 1", expect="1"),
        _runtime(),  # no db
    )
    assert raw.error is not None and "runtime.db is unset" in raw.error


# --- R2 hardening: env allowlist (gemini P0 + codex P2-5, consensus) ---------


def test_env_does_not_leak_host_secrets(tmp_path, monkeypatch):
    # A shell=True oracle spec must NOT see DATABASE_URL / cloud creds (C′穿透).
    monkeypatch.setenv("DATABASE_URL", "postgres://prod/erp")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_leak")
    ex = sup.SubprocessExecutor()
    rt = _runtime(cwd=str(tmp_path))
    # echo the would-be-leaked vars; they must come back EMPTY in the sandbox env
    crit = _crit("leak", spec='echo "[$DATABASE_URL][$AWS_SECRET_ACCESS_KEY][$GITHUB_TOKEN]"')
    raw = ex.execute(crit, rt)
    assert raw.stdout.strip() == "[][][]"
    # but the oracle's own knobs ARE present
    crit2 = _crit("knob", spec='echo "$HANDOFF_ORACLE_NETWORK"')
    assert ex.execute(crit2, rt).stdout.strip() == "deny"


def test_env_keeps_path_so_commands_resolve(tmp_path):
    ex = sup.SubprocessExecutor()
    raw = ex.execute(_crit("p", spec='test -n "$PATH"'), _runtime(cwd=str(tmp_path)))
    assert raw.exit_code == 0  # PATH is allowlisted → shell can find built-ins


# --- R2 hardening: negative exit code (signal kill) → UNKNOWN (gemini P1) -----


def test_signal_killed_is_unknown_not_red():
    # SIGKILL/OOM → subprocess returns -9; an infra kill must NOT be graded RED
    # (else the control plane routes an OOM to an LLM Fixer — retry storm).
    for ctype in (sup.OracleType.CMD, sup.OracleType.INVARIANT, sup.OracleType.TEST):
        outcome, _, detail = _decide(
            _crit("k", ctype=ctype, expect="0"), sup.RawExecution(exit_code=-9)
        )
        assert outcome is sup.OracleOutcome.UNKNOWN, ctype
        assert "signal 9" in detail


# --- R2 hardening: runner boundary fail-closed (codex P1-2) ------------------


def test_runner_rejects_drifted_oracle_hash():
    oracle = _oracle([_crit("o1")])
    from handoff_fanout.supervisor import oracle_hash

    good = oracle_hash(oracle)
    # correct hash → ok
    sup.OracleRunner(
        oracle, expected_oracle_hash=good, executor=_ScriptedExecutor({}), sandbox_db=None
    )
    # wrong hash → fail closed (INV-5)
    with pytest.raises(SchemaError, match="drifted from its approved lock"):
        sup.OracleRunner(
            oracle, expected_oracle_hash="deadbeef", executor=_ScriptedExecutor({}), sandbox_db=None
        )


def test_runner_revalidates_oracle_at_boundary(monkeypatch):
    # A mutated oracle (validate would now fail) is rejected at the runner boundary,
    # not silently run. Force the oracle's validate to raise to simulate corruption.
    oracle = _oracle([_crit("o1")])

    def boom(self):
        raise SchemaError("corrupted oracle")

    monkeypatch.setattr(type(oracle), "validate", boom)
    with pytest.raises(SchemaError, match="corrupted oracle"):
        sup.OracleRunner(oracle, executor=_ScriptedExecutor({}), sandbox_db=None)


# --- R2 hardening: SQL flaky retry recreates the DB (gemini P2) --------------


def test_sql_flaky_retry_recreates_db_before_retry():
    # run-level cleanup (1) + recreate before the SQL retry (2) = 2 total recreates;
    # the retry must NOT run on the dirty DB the first attempt left.
    crit = _crit("sq", ctype=sup.OracleType.SQL, spec="select 0", expect="0", flaky=1)
    sandbox = _RecordingSandbox()
    runner = sup.OracleRunner(
        _oracle([crit], runtime=_sql_runtime()),
        executor=_ScriptedExecutor(
            {"sq": [sup.RawExecution(stdout="9"), sup.RawExecution(stdout="0")]}
        ),
        sandbox_db=sandbox,
    )
    res = runner.run(sup.OracleScope.AFFECTED)
    assert res.outcome is sup.OracleOutcome.GREEN
    assert res.criteria[0].attempts == 2
    assert len(sandbox.calls) == 2  # run-level + pre-retry


def test_cmd_flaky_retry_does_not_recreate_db():
    crit = _crit("c", ctype=sup.OracleType.CMD, expect="0", flaky=1)
    sandbox = _RecordingSandbox()
    runner = sup.OracleRunner(
        _oracle([crit]),  # cmd-only runtime, cleanup=none
        executor=_ScriptedExecutor(
            {"c": [sup.RawExecution(exit_code=1), sup.RawExecution(exit_code=0)]}
        ),
        sandbox_db=sandbox,
    )
    runner.run(sup.OracleScope.AFFECTED)
    assert sandbox.calls == []  # non-SQL retry never touches the DB


# --- R2 hardening: OracleRunResult dup-id self-consistency (codex P2-3) ------


def test_run_result_rejects_duplicate_criterion_ids():
    with pytest.raises(SchemaError, match="duplicate ids"):
        sup.OracleRunResult(
            oracle_version=1,
            scope=sup.OracleScope.AFFECTED,
            outcome=sup.OracleOutcome.GREEN,
            criteria=[
                _result("dup", sup.OracleOutcome.GREEN),
                _result("dup", sup.OracleOutcome.GREEN),
            ],
        )


# --- R2 hardening: sandbox allowlist (codex P1-1 + gemini P1, consensus) ------


def test_sandbox_allows_marked_sandbox_db():
    db = sup.PsqlSandboxDb()
    db._guard("sandbox_test", "sandbox_baseline")  # no raise — both pass


def test_sandbox_refuses_unmarked_db_even_if_not_denylisted():
    db = sup.PsqlSandboxDb()
    # "mydb" is not a known live DB, but it lacks the sandbox marker → fail closed
    # (allowlist > denylist; this is the project-agnostic guard, gemini P1).
    with pytest.raises(sup.LiveDbError, match="must contain 'sandbox'"):
        db._guard("mydb", "sandbox_baseline")


def test_sandbox_normalizes_case_and_catches_live():
    db = sup.PsqlSandboxDb()
    with pytest.raises(sup.LiveDbError, match="never touches Live"):
        db._guard("ERP", "sandbox_baseline")  # casefold → "erp" in denylist


def test_sandbox_refuses_connection_string_shaped_names():
    db = sup.PsqlSandboxDb()
    with pytest.raises(sup.LiveDbError, match="connection string"):
        db._guard("postgres://host/erp", "sandbox_baseline")
    with pytest.raises(sup.LiveDbError, match="connection string"):
        db._guard("sandbox_db", "host:5432/erp")  # template with a path char


def test_sandbox_refuses_whitespace_padded_name():
    db = sup.PsqlSandboxDb()
    with pytest.raises(sup.LiveDbError, match="connection string"):
        db._guard(" sandbox_db ", "sandbox_baseline")  # spaces → unsafe chars


def test_sandbox_custom_marker_is_project_agnostic():
    db = sup.PsqlSandboxDb(sandbox_marker="hf_test_")
    db._guard("hf_test_db", "hf_baseline")  # ok — matches custom marker
    with pytest.raises(sup.LiveDbError, match="must contain 'hf_test_'"):
        db._guard("sandbox_db", "hf_baseline")  # default marker no longer accepted
