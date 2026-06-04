"""Regression tests for ``install/git-hooks/post-commit`` — auto-sync deployed runtime COPIES.

Two runtime entries are deployed COPIES of files in this repo's ``install/`` dir, kept current ONLY by a
manual ``install.sh --sync-*`` step:

    install/auto-continue.sh  → ~/.local/bin/auto-continue.sh   (com.dharmaxis.auto-continue launcher)
    install/dump-handoff.py   → ~/.local/bin/dump-handoff.py    (v5.4 engine re-exec shim)

Forgetting the manual sync after a fix left the runtime stale → the launchd launcher ran OLD logic for
every spawn until someone remembered to sync (owner pain 2026-06-05: cold-submit failed, manual Enter).
The post-commit hook closes that gap: a commit touching one of those canonical assets auto-fires the
matching sync so the runtime converges on the just-committed version (owner ruling 甲, B half of B+C).

The installer is STUBBED via ``HANDOFF_INSTALL_SH`` so tests never touch ~/.local/bin or ~/.claude-handoff.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "install" / "git-hooks" / "post-commit"
INSTALL_SH = REPO_ROOT / "install" / "install.sh"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    return tmp_path


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


def _run_hook(repo: Path, installer: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HANDOFF_INSTALL_SH"] = str(installer)
    return subprocess.run(
        ["bash", str(HOOK)], cwd=str(repo), env=env, capture_output=True, text=True
    )


def test_post_commit_syncs_launcher_when_auto_continue_changed(repo, tmp_path):
    stub, sink = _stub_installer(tmp_path)
    _commit(repo, "install/auto-continue.sh", "#!/bin/bash\nexit 0\n")
    r = _run_hook(repo, stub)
    assert r.returncode == 0, r.stderr
    assert sink.exists() and "--sync-launcher" in sink.read_text(), "launcher change must fire --sync-launcher"


def test_post_commit_syncs_dump_when_dump_shim_changed(repo, tmp_path):
    """Symmetric same-class gap (Phase 0 SOP 同类一次全改): dump shim drift is identical to launcher drift."""
    stub, sink = _stub_installer(tmp_path)
    _commit(repo, "install/dump-handoff.py", "# shim\n")
    r = _run_hook(repo, stub)
    assert r.returncode == 0, r.stderr
    assert "--sync-dump" in sink.read_text(), "dump shim change must fire --sync-dump"


def test_post_commit_noop_when_unrelated_file_changed(repo, tmp_path):
    stub, sink = _stub_installer(tmp_path)
    _commit(repo, "src/handoff_fanout/foo.py", "x = 1\n")
    r = _run_hook(repo, stub)
    assert r.returncode == 0, r.stderr
    assert not sink.exists(), "an unrelated change must not trigger any sync"


def test_post_commit_syncs_both_when_both_changed(repo, tmp_path):
    stub, sink = _stub_installer(tmp_path)
    for rel, body in (("install/auto-continue.sh", "#!/bin/bash\nexit 0\n"),
                      ("install/dump-handoff.py", "# shim\n")):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", rel], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "both"], check=True)
    r = _run_hook(repo, stub)
    assert r.returncode == 0, r.stderr
    calls = sink.read_text()
    assert "--sync-launcher" in calls and "--sync-dump" in calls


def test_post_commit_nonfatal_when_installer_fails(repo, tmp_path):
    """A post-commit hook must NEVER fail (the commit already happened) — a sync failure only warns."""
    stub, _ = _stub_installer(tmp_path, exit_code=1)
    _commit(repo, "install/auto-continue.sh", "#!/bin/bash\nexit 0\n")
    r = _run_hook(repo, stub)
    assert r.returncode == 0, "post-commit must be non-fatal even when the sync fails"
    assert "FAILED" in r.stderr, "a failed sync must warn loudly with the manual remedy"


def test_post_commit_handles_non_root_commit(repo, tmp_path):
    """An asset commit that is NOT the repo's first commit (has a parent) must still be inspected."""
    _commit(repo, "README.md", "seed\n")  # parent commit, unrelated
    stub, sink = _stub_installer(tmp_path)
    _commit(repo, "install/auto-continue.sh", "#!/bin/bash\nexit 0\n")
    r = _run_hook(repo, stub)
    assert r.returncode == 0, r.stderr
    assert "--sync-launcher" in sink.read_text()


def test_post_commit_handles_root_commit(repo, tmp_path):
    """The very first (root, no-parent) commit must still be inspected (uses --root)."""
    stub, sink = _stub_installer(tmp_path)
    _commit(repo, "install/auto-continue.sh", "#!/bin/bash\nexit 0\n")  # root commit
    r = _run_hook(repo, stub)
    assert r.returncode == 0, r.stderr
    assert "--sync-launcher" in sink.read_text(), "root commit changes must be detected (--root)"


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
