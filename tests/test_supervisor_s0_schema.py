"""S0 schema-validity tests (design §4).

Covers, for every frozen contract: round-trip (to_dict→from_dict→equal),
JSON-serializability, strict unknown-key / missing-required / bad-enum rejection,
and the cross-field invariants the schema enforces (INV-2 verdict consistency,
INV-3 single writer, INV-4 CAS contiguity + anti-replay, DAG acyclicity, …).

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s0_schema.py
"""

from __future__ import annotations

import json

import pytest

from handoff_fanout import supervisor as sup
from handoff_fanout.supervisor import SchemaError

# --- one valid instance per concrete contract --------------------------------

VALID: dict[type, object] = {
    sup.Provenance: sup.Provenance(commit="abc", models=["codex", "gemini"]),
    sup.Event: sup.Event(
        schema_version=1,
        event_id="e1",
        seq=0,
        ts="2026-06-06T00:00:00Z",
        plan_id="p1",
        type=sup.EventType.PLAN_CREATED,
        expected_prev_seq=-1,
        dedupe_key="d1",
    ),
    sup.Node: sup.Node(node_id="n1", brief="b", base_ref="main"),
    sup.Plan: sup.Plan(
        schema_version=1,
        plan_id="p1",
        objective="o",
        acceptance_oracle_ref="oracle.json",
        nodes=[
            sup.Node(node_id="n1", brief="b1", base_ref="main"),
            sup.Node(node_id="n2", brief="b2", base_ref="main", deps=["n1"]),
        ],
    ),
    sup.ProviderFindings: sup.ProviderFindings(status=sup.ProviderStatus.OK),
    sup.Verdict: sup.Verdict(
        verdict=sup.VerdictValue.GREEN,
        by="rule:any-p0p1",
        codex=sup.ProviderFindings(status=sup.ProviderStatus.OK),
        gemini=sup.ProviderFindings(status=sup.ProviderStatus.OK),
        bound_to="diffhash",
        findings_ref="/tmp/findings.json",
    ),
    sup.OracleRuntime: sup.OracleRuntime(
        cwd="/tmp/wt",
        db="sandbox:erp_test",
        db_template="erp_baseline",
    ),
    sup.OracleCriterion: sup.OracleCriterion(
        id="o1",
        scope=sup.OracleScope.AFFECTED,
        type=sup.OracleType.TEST,
        spec="pytest tests/x.py",
        expect="pass",
        severity=sup.Severity.P0,
    ),
    sup.Oracle: sup.Oracle(
        schema_version=1,
        oracle_version=1,
        runtime=sup.OracleRuntime(
            cwd="/tmp/wt",
            db="sandbox:erp_test",
            db_template="erp_baseline",
        ),
        criteria=[
            sup.OracleCriterion(
                id="o1",
                scope=sup.OracleScope.FINAL,
                type=sup.OracleType.CMD,
                spec="make check",
                expect="0",
                severity=sup.Severity.P1,
            )
        ],
    ),
    sup.SideEffect: sup.SideEffect(kind=sup.SideEffectKind.DB_MIGRATION),
    sup.Action: sup.Action(
        action_id="a1",
        idempotency_key="k1",
        input_hash="h1",
        fencing_token="f1",
        effects=[sup.SideEffect(kind=sup.SideEffectKind.ARTIFACT)],
    ),
    sup.Ack: sup.Ack(node="n1", run_id="r1", attempt=1, tree_oid="t1", staged_diff_hash="s1"),
    sup.JudgeManifestEntry: sup.JudgeManifestEntry(path="oracle.json", hash="h"),
    sup.JudgeManifest: sup.JudgeManifest(
        entries=[sup.JudgeManifestEntry(path="oracle.json", hash="h")]
    ),
    sup.Approval: sup.Approval(
        node="n1",
        grantor="owner",
        granted_at="2026-06-06T00:00:00Z",
        expires_at="2026-06-13T00:00:00Z",
        bound_hash="bh",
    ),
    sup.DLQEntry: sup.DLQEntry(
        node="n1",
        reason="fix loop exhausted",
        impact="n2 blocked",
        recommended="rollback to n0",
        worst_case="manual repair",
        options=["rollback", "retry", "abort"],
    ),
    sup.Fixer: sup.Fixer(
        fixer_id="f-n1-1",
        parent_node="n1",
        attempt=1,
        trigger=sup.FixerTrigger.VERDICT_RED,
        base_ref="abc",
        file_ownership=["src/a/**"],
    ),
    sup.PlanAmendment: sup.PlanAmendment(
        plan_id="p1",
        diff="--- a\n+++ b",
        reason="add node",
        approver="owner",
        bound_hash="bh",
    ),
    sup.ContextPatchOp: sup.ContextPatchOp(
        op=sup.ContextPatchOpKind.UPSERT,
        key="k",
        value="v",
    ),
    sup.ContextPatch: sup.ContextPatch(
        patches=[sup.ContextPatchOp(op=sup.ContextPatchOpKind.UPSERT, key="k", value="v")]
    ),
    sup.RollbackRecord: sup.RollbackRecord(to_node="n0", to_commit="abc123"),
}


