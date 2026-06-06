"""S2 verifier-core tests (design §3 C7/C8 / §7 / §12 S2).

Covers binding resolution against a real throwaway git repo for all three
``BindingTarget`` kinds (head / staged_diff_hash / tree_oid), the anti-replay
``is_bound_to`` check, the read-only raw → ``ProviderFindings`` parser (reusing the
live codex_audit identity helpers), the symmetric codex/gemini adapters (codex never
degrades; gemini can), and the single-authority ``verify_findings`` entrypoint.

Run (from the handoff-fanout worktree):
    PYTHONPATH=src python -m pytest tests/test_supervisor_s2_verifier_core.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from handoff_fanout import codex_audit
from handoff_fanout import supervisor as sup
from handoff_fanout.supervisor import SchemaError
from handoff_fanout.supervisor.verdict import ProviderStatus, VerdictValue
from handoff_fanout.supervisor.verifier_core import RawBrainOutcome, _map_status

OK = ProviderStatus.OK
DEGRADED = ProviderStatus.DEGRADED
UNAVAILABLE = ProviderStatus.UNAVAILABLE
PARSE_ERROR = ProviderStatus.PARSE_ERROR


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _stage(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)


# --- Binding + resolution ----------------------------------------------------


def test_binding_value_required():
    with pytest.raises(SchemaError):
        sup.Binding(target=sup.BindingTarget.HEAD, value="")


def test_resolve_head(git_repo: Path):
    _stage(git_repo, "a.py", "x = 1\n")
    _git(git_repo, "commit", "-m", "first", "--quiet")
    head = _git(git_repo, "rev-parse", "HEAD")
    b = sup.resolve_binding(git_repo, sup.BindingTarget.HEAD)
    assert b.target is sup.BindingTarget.HEAD
    assert b.value == head


def test_resolve_head_without_commit_raises(git_repo: Path):
    # No commit yet → no HEAD → infra failure (BindingError), not a defect.
    with pytest.raises(sup.BindingError):
        sup.resolve_binding(git_repo, sup.BindingTarget.HEAD)


def test_resolve_staged_diff_hash_is_sensitive_to_staged_changes(git_repo: Path):
    _stage(git_repo, "a.py", "x = 1\n")
    b1 = sup.resolve_binding(git_repo, sup.BindingTarget.STAGED_DIFF_HASH)
    assert b1.value.startswith("sha256:")
    _stage(git_repo, "a.py", "x = 2\n")  # different staged content
    b2 = sup.resolve_binding(git_repo, sup.BindingTarget.STAGED_DIFF_HASH)
    assert b1.value != b2.value  # anti-replay basis changed


def test_resolve_staged_diff_hash_stable_for_same_content(git_repo: Path):
    _stage(git_repo, "a.py", "x = 1\n")
    b1 = sup.resolve_binding(git_repo, sup.BindingTarget.STAGED_DIFF_HASH)
    b2 = sup.resolve_binding(git_repo, sup.BindingTarget.STAGED_DIFF_HASH)
    assert b1.value == b2.value  # deterministic


def test_resolve_staged_diff_hash_detects_binary_content_change(git_repo: Path):
    # A `.gitattributes -diff` path renders an identical "Binary files … differ"
    # line for different content — the tree-oid anchor must still distinguish the
    # two staged states, else a stale verdict could replay onto changed bytes
    # (anti-replay hole, Gemini R2 P0).
    _stage(git_repo, ".gitattributes", "secret.bin -diff\n")
    _stage(git_repo, "secret.bin", "AAAA")
    b1 = sup.resolve_binding(git_repo, sup.BindingTarget.STAGED_DIFF_HASH)
    _stage(git_repo, "secret.bin", "BBBB")  # different content, same text-diff line
    b2 = sup.resolve_binding(git_repo, sup.BindingTarget.STAGED_DIFF_HASH)
    assert b1.value != b2.value  # tree-oid anchor catches the content change


def test_resolve_tree_oid(git_repo: Path):
    _stage(git_repo, "a.py", "x = 1\n")
    b = sup.resolve_binding(git_repo, sup.BindingTarget.TREE_OID)
    assert b.target is sup.BindingTarget.TREE_OID
    assert b.value == _git(git_repo, "write-tree")  # git-native index tree id


def test_resolve_non_repo_raises(tmp_path: Path):
    with pytest.raises(sup.BindingError):
        sup.resolve_binding(tmp_path, sup.BindingTarget.TREE_OID)


# --- anti-replay -------------------------------------------------------------


def test_is_bound_to_matches_same_state(git_repo: Path):
    _stage(git_repo, "a.py", "x = 1\n")
    binding = sup.resolve_binding(git_repo, sup.BindingTarget.STAGED_DIFF_HASH)
    v = sup.compute_verdict(
        codex=sup.ProviderFindings(status=OK),
        gemini=sup.ProviderFindings(status=OK),
        bound_to=binding.value,
        binding_target=binding.target,
        findings_ref="ref",
    )
    assert sup.is_bound_to(v, binding) is True


def test_is_bound_to_rejects_replay_on_different_diff(git_repo: Path):
    _stage(git_repo, "a.py", "x = 1\n")
    binding_old = sup.resolve_binding(git_repo, sup.BindingTarget.STAGED_DIFF_HASH)
    v = sup.compute_verdict(
        codex=sup.ProviderFindings(status=OK),
        gemini=sup.ProviderFindings(status=OK),
        bound_to=binding_old.value,
        binding_target=binding_old.target,
        findings_ref="ref",
    )
    _stage(git_repo, "a.py", "x = 999\n")  # diff moved on
    binding_new = sup.resolve_binding(git_repo, sup.BindingTarget.STAGED_DIFF_HASH)
    assert sup.is_bound_to(v, binding_new) is False  # stale verdict can't replay


def test_is_bound_to_rejects_different_binding_kind(git_repo: Path):
    _stage(git_repo, "a.py", "x = 1\n")
    _git(git_repo, "commit", "-m", "c", "--quiet")
    head = sup.resolve_binding(git_repo, sup.BindingTarget.HEAD)
    v = sup.compute_verdict(
        codex=sup.ProviderFindings(status=OK),
        gemini=sup.ProviderFindings(status=OK),
        bound_to=head.value,  # same hex value...
        binding_target=sup.BindingTarget.HEAD,
        findings_ref="ref",
    )
    other = sup.Binding(target=sup.BindingTarget.TREE_OID, value=head.value)  # ...wrong kind
    assert sup.is_bound_to(v, other) is False


# --- parse_provider_findings (read-only raw → ProviderFindings) --------------


def _run(provider="codex", status=OK, findings=None):
    return sup.ProviderRun(provider=provider, status=status, findings=findings)


def test_parse_clean_run():
    pf = sup.parse_provider_findings(_run(findings={"original_findings": []}))
    assert pf.status is OK
    assert pf.p0 == 0 and pf.p1 == 0 and pf.fingerprints == []


def test_parse_counts_p0_p1_and_fingerprints():
    findings = {
        "original_findings": [
            {"severity": "P0", "file": "a.py", "line": 3, "title": "boom"},
            {"severity": "P1", "file": "b.py", "line": 9, "title": "leak"},
            {"severity": "P2", "file": "c.py", "title": "nit"},  # non-blocking
        ]
    }
    pf = sup.parse_provider_findings(_run(findings=findings))
    assert pf.p0 == 1 and pf.p1 == 1
    assert len(pf.fingerprints) == 2  # only the blocking ones
    # Fingerprints reuse the live codex_audit identity (cross-gate consistency).
    assert pf.fingerprints[0] == codex_audit.compute_finding_hash(findings["original_findings"][0])


def test_parse_unrecognized_severity_is_failclosed_blocking():
    findings = {"original_findings": [{"severity": "P5", "file": "x.py"}]}
    pf = sup.parse_provider_findings(_run(findings=findings))
    assert pf.p0 == 1  # spoofed/typo'd severity → blocking, never clean
    assert pf.fingerprints


def test_parse_identity_less_blocking_gets_distinct_fallback_fingerprints():
    findings = {"original_findings": [{"severity": "P0"}, {"severity": "P0"}]}
    pf = sup.parse_provider_findings(_run(provider="codex", findings=findings))
    assert pf.p0 == 2
    # Distinct positional fallbacks (else ProviderFindings' no-dup rule would trip).
    assert pf.fingerprints == ["noident:codex:0", "noident:codex:1"]
    assert len(set(pf.fingerprints)) == 2


def test_parse_ok_but_no_findings_list_is_parse_error():
    # A claimed-OK run with no parseable findings list must NOT read as clean.
    pf = sup.parse_provider_findings(_run(findings={"junk": 1}))
    assert pf.status is PARSE_ERROR
    pf2 = sup.parse_provider_findings(_run(findings=None))
    assert pf2.status is PARSE_ERROR


@pytest.mark.parametrize("status", [UNAVAILABLE, PARSE_ERROR])
def test_parse_non_evaluable_status_passthrough(status):
    pf = sup.parse_provider_findings(_run(status=status, findings={"original_findings": []}))
    assert pf.status is status
    assert pf.p0 == 0 and pf.fingerprints == []


def test_parse_skips_non_dict_findings():
    findings = {"original_findings": ["not a dict", {"severity": "P0", "file": "a.py"}]}
    pf = sup.parse_provider_findings(_run(findings=findings))
    assert pf.p0 == 1  # the string entry is ignored, the real P0 counted


def test_parse_dedups_repeated_finding_within_provider():
    # The same blocking finding restated within one provider must NOT crash on
    # ProviderFindings' no-dup rule, nor double-count p0.
    same = {"severity": "P0", "file": "a.py", "line": 3, "title": "boom"}
    findings = {"original_findings": [same, dict(same), dict(same)]}
    pf = sup.parse_provider_findings(_run(findings=findings))
    assert pf.p0 == 1  # counted once
    assert len(pf.fingerprints) == 1  # one fingerprint, no duplicate → no crash


def test_parse_same_location_different_severity_counts_both():
    # Same file/line but P0 vs P1 are distinct claims (severity is part of the
    # identity) → both counted, not collapsed.
    findings = {
        "original_findings": [
            {"severity": "P0", "file": "a.py", "line": 3, "title": "boom"},
            {"severity": "P1", "file": "a.py", "line": 3, "title": "boom"},
        ]
    }
    pf = sup.parse_provider_findings(_run(findings=findings))
    assert pf.p0 == 1 and pf.p1 == 1
    assert len(pf.fingerprints) == 2


# --- adapters (codex / gemini, symmetric) ------------------------------------


def test_map_status():
    parseable = {"original_findings": []}
    assert _map_status(RawBrainOutcome(ok=True, findings=parseable)) is OK
    assert _map_status(RawBrainOutcome(ok=True, degraded=True, findings=parseable)) is DEGRADED
    assert _map_status(RawBrainOutcome(ok=False)) is UNAVAILABLE
    assert _map_status(RawBrainOutcome(ok=False, timed_out=True)) is UNAVAILABLE
    # Claimed-OK but unparseable → PARSE_ERROR *before* retry (Codex R2 P1), so the
    # retry layer (which only sees status) re-tries instead of accepting it as clean.
    assert _map_status(RawBrainOutcome(ok=True, findings=None)) is PARSE_ERROR
    assert _map_status(RawBrainOutcome(ok=True, findings={"junk": 1})) is PARSE_ERROR
    # parse failure outranks a degraded-model signal (unusable raw, not just weak).
    assert _map_status(RawBrainOutcome(ok=True, degraded=True, findings=None)) is PARSE_ERROR


def test_adapter_marks_unparseable_ok_as_parse_error():
    run = sup.codex_adapter(lambda b: RawBrainOutcome(ok=True, findings=None)).run(
        sup.Binding(target=sup.BindingTarget.HEAD, value="abc")
    )
    assert run.status is PARSE_ERROR  # not OK → retry runner will re-try it


def test_codex_adapter_runs_and_names_provider():
    calls = []

    def invoke(binding):
        calls.append(binding)
        return RawBrainOutcome(ok=True, findings={"original_findings": []}, model="gpt-5")

    adapter = sup.codex_adapter(invoke)
    assert adapter.provider == "codex"
    binding = sup.Binding(target=sup.BindingTarget.STAGED_DIFF_HASH, value="sha256:x")
    run = adapter.run(binding)
    assert run.provider == "codex"
    assert run.status is OK
    assert run.model == "gpt-5"
    assert calls == [binding]  # the injected invoker was called with the binding


def test_gemini_adapter_can_degrade():
    def invoke(binding):
        return RawBrainOutcome(
            ok=True, degraded=True, findings={"original_findings": []}, model="gemini-2.5-flash"
        )

    run = sup.gemini_adapter(invoke).run(sup.Binding(target=sup.BindingTarget.HEAD, value="abc"))
    assert run.provider == "gemini"
    assert run.status is DEGRADED


def test_adapter_requires_provider_name():
    with pytest.raises(SchemaError):
        sup.AuditAdapter("", lambda b: RawBrainOutcome(ok=True))


# --- verify_findings (the single verifier) -----------------------------------


def _binding():
    return sup.Binding(target=sup.BindingTarget.STAGED_DIFF_HASH, value="sha256:diffA")


def test_verify_green_end_to_end():
    codex_run = _run("codex", OK, {"original_findings": []})
    gemini_run = _run("gemini", OK, {"original_findings": []})
    v = sup.verify_findings(codex_run, gemini_run, binding=_binding(), findings_ref="ref")
    assert v.verdict is VerdictValue.GREEN
    assert v.bound_to == "sha256:diffA"


def test_verify_red_end_to_end():
    codex_run = _run("codex", OK, {"original_findings": [{"severity": "P0", "file": "a.py"}]})
    gemini_run = _run("gemini", OK, {"original_findings": []})
    v = sup.verify_findings(codex_run, gemini_run, binding=_binding(), findings_ref="ref")
    assert v.verdict is VerdictValue.RED
    assert v.deduped_fingerprints  # auditable


def test_verify_degraded_provider_forces_unknown():
    codex_run = _run("codex", DEGRADED, {"original_findings": []})
    gemini_run = _run("gemini", OK, {"original_findings": []})
    v = sup.verify_findings(codex_run, gemini_run, binding=_binding(), findings_ref="ref")
    assert v.verdict is VerdictValue.UNKNOWN
    assert v.degraded is True


def test_verify_unavailable_provider_forces_unknown():
    codex_run = _run("codex", UNAVAILABLE)
    gemini_run = _run("gemini", OK, {"original_findings": []})
    v = sup.verify_findings(codex_run, gemini_run, binding=_binding(), findings_ref="ref")
    assert v.verdict is VerdictValue.UNKNOWN
    # An unavailable provider is NOT a "degraded model" — degraded flag stays off.
    assert v.degraded is False


def test_verify_attempts_defaults_to_max_of_runs():
    codex_run = sup.ProviderRun(
        provider="codex", status=OK, findings={"original_findings": []}, attempts=2
    )
    gemini_run = sup.ProviderRun(
        provider="gemini", status=OK, findings={"original_findings": []}, attempts=3
    )
    v = sup.verify_findings(codex_run, gemini_run, binding=_binding(), findings_ref="ref")
    assert v.attempts == 3


def test_verify_cross_provider_dedup_collapses_shared_finding():
    same = {"severity": "P0", "file": "shared.py", "line": 10, "title": "same bug"}
    codex_run = _run("codex", OK, {"original_findings": [same]})
    gemini_run = _run("gemini", OK, {"original_findings": [dict(same)]})
    v = sup.verify_findings(codex_run, gemini_run, binding=_binding(), findings_ref="ref")
    assert v.verdict is VerdictValue.RED
    # Both brains found the same identity → one deduped fingerprint.
    assert len(v.deduped_fingerprints) == 1
