"""S0 — Verdict contract (design §4.3 / INV-2).

The Verdict is the machine-computed (never LLM-judged) GREEN/RED/UNKNOWN signal
the supervisor reads from raw external-brain findings. This module freezes the
*shape* of a Verdict and the *consistency rule* that makes a false-GREEN
impossible. It does NOT compute verdicts from findings — that is the
VerdictComputer (slice S2). The boundary: S0 defines "what a valid Verdict looks
like"; S2 defines "how to derive one." By enforcing consistency here, S2 (or any
buggy/adversarial producer) can never emit a Verdict that violates INV-2.

INV-2 — verdict 只认 raw 外脑 findings:
  * GREEN is legal **only** when BOTH providers ran clean (status OK), there are
    zero P0 and zero P1 findings, and the read is not degraded.
  * Any provider not OK (unavailable / parse error / degraded model) OR the
    top-level ``degraded`` flag ⇒ UNKNOWN (绝不单脑放行红线 → escalate).
  * Otherwise (both OK, but some P0/P1) ⇒ RED.

This precedence (degraded ⇒ UNKNOWN dominates P0/P1 ⇒ RED) is the redline-safe
default the design fixes. Relaxing it (e.g. risk-tier-gated tolerance of a
degraded model) is a deliberate contract amendment, not an ad-hoc S2 choice.
"""

from __future__ import annotations

import dataclasses
import enum

from ._base import Contract, SchemaError

#: The verdict rule must be a deterministic ``rule:*`` id — never an LLM judgement
#: (INV-1 control plane is zero-LLM).
RULE_PREFIX = "rule:"

#: The closed allow-list of deterministic verdict rules (INV-1). ``Verdict.by``
#: must be one of these — a bare ``rule:`` prefix is not enough (``rule:ask-llm``
#: would slip a model into the control plane). A new deterministic rule is added
#: here as a contract amendment, never ad-hoc (R2 codex C-P2-2).
KNOWN_VERDICT_RULES = frozenset({"rule:any-p0p1"})


class VerdictValue(enum.StrEnum):
    GREEN = "GREEN"
    RED = "RED"
    UNKNOWN = "UNKNOWN"


class ProviderStatus(enum.StrEnum):
    """Outcome of one external brain (codex / gemini) for this verdict.

    Only :attr:`OK` contributes a *trustworthy* read. Every other status forces
    the verdict to UNKNOWN (no single-brain pass on a redline).
    """

    OK = "ok"
    DEGRADED = "degraded"  # ran on a fallback/weaker model — read not fully trusted
    UNAVAILABLE = "unavailable"  # down / rate-limited / never produced findings
    PARSE_ERROR = "parse_error"  # produced output but raw findings unparseable


class BindingTarget(enum.StrEnum):
    """What :attr:`Verdict.bound_to` hashes — anti-replay binding (design §7)."""

    HEAD = "head"
    STAGED_DIFF_HASH = "staged_diff_hash"
    TREE_OID = "tree_oid"


@dataclasses.dataclass
class ProviderFindings(Contract):
    """Raw finding counts from one external brain. P0/P1 are the only severities
    that drive the verdict (design §4.3 ``codex{status,p0,p1}``)."""

    status: ProviderStatus
    p0: int = 0
    p1: int = 0

    def validate(self) -> None:
        if self.p0 < 0 or self.p1 < 0:
            raise SchemaError("ProviderFindings.p0/p1 must be >= 0")


@dataclasses.dataclass
class Verdict(Contract):
    """Machine verdict over a node's diff (design §4.3).

    The wire field is ``verdict`` (matching §4.3's ``{verdict: GREEN|RED|...}``),
    NOT ``value`` — R2 codex C-P1-2 (wire-name drift). ``bound_to`` ties this
    verdict to one exact code state (``binding_target`` says which hash kind) so a
    stale verdict can never be replayed against a different diff (INV-4
    anti-replay). ``findings_ref`` is required: a verdict the supervisor cannot
    trace back to the raw external-brain findings is, per INV-2, not auditable and
    therefore malformed (R2 codex C-P1-3).
    """

    verdict: VerdictValue
    by: str
    codex: ProviderFindings
    gemini: ProviderFindings
    bound_to: str
    findings_ref: str
    binding_target: BindingTarget = BindingTarget.STAGED_DIFF_HASH
    degraded: bool = False
    attempts: int = 1

    def _expected_verdict(self) -> VerdictValue:
        """The only verdict consistent with the findings (the frozen INV-2 rule)."""
        providers = (self.codex, self.gemini)
        if self.degraded or any(p.status is not ProviderStatus.OK for p in providers):
            return VerdictValue.UNKNOWN
        if any(p.p0 > 0 or p.p1 > 0 for p in providers):
            return VerdictValue.RED
        return VerdictValue.GREEN

    def validate(self) -> None:
        if self.by not in KNOWN_VERDICT_RULES:
            raise SchemaError(
                f"Verdict.by must be one of the deterministic rules "
                f"{sorted(KNOWN_VERDICT_RULES)} (INV-1 zero-LLM control plane), "
                f"got {self.by!r}"
            )
        if not self.bound_to:
            raise SchemaError("Verdict.bound_to required (INV-4 anti-replay)")
        if not self.findings_ref:
            raise SchemaError(
                "Verdict.findings_ref required (INV-2: a verdict must trace to raw "
                "external-brain findings)"
            )
        if self.attempts < 1:
            raise SchemaError("Verdict.attempts must be >= 1")
        expected = self._expected_verdict()
        if self.verdict is not expected:
            raise SchemaError(
                f"Verdict.verdict={self.verdict.value} is inconsistent with findings "
                f"(INV-2 requires {expected.value}): "
                f"degraded={self.degraded}, "
                f"codex={self.codex.status.value}/p0={self.codex.p0}/p1={self.codex.p1}, "
                f"gemini={self.gemini.status.value}/p0={self.gemini.p0}/p1={self.gemini.p1}"
            )