def test_registry_covers_every_concrete_contract() -> None:
    assert set(sup.ALL_CONTRACTS) == set(VALID), (
        "ALL_CONTRACTS and the VALID fixture must stay in lockstep"
    )


@pytest.mark.parametrize("cls", sup.ALL_CONTRACTS, ids=lambda c: c.__name__)
def test_roundtrip(cls: type) -> None:
    obj = VALID[cls]
    rebuilt = cls.from_dict(obj.to_dict())  # type: ignore[attr-defined]
    assert rebuilt == obj


@pytest.mark.parametrize("cls", sup.ALL_CONTRACTS, ids=lambda c: c.__name__)
def test_to_dict_is_json_serializable(cls: type) -> None:
    payload = VALID[cls].to_dict()  # type: ignore[attr-defined]
    text = json.dumps(payload)  # raises if any enum/nested leaked through
    # and the json round-trips back into the contract too
    cls.from_dict(json.loads(text))  # type: ignore[attr-defined]


@pytest.mark.parametrize("cls", sup.ALL_CONTRACTS, ids=lambda c: c.__name__)
def test_unknown_key_rejected(cls: type) -> None:
    data = dict(VALID[cls].to_dict())  # type: ignore[attr-defined]
    data["__bogus__"] = 1
    with pytest.raises(SchemaError):
        cls.from_dict(data)  # type: ignore[attr-defined]


@pytest.mark.parametrize("cls", sup.ALL_CONTRACTS, ids=lambda c: c.__name__)
def test_missing_required_field_rejected(cls: type) -> None:
    import dataclasses

    required = [
        f.name
        for f in dataclasses.fields(cls)  # type: ignore[arg-type]
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    ]
    if not required:
        pytest.skip(f"{cls.__name__} has no required fields")
    full = VALID[cls].to_dict()  # type: ignore[attr-defined]
    data = {k: v for k, v in full.items() if k != required[0]}
    with pytest.raises(SchemaError):
        cls.from_dict(data)  # type: ignore[attr-defined]


def test_from_dict_rejects_non_mapping() -> None:
    with pytest.raises(SchemaError):
        sup.Node.from_dict([1, 2, 3])  # type: ignore[arg-type]


def test_bad_enum_value_rejected() -> None:
    data = VALID[sup.Node].to_dict()
    data["risk_tier"] = "EXTREME"
    with pytest.raises(SchemaError):
        sup.Node.from_dict(data)


# --- INV-3 single writer ------------------------------------------------------


def test_event_writer_must_be_supervisor() -> None:
    with pytest.raises(SchemaError):
        sup.Event(
            schema_version=1,
            event_id="e1",
            seq=0,
            ts="t",
            plan_id="p1",
            type=sup.EventType.PLAN_CREATED,
            expected_prev_seq=-1,
            dedupe_key="d1",
            writer="worker",
        )


# --- INV-4 CAS contiguity + idempotency --------------------------------------


