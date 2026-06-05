"""S1 Plan draft → approve → lock-hash → amend tests (design §4.1 / §13 / INV-9).

Covers the two-party draft/approve flow, the canonical-hash determinism the lock
depends on, the LockedPlan self-consistency guard, amend-as-the-only-legal-change
(producing a frozen PlanAmendment, rejecting identity changes + no-ops), and the
anti-drift verify_lock enforcement (plan + bound oracle).

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s1_plan_draft.py
"""

from __future__ import annotations

import json

import pytest

from handoff_fanout import supervisor as sup
from handoff_fanout.supervisor import SchemaError

# --- helpers -----------------------------------------------------------------


def _plan(plan_id="p1", *, objective="ship S1", nodes=None):
    nodes = (
        nodes
        if nodes is not None
        else [
            sup.Node(node_id="n1", brief="oracle runner", base_ref="main"),
            sup.Node(node_id="n2", brief="plan draft", base_ref="main", deps=["n1"]),
        ]
    )
    return sup.Plan(
        schema_version=1,
        plan_id=plan_id,
        objective=objective,
        acceptance_oracle_ref="oracle.json",
        nodes=nodes,
    )


def _oracle(version=2):
    return sup.Oracle(
        schema_version=1,
        oracle_version=version,
        runtime=sup.OracleRuntime(cwd="/tmp/wt", cleanup=sup.CleanupPolicy.NONE),
        criteria=[
            sup.OracleCriterion(
                id="o1",
                scope=sup.OracleScope.FINAL,
                type=sup.OracleType.TEST,
                spec="pytest -q",
                expect="0",
                severity=sup.Severity.P0,
            )
        ],
    )


def _draft(plan=None):
    return sup.draft_plan(
        plan or _plan(), drafted_by="supervisor", drafted_at="2026-06-06T00:00:00Z"
    )


# --- canonical hash determinism ----------------------------------------------


def test_plan_hash_is_deterministic_and_field_order_independent():
    # Two structurally-equal plans built in a different *argument* order hash equal
    # (to_dict sorts keys), proving the hash is over content, not declaration order.
    p_a = sup.Plan(
        schema_version=1, plan_id="p1", objective="o", acceptance_oracle_ref="oracle.json", nodes=[]
    )
    p_b = sup.Plan(
        acceptance_oracle_ref="oracle.json", objective="o", plan_id="p1", schema_version=1, nodes=[]
    )
    assert sup.plan_hash(p_a) == sup.plan_hash(p_b)


def test_plan_hash_changes_when_node_order_changes():
    # A reordered DAG is a different plan the owner did not approve → different hash.
    n1 = sup.Node(node_id="n1", brief="a", base_ref="main")
    n2 = sup.Node(node_id="n2", brief="b", base_ref="main")
    assert sup.plan_hash(_plan(nodes=[n1, n2])) != sup.plan_hash(_plan(nodes=[n2, n1]))


def test_oracle_hash_is_version_sensitive():
    assert sup.oracle_hash(_oracle(version=2)) != sup.oracle_hash(_oracle(version=3))


def test_canonical_bytes_are_compact_sorted_json():
    raw = sup.canonical_bytes(_plan())
    parsed = json.loads(raw)
    assert parsed["plan_id"] == "p1"
    # compact separators: no ", " / ": " spacing
    assert b", " not in raw and b": " not in raw


# --- draft → approve ---------------------------------------------------------


def test_draft_then_approve_locks_to_canonical_hash():
    plan = _plan()
    locked = sup.approve_plan(_draft(plan), approver="owner", approved_at="2026-06-06T01:00:00Z")
    assert locked.plan_hash == sup.plan_hash(plan)
    assert locked.approver == "owner"
    assert locked.plan_id == "p1"
    assert locked.oracle_hash is None


def test_approve_can_lock_oracle_together():
    oracle = _oracle()
    locked = sup.approve_plan(_draft(), approver="owner", approved_at="t", oracle=oracle)
    assert locked.oracle_hash == sup.oracle_hash(oracle)


def test_draft_requires_provenance():
    with pytest.raises(SchemaError, match="drafted_by required"):
        sup.draft_plan(_plan(), drafted_by="", drafted_at="t")
    with pytest.raises(SchemaError, match="drafted_at required"):
        sup.draft_plan(_plan(), drafted_by="supervisor", drafted_at="")


def test_approve_requires_owner_and_timestamp():
    d = _draft()
    with pytest.raises(SchemaError, match="requires an approver"):
        sup.approve_plan(d, approver="", approved_at="t")
    with pytest.raises(SchemaError, match="requires approved_at"):
        sup.approve_plan(d, approver="owner", approved_at="")


def test_locked_plan_rejects_mismatched_hash():
    plan = _plan()
    with pytest.raises(SchemaError, match="does not match its plan"):
        sup.LockedPlan(plan=plan, plan_hash="deadbeef", approver="owner", approved_at="t")


