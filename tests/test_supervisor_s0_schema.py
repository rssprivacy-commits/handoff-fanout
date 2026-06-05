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
        type=sup.EventType.NODE_ADVANCED,
        expected_prev_seq=-1,
        dedupe_key="d1",
        # S0-fix P0-3: every event carries a frozen typed payload (NODE_ADVANCED →
        # NodeAttempt) — an Event with the wrong/empty payload is now rejected.
        payload={"node": "n1", "attempt": 1},
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
    # --- S0-fix P0-1: formerly-open ("None") event payloads, now frozen ---------
    sup.NodeAttempt: sup.NodeAttempt(node="n1", attempt=1),
    sup.NodeReason: sup.NodeReason(node="n1", reason="verdict UNKNOWN: infra failure"),
    sup.AuditDone: sup.AuditDone(
        node="n1",
        attempt=1,
        verdict=sup.Verdict(
            verdict=sup.VerdictValue.GREEN,
            by="rule:any-p0p1",
            codex=sup.ProviderFindings(status=sup.ProviderStatus.OK),
            gemini=sup.ProviderFindings(status=sup.ProviderStatus.OK),
            bound_to="diffhash",
            findings_ref="/tmp/findings.json",
        ),
    ),
    sup.OracleChecked: sup.OracleChecked(node="n1", scope=sup.OracleScope.AFFECTED, passed=True),
    sup.FixerDone: sup.FixerDone(
        fixer_id="f-n1-1", parent_node="n1", attempt=1, state=sup.FixerState.DONE
    ),
    sup.IrreversibleExecuted: sup.IrreversibleExecuted(
        node="n1", side_effect=sup.SideEffect(kind=sup.SideEffectKind.DB_MIGRATION)
    ),
    sup.GlobalPaused: sup.GlobalPaused(reason="owner", actor="owner"),
    sup.GlobalResumed: sup.GlobalResumed(actor="owner"),
    sup.OwnerOverride: sup.OwnerOverride(
        node="n1",
        target_state=sup.RecoveryTarget.PENDING,
        actor="owner",
        reason="manually verified safe",
        bound_hash="bh",
    ),
    sup.SnapshotTaken: sup.SnapshotTaken(through_seq=10, state_hash="h"),
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
        type=sup.EventType.NODE_ADVANCED,
        expected_prev_seq=-1,
        dedupe_key="d0",
        payload={"node": "n1", "attempt": 1},
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
    # P2-8: a RED verdict must carry deduped fingerprints (auditable dedup); the
    # contributing providers must then carry them too (subset check). Auto-fill so
    # the verdict-consistency tests stay focused on the GREEN/RED/UNKNOWN rule.
    cfp = ["c1"] if (c_p0 or c_p1) else []
    gfp = ["g1"] if (g_p0 or g_p1) else []
    deduped = (cfp + gfp) if value is sup.VerdictValue.RED else []
    return sup.Verdict(
        verdict=value,
        by="rule:any-p0p1",
        codex=sup.ProviderFindings(status=codex_status, p0=c_p0, p1=c_p1, fingerprints=cfp),
        gemini=sup.ProviderFindings(status=gemini_status, p0=g_p0, p1=g_p1, fingerprints=gfp),
        bound_to="diff",
        findings_ref="/tmp/f.json",
        degraded=degraded,
        deduped_fingerprints=deduped,
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


def test_verdict_red_requires_deduped_fingerprints() -> None:
    # P2-8 (codex R2): a RED verdict with no finding ids cannot be dedup-audited.
    with pytest.raises(SchemaError):
        sup.Verdict(
            verdict=sup.VerdictValue.RED,
            by="rule:any-p0p1",
            codex=sup.ProviderFindings(status=sup.ProviderStatus.OK, p0=1),
            gemini=sup.ProviderFindings(status=sup.ProviderStatus.OK),
            bound_to="diff",
            findings_ref="/tmp/f.json",
        )
    # with fingerprints it is legal
    sup.Verdict(
        verdict=sup.VerdictValue.RED,
        by="rule:any-p0p1",
        codex=sup.ProviderFindings(status=sup.ProviderStatus.OK, p0=1, fingerprints=["a"]),
        gemini=sup.ProviderFindings(status=sup.ProviderStatus.OK),
        bound_to="diff",
        findings_ref="/tmp/f.json",
        deduped_fingerprints=["a"],
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


# --- P0-1 + P0-3: every event has a frozen payload, enforced at construction ---


def _mk_event(event_type: sup.EventType, payload: dict, **over) -> sup.Event:
    base = dict(
        schema_version=1,
        event_id="e",
        seq=0,
        ts="t",
        plan_id="p",
        type=event_type,
        expected_prev_seq=-1,
        dedupe_key="d",
        payload=payload,
    )
    base.update(over)
    return sup.Event(**base)  # type: ignore[arg-type]


def test_payload_map_has_no_open_payloads() -> None:
    # P0-1 (both brains): the original S0 mapped 14 events to None ("留给以后那片").
    # Every event must now map to a concrete contract — no None placeholders.
    assert all(c is not None for c in sup.EVENT_PAYLOAD_CONTRACT.values())


def test_every_event_constructs_with_its_valid_payload() -> None:
    # For every EventType, the VALID fixture for its mapped contract is a legal
    # payload (proves the frozen map + the fixtures are mutually consistent).
    for et, contract in sup.EVENT_PAYLOAD_CONTRACT.items():
        payload = VALID[contract].to_dict()  # type: ignore[attr-defined]
        ev = _mk_event(et, payload)
        assert ev.type is et


def test_event_payload_enforced_at_construction() -> None:
    # P0-3 (both brains): a malformed payload can no longer become a legal Event —
    # validation fires during construction (not only via a separate function call).
    good = sup.ContextPatch(
        patches=[sup.ContextPatchOp(op=sup.ContextPatchOpKind.UPSERT, key="k", value="v")]
    ).to_dict()
    ev = _mk_event(sup.EventType.CONTEXT_PATCHED, good)
    sup.validate_event_payload(ev)  # idempotent re-check still passes

    with pytest.raises(SchemaError):
        _mk_event(sup.EventType.CONTEXT_PATCHED, {"patches": "not-a-list"})


def test_event_empty_payload_rejected_for_required_contract() -> None:
    # An event whose payload contract has required fields rejects an empty payload —
    # this is the freeze: NODE_DISPATCHED can't carry "nothing" anymore.
    with pytest.raises(SchemaError):
        _mk_event(sup.EventType.NODE_DISPATCHED, {})
    with pytest.raises(SchemaError):
        _mk_event(sup.EventType.SNAPSHOT_TAKEN, {"anything": [1, 2, 3]})


def test_event_wrong_payload_for_type_rejected() -> None:
    # A NodeAttempt payload under a PLAN_CREATED event (expects a Plan) is rejected.
    with pytest.raises(SchemaError):
        _mk_event(sup.EventType.PLAN_CREATED, {"node": "n1", "attempt": 1})


def test_event_payload_rejects_non_json_primitive() -> None:
    # P2-9: the open wire payload dict is fail-closed on non-JSON contents.
    with pytest.raises(SchemaError):
        sup.Event.from_dict(
            {
                "schema_version": 1,
                "event_id": "e",
                "seq": 0,
                "ts": "t",
                "plan_id": "p",
                "type": "snapshot_taken",
                "expected_prev_seq": -1,
                "dedupe_key": "d",
                "payload": {"through_seq": 1, "state_hash": "h", "bad": {1: "int-key"}},
            }
        )


def test_coerce_payload_returns_typed_contract() -> None:
    fixer = VALID[sup.Fixer].to_dict()  # type: ignore[attr-defined]
    obj = sup.coerce_payload(sup.EventType.FIXER_SPAWNED, fixer)
    assert isinstance(obj, sup.Fixer)


# --- S0-fix new payload invariants -------------------------------------------


def test_audit_done_carries_machine_verdict() -> None:
    # gemini #4: audit_done binds the deterministic Verdict (INV-2) into the log.
    with pytest.raises(SchemaError):
        sup.AuditDone(node="", attempt=1, verdict=VALID[sup.Verdict])  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        sup.AuditDone(node="n1", attempt=0, verdict=VALID[sup.Verdict])  # type: ignore[arg-type]


def test_oracle_checked_red_must_name_failures() -> None:
    sup.OracleChecked(node="n1", scope=sup.OracleScope.FINAL, passed=True)
    with pytest.raises(SchemaError):  # RED with no failed criteria
        sup.OracleChecked(node="n1", scope=sup.OracleScope.FINAL, passed=False)
    with pytest.raises(SchemaError):  # GREEN cannot list failures
        sup.OracleChecked(
            node="n1", scope=sup.OracleScope.FINAL, passed=True, failed_criteria=["o1"]
        )
    sup.OracleChecked(node="n1", scope=sup.OracleScope.FINAL, passed=False, failed_criteria=["o1"])


def test_fixer_done_must_be_terminal() -> None:
    sup.FixerDone(fixer_id="f", parent_node="n", attempt=1, state=sup.FixerState.DONE)
    sup.FixerDone(fixer_id="f", parent_node="n", attempt=1, state=sup.FixerState.FAILED)
    with pytest.raises(SchemaError):  # DISPATCHED is not a terminal fixer state
        sup.FixerDone(fixer_id="f", parent_node="n", attempt=1, state=sup.FixerState.DISPATCHED)


def test_global_paused_and_resumed_require_actor() -> None:
    with pytest.raises(SchemaError):
        sup.GlobalPaused(reason="owner", actor="")
    with pytest.raises(SchemaError):
        sup.GlobalPaused(reason="", actor="owner")
    with pytest.raises(SchemaError):
        sup.GlobalResumed(actor="")


def test_owner_override_requires_bound_hash_and_legal_target() -> None:
    sup.OwnerOverride(
        node="n1",
        target_state=sup.RecoveryTarget.DISPATCHED,
        actor="owner",
        reason="safe",
        bound_hash="bh",
    )
    with pytest.raises(SchemaError):  # anti-replay bind required
        sup.OwnerOverride(
            node="n1",
            target_state=sup.RecoveryTarget.PENDING,
            actor="owner",
            reason="r",
            bound_hash="",
        )
    with pytest.raises(SchemaError):  # DONE is not a legal recovery target
        sup.OwnerOverride.from_dict(
            {
                "node": "n1",
                "target_state": "DONE",
                "actor": "o",
                "reason": "r",
                "bound_hash": "bh",
            }
        )


def test_recovery_target_values_are_node_states() -> None:
    node_state_values = {s.value for s in sup.NodeState}
    assert {t.value for t in sup.RecoveryTarget} <= node_state_values


def test_side_effect_rejects_unauthorized_irreversible() -> None:
    # P1-7 / INV-6: non-sandboxed, non-dry-run, no compensation, no preauth → reject.
    with pytest.raises(SchemaError):
        sup.SideEffect(
            kind=sup.SideEffectKind.EXTERNAL_ACCOUNT,
            sandboxed=False,
            dry_run=False,
            compensation=None,
            needs_preauth=False,
        )
    # any one escape hatch makes it legal
    sup.SideEffect(kind=sup.SideEffectKind.EXTERNAL_ACCOUNT, sandboxed=False, needs_preauth=True)
    sup.SideEffect(kind=sup.SideEffectKind.EXTERNAL_ACCOUNT, sandboxed=False, compensation="refund")
    sup.SideEffect(kind=sup.SideEffectKind.EXTERNAL_ACCOUNT, sandboxed=True)


def test_node_reversible_side_effect_consistency() -> None:
    preauth = sup.SideEffect(kind=sup.SideEffectKind.DB_MIGRATION, needs_preauth=True)
    # a preauth (irreversible) effect on a reversible node is inconsistent
    with pytest.raises(SchemaError):
        sup.Node(node_id="n1", brief="b", base_ref="m", reversible=True, side_effects=[preauth])
    # reversible=false with no declared side effects is inconsistent (INV-6)
    with pytest.raises(SchemaError):
        sup.Node(node_id="n1", brief="b", base_ref="m", reversible=False)
    # consistent: irreversible node declaring its preauth effect
    sup.Node(node_id="n1", brief="b", base_ref="m", reversible=False, side_effects=[preauth])


def test_oracle_network_allow_requires_exemption() -> None:
    # P1-6: network=allow is rejected unless an explicit isolation_exemption is set.
    with pytest.raises(SchemaError):
        sup.OracleRuntime(
            cwd="/x",
            db="sandbox:erp_test",
            db_template="erp_baseline",
            network=sup.NetworkPolicy.ALLOW,
        )
    sup.OracleRuntime(
        cwd="/x",
        db="sandbox:erp_test",
        db_template="erp_baseline",
        network=sup.NetworkPolicy.ALLOW,
        isolation_exemption="owner-approved: needs live API in test #42",
    )


def test_oracle_db_bearing_cleanup_none_requires_exemption() -> None:
    # P1-6: a DB-bearing runtime that does not drop-recreate leaks schema pollution.
    with pytest.raises(SchemaError):
        sup.OracleRuntime(cwd="/x", db="sandbox:erp_test", cleanup=sup.CleanupPolicy.NONE)
    sup.OracleRuntime(
        cwd="/x",
        db="sandbox:erp_test",
        cleanup=sup.CleanupPolicy.NONE,
        isolation_exemption="read-only oracle, no schema writes",
    )


def test_verdict_deduped_fingerprints_must_subset_union() -> None:
    # P2-8: the cross-provider deduped set must trace to the raw provider findings.
    sup.Verdict(
        verdict=sup.VerdictValue.RED,
        by="rule:any-p0p1",
        codex=sup.ProviderFindings(status=sup.ProviderStatus.OK, p0=1, fingerprints=["a", "b"]),
        gemini=sup.ProviderFindings(status=sup.ProviderStatus.OK, p1=1, fingerprints=["b", "c"]),
        bound_to="diff",
        findings_ref="/tmp/f.json",
        deduped_fingerprints=["a", "b", "c"],
    )
    with pytest.raises(SchemaError):  # 'z' is not in either provider's fingerprints
        sup.Verdict(
            verdict=sup.VerdictValue.RED,
            by="rule:any-p0p1",
            codex=sup.ProviderFindings(status=sup.ProviderStatus.OK, p0=1, fingerprints=["a"]),
            gemini=sup.ProviderFindings(status=sup.ProviderStatus.OK),
            bound_to="diff",
            findings_ref="/tmp/f.json",
            deduped_fingerprints=["a", "z"],
        )


def test_dlq_provenance_is_typed() -> None:
    # P2-9: DLQEntry.provenance is a typed Provenance, not a free-form dict.
    dlq = sup.DLQEntry(
        node="n1",
        reason="r",
        impact="i",
        recommended="rec",
        worst_case="w",
        options=["a"],
        provenance=sup.Provenance(commit="abc"),
    )
    assert dlq.provenance is not None
    rebuilt = sup.DLQEntry.from_dict(dlq.to_dict())
    assert rebuilt == dlq
