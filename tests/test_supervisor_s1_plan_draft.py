"""S1 — Plan draft → owner-approve → lock-hash tests (design §4.1 / §13).

The supervisor *drafts* a Plan (and optionally its acceptance Oracle); the owner
*approves* it, which binds it to a canonical hash. After that, the plan is never
edited in place — a change is an ``amend`` that produces a frozen
:class:`~handoff_fanout.supervisor.PlanAmendment` (diff / reason / approver / hash)
ready to be emitted as a ``plan_amended`` event (INV-9). This is the "立靶子"
(lock the target) half of slice S1.

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s1_plan_draft.py
"""

from __future__ import annotations

import json

import pytest

from handoff_fanout.supervisor import (
    Node,
    Oracle,
    OracleCriterion,
    OracleRuntime,
    OracleScope,
    OracleType,
    PlanAmendment,
    SchemaError,
    Severity,
)
from handoff_fanout.supervisor import Plan as PlanContract
from handoff_fanout.supervisor.plan_draft import (
    LockedPlan,
    PlanDraft,
    amend_locked_plan,
    approve_plan,
    canonical_bytes,
    draft_plan,
    is_lock_valid,
    oracle_hash,
    plan_hash,
    verify_lock,
)

# --- fixtures ----------------------------------------------------------------


def _plan(objective: str = "ship feature X") -> PlanContract:
    return PlanContract(
        schema_version=1,
        plan_id="p-feature-x",
        objective=objective,
        acceptance_oracle_ref="oracle.json",
        nodes=[
            Node(node_id="n1", brief="scaffold", base_ref="main"),
            Node(node_id="n2", brief="impl", base_ref="main", deps=["n1"]),
        ],
    )


def _oracle(version: int = 1) -> Oracle:
    return Oracle(
        schema_version=1,
        oracle_version=version,
        runtime=OracleRuntime(cwd="/tmp/wt", db="sandbox:erp_test", db_template="erp_baseline"),
        criteria=[
            OracleCriterion(
                id="o1",
                scope=OracleScope.FINAL,
                type=OracleType.CMD,
                spec="make check",
                expect="0",
                severity=Severity.P0,
            )
        ],
    )


# --- canonical hashing -------------------------------------------------------


def test_plan_hash_is_deterministic() -> None:
    assert plan_hash(_plan()) == plan_hash(_plan())


def test_plan_hash_is_sha256_hex() -> None:
    h = plan_hash(_plan())
    assert len(h) == 64
    int(h, 16)  # raises if not hex


def test_plan_hash_changes_when_any_field_changes() -> None:
    assert plan_hash(_plan("A")) != plan_hash(_plan("B"))


def test_canonical_bytes_are_stable_and_parse_as_json() -> None:
    b = canonical_bytes(_plan())
    assert b == canonical_bytes(_plan())
    assert json.loads(b.decode())["plan_id"] == "p-feature-x"


def test_oracle_hash_is_deterministic_and_version_sensitive() -> None:
    assert oracle_hash(_oracle(1)) == oracle_hash(_oracle(1))
    assert oracle_hash(_oracle(1)) != oracle_hash(_oracle(2))


# --- draft -------------------------------------------------------------------


