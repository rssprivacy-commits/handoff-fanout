"""Regression tests for ``install/git-hooks/post-commit`` — auto-sync deployed runtime COPIES.

Two runtime entries are deployed COPIES of files in this repo's ``install/`` dir, kept current ONLY by a
manual ``install.sh --sync-*`` step:

    install/auto-continue.sh  → ~/.local/bin/auto-continue.sh   (com.dharmaxis.auto-continue launcher)
    install/dump-handoff.py   → ~/.local/bin/dump-handoff.py    (v5.4 engine re-exec shim)

Forgetting the manual sync after a fix left the runtime stale → the launchd launcher ran OLD logic for
every spawn until someone remembered to sync (owner pain 2026-06-05: cold-submit failed, manual Enter).
The post-commit hook closes that gap: a commit touching one of those canonical assets auto-fires the
matching sync so the runtime converges on the just-committed version (owner ruling 甲, B half of B+C).

2026-06-12 delivery-audit deploy gate (codex MUST「闸 push 不闸部署=合了没推已 live 窗口」): the
auto-sync additionally requires matching dual-brain audit evidence (``handoff audit-check``) for the
just-made commit — no evidence → loud WARN + NO deploy (fail-closed, still exit 0). The deliberate
human remedy (``install.sh --sync-*``) stays ungated.

The installer is STUBBED via ``HANDOFF_INSTALL_SH`` so tests never touch ~/.local/bin or
~/.claude-handoff; the checker CLI is pointed at THIS worktree's code via ``HANDOFF_BIN``
(the live ``handoff`` predates the audit-check subcommand) + an isolated ``HANDOFF_HOME``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "install" / "git-hooks" / "post-commit"
INSTALL_SH = REPO_ROOT / "install" / "install.sh"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.name", "t"], check=True)
    return r


def _commit(repo: Path, rel: str, content: str = "x\n") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", rel], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"touch {rel}"], check=True)


def _stub_installer(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    sink = tmp_path / "installer.calls"
    stub = tmp_path / "install.sh"
    stub.write_text(
        f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{sink}"\nexit {exit_code}\n', encoding="utf-8"
    )
    stub.chmod(0o755)
    return stub, sink


def _stub_handoff(tmp_path: Path) -> Path:
    stub = tmp_path / "handoff-stub"
    stub.write_text(
        "#!/bin/bash\n"
        f'export PYTHONPATH="{REPO_ROOT / "src"}"\n'
        f'exec "{sys.executable}" -m handoff_fanout.cli "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _write_evidence(home: Path, project: str, head_sha: str) -> None:
    """A matching GREEN dual-brain evidence — the audit gate's legitimate pass condition."""
    audits = home / project / "audits"
    audits.mkdir(parents=True, exist_ok=True)
    (audits / "t.evidence.json").write_text(
        json.dumps(
            {"schema_version": 1, "overall_verdict": "GREEN", "reviewed_head_sha": head_sha}
        ),
        encoding="utf-8",
    )


def _audited(repo: Path, home: Path) -> None:
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    _write_evidence(home, repo.name, head)


