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
import subprocess
import tempfile

import pytest

from handoff_fanout import supervisor as sup
from handoff_fanout.supervisor import SchemaError
from handoff_fanout.supervisor import oracle_runner as _orm
from handoff_fanout.supervisor.oracle_runner import _CLEANUP_FAILURE_ID, _decide

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


# === s1-fix C′ hardening (中枢独立审 d31ea20 = RED/RED) ======================
# The supervisor-coord's independent dual-brain audit (codex+gemini, no degrade)
# found the C′ "never touches Live" red line punched through. These tests pin the
# fixes: sanitized env on EVERY subprocess (no host PG*/cloud creds), a sandbox
# HOME, a ``--`` argv terminator + strict db-name guard against option/conninfo
# injection, cleanup-failure → UNKNOWN, and DB-bearing test/cmd cleanup.


# --- P0-1/3: shell env strips host PG*/cloud creds + sandbox HOME -------------


def test_shell_env_strips_pg_and_cloud_creds(tmp_path, monkeypatch):
    # A shell=True oracle spec must NOT see PG* / DATABASE_URL / cloud creds — even
    # though dropdb/psql legitimately use PG*, those come from the trusted sandbox_env
    # injection, NEVER the host (C′; gemini P0 + codex P2-5, s1-fix extends to PG*).
    for k in ("PGHOST", "PGUSER", "PGPASSWORD", "PGSERVICE", "PGDATABASE", "DATABASE_URL"):
        monkeypatch.setenv(k, "live-secret")
    ex = sup.SubprocessExecutor()
    rt = _runtime(cwd=str(tmp_path))
    spec = 'echo "[$PGHOST][$PGPASSWORD][$PGSERVICE][$PGDATABASE][$DATABASE_URL]"'
    assert ex.execute(_crit("c", spec=spec), rt).stdout.strip() == "[][][][][]"


def test_shell_home_is_sandbox_cwd_not_real_home(tmp_path, monkeypatch):
    # HOME (and therefore ~) resolves into the disposable worktree, not the real home
    # — so a shell spec can't read ~/.aws / ~/.ssh / ~/.pgpass via the ~ shortcut.
    monkeypatch.setenv("HOME", "/Users/real-home-must-not-leak")
    ex = sup.SubprocessExecutor()
    rt = _runtime(cwd=str(tmp_path))
    assert ex.execute(_crit("h", spec='echo "$HOME"'), rt).stdout.strip() == str(tmp_path)
    assert ex.execute(_crit("t", spec="echo ~"), rt).stdout.strip() == str(tmp_path)


def test_executor_injects_explicit_sandbox_pg_env(tmp_path, monkeypatch):
    # The sandbox PG* a SQL criterion legitimately needs is INJECTED (trusted
    # construction site), and is the only PG* the subprocess sees — a host PGHOST
    # pointing at Live is overridden, not inherited.
    monkeypatch.setenv("PGHOST", "live-host")
    ex = sup.SubprocessExecutor(sandbox_env={"PGHOST": "127.0.0.1", "PGPORT": "5499"})
    rt = _runtime(cwd=str(tmp_path))
    out = ex.execute(_crit("c", spec='echo "[$PGHOST][$PGPORT]"'), rt).stdout.strip()
    assert out == "[127.0.0.1][5499]"


# --- P0-1/2: dropdb/createdb get a sanitized env + ``--`` terminator ----------


def _capture_db_subprocess(monkeypatch):
    """Capture argv + env of every ``subprocess.run`` the sandbox DB issues, without
    spawning a real ``dropdb``/``createdb`` (so the test never touches a server)."""
    calls: list[dict] = []

    def fake_run(argv, **kw):
        calls.append({"argv": list(argv), "env": kw.get("env")})
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(_orm.subprocess, "run", fake_run)
    return calls