def test_event_cas_genesis_ok() -> None:
    sup.Event(
        schema_version=1,
        event_id="e0",
        seq=0,
        ts="t",
        plan_id="p1",
        type=sup.EventType.PLAN_CREATED,
        expected_prev_seq=-1,
        dedupe_key="d0",
    )


def test_event_cas_contiguity_enforced() -> None:
    with pytest.raises(SchemaError):
        sup.Event(
            schema_version=1,
            event_id="e2",
            seq=5,
            ts="t",
            plan_id="p1",
            type=sup.EventType.NODE_DISPATCHED,
            expected_prev_seq=2,
            dedupe_key="d2",
        )


def test_event_dedupe_key_required() -> None:
    with pytest.raises(SchemaError):
        sup.Event(
            schema_version=1,
            event_id="e3",
            seq=0,
            ts="t",
            plan_id="p1",
            type=sup.EventType.PLAN_CREATED,
            expected_prev_seq=-1,
            dedupe_key="",
        )


# --- INV-2 verdict consistency (the crux: no false GREEN) ---------------------


def _verdict(value, codex_status, gemini_status, *, c_p0=0, c_p1=0, g_p0=0, g_p1=0, degraded=False):
    return sup.Verdict(
        verdict=value,
        by="rule:any-p0p1",
        codex=sup.ProviderFindings(status=codex_status, p0=c_p0, p1=c_p1),
        gemini=sup.ProviderFindings(status=gemini_status, p0=g_p0, p1=g_p1),
        bound_to="diff",
        findings_ref="/tmp/f.json",
        degraded=degraded,
    )


def test_verdict_green_only_when_clean() -> None:
    # clean dual brain → GREEN is legal
    _verdict(sup.VerdictValue.GREEN, sup.ProviderStatus.OK, sup.ProviderStatus.OK)


def test_verdict_green_with_p0_rejected() -> None:
    with pytest.raises(SchemaError):
        _verdict(sup.VerdictValue.GREEN, sup.ProviderStatus.OK, sup.ProviderStatus.OK, c_p0=1)


def test_verdict_green_with_degraded_rejected() -> None:
    with pytest.raises(SchemaError):
        _verdict(
            sup.VerdictValue.GREEN, sup.ProviderStatus.OK, sup.ProviderStatus.OK, degraded=True
        )


def test_verdict_green_with_provider_down_rejected() -> None:
    with pytest.raises(SchemaError):
        _verdict(sup.VerdictValue.GREEN, sup.ProviderStatus.OK, sup.ProviderStatus.UNAVAILABLE)


def test_verdict_red_required_when_findings_and_clean_status() -> None:
    # both OK, a P1 exists → must be RED; claiming UNKNOWN is inconsistent
    _verdict(sup.VerdictValue.RED, sup.ProviderStatus.OK, sup.ProviderStatus.OK, g_p1=2)
    with pytest.raises(SchemaError):
        _verdict(sup.VerdictValue.UNKNOWN, sup.ProviderStatus.OK, sup.ProviderStatus.OK, g_p1=2)


def test_verdict_unknown_required_when_degraded() -> None:
    # degraded dominates: even with a P0 the trustworthy verdict is UNKNOWN, not RED
    _verdict(
        sup.VerdictValue.UNKNOWN,
        sup.ProviderStatus.OK,
        sup.ProviderStatus.OK,
        c_p0=1,
        degraded=True,
    )
    with pytest.raises(SchemaError):
        _verdict(
            sup.VerdictValue.RED,
            sup.ProviderStatus.OK,
            sup.ProviderStatus.OK,
            c_p0=1,
            degraded=True,
        )


def test_verdict_by_must_be_a_known_deterministic_rule() -> None:
    # not a rule at all
    with pytest.raises(SchemaError):
        _bad_by("llm:looks-fine")
    # has the rule: prefix but is not in the allow-list (INV-1 hole closed)
    with pytest.raises(SchemaError):
        _bad_by("rule:ask-llm")