def _run_hook(repo: Path, installer: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HANDOFF_INSTALL_SH"] = str(installer)
    env["HANDOFF_HOME"] = str(tmp_path / "hh")
    env["HANDOFF_BIN"] = str(_stub_handoff(tmp_path))
    env.pop("HANDOFF_AUDIT_GATE_BYPASS", None)
    return subprocess.run(
        ["bash", str(HOOK)], cwd=str(repo), env=env, capture_output=True, text=True
    )


def test_post_commit_syncs_launcher_when_auto_continue_changed(repo, tmp_path):
    stub, sink = _stub_installer(tmp_path)
    _commit(repo, "install/auto-continue.sh", "#!/bin/bash\nexit 0\n")
    _audited(repo, tmp_path / "hh")
    r = _run_hook(repo, stub, tmp_path)
    assert r.returncode == 0, r.stderr
    assert sink.exists() and "--sync-launcher" in sink.read_text(), "launcher change must fire --sync-launcher"


def test_post_commit_syncs_dump_when_dump_shim_changed(repo, tmp_path):
    """Symmetric same-class gap (Phase 0 SOP 同类一次全改): dump shim drift is identical to launcher drift."""
    stub, sink = _stub_installer(tmp_path)
    _commit(repo, "install/dump-handoff.py", "# shim\n")
    _audited(repo, tmp_path / "hh")
    r = _run_hook(repo, stub, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--sync-dump" in sink.read_text(), "dump shim change must fire --sync-dump"


def test_post_commit_noop_when_unrelated_file_changed(repo, tmp_path):
    stub, sink = _stub_installer(tmp_path)
    _commit(repo, "src/handoff_fanout/foo.py", "x = 1\n")
    r = _run_hook(repo, stub, tmp_path)
    assert r.returncode == 0, r.stderr
    assert not sink.exists(), "an unrelated change must not trigger any sync"
    assert "audit" not in r.stderr, "the audit gate must stay silent when no deploy asset changed"


def test_post_commit_syncs_both_when_both_changed(repo, tmp_path):
    stub, sink = _stub_installer(tmp_path)
    for rel, body in (("install/auto-continue.sh", "#!/bin/bash\nexit 0\n"),
                      ("install/dump-handoff.py", "# shim\n")):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", rel], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "both"], check=True)
    _audited(repo, tmp_path / "hh")
    r = _run_hook(repo, stub, tmp_path)
    assert r.returncode == 0, r.stderr
    calls = sink.read_text()
    assert "--sync-launcher" in calls and "--sync-dump" in calls


def test_post_commit_nonfatal_when_installer_fails(repo, tmp_path):
    """A post-commit hook must NEVER fail (the commit already happened) — a sync failure only warns."""
    stub, _ = _stub_installer(tmp_path, exit_code=1)
    _commit(repo, "install/auto-continue.sh", "#!/bin/bash\nexit 0\n")
    _audited(repo, tmp_path / "hh")
    r = _run_hook(repo, stub, tmp_path)
    assert r.returncode == 0, "post-commit must be non-fatal even when the sync fails"
    assert "FAILED" in r.stderr, "a failed sync must warn loudly with the manual remedy"


def test_post_commit_handles_non_root_commit(repo, tmp_path):
    """An asset commit that is NOT the repo's first commit (has a parent) must still be inspected."""
    _commit(repo, "README.md", "seed\n")  # parent commit, unrelated
    stub, sink = _stub_installer(tmp_path)
    _commit(repo, "install/auto-continue.sh", "#!/bin/bash\nexit 0\n")
    _audited(repo, tmp_path / "hh")
    r = _run_hook(repo, stub, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--sync-launcher" in sink.read_text()


def test_post_commit_handles_root_commit(repo, tmp_path):
    """The very first (root, no-parent) commit must still be inspected (uses --root / empty-tree base)."""
    stub, sink = _stub_installer(tmp_path)
    _commit(repo, "install/auto-continue.sh", "#!/bin/bash\nexit 0\n")  # root commit
    _audited(repo, tmp_path / "hh")
    r = _run_hook(repo, stub, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--sync-launcher" in sink.read_text(), "root commit changes must be detected (--root)"


# ─── delivery-audit deploy gate (fail-closed) ────────────────────────────────


def test_post_commit_blocks_sync_without_evidence(repo, tmp_path):
    """codex MUST: the deploy entry checks the SAME evidence as the pre-push gate —
    an un-audited asset commit must NOT auto-deploy, and must warn loudly."""
    stub, sink = _stub_installer(tmp_path)
    _commit(repo, "install/auto-continue.sh", "#!/bin/bash\nexit 0\n")
    r = _run_hook(repo, stub, tmp_path)  # no evidence written
    assert r.returncode == 0, "post-commit must still never fail the commit"
    assert not sink.exists(), "no audit evidence → the auto-deploy must be skipped (fail-closed)"
    assert "不自动部署" in r.stderr and "--sync-" in r.stderr, "must WARN loudly with the human remedy"


def test_post_commit_blocks_sync_when_checker_cli_missing(repo, tmp_path):
    stub, sink = _stub_installer(tmp_path)
    _commit(repo, "install/auto-continue.sh", "#!/bin/bash\nexit 0\n")
    env = os.environ.copy()
    env["HANDOFF_INSTALL_SH"] = str(stub)
    env["HANDOFF_HOME"] = str(tmp_path / "hh")
    env["HANDOFF_BIN"] = str(tmp_path / "no-such-handoff")
    r = subprocess.run(["bash", str(HOOK)], cwd=str(repo), env=env, capture_output=True, text=True)
    assert r.returncode == 0
    assert not sink.exists(), "checker unavailable → fail-closed, no deploy"
    assert "不可用" in r.stderr


def test_post_commit_hook_is_executable():
    assert HOOK.exists(), f"hook missing: {HOOK}"
    assert os.access(HOOK, os.X_OK), "post-commit hook must ship +x"


def test_install_sh_symlinks_post_commit_hook(tmp_path):
    """install.sh step 3 must symlink the post-commit hook into the repo (alongside pre-commit)."""
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    r = subprocess.run(
        ["bash", str(INSTALL_SH), "--no-launchd", "--no-config", "--no-extension",
         "--home", str(tmp_path / "hh")],
        cwd=str(repo), env=os.environ.copy(), capture_output=True, text=True,
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    hook = repo / ".git" / "hooks" / "post-commit"
    assert hook.is_symlink(), f"post-commit not symlinked:\n{r.stdout}\n{r.stderr}"
    assert "post-commit" in os.readlink(hook) and "handoff-fanout" in os.readlink(hook)


def test_install_sh_symlinks_prepush_and_postmerge(tmp_path):
    """install.sh step 3 must also wire the delivery-audit gate hooks (idempotently)."""
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    out = ""
    for _ in range(2):  # twice → idempotent
        r = subprocess.run(
            ["bash", str(INSTALL_SH), "--no-launchd", "--no-config", "--no-extension",
             "--home", str(tmp_path / "hh")],
            cwd=str(repo), env=os.environ.copy(), capture_output=True, text=True,
        )
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        out = r.stdout
    for name in ("pre-push", "post-merge"):
        hook = repo / ".git" / "hooks" / name
        assert hook.is_symlink(), f"{name} not symlinked:\n{out}"
        assert name in os.readlink(hook) and "handoff-fanout" in os.readlink(hook)