def test_sandbox_dropdb_createdb_use_terminator_and_sanitized_env(monkeypatch):
    # codex #2 + gemini P1: dropdb/createdb previously inherited the FULL host env
    # (PGHOST/PGPASSWORD/DATABASE_URL) → a misconfigured host = dropping a Live DB.
    for k in ("PGHOST", "PGPASSWORD", "PGSERVICE", "DATABASE_URL", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "live-secret")
    calls = _capture_db_subprocess(monkeypatch)
    sup.PsqlSandboxDb().recreate_from_template("sandbox_test", "sandbox_baseline")
    # gemini P0: a ``--`` terminator means a forged name can never be read as a flag.
    assert [c["argv"] for c in calls] == [
        ["dropdb", "--if-exists", "--", "sandbox_test"],
        ["createdb", "--template", "sandbox_baseline", "--", "sandbox_test"],
    ]
    for c in calls:
        env = c["env"]
        assert env is not None  # never None → never inherits the full host env
        for leaked in (
            "PGHOST",
            "PGPASSWORD",
            "PGSERVICE",
            "DATABASE_URL",
            "AWS_SECRET_ACCESS_KEY",
        ):
            assert leaked not in env  # host Live creds stripped (C′)
        assert env["HOME"] == tempfile.gettempdir()  # HOME is a tmp dir, not real home


def test_sandbox_injects_explicit_pg_env(monkeypatch):
    monkeypatch.setenv("PGHOST", "live-host")  # host PGHOST must NOT win
    calls = _capture_db_subprocess(monkeypatch)
    sup.PsqlSandboxDb(env={"PGHOST": "sandbox-host", "PGPASSWORD": "sb"}).recreate_from_template(
        "sandbox_test", "sandbox_baseline"
    )
    for c in calls:
        assert c["env"]["PGHOST"] == "sandbox-host"  # explicit sandbox PG* injected
        assert c["env"]["PGPASSWORD"] == "sb"


# --- P0-2: strict db-name guard rejects option / conninfo injection -----------


def test_guard_refuses_option_injection_db_name():
    db = sup.PsqlSandboxDb()
    # contains the 'sandbox' marker (so it would pass the allowlist), but dropdb would
    # parse '--host=sandbox' as an option → must be refused as a non-plain name.
    with pytest.raises(sup.LiveDbError, match="not a plain database name"):
        db._guard("--host=sandbox", "sandbox_baseline")


def test_guard_refuses_conninfo_keyword_db_name():
    db = sup.PsqlSandboxDb()
    with pytest.raises(sup.LiveDbError, match="not a plain database name"):
        db._guard("service=sandbox", "sandbox_baseline")  # libpq conninfo shape


def test_guard_refuses_equals_and_leading_dash():
    db = sup.PsqlSandboxDb()
    with pytest.raises(sup.LiveDbError, match="not a plain database name"):
        db._guard("sandbox=x", "sandbox_baseline")
    with pytest.raises(sup.LiveDbError, match="not a plain database name"):
        db._guard("-sandbox", "sandbox_baseline")


def test_guard_refuses_option_injection_template():
    db = sup.PsqlSandboxDb()
    with pytest.raises(sup.LiveDbError, match="not a plain database name"):
        db._guard("sandbox_test", "--template-injection")


def test_guard_still_allows_the_design_sandbox_label():
    # the design's own ``sandbox:erp_test`` (a scheme-style label) stays valid.
    sup.PsqlSandboxDb()._guard("sandbox:erp_test", "erp_baseline")  # no raise


# --- P0-3 (psql path): runtime.db conninfo refused + non-interactive ----------