def _bad_by(by: str):
    return sup.Verdict(
        verdict=sup.VerdictValue.GREEN,
        by=by,
        codex=sup.ProviderFindings(status=sup.ProviderStatus.OK),
        gemini=sup.ProviderFindings(status=sup.ProviderStatus.OK),
        bound_to="diff",
        findings_ref="/tmp/f.json",
    )


def test_verdict_bound_to_required() -> None:
    with pytest.raises(SchemaError):
        sup.Verdict(
            verdict=sup.VerdictValue.GREEN,
            by="rule:any-p0p1",
            codex=sup.ProviderFindings(status=sup.ProviderStatus.OK),
            gemini=sup.ProviderFindings(status=sup.ProviderStatus.OK),
            bound_to="",
            findings_ref="/tmp/f.json",
        )


def test_verdict_findings_ref_required() -> None:
    with pytest.raises(SchemaError):
        sup.Verdict(
            verdict=sup.VerdictValue.GREEN,
            by="rule:any-p0p1",
            codex=sup.ProviderFindings(status=sup.ProviderStatus.OK),
            gemini=sup.ProviderFindings(status=sup.ProviderStatus.OK),
            bound_to="diff",
            findings_ref="",
        )


def test_verdict_wire_key_is_verdict_not_value() -> None:
    payload = VALID[sup.Verdict].to_dict()  # type: ignore[attr-defined]
    assert "verdict" in payload and "value" not in payload


# --- Plan / DAG ---------------------------------------------------------------


def test_plan_duplicate_node_ids_rejected() -> None:
    with pytest.raises(SchemaError):
        sup.Plan(
            schema_version=1,
            plan_id="p",
            objective="o",
            acceptance_oracle_ref="x",
            nodes=[
                sup.Node(node_id="n1", brief="b", base_ref="m"),
                sup.Node(node_id="n1", brief="b", base_ref="m"),
            ],
        )


def test_plan_unknown_dep_rejected() -> None:
    with pytest.raises(SchemaError):
        sup.Plan(
            schema_version=1,
            plan_id="p",
            objective="o",
            acceptance_oracle_ref="x",
            nodes=[sup.Node(node_id="n1", brief="b", base_ref="m", deps=["ghost"])],
        )


def test_plan_cycle_rejected() -> None:
    with pytest.raises(SchemaError):
        sup.Plan(
            schema_version=1,
            plan_id="p",
            objective="o",
            acceptance_oracle_ref="x",
            nodes=[
                sup.Node(node_id="n1", brief="b", base_ref="m", deps=["n2"]),
                sup.Node(node_id="n2", brief="b", base_ref="m", deps=["n1"]),
            ],
        )


def test_node_self_dependency_rejected() -> None:
    with pytest.raises(SchemaError):
        sup.Node(node_id="n1", brief="b", base_ref="m", deps=["n1"])


# --- Oracle / Approval / DLQ / Fixer edge invariants -------------------------


def test_oracle_duplicate_criterion_ids_rejected() -> None:
    with pytest.raises(SchemaError):
        sup.Oracle(
            schema_version=1,
            oracle_version=1,
            runtime=sup.OracleRuntime(cwd="/x"),
            criteria=[
                sup.OracleCriterion(
                    id="o1",
                    scope=sup.OracleScope.FINAL,
                    type=sup.OracleType.TEST,
                    spec="s",
                    expect="e",
                    severity=sup.Severity.P0,
                ),
                sup.OracleCriterion(
                    id="o1",
                    scope=sup.OracleScope.FINAL,
                    type=sup.OracleType.TEST,
                    spec="s",
                    expect="e",
                    severity=sup.Severity.P0,
                ),
            ],
        )


def test_oracle_runtime_timeout_positive() -> None:
    with pytest.raises(SchemaError):
        sup.OracleRuntime(cwd="/x", timeout_s=0)


def test_approval_expiry_and_bound_hash_required() -> None:
    with pytest.raises(SchemaError):
        sup.Approval(node="n1", grantor="o", granted_at="t", expires_at="", bound_hash="bh")
    with pytest.raises(SchemaError):
        sup.Approval(node="n1", grantor="o", granted_at="t", expires_at="t2", bound_hash="")


