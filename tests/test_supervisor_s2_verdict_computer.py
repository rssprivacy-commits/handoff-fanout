"""S2 VerdictComputer tests (design §3 C8 / §4.3 / INV-1 / INV-2).

Covers the deterministic GREEN/RED/UNKNOWN decision table, the UNKNOWN-dominates-RED
redline-safe precedence, cross-provider P0/P1 de-duplication, the RED-must-be-auditable
rule, anti-replay binding fields, and producer⇄validator agreement (the computer can
never emit a Verdict S0 would reject).

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s2_verdict_computer.py
"""

from __future__ import annotations

import pytest

from handoff_fanout import supervisor as sup
from handoff_fanout.supervisor import SchemaError
from handoff_fanout.supervisor.verdict import ProviderFindings, ProviderStatus, VerdictValue

OK = ProviderStatus.OK
DEGRADED = ProviderStatus.DEGRADED
UNAVAILABLE = ProviderStatus.UNAVAILABLE
PARSE_ERROR = ProviderStatus.PARSE_ERROR


def _pf(status=OK, p0=0, p1=0, fps=None):
    return ProviderFindings(status=status, p0=p0, p1=p1, fingerprints=list(fps or []))


def _verdict(codex, gemini, *, degraded=False, attempts=1, bound="sha256:diffA"):
    return sup.compute_verdict(
        codex=codex,
        gemini=gemini,
        bound_to=bound,
        findings_ref="ack/n2.run1.findings.json",
        degraded=degraded,
        attempts=attempts,
    )


# --- the decision table ------------------------------------------------------


def test_green_both_clean():
    v = _verdict(_pf(OK), _pf(OK))
    assert v.verdict is VerdictValue.GREEN
    assert v.deduped_fingerprints == []  # nothing drove a green


@pytest.mark.parametrize(
    "codex, gemini",
    [
        (_pf(OK, p0=1, fps=["c-p0"]), _pf(OK)),  # codex P0
        (_pf(OK), _pf(OK, p1=1, fps=["g-p1"])),  # gemini P1
        (_pf(OK, p0=1, fps=["c-p0"]), _pf(OK, p1=2, fps=["g-a", "g-b"])),  # both
    ],
)
def test_red_when_any_p0p1(codex, gemini):
    v = _verdict(codex, gemini)
    assert v.verdict is VerdictValue.RED
    assert v.deduped_fingerprints  # RED is auditable


@pytest.mark.parametrize("status", [DEGRADED, UNAVAILABLE, PARSE_ERROR])
def test_unknown_when_a_provider_not_ok(status):
    # Even with zero findings, a provider that isn't fully OK forces UNKNOWN.
    v = _verdict(_pf(OK), _pf(status))
    assert v.verdict is VerdictValue.UNKNOWN


def test_unknown_when_degraded_flag_even_if_both_ok():
    v = _verdict(_pf(OK), _pf(OK), degraded=True)
    assert v.verdict is VerdictValue.UNKNOWN


def test_unknown_dominates_red_redline_safe():
    # A degraded brain that *found* a P0 must NOT single-brain it to RED — the
    # read is untrusted, so it escalates (UNKNOWN). This is the core redline rule.
    codex = _pf(DEGRADED, p0=1, fps=["c-p0"])
    gemini = _pf(OK, p0=1, fps=["g-p0"])
    v = _verdict(codex, gemini, degraded=True)
    assert v.verdict is VerdictValue.UNKNOWN
    # No deduped list is attributed to an UNKNOWN (findings didn't *drive* it).
    assert v.deduped_fingerprints == []


# --- cross-provider de-duplication (§4.3) ------------------------------------


def test_cross_provider_dedup_union():
    codex = _pf(OK, p0=2, fps=["a", "b"])
    gemini = _pf(OK, p0=2, fps=["b", "c"])  # 'b' is the same finding both saw
    v = _verdict(codex, gemini)
    assert v.verdict is VerdictValue.RED
    assert v.deduped_fingerprints == ["a", "b", "c"]  # sorted, deduped


