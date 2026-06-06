"""S2 — verifier core + provider adapters (design §3 C7/C8 + §7 + §12 S2).

The **single verifier** the supervisor trusts: it reads *raw external-brain
findings only* and derives a bound :class:`~handoff_fanout.supervisor.verdict.Verdict`
deterministically. It is provider-agnostic (codex / gemini go through symmetric
adapters that normalize to the frozen S0 ``ProviderFindings`` contract) and
binding-agnostic (a :class:`Binding` can be a ``head`` / ``staged_diff_hash`` /
``tree_oid`` — design §7 ``BindingTarget``). There is deliberately **no parameter
to pass in a pre-computed verdict** — the only authority is :func:`verify_findings`
over raw findings (INV-2: 绝不信 CC 自报 verdict / 单一 verifier 唯一权威).

What it abstracts out of the live ``codex_audit`` G0-G9 gate (the prompt's "抽
verifier core"):

* **read-only raw findings → ProviderFindings** (:func:`parse_provider_findings`),
  reusing ``codex_audit``'s *public* finding-identity helpers (``finding_identity``
  / ``compute_finding_hash`` / ``has_finding_identity``) so "what is a P0" and
  "what makes two findings the same" never drift between the live gate and the
  supervisor. The live ``codex_audit`` runtime is **not modified** — this only
  imports its pure helpers (S2 红线: 只增隔离模块, import/wrap 不就地重构).
* **binding to one exact code state** (:class:`Binding` + :func:`resolve_binding`),
  the anti-replay / anti-TOCTOU mechanism the verdict carries (``bound_to``).

Out of scope for S2 (later slices): persisting the raw findings + verifying their
on-disk hash/manifest (event-log / S3), the dispatcher that *spawns* the audit
subagent (S4). The real brain invocation is an injected port (:data:`BrainInvoker`)
so this module stays pure-deterministic and testable; production wiring lands when
the dispatcher does (S3+).
"""

from __future__ import annotations

import abc
import dataclasses
import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path

from .. import codex_audit
from ..handoff_precheck import AUDIT_SEVERITIES
from ._base import SchemaError
from .verdict import BindingTarget, ProviderFindings, ProviderStatus, Verdict
from .verdict_computer import compute_verdict

_GIT_TIMEOUT_S = 30


# --- binding (anti-replay: tie a verdict to one exact code state) -------------


class BindingError(RuntimeError):
    """A binding could not be resolved from the workspace (infra failure, e.g. git
    not a repo / no HEAD). Distinct from a schema problem: the caller treats it as
    a could-not-evaluate (escalate), never a defect."""


@dataclasses.dataclass(frozen=True)
class Binding:
    """One exact code state a :class:`Verdict` is bound to (design §7).

    ``target`` says which hash kind ``value`` is — the supervisor compares a
    verdict's ``binding_target`` + ``bound_to`` against the *current* binding
    before acting on it, so a stale verdict can never be replayed against a
    different diff (INV-4 anti-replay)."""

    target: BindingTarget
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise SchemaError(
                "Binding.value required — an anti-replay binding needs a non-empty code-state hash"
            )


