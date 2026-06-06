"""S2 — VerdictComputer (C8 / design §3 C8 + §4.3 + INV-1 + INV-2).

The **producer** half of the verdict: a pure, deterministic function that turns
two providers' raw :class:`~handoff_fanout.supervisor.verdict.ProviderFindings`
into a fully-formed S0 :class:`~handoff_fanout.supervisor.verdict.Verdict`. S0's
``verdict.py`` froze *what a valid Verdict looks like* (the consistency rule that
makes a false-GREEN impossible); this module is *how to derive one*.

The two halves are deliberately separate and double-check each other:

* :func:`compute_verdict_value` re-encodes the INV-2 rule as the **producer**.
* :meth:`Verdict.validate` (S0) re-checks it as the **validator** on construction.

A bug in the producer can therefore never emit an inconsistent Verdict — S0
rejects it. Tests assert the two always agree (defense in depth).

INV-1 (control plane zero-LLM): this is the verdict authority and it is a plain
``if/else`` over integer finding counts — no model, no judgement. ``Verdict.by`` is
pinned to the single deterministic rule id (``rule:any-p0p1``).

INV-2 (verdict only reads raw findings): the only inputs are the two providers'
raw findings + a degraded flag. The precedence is the redline-safe default S0
fixed: **degraded/any-provider-not-OK ⇒ UNKNOWN dominates any-P0/P1 ⇒ RED
dominates clean ⇒ GREEN**. A degraded read never single-brains a redline to GREEN
— it escalates (UNKNOWN).
"""

from __future__ import annotations

from ._base import SchemaError
from .verdict import (
    KNOWN_VERDICT_RULES,
    BindingTarget,
    ProviderFindings,
    ProviderStatus,
    Verdict,
    VerdictValue,
)

#: The single deterministic rule this computer applies (INV-1). Kept here as the
#: producer's identity; it must be a member of S0's :data:`KNOWN_VERDICT_RULES`
#: (asserted at import time below) so the producer can never name a rule the
#: validator would reject.
VERDICT_RULE = "rule:any-p0p1"
assert VERDICT_RULE in KNOWN_VERDICT_RULES  # producer/validator rule agreement


class VerdictComputationError(SchemaError):
    """A verdict cannot be derived consistently from the given raw findings.

    Raised (fail-closed) rather than emitting a wrong Verdict — e.g. RED is
    required but neither provider supplied an auditable fingerprint. A
    :class:`SchemaError` subclass so the supervisor's quarantine path catches it
    the same as a malformed contract.
    """


def compute_verdict_value(
    codex: ProviderFindings, gemini: ProviderFindings, *, degraded: bool
) -> VerdictValue:
    """The one verdict consistent with the raw findings (the frozen INV-2 rule).

    Mirrors :meth:`Verdict._expected_verdict` exactly — this is the **producer**
    of the value S0 validates. UNKNOWN dominates RED dominates GREEN:

    * any provider not OK, or ``degraded`` ⇒ UNKNOWN (絕不单脑放行红线 → escalate);
    * else any P0/P1 ⇒ RED;
    * else GREEN.
    """
    providers = (codex, gemini)
    if degraded or any(p.status is not ProviderStatus.OK for p in providers):
        return VerdictValue.UNKNOWN
    if any(p.p0 > 0 or p.p1 > 0 for p in providers):
        return VerdictValue.RED
    return VerdictValue.GREEN


def deduped_fingerprints(codex: ProviderFindings, gemini: ProviderFindings) -> list[str]:
    """The cross-provider de-duplicated union of the two providers' fingerprints
    (design §4.3 "P0/P1 跨 provider 去重").

    The same finding surfaced by *both* brains (identical fingerprint) collapses
    to one. Sorted for determinism (INV-1 / reproducible control plane). This is
    what makes a RED auditable: every entry traces to a raw finding one or both
    providers reported.
    """
    return sorted(set(codex.fingerprints) | set(gemini.fingerprints))


def compute_verdict(
    *,
    codex: ProviderFindings,
    gemini: ProviderFindings,
    bound_to: str,
    findings_ref: str,
    binding_target: BindingTarget = BindingTarget.STAGED_DIFF_HASH,
    degraded: bool = False,
    attempts: int = 1,
) -> Verdict:
    """Derive the S0 :class:`Verdict` for two providers' raw findings (C8).

    Deterministic and side-effect-free. The returned Verdict is S0-valid by
    construction: :func:`compute_verdict_value` picks the value, the cross-provider
    dedup populates ``deduped_fingerprints`` for a RED (required for auditability),
    and S0's :meth:`Verdict.validate` re-checks consistency on construction.

    ``bound_to`` / ``binding_target`` tie the verdict to one exact code state
    (anti-replay, INV-4); ``findings_ref`` points at where the raw findings are
    persisted (INV-2 auditability). Both are required by S0.

    Raises :class:`VerdictComputationError` when the value is RED but no provider
    supplied a fingerprint — a blocking finding that cannot be made auditable must
    not be silently dropped to GREEN nor emitted as an un-auditable RED; the
    adapter is expected to always fingerprint blocking findings (see
    :func:`~handoff_fanout.supervisor.verifier_core.parse_provider_findings`).
    """
    value = compute_verdict_value(codex, gemini, degraded=degraded)
    # Only a RED needs (and is allowed to require) the deduped finding ids: GREEN
    # and UNKNOWN are not *driven* by findings, so S0 keeps their list empty.
    deduped = deduped_fingerprints(codex, gemini) if value is VerdictValue.RED else []
    if value is VerdictValue.RED and not deduped:
        raise VerdictComputationError(
            "RED verdict requires auditable fingerprints, but neither provider "
            "supplied any. A blocking P0/P1 finding must carry a fingerprint so "
            "the cross-provider dedup is auditable (§4.3 / INV-2) — the adapter "
            "must fingerprint every blocking finding (positional fallback if it "
            "lacks a stable identity)."
        )
    return Verdict(
        verdict=value,
        by=VERDICT_RULE,
        codex=codex,
        gemini=gemini,
        bound_to=bound_to,
        findings_ref=findings_ref,
        binding_target=binding_target,
        degraded=degraded,
        attempts=attempts,
        deduped_fingerprints=deduped,
    )