def test_dlq_requires_options_and_recommendation() -> None:
    with pytest.raises(SchemaError):
        sup.DLQEntry(
            node="n1", reason="r", impact="i", recommended="rec", worst_case="w", options=[]
        )


def test_fixer_attempt_not_exceed_max() -> None:
    with pytest.raises(SchemaError):
        sup.Fixer(
            fixer_id="f",
            parent_node="n",
            attempt=3,
            trigger=sup.FixerTrigger.ORACLE_RED,
            base_ref="b",
            max_attempts=2,
        )


def test_ack_is_not_an_event() -> None:
    # Ack has no `writer` field — it's an AckInbox signal, not an event (INV-3).
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(sup.Ack)}
    assert "writer" not in field_names
    assert "node" in field_names and "staged_diff_hash" in field_names


def test_ack_requires_a_binding_hash() -> None:
    # at least one of tree_oid / staged_diff_hash must be present (R2 codex C-P1-9)
    with pytest.raises(SchemaError):
        sup.Ack(node="n1", run_id="r1", attempt=1)
    # one is enough
    sup.Ack(node="n1", run_id="r1", attempt=1, tree_oid="t1")
    sup.Ack(node="n1", run_id="r1", attempt=1, staged_diff_hash="s1")


# --- Oracle cleanup red line (schema-pollution) -------------------------------


def test_oracle_drop_recreate_requires_db_and_template() -> None:
    with pytest.raises(SchemaError):
        sup.OracleRuntime(cwd="/x", cleanup=sup.CleanupPolicy.DROP_RECREATE_FROM_TEMPLATE)
    # explicit cleanup=NONE is fine for a non-DB oracle
    sup.OracleRuntime(cwd="/x", cleanup=sup.CleanupPolicy.NONE)


def test_oracle_sql_criterion_requires_drop_recreate() -> None:
    rt_none = sup.OracleRuntime(cwd="/x", cleanup=sup.CleanupPolicy.NONE)
    sql = sup.OracleCriterion(
        id="s1",
        scope=sup.OracleScope.FINAL,
        type=sup.OracleType.SQL,
        spec="SELECT 1",
        expect="1",
        severity=sup.Severity.P0,
    )
    with pytest.raises(SchemaError):
        sup.Oracle(schema_version=1, oracle_version=1, runtime=rt_none, criteria=[sql])


def test_oracle_criterion_milestone_required_for_milestone_scope() -> None:
    with pytest.raises(SchemaError):
        sup.OracleCriterion(
            id="m1",
            scope=sup.OracleScope.MILESTONE,
            type=sup.OracleType.TEST,
            spec="s",
            expect="e",
            severity=sup.Severity.P1,
        )
    # supplying milestone makes it valid
    sup.OracleCriterion(
        id="m1",
        scope=sup.OracleScope.MILESTONE,
        type=sup.OracleType.TEST,
        spec="s",
        expect="e",
        severity=sup.Severity.P1,
        milestone="after-n2",
    )


def test_oracle_criterion_expect_required() -> None:
    with pytest.raises(SchemaError):
        sup.OracleCriterion(
            id="o",
            scope=sup.OracleScope.AFFECTED,
            type=sup.OracleType.CMD,
            spec="s",
            expect="",
            severity=sup.Severity.P1,
        )


# --- §4 required-semantic fields ---------------------------------------------


def test_plan_acceptance_oracle_ref_required() -> None:
    with pytest.raises(SchemaError):
        sup.Plan(schema_version=1, plan_id="p", objective="o", acceptance_oracle_ref="")


def test_node_brief_required() -> None:
    with pytest.raises(SchemaError):
        sup.Node(node_id="n1", brief="", base_ref="main")


def test_action_input_hash_required() -> None:
    with pytest.raises(SchemaError):
        sup.Action(action_id="a", idempotency_key="k", input_hash="", fencing_token="f")


# --- fail-closed: schema_version + primitive types ---------------------------


