"""S1 — Plan draft → owner-approve → lock-hash (design §4.1 / §13 / INV-9).

This is the "立靶子" (lock the target) half of slice **S1**. The supervisor
*drafts* a static Plan (and optionally its acceptance Oracle); the owner *approves*
it, which binds the exact artefact to a canonical hash. The authoritative design
is ``project-files/handoff/supervisor-orchestration-design.md`` (ERP repo) §13
("oracle/plan 起草权 = 中枢起草 + 主人批准锁 hash") and §4.1 ("改 plan =
plan_amended 事件（diff/理由/批准人/hash）").

Two parties, frozen explicitly so neither can be skipped:

* **draft** (:func:`draft_plan`) — the supervisor authors a :class:`Plan` and
  records *who drafted it, when*. A draft has no authority on its own.
* **approve / lock** (:func:`approve_plan`) — the owner approves, producing a
  :class:`LockedPlan` receipt that binds the plan (and optionally the oracle) to a
  canonical sha256. From here the plan is the immutable target.

After locking, the plan is never edited in place (INV-9): a change is an
:func:`amend_locked_plan`, which produces a frozen
:class:`~handoff_fanout.supervisor.payloads.PlanAmendment` (diff / reason /
approver / hash) ready to be emitted as a ``plan_amended`` event. Emitting that
event into the log is the single-writer reducer's job (slice S3) — S1 only
produces the artefacts and the lock-hash machinery.

:func:`verify_lock` is the anti-drift enforcement (the soft-write-protection idea
of INV-5 applied to the plan artefact): given a lock receipt and an on-disk plan,
it fails closed if the plan no longer hashes to what the owner approved (a worker
silently editing ``plan.json`` is caught here). Honest scope: this is *soft* on a
single machine — it catches drift/mistakes, not a determined local attacker who can
also rewrite the lock receipt (design §7).
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json

from ._base import Contract, SchemaError
from .oracle import Oracle
from .payloads import PlanAmendment
from .plan import Plan


def canonical_bytes(contract: Contract) -> bytes:
    """Deterministic canonical JSON encoding of a contract, for hashing.

    Keys are sorted and whitespace is stripped so the same logical artefact always
    produces the same bytes regardless of field declaration order — list order is
    preserved (a reordered DAG is a *different* plan the owner did not approve).
    """
    return json.dumps(
        contract.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plan_hash(plan: Plan) -> str:
    """Canonical sha256 of a plan — the value the owner's approval binds to."""
    return _sha256(canonical_bytes(plan))


def oracle_hash(oracle: Oracle) -> str:
    """Canonical sha256 of an oracle (it is drafted + approved alongside the
    plan, §13). Version-sensitive: an oracle amendment changes the hash."""
    return _sha256(canonical_bytes(oracle))


def _pretty(contract: Contract) -> list[str]:
    """Readable, stable multi-line JSON for a human-facing unified diff."""
    text = json.dumps(contract.to_dict(), sort_keys=True, indent=2, ensure_ascii=False)
    return text.splitlines(keepends=True)


@dataclasses.dataclass
class PlanDraft(Contract):
    """A plan the supervisor has authored but the owner has not yet approved
    (design §13: 中枢起草). Records drafter provenance; carries no authority until
    :func:`approve_plan` turns it into a :class:`LockedPlan`."""

    plan: Plan
    drafted_by: str
    drafted_at: str

    def validate(self) -> None:
        if not self.drafted_by:
            raise SchemaError("PlanDraft.drafted_by required (who drafted it)")
        if not self.drafted_at:
            raise SchemaError("PlanDraft.drafted_at required (when it was drafted)")


@dataclasses.dataclass
class LockedPlan(Contract):
    """An owner-approved plan bound to a canonical hash (design §13: 主人批准锁 hash).

    The receipt carries the plan body so an amendment can diff old→new and so the
    lock is self-consistent: ``plan_hash`` must equal the canonical hash of the
    embedded ``plan`` (a LockedPlan whose hash does not match its plan is malformed
    and is rejected — fail-closed). ``oracle_hash`` is present iff the plan's
    acceptance oracle was locked together.
    """

    plan: Plan
    plan_hash: str
    approver: str
    approved_at: str
    oracle_hash: str | None = None

    @property
    def plan_id(self) -> str:
        return self.plan.plan_id

    def validate(self) -> None:
        if not self.approver:
            raise SchemaError("LockedPlan.approver required")
        if not self.approved_at:
            raise SchemaError("LockedPlan.approved_at required")
        if not self.plan_hash:
            raise SchemaError("LockedPlan.plan_hash required")
        expected = plan_hash(self.plan)
        if self.plan_hash != expected:
            raise SchemaError(
                "LockedPlan.plan_hash does not match its plan "
                f"(stored={self.plan_hash}, actual={expected}) — a lock must bind "
                "the exact approved artefact"
            )


def draft_plan(plan: Plan, *, drafted_by: str, drafted_at: str) -> PlanDraft:
    """Supervisor drafts a plan (design §13 中枢起草)."""
    return PlanDraft(plan=plan, drafted_by=drafted_by, drafted_at=drafted_at)


def approve_plan(
    draft: PlanDraft,
    *,
    approver: str,
    approved_at: str,
    oracle: Oracle | None = None,
) -> LockedPlan:
    """Owner approves a draft, locking it to a canonical hash (design §13).

    Pass ``oracle`` to lock the acceptance oracle together with the plan so the two
    cannot drift apart after approval.
    """
    if not approver:
        raise SchemaError("approve_plan requires an approver (the owner)")
    if not approved_at:
        raise SchemaError("approve_plan requires approved_at")
    return LockedPlan(
        plan=draft.plan,
        plan_hash=plan_hash(draft.plan),
        approver=approver,
        approved_at=approved_at,
        oracle_hash=oracle_hash(oracle) if oracle is not None else None,
    )