def test_cross_provider_dedup_identical_collapses_to_one():
    codex = _pf(OK, p0=1, fps=["x"])
    gemini = _pf(OK, p0=1, fps=["x"])  # both brains, same finding
    v = _verdict(codex, gemini)
    assert v.deduped_fingerprints == ["x"]


def test_deduped_is_subset_of_union():
    codex = _pf(OK, p0=1, fps=["a"])
    gemini = _pf(OK, p1=1, fps=["z"])
    v = _verdict(codex, gemini)
    union = set(codex.fingerprints) | set(gemini.fingerprints)
    assert set(v.deduped_fingerprints) <= union


# --- RED must be auditable ---------------------------------------------------


def test_red_without_fingerprints_raises():
    # A P0 with no fingerprint can't make a RED auditable — fail closed rather
    # than emit an un-auditable RED or silently drop to GREEN.
    with pytest.raises(sup.VerdictComputationError):
        _verdict(_pf(OK, p0=1), _pf(OK))


def test_verdict_computation_error_is_schema_error():
    assert issubclass(sup.VerdictComputationError, SchemaError)


# --- carried metadata (anti-replay + provenance) -----------------------------


def test_bound_to_and_rule_recorded():
    v = _verdict(_pf(OK), _pf(OK), bound="sha256:deadbeef", attempts=3)
    assert v.bound_to == "sha256:deadbeef"
    assert v.binding_target is sup.BindingTarget.STAGED_DIFF_HASH
    assert v.by == sup.VERDICT_RULE == "rule:any-p0p1"
    assert v.attempts == 3
    assert v.findings_ref  # required for INV-2 auditability


def test_binding_target_passthrough():
    v = sup.compute_verdict(
        codex=_pf(OK),
        gemini=_pf(OK),
        bound_to="abc123",
        findings_ref="ref",
        binding_target=sup.BindingTarget.HEAD,
    )
    assert v.binding_target is sup.BindingTarget.HEAD


# --- producer ⇄ validator agreement (defense in depth) -----------------------


@pytest.mark.parametrize(
    "codex, gemini, degraded, expected",
    [
        (_pf(OK), _pf(OK), False, VerdictValue.GREEN),
        (_pf(OK, p0=1, fps=["a"]), _pf(OK), False, VerdictValue.RED),
        (_pf(OK), _pf(OK, p1=1, fps=["b"]), False, VerdictValue.RED),
        (_pf(OK), _pf(DEGRADED), True, VerdictValue.UNKNOWN),
        (_pf(UNAVAILABLE), _pf(OK), False, VerdictValue.UNKNOWN),
        (_pf(PARSE_ERROR), _pf(OK), False, VerdictValue.UNKNOWN),
        (_pf(OK), _pf(OK), True, VerdictValue.UNKNOWN),  # degraded flag alone
    ],
)
def test_compute_value_matches_expected(codex, gemini, degraded, expected):
    assert sup.compute_verdict_value(codex, gemini, degraded=degraded) is expected


def test_producer_value_always_accepted_by_s0_validator():
    # For every cell, the value the producer picks is exactly the value S0's
    # Verdict.validate accepts — so compute_verdict never raises a consistency
    # SchemaError, and a *different* value would be rejected.
    cases = [
        (_pf(OK), _pf(OK), False),
        (_pf(OK, p0=1, fps=["a"]), _pf(OK), False),
        (_pf(OK), _pf(DEGRADED), True),
        (_pf(UNAVAILABLE), _pf(OK), False),
    ]
    for codex, gemini, degraded in cases:
        v = _verdict(codex, gemini, degraded=degraded)
        # The S0 validator's own expectation agrees with the stored value.
        assert v._expected_verdict() is v.verdict
        # And the producer agrees with the validator.
        assert sup.compute_verdict_value(codex, gemini, degraded=degraded) is v.verdict


def test_deduped_fingerprints_helper_sorted_unique():
    a = _pf(OK, p0=1, fps=["m", "a"])
    b = _pf(OK, p0=1, fps=["a", "z"])
    assert sup.deduped_fingerprints(a, b) == ["a", "m", "z"]