def test_sql_executor_uses_sanitized_env_and_no_password_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv("PGPASSWORD", "live")
    calls: list[dict] = []

    def fake_run(argv, **kw):
        calls.append({"argv": list(argv), "env": kw.get("env")})
        return subprocess.CompletedProcess(argv, 0, "0\n", "")

    monkeypatch.setattr(_orm.subprocess, "run", fake_run)
    ex = sup.SubprocessExecutor(sandbox_env={"PGHOST": "sandbox-host"})
    raw = ex.execute(
        _crit("s", ctype=sup.OracleType.SQL, spec="select 0", expect="0"),
        _sql_runtime(cwd=str(tmp_path)),
    )
    assert raw.stdout.strip() == "0"
    env = calls[0]["env"]
    assert env["PGHOST"] == "sandbox-host"  # injected sandbox knob
    assert "PGPASSWORD" not in env  # host cred stripped
    assert calls[0]["argv"][0] == "psql" and "-w" in calls[0]["argv"]  # never prompts


def test_sql_executor_refuses_conninfo_runtime_db():
    ex = sup.SubprocessExecutor()
    # a runtime.db shaped like a libpq conninfo → could-not-evaluate (error→UNKNOWN),
    # never a silent connect to wherever ``service=prod`` points.
    raw = ex.execute(
        _crit("s", ctype=sup.OracleType.SQL, spec="select 1", expect="1"),
        _sql_runtime(db="service=prod"),
    )
    assert raw.error is not None and "non-plain runtime.db" in raw.error
    assert _decide(_crit("s", ctype=sup.OracleType.SQL), raw)[0] is sup.OracleOutcome.UNKNOWN


# --- P1-4: sandbox cleanup failure → UNKNOWN (never raise, never RED) ---------


class _RaisingSandbox(sup.SandboxDb):
    """Always raises in ``recreate_from_template`` (infra/guard failure simulation)."""

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    def recreate_from_template(self, db, db_template):
        self.calls += 1
        raise self._exc


class _FailOnNthSandbox(sup.SandboxDb):
    """Succeeds until the ``fail_on``-th recreate, then raises (pre-retry failure)."""

    def __init__(self, fail_on):
        self._fail_on = fail_on
        self.calls = 0

    def recreate_from_template(self, db, db_template):
        self.calls += 1
        if self.calls == self._fail_on:
            raise subprocess.CalledProcessError(1, ["createdb"])


def test_run_cleanup_failure_is_unknown_not_raise():
    crit = _crit("sq", ctype=sup.OracleType.SQL, spec="select 0", expect="0")
    sandbox = _RaisingSandbox(subprocess.CalledProcessError(1, ["dropdb"]))
    runner = sup.OracleRunner(
        _oracle([crit], runtime=_sql_runtime()),
        executor=_ScriptedExecutor({"sq": sup.RawExecution(stdout="0")}),
        sandbox_db=sandbox,
    )
    res = runner.run(sup.OracleScope.AFFECTED)  # must NOT raise
    assert res.outcome is sup.OracleOutcome.UNKNOWN
    assert res.criteria[0].id == _CLEANUP_FAILURE_ID
    # an UNKNOWN run escalates — the frozen S0 projection refuses it (no oracle_UNKNOWN)
    with pytest.raises(SchemaError, match="UNKNOWN"):
        res.to_oracle_checked("n")


def test_run_cleanup_livedberror_is_unknown_and_names_cause():
    crit = _crit("sq", ctype=sup.OracleType.SQL, spec="select 0", expect="0")
    sandbox = _RaisingSandbox(sup.LiveDbError("refusing to drop-recreate a Live database: 'erp'"))
    runner = sup.OracleRunner(
        _oracle([crit], runtime=_sql_runtime()),
        executor=_ScriptedExecutor({"sq": sup.RawExecution(stdout="0")}),
        sandbox_db=sandbox,
    )
    res = runner.run(sup.OracleScope.AFFECTED)
    assert res.outcome is sup.OracleOutcome.UNKNOWN
    assert "LiveDbError" in res.criteria[0].detail and "Live database" in res.criteria[0].detail


