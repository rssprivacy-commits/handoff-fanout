"""Integration tests for the delivery-audit gate git hooks.

``install/git-hooks/pre-push``  — physically refuses pushing un-audited new commits
to the protected branch (main); fail-closed when the handoff CLI is unavailable.
``install/git-hooks/post-merge`` — warn-only (never blocks): loud warning + the
``audits/.audit_pending`` marker when main absorbed an un-audited range.

The hooks shell out to ``handoff audit-check``; tests point ``HANDOFF_BIN`` at a
stub that runs THIS worktree's code (the live ``handoff`` console script resolves
the live editable src, which doesn't have the subcommand yet) — same stubbing
precedent as ``HANDOFF_INSTALL_SH`` in test_post_commit_sync.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PRE_PUSH = REPO_ROOT / "install" / "git-hooks" / "pre-push"
POST_MERGE = REPO_ROOT / "install" / "git-hooks" / "post-merge"
ZERO = "0" * 40
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, rel: str, content: str, msg: str) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def stub_handoff(tmp_path: Path) -> Path:
    stub = tmp_path / "bin" / "handoff-stub"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "#!/bin/bash\n"
        f'export PYTHONPATH="{REPO_ROOT / "src"}"\n'
        f'exec "{sys.executable}" -m handoff_fanout.cli "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


@pytest.fixture
def gate_env(tmp_path: Path, stub_handoff: Path):
    home = tmp_path / "hh"
    home.mkdir()
    env = os.environ.copy()
    env["HANDOFF_HOME"] = str(home)
    env["HANDOFF_BIN"] = str(stub_handoff)
    env.pop("HANDOFF_AUDIT_GATE_BYPASS", None)
    return home, env


def _write_evidence(
    home: Path, project: str, base_sha: str, head_sha: str, verdict: str = "GREEN"
) -> Path:
    """Runner-style evidence: the head-sha match path requires the base bound too
    (fail-closed since sw-ag-fix2 — evidence-v1 always emits both)."""
    audits = home / project / "audits"
    audits.mkdir(parents=True, exist_ok=True)
    p = audits / "t.evidence.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "overall_verdict": verdict,
                "reviewed_base_sha": base_sha,
                "reviewed_head_sha": head_sha,
            }
        ),
        encoding="utf-8",
    )
    return p


# ─── pre-push ────────────────────────────────────────────────────────────────


def _run_pre_push(repo: Path, env: dict, *lines: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(PRE_PUSH)],
        cwd=str(repo), env=env, input="".join(f"{line}\n" for line in lines),
        capture_output=True, text=True,
    )


def test_prepush_blocks_main_without_evidence(git_repo: Path, gate_env):
    _home, env = gate_env
    base = _commit(git_repo, "a.txt", "1\n", "seed")
    head = _commit(git_repo, "b.txt", "2\n", "feature")
    r = _run_pre_push(git_repo, env, f"refs/heads/main {head} refs/heads/main {base}")
    assert r.returncode == 1, r.stderr
    assert "拒推" in r.stderr


def test_prepush_passes_with_evidence(git_repo: Path, gate_env):
    home, env = gate_env
    base = _commit(git_repo, "a.txt", "1\n", "seed")
    head = _commit(git_repo, "b.txt", "2\n", "feature")
    _write_evidence(home, git_repo.name, base, head)
    r = _run_pre_push(git_repo, env, f"refs/heads/main {head} refs/heads/main {base}")
    assert r.returncode == 0, r.stderr


def test_prepush_ignores_non_main_refs(git_repo: Path, gate_env):
    _home, env = gate_env
    base = _commit(git_repo, "a.txt", "1\n", "seed")
    head = _commit(git_repo, "b.txt", "2\n", "feature")
    r = _run_pre_push(
        git_repo, env, f"refs/heads/feature {head} refs/heads/handoff/sw-x {base}"
    )
    assert r.returncode == 0, r.stderr


def test_prepush_first_push_uses_empty_tree_base(git_repo: Path, gate_env):
    home, env = gate_env
    head = _commit(git_repo, "a.txt", "1\n", "root")
    _write_evidence(home, git_repo.name, EMPTY_TREE, head)
    r = _run_pre_push(git_repo, env, f"refs/heads/main {head} refs/heads/main {ZERO}")
    assert r.returncode == 0, r.stderr
    # ...and without evidence the very same first push is refused
    (home / git_repo.name / "audits" / "t.evidence.json").unlink()
    r2 = _run_pre_push(git_repo, env, f"refs/heads/main {head} refs/heads/main {ZERO}")
    assert r2.returncode == 1


def test_prepush_ref_deletion_passes(git_repo: Path, gate_env):
    _home, env = gate_env
    base = _commit(git_repo, "a.txt", "1\n", "seed")
    r = _run_pre_push(git_repo, env, f"(delete) {ZERO} refs/heads/main {base}")
    assert r.returncode == 0, r.stderr


def test_prepush_fail_closed_when_cli_missing(git_repo: Path, gate_env, tmp_path):
    _home, env = gate_env
    env["HANDOFF_BIN"] = str(tmp_path / "no-such-handoff")
    base = _commit(git_repo, "a.txt", "1\n", "seed")
    head = _commit(git_repo, "b.txt", "2\n", "feature")
    r = _run_pre_push(git_repo, env, f"refs/heads/main {head} refs/heads/main {base}")
    assert r.returncode == 1
    assert "不可用" in r.stderr and "runbook" in r.stderr


def test_prepush_blocks_red_without_override_passes_with_it(git_repo: Path, gate_env):
    home, env = gate_env
    base = _commit(git_repo, "a.txt", "1\n", "seed")
    head = _commit(git_repo, "b.txt", "2\n", "feature")
    ev = _write_evidence(home, git_repo.name, base, head, verdict="RED")
    r = _run_pre_push(git_repo, env, f"refs/heads/main {head} refs/heads/main {base}")
    assert r.returncode == 1
    # owner override (forged here directly — the CLI path is unit-tested) flips it,
    # and the pass is loudly labelled as an override, not silently green.
    import hashlib

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from handoff_fanout.audit_evidence import range_facts

    facts = range_facts(git_repo, base, head)
    data = json.loads(ev.read_text(encoding="utf-8"))
    ts = "2026-06-12T10:00:00+0800"
    reason = "owner accepts"
    data.update(
        reviewed_patch_id=facts.patch_id,
        decision="accept_with_red_override",
        owner_ack={
            "reason": reason, "ts": ts,
            "checksum": hashlib.sha256(f"{facts.patch_id}|{reason}|{ts}".encode()).hexdigest(),
        },
    )
    ev.write_text(json.dumps(data), encoding="utf-8")
    r2 = _run_pre_push(git_repo, env, f"refs/heads/main {head} refs/heads/main {base}")
    assert r2.returncode == 0, r2.stderr
    assert "override" in r2.stdout


# ─── post-merge ──────────────────────────────────────────────────────────────


def _merge_feature(repo: Path) -> tuple[str, str]:
    """seed main, branch a feature, merge it back (no-ff) — returns (pre-merge, post-merge)."""
    _commit(repo, "a.txt", "1\n", "seed")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "b.txt", "2\n", "feature work")
    _git(repo, "checkout", "-q", "main")
    pre = _git(repo, "rev-parse", "HEAD")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "feature")
    return pre, _git(repo, "rev-parse", "HEAD")


def _run_post_merge(repo: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(POST_MERGE)], cwd=str(repo), env=env, capture_output=True, text=True
    )


def test_postmerge_warns_and_writes_pending(git_repo: Path, gate_env):
    home, env = gate_env
    _merge_feature(git_repo)
    r = _run_post_merge(git_repo, env)
    assert r.returncode == 0, "post-merge must NEVER block (warn-only by contract)"
    assert "audit_pending" in r.stderr or "无匹配" in r.stderr
    assert (home / git_repo.name / "audits" / ".audit_pending").exists()


def test_postmerge_quiet_with_evidence_and_clears_pending(git_repo: Path, gate_env):
    home, env = gate_env
    pre, post = _merge_feature(git_repo)
    marker = home / git_repo.name / "audits" / ".audit_pending"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    _write_evidence(home, git_repo.name, pre, post)
    r = _run_post_merge(git_repo, env)
    assert r.returncode == 0, r.stderr
    assert "⚠️⚠️⚠️" not in r.stderr
    assert not marker.exists(), "a passing post-merge check must clear .audit_pending"


def test_postmerge_noop_off_main(git_repo: Path, gate_env):
    home, env = gate_env
    _commit(git_repo, "a.txt", "1\n", "seed")
    _git(git_repo, "checkout", "-q", "-b", "side")
    _git(git_repo, "checkout", "-q", "-b", "other")
    _commit(git_repo, "b.txt", "2\n", "work")
    _git(git_repo, "checkout", "-q", "side")
    _git(git_repo, "merge", "-q", "--no-edit", "other")
    r = _run_post_merge(git_repo, env)
    assert r.returncode == 0
    assert not (home / git_repo.name / "audits" / ".audit_pending").exists()


def test_postmerge_exit0_even_when_cli_missing(git_repo: Path, gate_env, tmp_path):
    _home, env = gate_env
    env["HANDOFF_BIN"] = str(tmp_path / "no-such-handoff")
    _merge_feature(git_repo)
    r = _run_post_merge(git_repo, env)
    assert r.returncode == 0
    assert "不可用" in r.stderr


def test_hooks_are_executable():
    for hook in (PRE_PUSH, POST_MERGE):
        assert hook.exists(), f"hook missing: {hook}"
        assert os.access(hook, os.X_OK), f"{hook.name} must ship +x"