def test_draft_plan_records_drafter_provenance() -> None:
    d = draft_plan(_plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    assert isinstance(d, PlanDraft)
    assert d.drafted_by == "supervisor"
    assert d.drafted_at == "2026-06-06T00:00:00Z"
    assert d.plan.plan_id == "p-feature-x"


def test_draft_plan_round_trips() -> None:
    d = draft_plan(_plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    assert PlanDraft.from_dict(d.to_dict()) == d


def test_draft_requires_a_drafter() -> None:
    with pytest.raises(SchemaError):
        draft_plan(_plan(), drafted_by="", drafted_at="2026-06-06T00:00:00Z")


# --- approve / lock ----------------------------------------------------------


def test_approve_binds_canonical_plan_hash() -> None:
    d = draft_plan(_plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    locked = approve_plan(d, approver="owner", approved_at="2026-06-06T01:00:00Z")
    assert isinstance(locked, LockedPlan)
    assert locked.plan_id == "p-feature-x"
    assert locked.plan_hash == plan_hash(_plan())
    assert locked.approver == "owner"
    assert locked.approved_at == "2026-06-06T01:00:00Z"
    assert locked.oracle_hash is None


def test_approve_can_lock_the_oracle_together() -> None:
    d = draft_plan(_plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    locked = approve_plan(d, approver="owner", approved_at="2026-06-06T01:00:00Z", oracle=_oracle())
    assert locked.oracle_hash == oracle_hash(_oracle())


def test_locked_plan_round_trips() -> None:
    d = draft_plan(_plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    locked = approve_plan(d, approver="owner", approved_at="2026-06-06T01:00:00Z", oracle=_oracle())
    assert LockedPlan.from_dict(locked.to_dict()) == locked


def test_approve_requires_an_approver() -> None:
    d = draft_plan(_plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    with pytest.raises(SchemaError):
        approve_plan(d, approver="", approved_at="2026-06-06T01:00:00Z")


# --- verify (lock-hash enforcement / anti-drift) -----------------------------


def test_verify_lock_passes_for_the_approved_plan() -> None:
    d = draft_plan(_plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    locked = approve_plan(d, approver="owner", approved_at="2026-06-06T01:00:00Z")
    verify_lock(locked, _plan())  # no raise
    assert is_lock_valid(locked, _plan()) is True


def test_verify_lock_rejects_a_tampered_plan() -> None:
    d = draft_plan(_plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    locked = approve_plan(d, approver="owner", approved_at="2026-06-06T01:00:00Z")
    tampered = _plan(objective="ship feature X but also drop the prod DB")
    assert is_lock_valid(locked, tampered) is False
    with pytest.raises(SchemaError):
        verify_lock(locked, tampered)


def test_verify_lock_checks_the_oracle_when_locked_together() -> None:
    d = draft_plan(_plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    locked = approve_plan(
        d, approver="owner", approved_at="2026-06-06T01:00:00Z", oracle=_oracle(1)
    )
    verify_lock(locked, _plan(), oracle=_oracle(1))  # no raise
    with pytest.raises(SchemaError):
        verify_lock(locked, _plan(), oracle=_oracle(2))


def test_verify_lock_requires_the_oracle_when_one_was_locked() -> None:
    d = draft_plan(_plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    locked = approve_plan(d, approver="owner", approved_at="2026-06-06T01:00:00Z", oracle=_oracle())
    with pytest.raises(SchemaError):
        verify_lock(locked, _plan())  # oracle was locked but not provided


# --- amend (the only way to change a locked plan / INV-9) --------------------


def test_amend_produces_a_new_lock_and_a_frozen_amendment() -> None:
    d = draft_plan(_plan("A"), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    locked = approve_plan(d, approver="owner", approved_at="2026-06-06T01:00:00Z")
    new_plan = _plan("B")
    new_locked, amendment = amend_locked_plan(
        locked,
        new_plan,
        reason="owner asked for B",
        approver="owner",
        approved_at="2026-06-06T02:00:00Z",
    )
    assert isinstance(new_locked, LockedPlan)
    assert new_locked.plan_hash == plan_hash(new_plan)
    assert isinstance(amendment, PlanAmendment)
    assert amendment.plan_id == "p-feature-x"
    assert amendment.reason == "owner asked for B"
    assert amendment.approver == "owner"
    assert amendment.bound_hash == plan_hash(new_plan)
    assert amendment.diff  # non-empty unified diff
    assert "A" in amendment.diff and "B" in amendment.diff


def test_amendment_round_trips() -> None:
    d = draft_plan(_plan("A"), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    locked = approve_plan(d, approver="owner", approved_at="2026-06-06T01:00:00Z")
    _, amendment = amend_locked_plan(
        locked,
        _plan("B"),
        reason="r",
        approver="owner",
        approved_at="2026-06-06T02:00:00Z",
    )
    assert PlanAmendment.from_dict(amendment.to_dict()) == amendment


def test_amend_with_an_identical_plan_is_rejected() -> None:
    d = draft_plan(_plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    locked = approve_plan(d, approver="owner", approved_at="2026-06-06T01:00:00Z")
    with pytest.raises(SchemaError):
        amend_locked_plan(
            locked,
            _plan(),  # identical — nothing to amend
            reason="noop",
            approver="owner",
            approved_at="2026-06-06T02:00:00Z",
        )


def test_amend_cannot_silently_change_plan_identity() -> None:
    d = draft_plan(_plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    locked = approve_plan(d, approver="owner", approved_at="2026-06-06T01:00:00Z")
    renamed = PlanContract(
        schema_version=1,
        plan_id="p-different",
        objective="ship feature X",
        acceptance_oracle_ref="oracle.json",
        nodes=[Node(node_id="n1", brief="scaffold", base_ref="main")],
    )
    with pytest.raises(SchemaError):
        amend_locked_plan(
            locked,
            renamed,
            reason="rename",
            approver="owner",
            approved_at="2026-06-06T02:00:00Z",
        )


def test_amend_requires_a_reason_and_approver() -> None:
    d = draft_plan(_plan("A"), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z")
    locked = approve_plan(d, approver="owner", approved_at="2026-06-06T01:00:00Z")
    with pytest.raises(SchemaError):
        amend_locked_plan(
            locked, _plan("B"), reason="", approver="owner", approved_at="2026-06-06T02:00:00Z"
        )
    with pytest.raises(SchemaError):
        amend_locked_plan(
            locked, _plan("B"), reason="r", approver="", approved_at="2026-06-06T02:00:00Z"
        )