def test_locked_plan_round_trips():
    locked = sup.approve_plan(_draft(), approver="owner", approved_at="t", oracle=_oracle())
    again = sup.LockedPlan.from_dict(json.loads(json.dumps(locked.to_dict())))
    assert again == locked


# --- amend (the only legal change, INV-9) ------------------------------------


def test_amend_produces_plan_amendment_bound_to_new_hash():
    locked = sup.approve_plan(_draft(), approver="owner", approved_at="t")
    new_plan = _plan(objective="ship S1 (revised scope)")
    new_locked, amendment = sup.amend_locked_plan(
        locked,
        new_plan,
        reason="owner widened scope",
        approver="owner",
        approved_at="t2",
    )
    assert isinstance(amendment, sup.PlanAmendment)
    assert amendment.plan_id == "p1"
    assert amendment.bound_hash == sup.plan_hash(new_plan) == new_locked.plan_hash
    assert amendment.reason == "owner widened scope"
    assert amendment.diff  # a non-empty unified diff


def test_amend_rejects_identity_change():
    locked = sup.approve_plan(_draft(), approver="owner", approved_at="t")
    with pytest.raises(SchemaError, match="cannot change plan identity"):
        sup.amend_locked_plan(
            locked,
            _plan(plan_id="p2"),
            reason="r",
            approver="owner",
            approved_at="t2",
        )


def test_amend_rejects_noop():
    plan = _plan()
    locked = sup.approve_plan(_draft(plan), approver="owner", approved_at="t")
    with pytest.raises(SchemaError, match="no-op"):
        sup.amend_locked_plan(locked, _plan(), reason="r", approver="owner", approved_at="t2")


def test_amend_requires_reason_and_approver():
    locked = sup.approve_plan(_draft(), approver="owner", approved_at="t")
    new_plan = _plan(objective="changed")
    with pytest.raises(SchemaError, match="requires a reason"):
        sup.amend_locked_plan(locked, new_plan, reason="", approver="owner", approved_at="t2")
    with pytest.raises(SchemaError, match="requires an approver"):
        sup.amend_locked_plan(locked, new_plan, reason="r", approver="", approved_at="t2")


def test_amend_carries_oracle_hash_forward_when_not_respecified():
    oracle = _oracle()
    locked = sup.approve_plan(_draft(), approver="owner", approved_at="t", oracle=oracle)
    new_plan = _plan(objective="changed")
    new_locked, _ = sup.amend_locked_plan(
        locked,
        new_plan,
        reason="r",
        approver="owner",
        approved_at="t2",
    )
    assert new_locked.oracle_hash == sup.oracle_hash(oracle)  # carried forward


def test_amend_refuses_to_change_oracle(  # R2 codex P2-7
):
    locked = sup.approve_plan(
        _draft(), approver="owner", approved_at="t", oracle=_oracle(version=2)
    )
    new_plan = _plan(objective="changed")
    with pytest.raises(SchemaError, match="cannot also change the oracle"):
        sup.amend_locked_plan(
            locked,
            new_plan,
            reason="r",
            approver="owner",
            approved_at="t2",
            oracle=_oracle(version=3),  # a DIFFERENT oracle — not a plan amendment
        )


def test_amend_allows_resupplying_the_same_oracle():
    oracle = _oracle(version=2)
    locked = sup.approve_plan(_draft(), approver="owner", approved_at="t", oracle=oracle)
    new_plan = _plan(objective="changed")
    new_locked, _ = sup.amend_locked_plan(
        locked, new_plan, reason="r", approver="owner", approved_at="t2", oracle=oracle
    )
    assert new_locked.oracle_hash == sup.oracle_hash(oracle)


# --- verify_lock anti-drift --------------------------------------------------


def test_verify_lock_passes_for_approved_plan():
    plan = _plan()
    locked = sup.approve_plan(_draft(plan), approver="owner", approved_at="t")
    sup.verify_lock(locked, plan)  # no raise
    assert sup.is_lock_valid(locked, plan) is True


def test_verify_lock_detects_drift():
    locked = sup.approve_plan(_draft(_plan()), approver="owner", approved_at="t")
    tampered = _plan(objective="silently edited by a worker")
    with pytest.raises(SchemaError, match="drifted from its approved lock"):
        sup.verify_lock(locked, tampered)
    assert sup.is_lock_valid(locked, tampered) is False


def test_verify_lock_requires_oracle_when_bound():
    oracle = _oracle()
    plan = _plan()
    locked = sup.approve_plan(_draft(plan), approver="owner", approved_at="t", oracle=oracle)
    with pytest.raises(SchemaError, match="binds an oracle but none was supplied"):
        sup.verify_lock(locked, plan)  # oracle omitted
    sup.verify_lock(locked, plan, oracle=oracle)  # supplied → ok


def test_verify_lock_detects_oracle_drift():
    plan = _plan()
    locked = sup.approve_plan(
        _draft(plan), approver="owner", approved_at="t", oracle=_oracle(version=2)
    )
    with pytest.raises(SchemaError, match="oracle drifted"):
        sup.verify_lock(locked, plan, oracle=_oracle(version=3))