def test_schema_version_rejects_future() -> None:
    for cls, kw in (
        (
            sup.Event,
            dict(
                event_id="e",
                seq=0,
                ts="t",
                plan_id="p",
                type=sup.EventType.PLAN_CREATED,
                expected_prev_seq=-1,
                dedupe_key="d",
            ),
        ),
        (sup.Plan, dict(plan_id="p", objective="o", acceptance_oracle_ref="x")),
    ):
        with pytest.raises(SchemaError):
            cls(schema_version=sup.SCHEMA_VERSION + 1, **kw)  # type: ignore[arg-type]


def test_coerce_rejects_wrong_primitive_types() -> None:
    base = VALID[sup.Event].to_dict()  # type: ignore[attr-defined]
    with pytest.raises(SchemaError):
        sup.Event.from_dict({**base, "event_id": 123})  # int where str expected
    with pytest.raises(SchemaError):
        sup.Event.from_dict({**base, "seq": True})  # bool where int expected
    prov = VALID[sup.Provenance].to_dict()  # type: ignore[attr-defined]
    with pytest.raises(SchemaError):
        sup.Provenance.from_dict({**prov, "models": [1, 2]})  # list[str] with ints


# --- event payloads (§4.2 payload freezing) ----------------------------------


def test_payload_map_is_total() -> None:
    sup.assert_payload_map_total()
    assert set(sup.EVENT_PAYLOAD_CONTRACT) == set(sup.EventType)


def test_context_patch_op_rules() -> None:
    # upsert needs a value
    with pytest.raises(SchemaError):
        sup.ContextPatchOp(op=sup.ContextPatchOpKind.UPSERT, key="k")
    # delete must not carry a value
    with pytest.raises(SchemaError):
        sup.ContextPatchOp(op=sup.ContextPatchOpKind.DELETE, key="k", value="v")
    sup.ContextPatchOp(op=sup.ContextPatchOpKind.DELETE, key="k")


def test_context_patch_rejects_empty_and_dup_keys() -> None:
    with pytest.raises(SchemaError):
        sup.ContextPatch(patches=[])
    op = sup.ContextPatchOp(op=sup.ContextPatchOpKind.UPSERT, key="k", value="v")
    with pytest.raises(SchemaError):
        sup.ContextPatch(patches=[op, op])


def test_validate_event_payload_enforces_mapped_contract() -> None:
    good = sup.ContextPatch(
        patches=[sup.ContextPatchOp(op=sup.ContextPatchOpKind.UPSERT, key="k", value="v")]
    ).to_dict()
    ev = sup.Event(
        schema_version=1,
        event_id="e",
        seq=0,
        ts="t",
        plan_id="p",
        type=sup.EventType.CONTEXT_PATCHED,
        expected_prev_seq=-1,
        dedupe_key="d",
        payload=good,
    )
    sup.validate_event_payload(ev)  # ok

    bad = sup.Event(
        schema_version=1,
        event_id="e",
        seq=0,
        ts="t",
        plan_id="p",
        type=sup.EventType.CONTEXT_PATCHED,
        expected_prev_seq=-1,
        dedupe_key="d",
        payload={"patches": "not-a-list"},
    )
    with pytest.raises(SchemaError):
        sup.validate_event_payload(bad)


def test_validate_event_payload_open_payload_is_free() -> None:
    # snapshot_taken maps to None → any payload accepted
    ev = sup.Event(
        schema_version=1,
        event_id="e",
        seq=0,
        ts="t",
        plan_id="p",
        type=sup.EventType.SNAPSHOT_TAKEN,
        expected_prev_seq=-1,
        dedupe_key="d",
        payload={"anything": [1, 2, 3]},
    )
    sup.validate_event_payload(ev)  # no contract → no-op


def test_coerce_payload_returns_typed_contract() -> None:
    fixer = VALID[sup.Fixer].to_dict()  # type: ignore[attr-defined]
    obj = sup.coerce_payload(sup.EventType.FIXER_SPAWNED, fixer)
    assert isinstance(obj, sup.Fixer)
