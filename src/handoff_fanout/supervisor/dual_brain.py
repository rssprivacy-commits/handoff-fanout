"""S2 — dual-brain retry runner (design §7 ≥3 重试 / §3 C7).

Reuses the *semantics* of the group's ``~/.claude/scripts/dual-brain-runner.py``
``run_with_retry`` — re-run a brain on failure/degradation up to N times, stop
early on a full-strength clean run, otherwise keep the best result seen — but
provider-agnostic over the S2 :class:`AuditProvider` port so the supervisor can
drive it deterministically (a fake provider in tests, the real spawn-the-subagent
invoker in production / S3+).

Why ≥3 (主人 2026-06-05 立法): a single transient failure or a quota fallback to a
weaker model must not be accepted as the read — try again first. Only after the
retries are exhausted do we accept a degraded/failed run, and the verdict layer
turns that into UNKNOWN (绝不单脑放行红线 → escalate), never a single-brain GREEN.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable

from .verdict import ProviderStatus
from .verifier_core import AuditProvider, Binding, ProviderRun

#: Default retry ceiling — the ≥3 the owner legislated (try ≥3 before accepting a
#: degraded/failed read).
DEFAULT_ATTEMPTS = 3


def _rank(run: ProviderRun | None) -> int:
    """Result quality for "keep the best of N": full-strength OK (3) > degraded
    OK (2) > failed/unavailable/parse-error (1) > none (-1). Mirrors the group
    runner's ``_rank``."""
    if run is None:
        return -1
    if run.status is ProviderStatus.OK:
        return 3
    if run.status is ProviderStatus.DEGRADED:
        return 2
    return 1


def _is_clean(run: ProviderRun) -> bool:
    """Clean = succeeded on a full-strength model. Only a clean run stops the
    retry loop early (a degraded run keeps trying for a non-degraded one)."""
    return run.status is ProviderStatus.OK


def run_with_retry(
    provider: AuditProvider,
    binding: Binding,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> ProviderRun:
    """Run ``provider`` over ``binding`` up to ``attempts`` times; return the best
    run seen, stamped with the number of tries actually used.

    Stops early on the first full-strength clean run (no point retrying a good
    read). ``backoff`` seconds between tries (0 = none); ``sleep`` is injectable so
    tests don't actually wait. The returned run's ``attempts`` field is the total
    tries used (1 when the first run was clean), so the verdict can record it
    (design §4.3 ``attempts``)."""
    best: ProviderRun | None = None
    tries = 0
    ceiling = max(1, attempts)
    for i in range(ceiling):
        tries += 1
        run = provider.run(binding)
        if _rank(run) > _rank(best):
            best = run
        if _is_clean(run):
            break
        if i < ceiling - 1 and backoff > 0:
            sleep(backoff)
    assert best is not None  # ceiling >= 1 guarantees at least one run
    return dataclasses.replace(best, attempts=tries)


def run_dual_brain(
    codex: AuditProvider,
    gemini: AuditProvider,
    binding: Binding,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[ProviderRun, ProviderRun]:
    """Run both brains independently with :func:`run_with_retry` and return their
    runs ``(codex_run, gemini_run)``.

    The two reads are logically independent (each审各的 / 铁律: brains never see each
    other's answer) and kept sequential for a deterministic control plane (INV-1).
    NOTE (Codex R2 P2): with a real invoker that shares state across brains (a common
    quota / rate-limit / temp dir / env), the fixed codex-then-gemini order *can*
    influence which brain degrades first — so the order is **not** claimed to be
    result-neutral for stateful invokers. That is acceptable: any non-OK/DEGRADED
    read still resolves to UNKNOWN (never a single-brain GREEN). A production wiring
    may parallelize the two I/O-bound calls (isolated env per brain) instead."""
    codex_run = run_with_retry(codex, binding, attempts=attempts, backoff=backoff, sleep=sleep)
    gemini_run = run_with_retry(gemini, binding, attempts=attempts, backoff=backoff, sleep=sleep)
    return codex_run, gemini_run