def test_milestone_cleanup_failure_keeps_milestone_attribution():
    crit = _crit(
        "m",
        scope=sup.OracleScope.MILESTONE,
        milestone="after-n2",
        ctype=sup.OracleType.SQL,
        spec="select 0",
        expect="0",
    )
    sandbox = _RaisingSandbox(OSError("dropdb binary missing"))
    runner = sup.OracleRunner(
        _oracle([crit], runtime=_sql_runtime()),
        executor=_ScriptedExecutor({"m": sup.RawExecution(stdout="0")}),
        sandbox_db=sandbox,
    )
    res = runner.run(sup.OracleScope.MILESTONE, milestone="after-n2")
    assert res.outcome is sup.OracleOutcome.UNKNOWN
    assert res.milestone == "after-n2"  # the UNKNOWN is still attributed to the gate


def test_retry_cleanup_failure_makes_criterion_unknown():
    # run-level cleanup ok (call 1); the first attempt is RED; the pre-retry cleanup
    # (call 2) fails → the criterion is UNKNOWN, not retried onto an unreset DB.
    crit = _crit("sq", ctype=sup.OracleType.SQL, spec="select 0", expect="0", flaky=1)
    sandbox = _FailOnNthSandbox(fail_on=2)
    runner = sup.OracleRunner(
        _oracle([crit], runtime=_sql_runtime()),
        executor=_ScriptedExecutor(
            {"sq": [sup.RawExecution(stdout="9"), sup.RawExecution(stdout="0")]}
        ),
        sandbox_db=sandbox,
    )
    res = runner.run(sup.OracleScope.AFFECTED)
    assert res.outcome is sup.OracleOutcome.UNKNOWN
    assert res.criteria[0].attempts == 2
    assert "cleanup before retry" in res.criteria[0].detail


# --- P1-5: DB-bearing test/cmd also trigger cleanup + retry rebuild -----------


def test_db_bearing_test_criterion_triggers_cleanup():
    # a DB-bearing runtime + a TEST criterion (runs as a shell command that can hit
    # the DB) MUST reset the schema first, even though no criterion is type=SQL.
    crit = _crit("t", ctype=sup.OracleType.TEST, expect="0")
    sandbox = _RecordingSandbox()
    runner = sup.OracleRunner(
        _oracle([crit], runtime=_sql_runtime()),
        executor=_ScriptedExecutor({"t": sup.RawExecution(exit_code=0)}),
        sandbox_db=sandbox,
    )
    runner.run(sup.OracleScope.AFFECTED)
    assert sandbox.calls == [("sandbox:erp_test", "erp_baseline")]


def test_db_bearing_cmd_flaky_retry_recreates_db():
    crit = _crit("c", ctype=sup.OracleType.CMD, expect="0", flaky=1)
    sandbox = _RecordingSandbox()
    runner = sup.OracleRunner(
        _oracle([crit], runtime=_sql_runtime()),
        executor=_ScriptedExecutor(
            {"c": [sup.RawExecution(exit_code=1), sup.RawExecution(exit_code=0)]}
        ),
        sandbox_db=sandbox,
    )
    res = runner.run(sup.OracleScope.AFFECTED)
    assert res.outcome is sup.OracleOutcome.GREEN
    assert res.criteria[0].attempts == 2
    assert len(sandbox.calls) == 2  # run-level + pre-retry, even for a cmd criterion


def test_empty_scope_skips_cleanup_even_when_db_bearing():
    # a DB-bearing runtime but the requested scope matches no criteria → nothing runs,
    # so no surprise DB I/O (the recreate is skipped).
    crit = _crit(
        "m",
        scope=sup.OracleScope.MILESTONE,
        milestone="x",
        ctype=sup.OracleType.SQL,
        spec="select 0",
        expect="0",
    )
    sandbox = _RecordingSandbox()
    runner = sup.OracleRunner(
        _oracle([crit], runtime=_sql_runtime()),
        executor=_ScriptedExecutor({"m": sup.RawExecution(stdout="0")}),
        sandbox_db=sandbox,
    )
    res = runner.run(sup.OracleScope.FINAL)  # no FINAL criteria
    assert res.outcome is sup.OracleOutcome.GREEN  # vacuous
    assert sandbox.calls == []