def amend_locked_plan(
    locked: LockedPlan,
    new_plan: Plan,
    *,
    reason: str,
    approver: str,
    approved_at: str,
    oracle: Oracle | None = None,
) -> tuple[LockedPlan, PlanAmendment]:
    """Change a locked plan the only legal way (INV-9): produce a new lock + a
    frozen :class:`PlanAmendment` (the payload of a future ``plan_amended`` event).

    Guards: the plan identity (``plan_id``) is stable — a different id is a
    different plan, not an amendment; an amendment that does not actually change the
    plan is rejected (a no-op would forge an approval trail for nothing); and an
    amendment **cannot touch the oracle** — a supplied ``oracle`` may only re-supply
    the exact locked one (carrying its hash forward), never a different one and never
    introduce one into a plan locked without an oracle (INV-9: an oracle change is its
    own attested event, which S0 does not yet have).
    """
    if not reason:
        raise SchemaError("amend_locked_plan requires a reason")
    if not approver:
        raise SchemaError("amend_locked_plan requires an approver")
    if new_plan.plan_id != locked.plan_id:
        raise SchemaError(
            "amend_locked_plan cannot change plan identity "
            f"(locked={locked.plan_id!r}, new={new_plan.plan_id!r}) — a new plan_id "
            "is a new plan, not an amendment"
        )
    # R2 codex P2-7 + s1-fix codex/gemini P1: do not smuggle an *oracle* change into a
    # *plan* amendment. A PlanAmendment binds only the plan hash, so any oracle delta
    # here would be unattested (INV-9). S0 has no ``oracle_amended`` event yet, so a
    # standalone/joint oracle change is out of S1 scope — the ``oracle`` arg may only
    # re-supply the SAME oracle that was already locked (to carry its hash forward),
    # never a different one, AND never *introduce* one into a plan that was locked
    # without an oracle. The original guard only fired when ``locked.oracle_hash`` was
    # already set, so introducing an oracle into an oracle-less lock slipped through
    # and got silently written below (s1-fix gemini P2 / INV-9 rigor).
    if oracle is not None and (
        locked.oracle_hash is None or oracle_hash(oracle) != locked.oracle_hash
    ):
        if locked.oracle_hash is None:
            raise SchemaError(
                "amend_locked_plan cannot introduce an oracle into a plan locked "
                "without one — a plan amendment binds only the plan hash, so a "
                "newly-supplied oracle would be unattested (INV-9). S0 has no "
                "`oracle_amended` event; lock the oracle at approve time, or raise an "
                "oracle amendment separately once that event is frozen."
            )
        raise SchemaError(
            "amend_locked_plan: the supplied oracle differs from the locked oracle "
            "— a plan amendment cannot also change the oracle (S0 has no "
            "`oracle_amended` event; out of S1 scope). Re-lock the same oracle, or "
            "raise an oracle amendment separately once that event is frozen."
        )
    new_hash = plan_hash(new_plan)
    if new_hash == locked.plan_hash:
        raise SchemaError(
            "amend_locked_plan is a no-op: the new plan is identical to the locked "
            "one (refusing to forge an amendment trail for an unchanged plan)"
        )
    diff = "".join(
        difflib.unified_diff(
            _pretty(locked.plan),
            _pretty(new_plan),
            fromfile=f"plan@{locked.plan_hash[:12]}",
            tofile=f"plan@{new_hash[:12]}",
        )
    )
    amendment = PlanAmendment(
        plan_id=locked.plan_id,
        diff=diff,
        reason=reason,
        approver=approver,
        bound_hash=new_hash,
    )
    new_locked = LockedPlan(
        plan=new_plan,
        plan_hash=new_hash,
        approver=approver,
        approved_at=approved_at,
        # The oracle is unchanged by an amendment: the guard above guarantees a
        # supplied ``oracle`` matches ``locked.oracle_hash`` (and an omitted one
        # carries forward), so the locked hash is always the right value — written
        # explicitly here so an amendment can never alter the oracle binding.
        oracle_hash=locked.oracle_hash,
    )
    return new_locked, amendment


def verify_lock(locked: LockedPlan, plan: Plan, *, oracle: Oracle | None = None) -> None:
    """Fail closed if ``plan`` no longer matches what the owner approved.

    This is the lock-hash enforcement (design §13): a worker that silently edits
    ``plan.json`` after approval is caught because the on-disk plan no longer hashes
    to ``locked.plan_hash``. If the lock bound an oracle, the oracle must be
    supplied and must match too.
    """
    actual = plan_hash(plan)
    if actual != locked.plan_hash:
        raise SchemaError(
            f"plan drifted from its approved lock (locked={locked.plan_hash}, actual={actual})"
        )
    if locked.oracle_hash is not None:
        if oracle is None:
            raise SchemaError("lock binds an oracle but none was supplied to verify against")
        actual_oracle = oracle_hash(oracle)
        if actual_oracle != locked.oracle_hash:
            raise SchemaError(
                "oracle drifted from its approved lock "
                f"(locked={locked.oracle_hash}, actual={actual_oracle})"
            )


def is_lock_valid(locked: LockedPlan, plan: Plan, *, oracle: Oracle | None = None) -> bool:
    """Non-raising :func:`verify_lock` — ``True`` iff the plan (and oracle, if
    bound) still match the approved lock."""
    try:
        verify_lock(locked, plan, oracle=oracle)
    except SchemaError:
        return False
    return True
