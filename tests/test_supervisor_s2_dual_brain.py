"""S2 dual-brain retry-runner tests (design §7 ≥3 重试 / §3 C7).

Covers the best-of-N retry semantics reused from the group dual-brain runner:
stop early on a full-strength clean run, retry on failure/degradation up to the
ceiling, keep the highest-ranked run seen, stamp the total tries used, and the
injectable backoff sleep.

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s2_dual_brain.py
"""

from __future__ import annotations

import pytest

from handoff_fanout import supervisor as sup
from handoff_fanout.supervisor.verdict import ProviderStatus
from handoff_fanout.supervisor.verifier_core import RawBrainOutcome

OK = ProviderStatus.OK
DEGRADED = ProviderStatus.DEGRADED
UNAVAILABLE = ProviderStatus.UNAVAILABLE

_BINDING = sup.Binding(target=sup.BindingTarget.STAGED_DIFF_HASH, value="sha256:x")


def _pr(status, provider="codex"):
    return sup.ProviderRun(provider=provider, status=status, findings={"original_findings": []})


class _ScriptedProvider(sup.AuditProvider):
    """Returns a scripted sequence of runs; repeats the last once exhausted."""

    def __init__(self, provider, statuses):
        self._provider = provider
        self._statuses = list(statuses)
        self.calls = 0

    @property
    def provider(self):
        return self._provider

    def run(self, binding):
        status = self._statuses[min(self.calls, len(self._statuses) - 1)]
        self.calls += 1
        return _pr(status, self._provider)


def test_clean_first_try_stops_early():
    p = _ScriptedProvider("codex", [OK, OK, OK])
    run = sup.run_with_retry(p, _BINDING, attempts=3)
    assert run.status is OK
    assert run.attempts == 1
    assert p.calls == 1  # no wasted retries on a good read


def test_retry_until_clean():
    p = _ScriptedProvider("codex", [UNAVAILABLE, UNAVAILABLE, OK])
    run = sup.run_with_retry(p, _BINDING, attempts=3)
    assert run.status is OK
    assert run.attempts == 3
    assert p.calls == 3


def test_all_fail_returns_best_with_total_tries():
    p = _ScriptedProvider("codex", [UNAVAILABLE, UNAVAILABLE, UNAVAILABLE])
    run = sup.run_with_retry(p, _BINDING, attempts=3)
    assert run.status is UNAVAILABLE
    assert run.attempts == 3  # exhausted the ceiling


def test_degraded_does_not_stop_early_but_is_kept():
    # A degraded run isn't "clean" (keep trying for full strength), but if every
    # try is degraded it is the best result → returned (verdict layer → UNKNOWN).
    p = _ScriptedProvider("gemini", [DEGRADED, DEGRADED, DEGRADED])
    run = sup.run_with_retry(p, _BINDING, attempts=3)
    assert run.status is DEGRADED
    assert run.attempts == 3
    assert p.calls == 3  # tried full ceiling for a non-degraded read


def test_degraded_then_clean_prefers_clean():
    p = _ScriptedProvider("gemini", [DEGRADED, OK, OK])
    run = sup.run_with_retry(p, _BINDING, attempts=3)
    assert run.status is OK
    assert run.attempts == 2  # stopped on the clean one


def test_best_of_keeps_highest_rank():
    # degraded(2) then unavailable(1) twice → best is the degraded run.
    p = _ScriptedProvider("gemini", [DEGRADED, UNAVAILABLE, UNAVAILABLE])
    run = sup.run_with_retry(p, _BINDING, attempts=3)
    assert run.status is DEGRADED
    assert run.attempts == 3


def test_attempts_one_single_try():
    p = _ScriptedProvider("codex", [UNAVAILABLE, OK])
    run = sup.run_with_retry(p, _BINDING, attempts=1)
    assert run.status is UNAVAILABLE  # only one try, no retry
    assert run.attempts == 1
    assert p.calls == 1


def test_default_attempts_is_three():
    assert sup.DEFAULT_ATTEMPTS == 3
    p = _ScriptedProvider("codex", [UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, OK])
    run = sup.run_with_retry(p, _BINDING)  # uses default
    assert run.status is UNAVAILABLE  # default ceiling = 3, never reaches the 4th OK
    assert p.calls == 3


def test_backoff_sleep_between_tries_only():
    slept = []
    p = _ScriptedProvider("codex", [UNAVAILABLE, UNAVAILABLE, UNAVAILABLE])
    sup.run_with_retry(p, _BINDING, attempts=3, backoff=1.5, sleep=slept.append)
    # 3 tries → 2 inter-try sleeps (none after the last).
    assert slept == [1.5, 1.5]


def test_backoff_not_slept_after_clean():
    slept = []
    p = _ScriptedProvider("codex", [OK])
    sup.run_with_retry(p, _BINDING, attempts=3, backoff=1.0, sleep=slept.append)
    assert slept == []


def test_no_sleep_when_backoff_zero():
    slept = []
    p = _ScriptedProvider("codex", [UNAVAILABLE, UNAVAILABLE, UNAVAILABLE])
    sup.run_with_retry(p, _BINDING, attempts=3, backoff=0.0, sleep=slept.append)
    assert slept == []


def test_run_dual_brain_runs_both():
    codex = _ScriptedProvider("codex", [OK])
    gemini = _ScriptedProvider("gemini", [DEGRADED, OK])
    codex_run, gemini_run = sup.run_dual_brain(codex, gemini, _BINDING, attempts=3)
    assert codex_run.provider == "codex" and codex_run.status is OK
    assert gemini_run.provider == "gemini" and gemini_run.status is OK
    assert codex_run.attempts == 1
    assert gemini_run.attempts == 2


def test_run_dual_brain_feeds_verify_findings():
    # End-to-end: retry runner → verifier core → bound verdict.
    codex = _ScriptedProvider("codex", [OK])
    gemini = _ScriptedProvider("gemini", [OK])
    codex_run, gemini_run = sup.run_dual_brain(codex, gemini, _BINDING)
    v = sup.verify_findings(codex_run, gemini_run, binding=_BINDING, findings_ref="ref")
    assert v.verdict is sup.VerdictValue.GREEN
    assert sup.is_bound_to(v, _BINDING)


def test_retry_covers_parse_failure_via_adapter():
    # Codex R2 P1: an invoker that returns a claimed-OK-but-unparseable run first,
    # then a clean run. Because the adapter maps unparseable-OK → PARSE_ERROR (not
    # clean), run_with_retry re-tries and recovers, instead of stopping at attempt 1.
    seq = [
        RawBrainOutcome(ok=True, findings=None),  # transient: ok but no findings
        RawBrainOutcome(ok=True, findings={"original_findings": []}),  # recovered
    ]
    state = {"n": 0}

    def invoke(binding):
        run = seq[min(state["n"], len(seq) - 1)]
        state["n"] += 1
        return run

    provider = sup.codex_adapter(invoke)
    run = sup.run_with_retry(provider, _BINDING, attempts=3)
    assert run.status is OK  # recovered on the retry, not stuck at the parse error
    assert run.attempts == 2


@pytest.mark.parametrize("attempts", [0, -1])
def test_attempts_floor_is_one(attempts):
    # A non-positive ceiling still runs once (never zero tries).
    p = _ScriptedProvider("codex", [UNAVAILABLE])
    run = sup.run_with_retry(p, _BINDING, attempts=attempts)
    assert run.attempts == 1
    assert p.calls == 1