def _git_bytes(args: list[str], cwd: Path) -> bytes:
    try:
        proc = subprocess.run(  # fixed argv, no shell
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BindingError(f"git {' '.join(args)} could not run in {cwd}: {exc}") from exc
    if proc.returncode != 0:
        raise BindingError(
            f"git {' '.join(args)} exited {proc.returncode} in {cwd}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def _git(args: list[str], cwd: Path) -> str:
    return _git_bytes(args, cwd).decode("utf-8", "replace").strip()


def resolve_binding(workspace: str | Path, target: BindingTarget) -> Binding:
    """Resolve the current :class:`Binding` of ``target`` kind from a git workspace.

    * ``HEAD`` → ``git rev-parse HEAD`` (the committed tip).
    * ``STAGED_DIFF_HASH`` → ``sha256:`` over the index **tree oid** (``git
      write-tree`` — git's content-exact Merkle id of the staged state) folded with
      the ``--binary`` staged diff. The tree oid is the tamper-proof anchor: a plain
      text diff is *lossy* (a binary file or a ``.gitattributes -diff`` path renders
      an identical ``Binary files … differ`` line for **different** content, which
      would collide the hash and let a stale verdict replay onto changed bytes —
      anti-replay hole, INV-4 / Gemini R2 P0). The diff is folded in so the value
      still moves with, and is traceable to, the staged change; ``--no-ext-diff``
      keeps an external diff driver from shelling out.
    * ``TREE_OID`` → ``git write-tree`` (git's own content id of the *index*), the
      git-native equivalent that also reflects staged state.

    Raises :class:`BindingError` (infra, not a defect) when git can't answer — e.g.
    not a repo, or no commit yet for ``HEAD``.
    """
    ws = Path(workspace)
    if target is BindingTarget.HEAD:
        value = _git(["rev-parse", "HEAD"], ws)
    elif target is BindingTarget.STAGED_DIFF_HASH:
        tree = _git(["write-tree"], ws)  # content-exact Merkle anchor (binary-safe)
        diff = _git_bytes(["diff", "--cached", "--no-color", "--no-ext-diff", "--binary"], ws)
        value = "sha256:" + hashlib.sha256(tree.encode("utf-8") + b"\0" + diff).hexdigest()
    elif target is BindingTarget.TREE_OID:
        value = _git(["write-tree"], ws)
    else:  # pragma: no cover — BindingTarget is a closed enum
        raise BindingError(f"unsupported BindingTarget {target!r}")
    if not value:
        raise BindingError(f"git produced an empty {target.value} binding in {ws}")
    return Binding(target=target, value=value)


def is_bound_to(verdict: Verdict, binding: Binding) -> bool:
    """True iff ``verdict`` was computed against exactly this code state.

    The anti-replay check the supervisor runs before acting on a verdict: a
    verdict bound to a different diff/commit (or a different binding *kind*) does
    NOT match, so it can't be replayed against the wrong state (INV-4)."""
    return verdict.binding_target is binding.target and verdict.bound_to == binding.value


# --- provider runs + adapters (codex / gemini, symmetric) --------------------


@dataclasses.dataclass(frozen=True)
class RawBrainOutcome:
    """The native outcome of invoking one external brain, before normalization.

    Immutable. ``ok`` is the run-completed-successfully signal; ``degraded`` marks
    a fallback/weaker model (gemini → flash; codex has no model-degradation so its
    invoker never sets this). ``findings`` is the normalized findings dict
    (``{"original_findings": [...]}``) the invoker extracted from the brain's raw
    output (the provider-specific text→JSON extraction lives in the *invoker*, kept
    out of the symmetric adapter)."""

    ok: bool
    findings: dict | None = None
    model: str = ""
    degraded: bool = False
    timed_out: bool = False
    detail: str = ""


@dataclasses.dataclass(frozen=True)
class ProviderRun:
    """One external brain's run, normalized to an S0 :class:`ProviderStatus`.

    Not a wire :class:`~handoff_fanout.supervisor._base.Contract` — it is a transient
    runtime value (like S1's ``RawExecution``), consumed by
    :func:`parse_provider_findings` and the verdict. ``attempts`` records how many
    tries the retry runner used to obtain this run (design §7 ≥3 重试)."""

    provider: str
    status: ProviderStatus
    findings: dict | None = None
    model: str = ""
    detail: str = ""
    attempts: int = 1


#: A side-effecting port: spawn one brain over a :class:`Binding`, return its raw
#: outcome. Injected so this module stays pure/testable; production wiring (spawn
#: the audit subagent, read codex-findings.json / normalize gemini text) lands with
#: the dispatcher (S3+).
BrainInvoker = Callable[["Binding"], RawBrainOutcome]


def _findings_parseable(findings: dict | None) -> bool:
    """True iff ``findings`` carries an ``original_findings`` list the verifier can
    actually read. A claimed-OK run that fails this is unusable raw, so the adapter
    marks it PARSE_ERROR **before** the retry layer (Codex R2 P1) — otherwise a
    transient empty/garbled read (``findings=None`` / non-list) would look "clean"
    to :func:`~handoff_fanout.supervisor.dual_brain.run_with_retry` (which only sees
    ``status``), stop at attempt 1, and only become UNKNOWN at verdict time — never
    retried, when a retry might have recovered a usable read."""
    return isinstance(findings, dict) and isinstance(findings.get("original_findings"), list)


def _map_status(outcome: RawBrainOutcome) -> ProviderStatus:
    """Native outcome → S0 :class:`ProviderStatus` (the adapter's whole job).

    A failed/timed-out run is UNAVAILABLE. A claimed-success run with no parseable
    findings is PARSE_ERROR (decided *here*, so the retry layer sees it and re-tries
    — Codex R2 P1). A fallback/weaker model is DEGRADED (gemini only, in practice);
    a clean parseable run is OK. ``parse_provider_findings`` keeps an equivalent
    defensive check for hand-built ``ProviderRun``\\ s that bypass an adapter."""
    if not outcome.ok:
        return ProviderStatus.UNAVAILABLE
    if not _findings_parseable(outcome.findings):
        return ProviderStatus.PARSE_ERROR
    if outcome.degraded:
        return ProviderStatus.DEGRADED
    return ProviderStatus.OK


class AuditProvider(abc.ABC):
    """Port: run an audit of one binding with one external brain → a ProviderRun."""

    @property
    @abc.abstractmethod
    def provider(self) -> str: ...

    @abc.abstractmethod
    def run(self, binding: Binding) -> ProviderRun: ...


class AuditAdapter(AuditProvider):
    """The symmetric codex/gemini adapter: invoke a brain, map its native outcome
    to a normalized :class:`ProviderRun`.

    codex and gemini differ only in (a) the provider name and (b) whether the
    invoker ever reports ``degraded`` (gemini can fall back to flash; codex can't)
    — so one adapter parameterized by name + invoker is the honest model of "two
    symmetric adapters". The provider-specific raw→normalized-findings extraction
    is the *invoker*'s responsibility, not the adapter's."""

    def __init__(self, provider: str, invoke: BrainInvoker) -> None:
        if not provider:
            raise SchemaError("AuditAdapter.provider must be a non-empty name")
        self._provider = provider
        self._invoke = invoke

    @property
    def provider(self) -> str:
        return self._provider

    def run(self, binding: Binding) -> ProviderRun:
        outcome = self._invoke(binding)
        return ProviderRun(
            provider=self._provider,
            status=_map_status(outcome),
            findings=outcome.findings,
            model=outcome.model,
            detail=outcome.detail,
        )


def codex_adapter(invoke: BrainInvoker) -> AuditAdapter:
    """The codex adapter (OpenAI). Its invoker never reports a degraded model."""
    return AuditAdapter("codex", invoke)


def gemini_adapter(invoke: BrainInvoker) -> AuditAdapter:
    """The gemini adapter (Google), symmetric to :func:`codex_adapter`. Its invoker
    may report ``degraded`` (fell back to flash on quota) → ProviderStatus.DEGRADED
    → UNKNOWN verdict (绝不单脑放行红线)."""
    return AuditAdapter("gemini", invoke)


def _blocking_fingerprint(provider: str, finding: dict, idx: int) -> str:
    """A distinct, auditable fingerprint for one blocking finding.

    Reuses ``codex_audit.compute_finding_hash`` (the live gate's identity) when the
    finding has a stable identity, so the *same* P0 found by both brains dedups
    across providers. A blocking finding with NO stable identity gets a
    provider-positional fallback (``noident:<provider>:<idx>``) instead — it must
    still be counted and made auditable (never silently dropped to GREEN), and the
    fallback is distinct within a provider (``compute_finding_hash`` would collide
    every identity-less finding onto one hash, tripping ProviderFindings' no-dup
    rule) and provider-prefixed so two identity-less findings from different brains
    never *falsely* dedup."""
    if codex_audit.has_finding_identity(finding):
        return codex_audit.compute_finding_hash(finding)
    return f"noident:{provider}:{idx}"


def parse_provider_findings(run: ProviderRun) -> ProviderFindings:
    """Normalize a :class:`ProviderRun` to the frozen S0 :class:`ProviderFindings`
    (read-only over the raw findings; INV-2).

    * UNAVAILABLE / PARSE_ERROR runs carry no trustworthy counts → status only.
    * An OK/DEGRADED run whose ``findings`` has no ``original_findings`` list is
      **downgraded to PARSE_ERROR** (fail-closed: an unparseable "OK" run must not
      read as a clean zero-finding pass).
    * Otherwise count P0/P1 (an unrecognized/spoofed severity is fail-closed as a
      blocking P0, mirroring ``codex_audit.derive_verdict``) and fingerprint every
      blocking finding so a resulting RED is auditable. The **same** blocking
      finding repeated within one provider's output is counted once (de-duplicated
      by fingerprint) — otherwise ``ProviderFindings``' no-duplicate-fingerprint
      rule would reject the result, and p0/p1 would double-count one issue.
    """
    status = run.status
    if status in (ProviderStatus.UNAVAILABLE, ProviderStatus.PARSE_ERROR):
        return ProviderFindings(status=status)

    raw = run.findings
    original = raw.get("original_findings") if isinstance(raw, dict) else None
    if not isinstance(original, list):
        return ProviderFindings(status=ProviderStatus.PARSE_ERROR)

    p0 = 0
    p1 = 0
    fingerprints: list[str] = []
    seen: set[str] = set()
    for idx, finding in enumerate(original):
        if not isinstance(finding, dict):
            continue
        sev = codex_audit.finding_identity(finding).get("severity", "")
        if sev == "P1":
            is_p0 = False
        elif sev == "P0" or (sev and sev not in AUDIT_SEVERITIES):
            # P0, or a typo'd / spoofed severity → fail-closed as a blocking P0
            # (codex R8-2): an unrecognized non-empty severity must never read as
            # clean. P2/P3/empty fall through as non-blocking.
            is_p0 = True
        else:
            continue
        fp = _blocking_fingerprint(run.provider, finding, idx)
        if fp in seen:
            # Same blocking finding restated within this provider's output → count
            # it once (a genuinely distinct finding has a distinct identity, and an
            # identity-less one gets a distinct positional fallback, so this only
            # collapses true repeats).
            continue
        seen.add(fp)
        fingerprints.append(fp)
        if is_p0:
            p0 += 1
        else:
            p1 += 1
    return ProviderFindings(status=status, p0=p0, p1=p1, fingerprints=fingerprints)


def verify_findings(
    codex_run: ProviderRun,
    gemini_run: ProviderRun,
    *,
    binding: Binding,
    findings_ref: str,
    attempts: int | None = None,
) -> Verdict:
    """The single verifier (INV-2 / 单一 verifier 唯一权威): derive a bound
    :class:`Verdict` from two providers' *raw* runs.

    Read-only over raw findings — there is no way to hand it a pre-computed verdict.
    ``degraded`` is set iff either provider degraded (a weaker model ran), which —
    like any non-OK status — forces UNKNOWN (escalate, never single-brain GREEN on a
    redline). ``attempts`` defaults to the max of the two runs' retry counts.
    """
    codex_pf = parse_provider_findings(codex_run)
    gemini_pf = parse_provider_findings(gemini_run)
    degraded = any(pf.status is ProviderStatus.DEGRADED for pf in (codex_pf, gemini_pf))
    eff_attempts = (
        attempts if attempts is not None else max(codex_run.attempts, gemini_run.attempts)
    )
    return compute_verdict(
        codex=codex_pf,
        gemini=gemini_pf,
        bound_to=binding.value,
        binding_target=binding.target,
        findings_ref=findings_ref,
        degraded=degraded,
        attempts=eff_attempts,
    )
