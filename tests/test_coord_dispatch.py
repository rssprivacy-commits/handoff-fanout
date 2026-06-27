"""Tests for ``handoff coord-dispatch`` (coord_dispatch.py).

Covers the machine-judged concurrency-conflict gate (file overlap / same-repo
push / shared-resource intersection / fail-closed on missing-or-unknown fields /
unexpandable glob = potential conflict), the brief skeleton's welded-in hard
boundaries, and the dry-run-default vs --execute behavior. The --execute tests
use a FAKE ``DX_SPAWN_SH`` that records its argv — never a real worker window.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from handoff_fanout import config as _config
from handoff_fanout import coord_dispatch as cd

# ─── helpers ─────────────────────────────────────────────────────────────────


def _mkproject(tmp_path: Path, name: str, files: list[str] | None = None) -> Path:
    """Create a project dir with optional existing files (for glob expansion)."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for rel in files or []:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# stub\n", encoding="utf-8")
    return root


def _task(
    project: Path,
    task_id: str,
    *,
    purpose: str = "做一件小事",
    predicted_files: object = None,
    repo_branch: object = "proj@main",
    will_push: object = False,
    worktree_isolation: object = True,
    shared_writes: object = "none",
    credential_scopes: object = "none",
    runtime_targets: object = "none",
    brief_points: object = None,
    drop: set[str] | None = None,
) -> dict:
    """Build one task dict. ``drop`` removes keys entirely (to test missing fields).
    Pass ``"unknown"`` (or omit via drop) to exercise fail-closed."""
    d: dict = {
        "task_id": task_id,
        "project": str(project),
        "purpose_plain": purpose,
        "predicted_files": predicted_files if predicted_files is not None else [f"src/{task_id}.py"],
        "repo_branch": repo_branch,
        "will_push": will_push,
        "worktree_isolation": worktree_isolation,
        "shared_writes": shared_writes,
        "credential_scopes": credential_scopes,
        "runtime_targets": runtime_targets,
    }
    if brief_points is not None:
        d["brief_points"] = brief_points
    for k in drop or set():
        d.pop(k, None)
    return d


def _write_json(tmp_path: Path, tasks: list[dict]) -> Path:
    p = tmp_path / "tasks.json"
    p.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return p


def _verdict_for(analysis: cd.BatchAnalysis, a: str, b: str) -> str:
    for pv in analysis.pairs:
        if {pv.a, pv.b} == {a, b}:
            return pv.verdict
    raise AssertionError(f"no pair {a}↔{b}")


# ─── conflict gate: SAFE-PARALLEL ────────────────────────────────────────────


def test_three_disjoint_tasks_are_safe_parallel(tmp_path: Path) -> None:
    """All fields explicit + disjoint/none across distinct projects → SAFE."""
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    pc = _mkproject(tmp_path, "proj-c")
    tasks = [
        cd._parse_identity(_task(pa, "t-a", repo_branch="proj-a@main"), 0),
        cd._parse_identity(_task(pb, "t-b", repo_branch="proj-b@main"), 1),
        cd._parse_identity(_task(pc, "t-c", repo_branch="proj-c@main"), 2),
    ]
    analysis = cd.analyze_batch(tasks)
    assert analysis.parallel_safe
    assert all(p.verdict == cd.SAFE_PARALLEL for p in analysis.pairs)


def test_same_relative_path_different_projects_is_disjoint(tmp_path: Path) -> None:
    """Identical relative predicted_files in DIFFERENT projects anchor to distinct
    absolute paths → provably file-disjoint → SAFE."""
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    tasks = [
        cd._parse_identity(_task(pa, "t-a", predicted_files=["src/shared.py"], repo_branch="a@main"), 0),
        cd._parse_identity(_task(pb, "t-b", predicted_files=["src/shared.py"], repo_branch="b@main"), 1),
    ]
    assert cd.analyze_batch(tasks).parallel_safe


def test_single_task_is_trivially_parallel_safe(tmp_path: Path) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    analysis = cd.analyze_batch([cd._parse_identity(_task(pa, "solo"), 0)])
    assert analysis.parallel_safe
    assert analysis.pairs == []


# ─── conflict gate: file overlap → MUST-SERIAL ───────────────────────────────


def test_same_file_same_project_is_must_serial(tmp_path: Path) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    tasks = [
        cd._parse_identity(_task(pa, "t-one", predicted_files=["src/x.py"]), 0),
        cd._parse_identity(_task(pa, "t-two", predicted_files=["src/x.py"]), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert not analysis.parallel_safe
    assert _verdict_for(analysis, "t-one", "t-two") == cd.MUST_SERIAL
    reasons = analysis.pairs[0].reasons
    assert any("predicted_files overlap" in r for r in reasons)


def test_glob_matching_other_new_literal_is_must_serial(tmp_path: Path) -> None:
    """A's expandable glob over a dir that B adds a new literal file into → the
    glob fnmatches the new file → MUST-SERIAL (catches the new-file collision)."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/existing.py"])
    tasks = [
        cd._parse_identity(_task(pa, "t-glob", predicted_files=["src/*.py"]), 0),
        cd._parse_identity(_task(pa, "t-new", predicted_files=["src/brand_new.py"]), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-glob", "t-new") == cd.MUST_SERIAL


def test_unexpandable_glob_is_potential_conflict_not_empty_set(tmp_path: Path) -> None:
    """A glob matching nothing on disk must NOT be treated as 'touches nothing'
    (which would falsely clear it for parallel). It marks the file set
    indeterminate → MUST-SERIAL even against a disjoint-looking task."""
    pa = _mkproject(tmp_path, "proj-a")  # empty: src/*.py matches 0 files
    pb = _mkproject(tmp_path, "proj-b", files=["src/y.py"])
    t_bad = cd._parse_identity(_task(pa, "t-bad", predicted_files=["src/*.py"], repo_branch="a@main"), 0)
    t_ok = cd._parse_identity(_task(pb, "t-ok", predicted_files=["src/y.py"], repo_branch="b@main"), 1)
    analysis = cd.analyze_batch([t_bad, t_ok])
    assert _verdict_for(analysis, "t-bad", "t-ok") == cd.MUST_SERIAL
    assert any("indeterminate" in r for p in analysis.pairs for r in p.reasons)


# ─── conflict gate: symlink / realpath canonicalization (P0) ─────────────────


def test_symlink_alias_existing_file_is_must_serial(tmp_path: Path) -> None:
    """P0 regression: two tasks reach the SAME real file via different path
    spellings — task A through a symlinked dir (``link``→``src``), task B through
    the real dir. realpath must collapse both to one absolute path → MUST-SERIAL
    (without the fix, the two distinct strings falsely read as SAFE-PARALLEL and
    two concurrent workers would clobber the same file)."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/foo.py"])
    (pa / "link").symlink_to("src")  # link/foo.py and src/foo.py are the same file
    tasks = [
        cd._parse_identity(_task(pa, "t-via-link", predicted_files=["link/foo.py"]), 0),
        cd._parse_identity(_task(pa, "t-via-real", predicted_files=["src/foo.py"]), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert not analysis.parallel_safe
    assert _verdict_for(analysis, "t-via-link", "t-via-real") == cd.MUST_SERIAL
    assert any("predicted_files overlap" in r for r in analysis.pairs[0].reasons)


def test_symlink_alias_new_file_through_link_is_must_serial(tmp_path: Path) -> None:
    """The collision target need not exist yet: a brand-new file declared once
    through the symlinked dir and once through the real dir still canonicalizes to
    one path (realpath resolves the symlink prefix, keeps the not-yet-created
    tail) → MUST-SERIAL."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/keep.py"])  # ensure src/ exists
    (pa / "link").symlink_to("src")
    tasks = [
        cd._parse_identity(_task(pa, "t-link-new", predicted_files=["link/brand_new.py"]), 0),
        cd._parse_identity(_task(pa, "t-real-new", predicted_files=["src/brand_new.py"]), 1),
    ]
    assert _verdict_for(cd.analyze_batch(tasks), "t-link-new", "t-real-new") == cd.MUST_SERIAL


def test_symlink_to_distinct_files_stays_safe_parallel(tmp_path: Path) -> None:
    """Precision guard: the realpath fix must NOT collapse genuinely-distinct
    files. A reaches ``src/foo.py`` via the symlink, B touches ``src/bar.py`` —
    different real files → still SAFE-PARALLEL (proves the fix is canonicalization,
    not a blanket 'symlink present → serialize')."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/foo.py", "src/bar.py"])
    (pa / "link").symlink_to("src")
    tasks = [
        cd._parse_identity(_task(pa, "t-link-foo", predicted_files=["link/foo.py"],
                                 repo_branch="proj-a@main", will_push=False,
                                 worktree_isolation=True), 0),
        cd._parse_identity(_task(pa, "t-real-bar", predicted_files=["src/bar.py"],
                                 repo_branch="proj-a@main", will_push=False,
                                 worktree_isolation=True), 1),
    ]
    assert cd.analyze_batch(tasks).parallel_safe


def test_glob_through_symlink_matches_real_literal_is_must_serial(tmp_path: Path) -> None:
    """A glob expanded through a symlinked dir must canonicalize its matches so it
    overlaps a literal declared through the real dir (the glob's expansion would
    otherwise carry the unresolved ``link/`` prefix and miss the real file)."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/existing.py"])
    (pa / "link").symlink_to("src")
    tasks = [
        cd._parse_identity(_task(pa, "t-glob", predicted_files=["link/*.py"]), 0),
        cd._parse_identity(_task(pa, "t-lit", predicted_files=["src/existing.py"]), 1),
    ]
    assert _verdict_for(cd.analyze_batch(tasks), "t-glob", "t-lit") == cd.MUST_SERIAL


# ─── conflict gate: directory entries / fail-closed file sets ────────────────


def test_existing_directory_entry_is_indeterminate_must_serial(tmp_path: Path) -> None:
    """A bare directory in predicted_files claims the WHOLE subtree (incl. files
    not created yet) — it cannot be enumerated to a fixed set → fail-closed
    indeterminate → MUST-SERIAL against an otherwise-disjoint task."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/a.py"])
    pb = _mkproject(tmp_path, "proj-b", files=["src/b.py"])
    tasks = [
        cd._parse_identity(_task(pa, "t-dir", predicted_files=["src"], repo_branch="a@main"), 0),
        cd._parse_identity(_task(pb, "t-ok", predicted_files=["src/b.py"], repo_branch="b@main"), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-dir", "t-ok") == cd.MUST_SERIAL
    assert any("indeterminate" in r for p in analysis.pairs for r in p.reasons)


def test_trailing_slash_nonexistent_dir_is_must_serial(tmp_path: Path) -> None:
    """A trailing-slash entry that does not exist on disk is a directory claim we
    can't enumerate at all → fail-closed indeterminate → MUST-SERIAL."""
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b", files=["src/b.py"])
    tasks = [
        cd._parse_identity(_task(pa, "t-newdir", predicted_files=["build/out/"], repo_branch="a@main"), 0),
        cd._parse_identity(_task(pb, "t-ok", predicted_files=["src/b.py"], repo_branch="b@main"), 1),
    ]
    assert _verdict_for(cd.analyze_batch(tasks), "t-newdir", "t-ok") == cd.MUST_SERIAL


def test_symlinked_directory_entry_is_indeterminate(tmp_path: Path) -> None:
    """A directory reached through a symlink is still a directory claim →
    indeterminate (realpath resolves the link, isdir then catches it)."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/a.py"])
    (pa / "link").symlink_to("src")
    pb = _mkproject(tmp_path, "proj-b", files=["src/b.py"])
    tasks = [
        cd._parse_identity(_task(pa, "t-linkdir", predicted_files=["link"], repo_branch="a@main"), 0),
        cd._parse_identity(_task(pb, "t-ok", predicted_files=["src/b.py"], repo_branch="b@main"), 1),
    ]
    assert _verdict_for(cd.analyze_batch(tasks), "t-linkdir", "t-ok") == cd.MUST_SERIAL


# ─── conflict gate: repo_branch alias normalization (🟠) ──────────────────────


def test_normalize_repo_branch_strips_ref_prefixes() -> None:
    """Unit: ``refs/heads/`` and ``heads/`` ref-prefix spellings of a branch
    canonicalize to the same string (in both bare-branch and repo@branch forms)."""
    assert cd._normalize_repo_branch("main") == cd._normalize_repo_branch("refs/heads/main")
    assert cd._normalize_repo_branch("main") == cd._normalize_repo_branch("heads/main")
    assert cd._normalize_repo_branch("proj-a@main") == cd._normalize_repo_branch("proj-a@refs/heads/main")
    # distinct branches must stay distinct
    assert cd._normalize_repo_branch("proj-a@main") != cd._normalize_repo_branch("proj-a@dev")
    # distinct repos with same branch stay distinct
    assert cd._normalize_repo_branch("proj-a@main") != cd._normalize_repo_branch("proj-b@main")


def test_repo_branch_alias_judged_same_branch_must_serial(tmp_path: Path) -> None:
    """End-to-end: a self-reported ``refs/heads/main`` must not slip past the
    same-repo+branch-push rule by aliasing — normalize, then the push without
    isolation fires → MUST-SERIAL (without the fix the string mismatch falsely
    clears the pair)."""
    pa = _mkproject(tmp_path, "proj-a")
    tasks = [
        cd._parse_identity(
            _task(pa, "t-one", predicted_files=["src/one.py"], repo_branch="proj-a@main",
                  will_push=True, worktree_isolation=False), 0),
        cd._parse_identity(
            _task(pa, "t-two", predicted_files=["src/two.py"], repo_branch="proj-a@refs/heads/main",
                  will_push=True, worktree_isolation=False), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-one", "t-two") == cd.MUST_SERIAL
    assert any("same repo+branch push" in r for r in analysis.pairs[0].reasons)


# ─── conflict gate: same repo+branch push → MUST-SERIAL ──────────────────────


def test_same_repo_branch_push_without_worktree_is_must_serial(tmp_path: Path) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    tasks = [
        cd._parse_identity(
            _task(pa, "t-one", predicted_files=["src/one.py"], repo_branch="proj-a@main",
                  will_push=True, worktree_isolation=False), 0),
        cd._parse_identity(
            _task(pa, "t-two", predicted_files=["src/two.py"], repo_branch="proj-a@main",
                  will_push=True, worktree_isolation=False), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-one", "t-two") == cd.MUST_SERIAL
    assert any("same repo+branch push" in r for r in analysis.pairs[0].reasons)


def test_same_repo_branch_both_worktree_isolated_no_push_is_safe(tmp_path: Path) -> None:
    """Both isolated + no push + disjoint files → the same-branch rule does not
    fire (per the brief's exact predicate: needs a push AND a non-isolated side)."""
    pa = _mkproject(tmp_path, "proj-a")
    tasks = [
        cd._parse_identity(
            _task(pa, "t-one", predicted_files=["src/one.py"], repo_branch="proj-a@main",
                  will_push=False, worktree_isolation=True), 0),
        cd._parse_identity(
            _task(pa, "t-two", predicted_files=["src/two.py"], repo_branch="proj-a@main",
                  will_push=False, worktree_isolation=True), 1),
    ]
    assert cd.analyze_batch(tasks).parallel_safe


# ─── conflict gate: shared resource intersection → MUST-SERIAL ───────────────


@pytest.mark.parametrize("dim", ["shared_writes", "credential_scopes", "runtime_targets"])
def test_shared_resource_intersection_is_must_serial(tmp_path: Path, dim: str) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    ta = _task(pa, "t-a", predicted_files=["src/a.py"], repo_branch="a@main")
    tb = _task(pb, "t-b", predicted_files=["src/b.py"], repo_branch="b@main")
    ta[dim] = ["live-erp-db"]
    tb[dim] = ["live-erp-db", "other"]
    tasks = [cd._parse_identity(ta, 0), cd._parse_identity(tb, 1)]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-a", "t-b") == cd.MUST_SERIAL
    assert any(f"shared {dim}" in r for r in analysis.pairs[0].reasons)


def test_disjoint_shared_resources_are_safe(tmp_path: Path) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    tasks = [
        cd._parse_identity(_task(pa, "t-a", predicted_files=["src/a.py"], repo_branch="a@main",
                                 shared_writes=["db-a"]), 0),
        cd._parse_identity(_task(pb, "t-b", predicted_files=["src/b.py"], repo_branch="b@main",
                                 shared_writes=["db-b"]), 1),
    ]
    assert cd.analyze_batch(tasks).parallel_safe


# ─── conflict gate: fail-closed on missing / unknown fields ──────────────────


@pytest.mark.parametrize(
    "drop_key",
    ["repo_branch", "will_push", "worktree_isolation", "shared_writes",
     "credential_scopes", "runtime_targets", "predicted_files"],
)
def test_missing_field_is_fail_closed_must_serial(tmp_path: Path, drop_key: str) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    bad = _task(pb, "t-bad", repo_branch="b@main", drop={drop_key})
    tasks = [
        cd._parse_identity(_task(pa, "t-ok", repo_branch="a@main"), 0),
        cd._parse_identity(bad, 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-ok", "t-bad") == cd.MUST_SERIAL


@pytest.mark.parametrize("bad_field", ["repo_branch", "credential_scopes"])
def test_unknown_value_is_fail_closed_must_serial(tmp_path: Path, bad_field: str) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    bad = _task(pb, "t-bad", repo_branch="b@main")
    bad[bad_field] = "unknown"  # the literal "unknown" sentinel on the parametrized field
    tasks = [
        cd._parse_identity(_task(pa, "t-ok", repo_branch="a@main"), 0),
        cd._parse_identity(bad, 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-ok", "t-bad") == cd.MUST_SERIAL


def test_unknown_in_list_field_is_fail_closed(tmp_path: Path) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    tasks = [
        cd._parse_identity(_task(pa, "t-ok", repo_branch="a@main"), 0),
        cd._parse_identity(_task(pb, "t-bad", repo_branch="b@main",
                                 runtime_targets=["live", "unknown"]), 1),
    ]
    assert _verdict_for(cd.analyze_batch(tasks), "t-ok", "t-bad") == cd.MUST_SERIAL


def test_will_push_non_bool_is_fail_closed(tmp_path: Path) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    tasks = [
        cd._parse_identity(_task(pa, "t-ok", repo_branch="a@main"), 0),
        cd._parse_identity(_task(pb, "t-bad", repo_branch="b@main", will_push="yes"), 1),
    ]
    assert _verdict_for(cd.analyze_batch(tasks), "t-ok", "t-bad") == cd.MUST_SERIAL


# ─── brief skeleton ──────────────────────────────────────────────────────────


def test_brief_contains_all_required_sections(tmp_path: Path) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    task = cd._parse_identity(
        _task(pa, "t-x", purpose="给 A 加只读端点", predicted_files=["src/a.py", "src/b.py"],
              brief_points=["读入口", "加路由"]), 0)
    brief = cd.build_brief(task)
    # ① purpose_plain present
    assert "给 A 加只读端点" in brief
    # ② open-line echo instruction
    assert "🆔t-x" in brief and "任务目的：给 A 加只读端点" in brief
    # ③ predicted_files hard boundary + warning
    assert "禁改预测外文件" in brief
    assert "src/a.py" in brief and "src/b.py" in brief
    assert "越界" in brief
    # ④ §6b worker red-lines
    assert "禁自派" in brief
    assert "禁写共享 MEMORY.md" in brief
    assert "禁自我 discharge" in brief
    # ⑤ worker_reported sentinel with the correct slug+task
    assert "touch ~/.claude-handoff/proj-a/ack/t-x.worker_reported" in brief
    # brief points rendered
    assert "读入口" in brief and "加路由" in brief


def test_brief_warns_when_predicted_files_absent(tmp_path: Path) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    raw = _task(pa, "t-x", drop={"predicted_files"})
    task = cd._parse_identity(raw, 0)
    brief = cd.build_brief(task)
    assert "未声明 predicted_files" in brief


def test_missing_purpose_plain_refuses_brief(tmp_path: Path) -> None:
    """缺 purpose_plain → 拒绝生成 brief (whole command fails exit 2)."""
    pa = _mkproject(tmp_path, "proj-a")
    raw = _task(pa, "t-x", drop={"purpose_plain"})
    p = _write_json(tmp_path, [raw])
    rc = cd.run(p, execute=False)
    assert rc == cd.EXIT_FAIL


def test_empty_purpose_plain_refuses_brief(tmp_path: Path) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    raw = _task(pa, "t-x", purpose="   ")
    p = _write_json(tmp_path, [raw])
    assert cd.run(p, execute=False) == cd.EXIT_FAIL


# ─── CLI / dry-run vs execute ────────────────────────────────────────────────


def test_dry_run_writes_no_uri_and_no_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    home = tmp_path / "home"
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    # a fake that would record IF it were ever called — it must NOT be in dry-run
    fake_log = tmp_path / "spawn.log"
    fake = _write_fake_dx_spawn(tmp_path, fake_log, rc=0)
    monkeypatch.setenv("DX_SPAWN_SH", str(fake))

    p = _write_json(tmp_path, [
        _task(pa, "t-a", repo_branch="a@main"),
        _task(pb, "t-b", repo_branch="b@main"),
    ])
    rc = cd.run(p, execute=False)
    assert rc == cd.EXIT_OK
    assert not fake_log.exists(), "dry-run must not invoke the spawn engine"
    assert not (home / "_dispatch_briefs").exists(), "dry-run must not write brief files"
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "SAFE-PARALLEL" in out


def test_execute_safe_batch_invokes_fake_dx_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    home = tmp_path / "home"
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    fake_log = tmp_path / "spawn.log"
    fake = _write_fake_dx_spawn(tmp_path, fake_log, rc=0)
    monkeypatch.setenv("DX_SPAWN_SH", str(fake))

    p = _write_json(tmp_path, [
        _task(pa, "t-a", repo_branch="a@main"),
        _task(pb, "t-b", repo_branch="b@main"),
    ])
    rc = cd.run(p, execute=True, max_width=2)  # explicit width → deterministic vs machine load
    assert rc == cd.EXIT_OK
    log = fake_log.read_text()
    # both disjoint tasks dispatched in one concurrent wave, each with --project / --brief / --task-id
    assert "--task-id t-a" in log and "--task-id t-b" in log
    assert "--project " + str(pa) in log
    assert "--brief " in log
    # brief files persisted for the spawned sessions to read
    assert (home / "_dispatch_briefs" / "t-a.md").is_file()
    assert (home / "_dispatch_briefs" / "t-b.md").is_file()


def test_execute_unsafe_pair_dispatches_one_defers_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tasks editing the same file are MUST-SERIAL: --execute dispatches the
    proven-safe wave (exactly one of them) and DEFERS the conflicting peer — it
    NEVER co-dispatches a conflicting pair into the same concurrent wave."""
    pa = _mkproject(tmp_path, "proj-a")
    fake_log = tmp_path / "spawn.log"
    fake = _write_fake_dx_spawn(tmp_path, fake_log, rc=0)
    monkeypatch.setenv("DX_SPAWN_SH", str(fake))
    monkeypatch.setenv("HANDOFF_HOME", str(tmp_path / "home"))
    # two tasks editing the same file → MUST-SERIAL
    p = _write_json(tmp_path, [
        _task(pa, "t-one", predicted_files=["src/x.py"]),
        _task(pa, "t-two", predicted_files=["src/x.py"]),
    ])
    rc = cd.run(p, execute=True, max_width=4)
    assert rc == cd.EXIT_OK  # the wave dispatched cleanly; the conflict is deferred, not an error
    log = fake_log.read_text()
    # exactly ONE dispatched (the earlier-declared t-one), the conflicting peer deferred
    assert "--task-id t-one" in log
    assert "--task-id t-two" not in log, "a conflicting pair must never both reach one wave"


def test_execute_without_dx_spawn_env_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    monkeypatch.delenv("DX_SPAWN_SH", raising=False)
    monkeypatch.setenv("HANDOFF_HOME", str(tmp_path / "home"))
    p = _write_json(tmp_path, [_task(pa, "solo")])
    assert cd.run(p, execute=True) == cd.EXIT_FAIL


def test_execute_failure_isolation_attempts_all_wave_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing dx-spawn (rc!=0) is ISOLATED: every wave task is still attempted
    (no stop-on-first), and the batch exits FAIL because a spawn failed."""
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    home = tmp_path / "home"
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    fake_log = tmp_path / "spawn.log"
    fake = _write_fake_dx_spawn(tmp_path, fake_log, rc=1)
    monkeypatch.setenv("DX_SPAWN_SH", str(fake))
    p = _write_json(tmp_path, [
        _task(pa, "t-a", repo_branch="a@main"),
        _task(pb, "t-b", repo_branch="b@main"),
    ])
    assert cd.run(p, execute=True, max_width=2) == cd.EXIT_FAIL
    log = fake_log.read_text()
    # failure isolation: BOTH wave tasks were attempted, not just the first
    assert "--task-id t-a" in log and "--task-id t-b" in log


# ─── concurrent wave planning: partition (compute_wave) ──────────────────────


def _analysis(tasks: list[dict]) -> cd.BatchAnalysis:
    parsed = [cd._parse_identity(t, i) for i, t in enumerate(tasks)]
    return cd.analyze_batch(parsed)


def test_compute_wave_all_disjoint_is_single_wave(tmp_path: Path) -> None:
    """Three mutually-disjoint tasks under a generous width → one full wave, nothing deferred."""
    pa, pb, pc = (_mkproject(tmp_path, n) for n in ("a", "b", "c"))
    analysis = _analysis([
        _task(pa, "t-a", repo_branch="a@main"),
        _task(pb, "t-b", repo_branch="b@main"),
        _task(pc, "t-c", repo_branch="c@main"),
    ])
    plan = cd.compute_wave(analysis, max_width=8)
    assert plan.wave == ["t-a", "t-b", "t-c"]
    assert plan.conflict_deferred == [] and plan.load_deferred == []


def test_compute_wave_conflict_defers_minimum(tmp_path: Path) -> None:
    """A-B share a file (conflict); C disjoint. The maximum independent set is
    {one of A/B, C}; the conflicting peer is the only task deferred."""
    pa = _mkproject(tmp_path, "a")
    pc = _mkproject(tmp_path, "c")
    analysis = _analysis([
        _task(pa, "t-a", predicted_files=["src/shared.py"]),
        _task(pa, "t-b", predicted_files=["src/shared.py"]),
        _task(pc, "t-c", repo_branch="c@main"),
    ])
    plan = cd.compute_wave(analysis, max_width=8)
    # t-a (earlier of the conflicting pair) + t-c go; t-b deferred. Wave is pairwise-disjoint.
    assert set(plan.wave) == {"t-a", "t-c"}
    assert plan.conflict_deferred == ["t-b"]
    assert plan.load_deferred == []


def test_compute_wave_underdeclared_task_deferred_not_good_batch(tmp_path: Path) -> None:
    """An under-declared task (missing predicted_files → fail-closed, conflicts with
    EVERYTHING) must be the one deferred — the maximum independent set keeps the
    larger clean batch, never letting a position-0 hub poison the wave."""
    pa, pb, pc = (_mkproject(tmp_path, n) for n in ("a", "b", "c"))
    analysis = _analysis([
        _task(pa, "t-bad", drop={"predicted_files"}),   # declared first, conflicts with all
        _task(pb, "t-b", repo_branch="b@main"),
        _task(pc, "t-c", repo_branch="c@main"),
    ])
    plan = cd.compute_wave(analysis, max_width=8)
    assert set(plan.wave) == {"t-b", "t-c"}
    assert plan.conflict_deferred == ["t-bad"]


def test_compute_wave_load_cap_truncates_and_load_defers(tmp_path: Path) -> None:
    """Disjoint tasks beyond the width cap are load-deferred (declared order kept),
    and the capped wave stays pairwise-disjoint."""
    pa, pb, pc = (_mkproject(tmp_path, n) for n in ("a", "b", "c"))
    analysis = _analysis([
        _task(pa, "t-a", repo_branch="a@main"),
        _task(pb, "t-b", repo_branch="b@main"),
        _task(pc, "t-c", repo_branch="c@main"),
    ])
    plan = cd.compute_wave(analysis, max_width=2)
    assert plan.wave == ["t-a", "t-b"]
    assert plan.load_deferred == ["t-c"]
    assert plan.conflict_deferred == []


def test_compute_wave_width_clamped_to_at_least_one(tmp_path: Path) -> None:
    pa = _mkproject(tmp_path, "a")
    analysis = _analysis([_task(pa, "solo")])
    plan = cd.compute_wave(analysis, max_width=0)  # clamped to 1
    assert plan.wave == ["solo"]


def test_load_headroom_at_least_one() -> None:
    """The auto width ceiling is always a positive int (never 0 / negative)."""
    h = cd._load_headroom()
    assert isinstance(h, int) and h >= 1


# ─── concurrent wave dispatch: parallelism + failure isolation + render ────────


def test_dispatch_wave_runs_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic concurrency proof: a 2-party barrier inside a patched
    dispatch_one only clears if BOTH wave tasks run at the same time. A serial loop
    would block the first party until the 5s timeout → broken barrier → test fails."""
    pa, pb = _mkproject(tmp_path, "a"), _mkproject(tmp_path, "b")
    home = tmp_path / "home"
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    fake = _write_fake_dx_spawn(tmp_path, tmp_path / "spawn.log", rc=0)
    monkeypatch.setenv("DX_SPAWN_SH", str(fake))

    barrier = threading.Barrier(2, timeout=5)
    seen: list[str] = []
    lock = threading.Lock()

    def fake_dispatch_one(task: cd.Task, *, dx_spawn: Path, home: Path) -> tuple[bool, str]:
        del dx_spawn, home  # mock matches the real keyword interface; values unused here
        barrier.wait()  # serial dispatch ⇒ deadlock here ⇒ BrokenBarrierError ⇒ failure
        with lock:
            seen.append(task.task_id)
        return True, "ok"

    monkeypatch.setattr(cd, "dispatch_one", fake_dispatch_one)
    p = _write_json(tmp_path, [
        _task(pa, "t-a", repo_branch="a@main"),
        _task(pb, "t-b", repo_branch="b@main"),
    ])
    rc = cd.run(p, execute=True, max_width=2)
    assert rc == cd.EXIT_OK
    assert sorted(seen) == ["t-a", "t-b"], "both wave tasks must run concurrently"


def test_max_width_one_forces_serial_single_task_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--max-width 1 collapses even a disjoint batch to a width-1 wave (the rest
    load-deferred) — the explicit serial escape hatch."""
    pa, pb = _mkproject(tmp_path, "a"), _mkproject(tmp_path, "b")
    home = tmp_path / "home"
    monkeypatch.setenv("HANDOFF_HOME", str(home))
    fake_log = tmp_path / "spawn.log"
    fake = _write_fake_dx_spawn(tmp_path, fake_log, rc=0)
    monkeypatch.setenv("DX_SPAWN_SH", str(fake))
    p = _write_json(tmp_path, [
        _task(pa, "t-a", repo_branch="a@main"),
        _task(pb, "t-b", repo_branch="b@main"),
    ])
    rc = cd.run(p, execute=True, max_width=1)
    assert rc == cd.EXIT_OK
    log = fake_log.read_text()
    assert "--task-id t-a" in log
    assert "--task-id t-b" not in log, "width-1 wave must defer the second disjoint task"


def test_dry_run_shows_wave_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    pa, pb = _mkproject(tmp_path, "a"), _mkproject(tmp_path, "b")
    monkeypatch.setenv("HANDOFF_HOME", str(tmp_path / "home"))
    p = _write_json(tmp_path, [
        _task(pa, "t-a", repo_branch="a@main"),
        _task(pb, "t-b", repo_branch="b@main"),
    ])
    assert cd.run(p, execute=False, max_width=4) == cd.EXIT_OK
    out = capsys.readouterr().out
    assert "并发波次计划" in out          # wave plan section rendered
    assert "wave" in out and "并发宽度上界" in out


def test_cli_routing_accepts_max_width(tmp_path: Path) -> None:
    """`handoff coord-dispatch --max-width N` parses + routes through the unified CLI."""
    from handoff_fanout import cli

    pa = _mkproject(tmp_path, "proj-a")
    p = _write_json(tmp_path, [_task(pa, "solo")])
    assert cli.main(["coord-dispatch", "--tasks-json", str(p), "--max-width", "3"]) == 0


# ─── input validation ────────────────────────────────────────────────────────


def test_bad_json_fails(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert cd.run(p, execute=False) == cd.EXIT_FAIL


def test_empty_list_fails(tmp_path: Path) -> None:
    p = _write_json(tmp_path, [])
    assert cd.run(p, execute=False) == cd.EXIT_FAIL


def test_duplicate_task_ids_fail(tmp_path: Path) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    p = _write_json(tmp_path, [_task(pa, "dup"), _task(pa, "dup")])
    assert cd.run(p, execute=False) == cd.EXIT_FAIL


def test_non_kebab_task_id_fails(tmp_path: Path) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    p = _write_json(tmp_path, [_task(pa, "Bad_ID")])
    assert cd.run(p, execute=False) == cd.EXIT_FAIL


def test_cli_routing(tmp_path: Path) -> None:
    """`handoff coord-dispatch` routes through the unified CLI."""
    from handoff_fanout import cli

    pa = _mkproject(tmp_path, "proj-a")
    p = _write_json(tmp_path, [_task(pa, "solo")])
    rc = cli.main(["coord-dispatch", "--tasks-json", str(p)])
    assert rc == 0


# ─── #0: glob pattern traversing a symlink SEGMENT → indeterminate ────────────


def test_glob_through_symlink_segment_is_indeterminate_must_serial(tmp_path: Path) -> None:
    """#0 P0: when the symlink lives in a glob SEGMENT (``l*/*.py``) — not a static
    prefix (``link/*.py``) — _anchor can't statically resolve it, so the stored
    pattern keeps the unresolved ``l*``. Task A's glob has an existing match
    through ``link``→``src``; task B declares a brand-new file under the real
    ``src``. Without flagging the symlink-traversing glob indeterminate, ``fnmatch``
    on the unresolved ``l*/*.py`` misses ``src/brand_new.py`` → false SAFE-PARALLEL,
    yet at runtime A's glob would expand through ``link``→``src`` and clobber B's
    new file. The fix marks A's file set indeterminate → MUST-SERIAL."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/existing.py"])
    (pa / "link").symlink_to("src")  # link/ → src/, but lives behind the l* glob
    tasks = [
        cd._parse_identity(_task(pa, "t-globlink", predicted_files=["l*/*.py"]), 0),
        cd._parse_identity(_task(pa, "t-newreal", predicted_files=["src/brand_new.py"]), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-globlink", "t-newreal") == cd.MUST_SERIAL
    assert any("symlink" in r for p in analysis.pairs for r in p.reasons)


def test_glob_no_symlink_segment_stays_precise(tmp_path: Path) -> None:
    """Precision guard for #0: a plain glob with NO symlink in its expansion must
    NOT be marked indeterminate — it still resolves to a fixed concrete set and a
    disjoint task stays SAFE-PARALLEL (proves the fix keys on symlink traversal,
    not 'a glob is present')."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/existing.py"])
    pb = _mkproject(tmp_path, "proj-b", files=["lib/other.py"])
    tasks = [
        cd._parse_identity(_task(pa, "t-glob", predicted_files=["src/*.py"], repo_branch="a@main"), 0),
        cd._parse_identity(_task(pb, "t-ok", predicted_files=["lib/other.py"], repo_branch="b@main"), 1),
    ]
    assert cd.analyze_batch(tasks).parallel_safe


# ─── glob ∩ glob fail-open fix (sw-coord-p53 / p51 finding ①) ─────────────────


def test_two_globs_sharing_a_future_file_are_must_serial(tmp_path: Path) -> None:
    """🔴 the fail-open p51/p53 closes: two globs whose CONCRETE expansions are disjoint TODAY but
    that can BOTH match a future file. ``src/foo*.py`` (→foo_old.py) and ``src/*_new.py``
    (→bar_new.py) expand disjoint, neither glob fnmatches the other's concrete file, so the old
    glob-vs-concrete-only gate emitted SAFE-PARALLEL — yet both match a future ``src/foo_new.py``.
    The glob-vs-glob check fails closed → MUST-SERIAL."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/foo_old.py", "src/bar_new.py"])
    tasks = [
        cd._parse_identity(_task(pa, "t-foo", predicted_files=["src/foo*.py"]), 0),
        cd._parse_identity(_task(pa, "t-new", predicted_files=["src/*_new.py"]), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-foo", "t-new") == cd.MUST_SERIAL
    assert any("may overlap" in r for p in analysis.pairs for r in p.reasons)


def test_two_globs_distinct_dirs_stay_safe_parallel(tmp_path: Path) -> None:
    """Precision guard (no over-serialization): two basename globs in DIFFERENT (literal) dirs can
    never share a file (no dir-segment wildcard to bridge them) → SAFE-PARALLEL."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/a_x.py", "lib/b_y.py"])
    tasks = [
        cd._parse_identity(_task(pa, "t-src", predicted_files=["src/a*.py"]), 0),
        cd._parse_identity(_task(pa, "t-lib", predicted_files=["lib/b*.py"]), 1),
    ]
    assert cd.analyze_batch(tasks).parallel_safe


def test_two_globs_incompatible_suffix_stay_safe_parallel(tmp_path: Path) -> None:
    """Precision guard: same dir, but provably-incompatible fixed suffixes (``.py`` vs ``.txt``) →
    no common basename possible → SAFE-PARALLEL."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/foo.py", "src/notes.txt"])
    tasks = [
        cd._parse_identity(_task(pa, "t-py", predicted_files=["src/*.py"]), 0),
        cd._parse_identity(_task(pa, "t-txt", predicted_files=["src/*.txt"]), 1),
    ]
    assert cd.analyze_batch(tasks).parallel_safe


def test_two_globs_incompatible_prefix_stay_safe_parallel(tmp_path: Path) -> None:
    """Precision guard: same dir, provably-incompatible fixed prefixes (``foo`` vs ``bar``) → no
    common basename possible → SAFE-PARALLEL."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/foo_a.py", "src/bar_b.py"])
    tasks = [
        cd._parse_identity(_task(pa, "t-foo", predicted_files=["src/foo*.py"]), 0),
        cd._parse_identity(_task(pa, "t-bar", predicted_files=["src/bar*.py"]), 1),
    ]
    assert cd.analyze_batch(tasks).parallel_safe


# ─── #0b: glob with a DIRECTORY-SEGMENT wildcard → indeterminate (generalized) ─


def test_glob_directory_segment_wildcard_is_indeterminate_must_serial(tmp_path: Path) -> None:
    """#0b P0 (codex counter-example generalizing #0): a wildcard in a glob's
    DIRECTORY (non-final) segment makes its future expansion unbounded, and #0's
    leaf-realpath check alone misses it. ``l*`` matches a plain dir ``logs`` (a
    NON-symlink leaf → #0's check stays silent) AND a symlink dir ``link``→``src``.
    Task A globs ``l*/*.py`` while ``src`` has no ``.py`` today, so the glob expands
    only to ``logs/existing.py`` (no symlink leaf); task B declares a brand-new
    ``src/brand_new.py``. The unresolved ``l*/*.py`` fnmatch-misses ``src/brand_new.py``
    → would falsely read SAFE-PARALLEL, yet at runtime once B creates the file A's
    glob expands through ``link``→``src`` onto the SAME real file → clobber. The fix
    fails closed on ANY non-final wildcard segment, regardless of what it expands to
    today → MUST-SERIAL."""
    pa = _mkproject(tmp_path, "proj-a", files=["logs/existing.py"])  # plain dir, l* match
    (pa / "src").mkdir()                  # real src dir, currently EMPTY of .py
    (pa / "link").symlink_to("src")       # l* ALSO matches link→src (symlink dir)
    tasks = [
        cd._parse_identity(_task(pa, "t-dirglob", predicted_files=["l*/*.py"]), 0),
        cd._parse_identity(_task(pa, "t-newsrc", predicted_files=["src/brand_new.py"]), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-dirglob", "t-newsrc") == cd.MUST_SERIAL
    assert any("indeterminate" in r for p in analysis.pairs for r in p.reasons)


def test_glob_recursive_doublestar_segment_is_indeterminate_must_serial(tmp_path: Path) -> None:
    """#0b: a recursive ``**`` is a non-final (directory) segment whose expansion
    spans arbitrarily-deep subtrees — including dirs/symlinks/files created later —
    so it cannot be statically bounded → fail-closed indeterminate. Here B declares
    a brand-new TOP-LEVEL ``newtop.py``: at runtime ``**/*.py`` (with ``**`` matching
    zero dirs) WILL hit it, but the stored pattern's ``fnmatch`` misses a top-level
    file (it demands an intermediate ``/``) → without the fix this falsely reads
    SAFE-PARALLEL. The directory-segment-wildcard rule catches it → MUST-SERIAL."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/existing.py"])
    tasks = [
        cd._parse_identity(_task(pa, "t-rec", predicted_files=["**/*.py"], repo_branch="a@main"), 0),
        cd._parse_identity(_task(pa, "t-top", predicted_files=["newtop.py"], repo_branch="a@main"), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-rec", "t-top") == cd.MUST_SERIAL
    assert any("indeterminate" in r for p in analysis.pairs for r in p.reasons)


def test_glob_static_symlink_prefix_final_wildcard_stays_precise(tmp_path: Path) -> None:
    """Precision guard for #0b (no over-correction): ``link/*.py`` where ``link`` is
    a STATIC symlink dir (no wildcard in the directory segment) must NOT be marked
    indeterminate. _anchor realpath-resolves the static ``link``→``src`` prefix,
    leaving an anchored ``src/*.py`` whose ONLY wildcard is the final (filename)
    segment — a precisely-enumerable set. Against a genuinely disjoint task it stays
    SAFE-PARALLEL, proving the fix keys on a wildcard in a DIRECTORY segment, not on
    a symlink merely being present in a static prefix."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/existing.py"])
    (pa / "link").symlink_to("src")  # static symlink dir, NOT behind a wildcard
    pb = _mkproject(tmp_path, "proj-b", files=["lib/other.py"])
    tasks = [
        cd._parse_identity(_task(pa, "t-linkglob", predicted_files=["link/*.py"], repo_branch="a@main"), 0),
        cd._parse_identity(_task(pb, "t-ok", predicted_files=["lib/other.py"], repo_branch="b@main"), 1),
    ]
    assert cd.analyze_batch(tasks).parallel_safe


@pytest.mark.parametrize("pattern", [
    "l*/*.py",        # ``*`` in the first directory segment (codex case)
    "**/*.py",        # recursive ``**`` directory segment
    "?dir/*.py",      # ``?`` in a directory segment
    "[ab]/*.py",      # ``[`` char-class in a directory segment
    "src/sub*/x.py",  # wildcard in a MIDDLE (deep) directory segment
])
def test_any_directory_segment_metachar_is_indeterminate(tmp_path: Path, pattern: str) -> None:
    """#0b generalization lock: the fix keys on ANY glob metachar (``*`` / ``?`` /
    ``[``, incl. ``**``) in ANY non-final segment — not just the codex ``l*`` case,
    and not just the first segment. Each pattern carries a wildcard in its directory
    position → fail-closed indeterminate regardless of what exists on disk today. The
    note-text assertion proves the *directory-segment* rule fired (distinguishing it
    from a coincidental 'unexpandable glob = 0 matches' indeterminate)."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/existing.py"])
    prof = cd.build_conflict_profile(
        cd._parse_identity(_task(pa, "t-seg", predicted_files=[pattern]), 0)
    )
    assert prof.files_indeterminate
    assert any("directory-segment" in n for n in prof.file_notes)


# ─── #0c: metachar-NAMED symlink segment — realpath erasure → indeterminate ───


def test_glob_metachar_named_symlink_segment_realpath_erasure_is_must_serial(
    tmp_path: Path,
) -> None:
    """#0c P0 (codex R4 counter-example): the #0b directory-segment-wildcard guard
    must key on the RAW DECLARED string, NOT on the realpath-anchored path. A symlink
    whose own NAME contains a metachar — a literal dir ``l*`` → ``src`` — is resolved
    AWAY by ``_anchor``'s realpath, ERASING the ``*`` from the anchored path: an
    anchored-keyed guard then sees a clean ``src/*.py`` (whose final-only wildcard is
    precise) and stays silent → false SAFE-PARALLEL. Yet at runtime A's ``l*/*.py``
    glob-expands ``l*`` onto B's brand-new ``lib/`` and clobbers ``lib/brand_new.py``.
    Keying on the raw ``l*/*.py`` (whose ``l*`` segment the filesystem can't rewrite)
    fails closed → MUST-SERIAL — and immunizes the whole symlink/alias class."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/existing.py"])
    (pa / "l*").symlink_to("src")  # literal symlink NAMED 'l*' → src; realpath erases the '*'
    tasks = [
        cd._parse_identity(_task(pa, "t-star", predicted_files=["l*/*.py"]), 0),
        cd._parse_identity(_task(pa, "t-newlib", predicted_files=["lib/brand_new.py"]), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-star", "t-newlib") == cd.MUST_SERIAL
    prof = cd.build_conflict_profile(
        cd._parse_identity(_task(pa, "t-star", predicted_files=["l*/*.py"]), 0)
    )
    assert prof.files_indeterminate
    assert any("directory-segment" in n for n in prof.file_notes)


@pytest.mark.parametrize("linkname,pattern", [
    ("?x", "?x/*.py"),    # ``?`` in the symlink-named directory segment
    ("[a]", "[a]/*.py"),  # ``[`` char-class symlink-named directory segment
])
def test_metachar_named_symlink_segment_closes_whole_class(
    tmp_path: Path, linkname: str, pattern: str
) -> None:
    """#0c generalization lock: ANY glob metachar (``*`` covered above; here ``?`` /
    ``[``) in a directory segment that is ALSO a literal symlink with that exact name
    (``?x`` / ``[a]`` → ``src``) is dissolved by realpath under anchored-keying — the
    whole metachar-named-symlink class would slip through. Raw-string keying flags
    every one → fail-closed indeterminate, regardless of the metachar or the symlink's
    target. (A genuine regression: ``glob`` matches ``src/existing.py`` so the
    0-match path can't accidentally cover this; only the raw directory-segment rule
    can fire.)"""
    pa = _mkproject(tmp_path, "proj-a", files=["src/existing.py"])
    (pa / linkname).symlink_to("src")  # literal symlink whose NAME holds the metachar
    prof = cd.build_conflict_profile(
        cd._parse_identity(_task(pa, "t-seg", predicted_files=[pattern]), 0)
    )
    assert prof.files_indeterminate
    assert any("directory-segment" in n for n in prof.file_notes)


# ─── #0d: metachar-NAMED symlink in the FINAL (filename) segment — glob-safe ──
#          anchoring (codex R5 counter-example) ──────────────────────────────


def test_glob_final_segment_metachar_named_symlink_stays_glob_must_serial(
    tmp_path: Path,
) -> None:
    """#0d P0 (codex R5 counter-example): the FINAL-segment wildcard case that the
    #0b/#0c directory-segment guard deliberately lets through as "precise" (a
    last-segment ``l*.py`` SHOULD enumerate exactly) is itself corrupted when a
    literal symlink is NAMED with a metachar — ``pa/l*.py`` (a FILE whose own name
    contains ``*``) → ``src/existing.py``. ``_anchor``'s whole-path realpath
    dissolves that symlink, ERASING the entire ``l*.py`` glob into a concrete
    ``src/existing.py``: the stored "pattern" is no longer a pattern, so A's
    ``l*.py`` no longer ``fnmatch``-matches B's brand-new ``lib.py`` → false
    SAFE-PARALLEL, yet at runtime A's ``l*.py`` glob expands onto ``lib.py`` and both
    clobber it. Glob-safe anchoring realpaths ONLY the static prefix (never the
    wildcard segment), so the ``l*.py`` pattern SURVIVES → fnmatch hits B's
    ``lib.py`` (and the leaf-realpath check independently trips on the symlink) →
    MUST-SERIAL. This closes the realpath-erasure class for the final segment too."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/existing.py"])
    (pa / "l*.py").symlink_to("src/existing.py")  # FILE symlink NAMED 'l*.py' (literal '*')
    tasks = [
        cd._parse_identity(_task(pa, "t-star", predicted_files=["l*.py"]), 0),
        cd._parse_identity(_task(pa, "t-newlib", predicted_files=["lib.py"]), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-star", "t-newlib") == cd.MUST_SERIAL
    # root-cause assertion: the glob metachar SURVIVES anchoring — the pattern is
    # NOT realpath-erased into the concrete ``src/existing.py``.
    prof_a = cd.build_conflict_profile(
        cd._parse_identity(_task(pa, "t-star", predicted_files=["l*.py"]), 0)
    )
    assert any(p.endswith("l*.py") for p in prof_a.glob_patterns), (
        "glob metachar must survive anchoring (not realpath-erased to a concrete file)"
    )


@pytest.mark.parametrize("linkname,pattern", [
    ("l?.py", "l?.py"),    # ``?`` in a metachar-named final-segment file symlink
    ("[a].py", "[a].py"),  # ``[`` char-class metachar-named final-segment symlink
])
def test_final_segment_metachar_named_symlink_closes_whole_class(
    tmp_path: Path, linkname: str, pattern: str
) -> None:
    """#0d generalization lock: ANY glob metachar (``*`` above; ``?`` / ``[`` here)
    in a FINAL-segment glob that is ALSO a literal symlink named with that metachar
    (``l?.py`` / ``[a].py`` → ``src/existing.py``) would be dissolved by realpath
    under whole-path anchoring, erasing the pattern → false SAFE-PARALLEL. Glob-safe
    anchoring keeps the wildcard segment literal so the erasure can never happen; the
    file set is then flagged indeterminate (the surviving pattern hits the
    metachar-named symlink and trips the leaf-realpath check, or expands to nothing)
    → MUST-SERIAL regardless of the metachar — the whole class is closed."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/existing.py"])
    (pa / linkname).symlink_to("src/existing.py")  # FILE symlink whose NAME holds the metachar
    tasks = [
        cd._parse_identity(_task(pa, "t-seg", predicted_files=[pattern]), 0),
        cd._parse_identity(_task(pa, "t-newlib", predicted_files=["lib.py"]), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-seg", "t-newlib") == cd.MUST_SERIAL
    prof = cd.build_conflict_profile(
        cd._parse_identity(_task(pa, "t-seg", predicted_files=[pattern]), 0)
    )
    assert prof.files_indeterminate


def test_glob_final_wildcard_no_metachar_symlink_stays_precise(tmp_path: Path) -> None:
    """Precision guard for #0d (no over-correction): a final-segment glob with NO
    metachar-named symlink in play — plain ``src/*.py`` — must still anchor its
    STATIC prefix and stay a precisely-enumerable set, so a genuinely disjoint task
    stays SAFE-PARALLEL. Glob-safe anchoring only stops realpath from touching the
    WILDCARD segment; it must keep resolving the static prefix exactly as before
    (here ``proj-a`` ≠ ``proj-b``, so the two never collide)."""
    pa = _mkproject(tmp_path, "proj-a", files=["src/existing.py"])
    pb = _mkproject(tmp_path, "proj-b", files=["lib/other.py"])
    tasks = [
        cd._parse_identity(_task(pa, "t-glob", predicted_files=["src/*.py"], repo_branch="a@main"), 0),
        cd._parse_identity(_task(pb, "t-ok", predicted_files=["lib/other.py"], repo_branch="b@main"), 1),
    ]
    assert cd.analyze_batch(tasks).parallel_safe


# ─── #0e: glob metachar in the realpath'd STATIC PREFIX — glob.escape the
#          concrete prefix (codex R6 counter-example) ─────────────────────────


def test_glob_metachar_in_realpathd_prefix_stays_literal_is_must_serial(
    tmp_path: Path,
) -> None:
    """#0e P0 (codex R6 counter-example): glob-safe anchoring realpaths the static
    prefix, but that CONCRETE prefix may itself carry a glob metachar in its REAL
    name — a dir literally named ``real[ab]`` reached via a static symlink ``link``.
    Spliced raw, ``real[ab]`` is read as a CHARACTER CLASS: A's ``link/*.py`` pattern
    ``<pa>/real[ab]/*.py`` glob-expands onto the decoy sibling ``reala/decoy.py`` (NOT
    the true file) AND fails to ``fnmatch`` B's exact ``real[ab]/foo.py`` → the two
    file sets read as disjoint → false ``SAFE-PARALLEL``, yet at runtime A's
    ``link/*.py`` resolves through ``link``→``real[ab]`` and clobbers ``foo.py`` =
    B's file. ``glob.escape`` makes the prefix a literal (``[``→``[[]``), so the
    pattern matches the true ``real[ab]/foo.py`` = B's file → MUST-SERIAL."""
    pa = _mkproject(tmp_path, "proj-a", files=["real[ab]/foo.py", "reala/decoy.py"])
    (pa / "link").symlink_to("real[ab]")  # static symlink → dir whose REAL name holds '['
    tasks = [
        cd._parse_identity(_task(pa, "t-glob", predicted_files=["link/*.py"]), 0),
        cd._parse_identity(_task(pa, "t-exact", predicted_files=["link/foo.py"]), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-glob", "t-exact") == cd.MUST_SERIAL
    # glob.glob-side: the escaped prefix matches the TRUE file, never the decoy sibling.
    prof = cd.build_conflict_profile(
        cd._parse_identity(_task(pa, "t-glob", predicted_files=["link/*.py"]), 0)
    )
    assert any(p.endswith("real[ab]/foo.py") for p in prof.files_concrete), (
        "escaped prefix must glob-expand onto the TRUE real[ab]/ file"
    )
    assert not any("decoy.py" in p for p in prof.files_concrete), (
        "escaped prefix must NOT mis-match the decoy sibling reala/"
    )


@pytest.mark.parametrize("realname,decoy", [
    ("d*x", "dyx"),   # ``*`` in the realpath'd prefix dir's name
    ("d?x", "dyx"),   # ``?`` in the realpath'd prefix dir's name
])
def test_metachar_in_realpathd_prefix_closes_whole_class(
    tmp_path: Path, realname: str, decoy: str
) -> None:
    """#0e generalization lock: ANY glob metachar (``[`` above; ``*`` / ``?`` here) in
    the realpath'd static prefix's REAL name must be escaped to a literal. Without the
    escape A's ``link/*.py`` pattern ``<pa>/<realname>/*.py`` glob-expands onto the
    decoy sibling ``<decoy>/decoy.py`` too — a spurious extra match. ``glob.escape``
    makes the prefix the exact literal dir, so A's concrete set is EXACTLY the true
    ``<realname>/foo.py`` = B's exact file (decoy excluded) → MUST-SERIAL, and the
    whole 'metachar in the concrete prefix' class is closed regardless of metachar."""
    pa = _mkproject(tmp_path, "proj-a", files=[f"{realname}/foo.py", f"{decoy}/decoy.py"])
    (pa / "link").symlink_to(realname)  # static symlink → dir whose REAL name holds the metachar
    tasks = [
        cd._parse_identity(_task(pa, "t-glob", predicted_files=["link/*.py"]), 0),
        cd._parse_identity(_task(pa, "t-exact", predicted_files=["link/foo.py"]), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-glob", "t-exact") == cd.MUST_SERIAL
    prof = cd.build_conflict_profile(
        cd._parse_identity(_task(pa, "t-glob", predicted_files=["link/*.py"]), 0)
    )
    assert not any("decoy.py" in p for p in prof.files_concrete), (
        "escaped prefix must match only the true dir, never the decoy sibling"
    )


# ─── #1: case-insensitive filesystem path comparison ─────────────────────────


def _fs_is_case_insensitive(base: Path) -> bool:
    """Independent ground truth (separate from the production probe ``cd.
    _fs_case_insensitive``): create a mixed-case file and check whether its
    lowercased name resolves to the same entry."""
    marker = base / "CaseGroundTruthZ.tmp"
    marker.write_text("x", encoding="utf-8")
    try:
        return (base / "casegroundtruthz.tmp").exists()
    finally:
        marker.unlink()


def test_fs_case_insensitive_probe_matches_reality(tmp_path: Path) -> None:
    """#1 unit: the gate's FS-sensitivity probe agrees with on-disk reality (so it
    folds case on macOS APFS and stays exact on a case-sensitive Linux CI)."""
    assert cd._fs_case_insensitive(str(tmp_path)) == _fs_is_case_insensitive(tmp_path)


def test_predicted_files_case_only_difference_follows_fs(tmp_path: Path) -> None:
    """#1: ``src/Foo.py`` and ``src/foo.py`` are NEW (not on disk), so realpath
    can't fold their case. On a case-INSENSITIVE FS (macOS APFS) they are the SAME
    file → MUST-SERIAL; on a case-SENSITIVE FS they are genuinely distinct →
    SAFE-PARALLEL. The verdict must track the filesystem (no blanket casefold that
    would over-serialize distinct files on Linux)."""
    pa = _mkproject(tmp_path, "proj-a")
    tasks = [
        cd._parse_identity(_task(pa, "t-upper", predicted_files=["src/Foo.py"]), 0),
        cd._parse_identity(_task(pa, "t-lower", predicted_files=["src/foo.py"]), 1),
    ]
    verdict = _verdict_for(cd.analyze_batch(tasks), "t-upper", "t-lower")
    if _fs_is_case_insensitive(pa):
        assert verdict == cd.MUST_SERIAL
    else:
        assert verdict == cd.SAFE_PARALLEL


def test_distinct_name_files_stay_parallel_regardless_of_fs(tmp_path: Path) -> None:
    """Precision guard for #1: genuinely different filenames (no case-only twist)
    must stay SAFE-PARALLEL on either FS — casefolding equal-cased distinct names
    must not collide them."""
    pa = _mkproject(tmp_path, "proj-a")
    tasks = [
        cd._parse_identity(_task(pa, "t-a", predicted_files=["src/alpha.py"]), 0),
        cd._parse_identity(_task(pa, "t-b", predicted_files=["src/beta.py"]), 1),
    ]
    assert cd.analyze_batch(tasks).parallel_safe


# ─── #2: repo_branch remote-tracking aliases + malformed fail-closed ──────────


def test_normalize_repo_branch_strips_remote_tracking_prefixes() -> None:
    """#2 unit: ``refs/remotes/<remote>/`` and ``remotes/<remote>/`` spellings
    normalize to the same bare local branch; a bare slashed LOCAL branch is not a
    remote alias and must stay distinct."""
    norm = cd._normalize_repo_branch
    assert norm("main") == norm("refs/remotes/origin/main")
    assert norm("main") == norm("remotes/origin/main")
    assert norm("proj@main") == norm("proj@refs/remotes/origin/main")
    # a slashed branch UNDER a remote keeps its slashes (only the remote name drops)
    assert norm("refs/remotes/origin/feature/x")[1] == "feature/x"
    # a bare local slashed branch is NOT a remote alias → must stay distinct
    assert norm("feature/main") != norm("main")


def test_repo_branch_remote_tracking_alias_judged_same_branch_must_serial(tmp_path: Path) -> None:
    """#2 e2e: a ``refs/remotes/origin/main`` spelling must normalize to the same
    branch as ``main`` so the same-repo+branch-push rule fires → MUST-SERIAL
    (without the alias-strip the string mismatch falsely clears the pair)."""
    pa = _mkproject(tmp_path, "proj-a")
    tasks = [
        cd._parse_identity(
            _task(pa, "t-one", predicted_files=["src/one.py"], repo_branch="proj-a@main",
                  will_push=True, worktree_isolation=False), 0),
        cd._parse_identity(
            _task(pa, "t-two", predicted_files=["src/two.py"],
                  repo_branch="proj-a@refs/remotes/origin/main",
                  will_push=True, worktree_isolation=False), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-one", "t-two") == cd.MUST_SERIAL
    assert any("same repo+branch push" in r for r in analysis.pairs[0].reasons)


@pytest.mark.parametrize("malformed", ["proj-a@", "refs/heads/", "refs/remotes/origin", "@", "proj-a@heads/"])
def test_malformed_repo_branch_empty_branch_is_fail_closed(tmp_path: Path, malformed: str) -> None:
    """#2: a declaration that normalizes to an EMPTY branch (``repo@`` /
    ``refs/heads/`` / ``refs/remotes/origin`` / ``@``) is untrustworthy → taint →
    MUST-SERIAL (previously the non-empty raw value slipped through as valid)."""
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    bad = _task(pb, "t-bad", repo_branch=malformed)
    tasks = [
        cd._parse_identity(_task(pa, "t-ok", repo_branch="a@main"), 0),
        cd._parse_identity(bad, 1),
    ]
    assert _verdict_for(cd.analyze_batch(tasks), "t-ok", "t-bad") == cd.MUST_SERIAL


# ─── #3: predicted_files "unknown" sentinel → indeterminate ───────────────────


@pytest.mark.parametrize("tok", ["unknown", "UNKNOWN", "Unknown"])
def test_predicted_files_unknown_token_is_indeterminate_must_serial(tmp_path: Path, tok: str) -> None:
    """#3: ``predicted_files=["unknown"]`` (any case) means 'I don't know which
    files' → indeterminate file set (fail-closed MUST-SERIAL), NOT a concrete file
    literally named ``unknown`` (which would read as disjoint from real files →
    false SAFE-PARALLEL)."""
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b", files=["src/b.py"])
    tasks = [
        cd._parse_identity(_task(pa, "t-unknown", predicted_files=[tok], repo_branch="a@main"), 0),
        cd._parse_identity(_task(pb, "t-ok", predicted_files=["src/b.py"], repo_branch="b@main"), 1),
    ]
    analysis = cd.analyze_batch(tasks)
    assert _verdict_for(analysis, "t-unknown", "t-ok") == cd.MUST_SERIAL
    assert any("indeterminate" in r or "unknown" in r for p in analysis.pairs for r in p.reasons)


# ─── shared test fixtures ────────────────────────────────────────────────────


def _write_fake_dx_spawn(tmp_path: Path, log: Path, *, rc: int) -> Path:
    """An executable fake dx-spawn-session.sh that records its argv and exits rc.
    NEVER opens a real window — the whole point of testing with a fake."""
    f = tmp_path / "fake-dx-spawn.sh"
    f.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "FAKE-DX-SPAWN $*" >> "{log}"\n'
        'echo "[spawn] fake intent"\n'
        f"exit {rc}\n",
        encoding="utf-8",
    )
    f.chmod(0o755)
    return f


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _mkgitrepo(tmp_path: Path, name: str, files: list[str] | None = None) -> Path:
    """A project dir that IS a git repo (≥1 commit, so a worktree can be added)."""
    root = _mkproject(tmp_path, name, files)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


# ─── bug 1: singlepane same-project — actuator allows only ONE active worker ───


def test_same_singlepane_project_two_tasks_are_must_serial(tmp_path: Path) -> None:
    """🔴 bug 1 (p74 RED — singlepane blind): two tasks in the SAME project that
    resolves to ``singlepane`` isolation must be MUST-SERIAL even when their declared
    files are disjoint and they declare ``worktree_isolation=True``. A singlepane
    project may hold only ONE active worker (``spawn._active_singlepane_worker`` hard-
    REJECTs a concurrent 2nd → the whole wave exits 2); the gate must defer the 2nd,
    not fan it out to be rejected. Config is the authoritative isolation source (it
    overrides the per-task declared field)."""
    pa = _mkproject(tmp_path, "proj-a")
    cfg = _config.Config(worker_isolation={"proj-a": "singlepane"})
    tasks = [
        cd._parse_identity(_task(pa, "t-one", predicted_files=["src/one.py"],
                                 worktree_isolation=True), 0),
        cd._parse_identity(_task(pa, "t-two", predicted_files=["src/two.py"],
                                 worktree_isolation=True), 1),
    ]
    analysis = cd.analyze_batch(tasks, cfg=cfg)
    assert _verdict_for(analysis, "t-one", "t-two") == cd.MUST_SERIAL
    assert any("singlepane" in r for r in analysis.pairs[0].reasons)


def test_singlepane_same_project_second_task_deferred_not_codispatched(tmp_path: Path) -> None:
    """🔴 bug 1: the singlepane MUST-SERIAL edge must make ``compute_wave`` DEFER the
    second same-project task (keep it out of the wave) rather than co-dispatch it —
    co-dispatch is exactly what makes the actuator reject the 2nd and exit 2."""
    pa = _mkproject(tmp_path, "proj-a")
    cfg = _config.Config(worker_isolation={"proj-a": "singlepane"})
    tasks = [
        cd._parse_identity(_task(pa, "t-one", predicted_files=["src/one.py"]), 0),
        cd._parse_identity(_task(pa, "t-two", predicted_files=["src/two.py"]), 1),
    ]
    plan = cd.compute_wave(cd.analyze_batch(tasks, cfg=cfg), max_width=8)
    assert plan.wave == ["t-one"]
    assert plan.conflict_deferred == ["t-two"]


def test_worktree_project_same_two_tasks_stay_safe_parallel(tmp_path: Path) -> None:
    """Precision guard for bug 1 (no over-serialization): two same-project tasks in a
    ``worktree``-isolated project with disjoint files + no push stay SAFE-PARALLEL —
    worktree isolation + the wait=120 spawn-lock queue serialize their shared-repo git
    writes safely. The fix must key on *singlepane*, never blanket 'same project'."""
    pa = _mkproject(tmp_path, "proj-a")
    cfg = _config.Config(worker_isolation={"proj-a": "worktree"})
    tasks = [
        cd._parse_identity(_task(pa, "t-one", predicted_files=["src/one.py"],
                                 repo_branch="proj-a@main", will_push=False,
                                 worktree_isolation=True), 0),
        cd._parse_identity(_task(pa, "t-two", predicted_files=["src/two.py"],
                                 repo_branch="proj-a@main", will_push=False,
                                 worktree_isolation=True), 1),
    ]
    assert cd.analyze_batch(tasks, cfg=cfg).parallel_safe


def test_singlepane_fallback_from_declared_worktree_isolation_false(tmp_path: Path) -> None:
    """🔴 bug 1 fail-closed fallback: when config does NOT pin the project's isolation
    (``resolve_isolation`` → None), a declared ``worktree_isolation=False`` means the
    worker runs in the REAL repo (not worktree-isolated) → only one at a time → two
    same-project tasks are MUST-SERIAL. ``will_push=False`` so the same-repo-push rule
    can't fire — the verdict is owed to the singlepane fallback axis alone."""
    pa = _mkproject(tmp_path, "proj-a")
    cfg = _config.Config()  # empty → resolve_isolation returns None → declared-field fallback
    tasks = [
        cd._parse_identity(_task(pa, "t-one", predicted_files=["src/one.py"],
                                 repo_branch="proj-a@main", will_push=False,
                                 worktree_isolation=False), 0),
        cd._parse_identity(_task(pa, "t-two", predicted_files=["src/two.py"],
                                 repo_branch="proj-a@main", will_push=False,
                                 worktree_isolation=False), 1),
    ]
    analysis = cd.analyze_batch(tasks, cfg=cfg)
    assert _verdict_for(analysis, "t-one", "t-two") == cd.MUST_SERIAL
    assert any("singlepane" in r for r in analysis.pairs[0].reasons)


# ─── bug 1 (二修): registry is the isolation source the actuator routes on ─────
#
# The actuator (dx-spawn-session.sh) reads project-registry.json, NOT handoff config.
# Live config carries ``worker_isolation={"default":"worktree"}`` which would mask every
# project's true mode; the gate must resolve isolation from the registry (via an explicit
# ``DX_PROJECT_REGISTRY``) so its verdict matches what the actuator actually does.


def _write_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: dict[Path, str | None],
    *,
    name: str = "project-registry.json",
) -> Path:
    """Write a mock project-registry.json ({key: {paths:{root}, worker_isolation}}) and
    pin ``DX_PROJECT_REGISTRY`` at it (an EXPLICIT registry → strict / fail-closed on miss).
    A ``None`` isolation value omits the ``worker_isolation`` key (illegal/missing mode)."""
    projects: dict = {}
    for i, (root, iso) in enumerate(entries.items()):
        entry: dict = {"paths": {"root": str(root)}}
        if iso is not None:
            entry["worker_isolation"] = iso
        projects[f"key{i}"] = entry
    reg = tmp_path / name
    reg.write_text(json.dumps({"projects": projects}), encoding="utf-8")
    monkeypatch.setenv("DX_PROJECT_REGISTRY", str(reg))
    return reg


def test_registry_singlepane_overrides_config_default_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 bug 1 二修 (live-shape regression): the LIVE shape is config
    ``worker_isolation={"default":"worktree"}`` (which ``resolve_isolation`` returns for
    every project) while the registry marks the project ``singlepane``. The actuator routes
    on the registry, so the gate must too: two same-project tasks → MUST-SERIAL even though
    config-default says worktree. (The previous fix read config and so missed this — it only
    passed because its test injected a matching cfg the live config never has.)"""
    pa = _mkproject(tmp_path, "proj-a")
    _write_registry(tmp_path, monkeypatch, {pa: "singlepane"})
    cfg = _config.Config(worker_isolation={"default": "worktree"})  # live shape: masks the truth
    tasks = [
        cd._parse_identity(_task(pa, "t-one", predicted_files=["src/one.py"],
                                 worktree_isolation=True), 0),
        cd._parse_identity(_task(pa, "t-two", predicted_files=["src/two.py"],
                                 worktree_isolation=True), 1),
    ]
    analysis = cd.analyze_batch(tasks, cfg=cfg)
    assert _verdict_for(analysis, "t-one", "t-two") == cd.MUST_SERIAL
    assert any("singlepane" in r for r in analysis.pairs[0].reasons)


def test_registry_worktree_same_project_stays_safe_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precision guard (no over-serialization): the registry explicitly marks the project
    ``worktree`` → two same-project tasks with disjoint files + no push stay SAFE-PARALLEL
    (worktree isolation + the wait=120 spawn-lock queue serialize them safely). config-default
    is worktree too, but the registry is the one that must clear them."""
    pa = _mkproject(tmp_path, "proj-a")
    _write_registry(tmp_path, monkeypatch, {pa: "worktree"})
    cfg = _config.Config(worker_isolation={"default": "worktree"})
    tasks = [
        cd._parse_identity(_task(pa, "t-one", predicted_files=["src/one.py"],
                                 repo_branch="proj-a@main", will_push=False,
                                 worktree_isolation=True), 0),
        cd._parse_identity(_task(pa, "t-two", predicted_files=["src/two.py"],
                                 repo_branch="proj-a@main", will_push=False,
                                 worktree_isolation=True), 1),
    ]
    assert cd.analyze_batch(tasks, cfg=cfg).parallel_safe


def test_explicit_registry_missing_file_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 fail-closed: an EXPLICIT ``DX_PROJECT_REGISTRY`` pointing at a missing file is a
    misconfig the gate must not silently treat as 'no singlepane' — two same-project tasks
    are MUST-SERIAL (may-be-singlepane → 宁可多串行别漏)."""
    pa = _mkproject(tmp_path, "proj-a")
    monkeypatch.setenv("DX_PROJECT_REGISTRY", str(tmp_path / "does-not-exist.json"))
    cfg = _config.Config(worker_isolation={"default": "worktree"})
    tasks = [
        cd._parse_identity(_task(pa, "t-one", predicted_files=["src/one.py"],
                                 worktree_isolation=True), 0),
        cd._parse_identity(_task(pa, "t-two", predicted_files=["src/two.py"],
                                 worktree_isolation=True), 1),
    ]
    analysis = cd.analyze_batch(tasks, cfg=cfg)
    assert _verdict_for(analysis, "t-one", "t-two") == cd.MUST_SERIAL
    assert any("registry" in r for r in analysis.pairs[0].reasons)


def test_project_not_in_explicit_registry_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 fail-closed: an EXPLICIT registry that exists but does NOT list the project
    (drift / unregistered) → MUST-SERIAL. The actuator would reject such a spawn outright;
    the gate must not read the absence as a clearance to co-dispatch."""
    pa = _mkproject(tmp_path, "proj-a")
    other = _mkproject(tmp_path, "other-proj")
    _write_registry(tmp_path, monkeypatch, {other: "worktree"})  # registry lists 'other', not proj-a
    cfg = _config.Config(worker_isolation={"default": "worktree"})
    tasks = [
        cd._parse_identity(_task(pa, "t-one", predicted_files=["src/one.py"],
                                 worktree_isolation=True), 0),
        cd._parse_identity(_task(pa, "t-two", predicted_files=["src/two.py"],
                                 worktree_isolation=True), 1),
    ]
    assert _verdict_for(cd.analyze_batch(tasks, cfg=cfg), "t-one", "t-two") == cd.MUST_SERIAL


def test_registry_illegal_isolation_mode_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 fail-closed: a project found in the registry with a missing/illegal
    ``worker_isolation`` (here a typo'd mode) → MUST-SERIAL (mirrors dx-spawn, which errors
    out on an unrecognized mode rather than guessing)."""
    pa = _mkproject(tmp_path, "proj-a")
    _write_registry(tmp_path, monkeypatch, {pa: "worktre"})  # typo → illegal mode
    cfg = _config.Config(worker_isolation={"default": "worktree"})
    tasks = [
        cd._parse_identity(_task(pa, "t-one", predicted_files=["src/one.py"],
                                 worktree_isolation=True), 0),
        cd._parse_identity(_task(pa, "t-two", predicted_files=["src/two.py"],
                                 worktree_isolation=True), 1),
    ]
    assert _verdict_for(cd.analyze_batch(tasks, cfg=cfg), "t-one", "t-two") == cd.MUST_SERIAL


def test_derived_registry_miss_falls_back_to_config_not_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit/derived asymmetry that keeps the suite green: a registry DERIVED from
    ``$DX_SPAWN_SH`` (no explicit ``DX_PROJECT_REGISTRY``) that does NOT list the project
    falls back to config — NOT fail-closed. The suite runs with ``$DX_SPAWN_SH`` pointing at
    the live engine, so every tmp_path fixture would otherwise serialize. Here config marks
    the project worktree → the same-project pair stays SAFE-PARALLEL."""
    monkeypatch.delenv("DX_PROJECT_REGISTRY", raising=False)
    dharm = tmp_path / "dharmaxis"
    scripts = dharm / "scripts"
    scripts.mkdir(parents=True)
    fake = scripts / "dx-spawn-session.sh"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DX_SPAWN_SH", str(fake))
    # derived registry = <dharm>/project-registry.json (sibling of scripts); exists, lists 'other'
    (dharm / "project-registry.json").write_text(
        json.dumps({"projects": {
            "other": {"paths": {"root": str(tmp_path / "elsewhere")}, "worker_isolation": "singlepane"}
        }}),
        encoding="utf-8",
    )
    pa = _mkproject(tmp_path, "proj-a")
    cfg = _config.Config(worker_isolation={"proj-a": "worktree"})  # config clears it
    tasks = [
        cd._parse_identity(_task(pa, "t-one", predicted_files=["src/one.py"],
                                 repo_branch="proj-a@main", will_push=False,
                                 worktree_isolation=True), 0),
        cd._parse_identity(_task(pa, "t-two", predicted_files=["src/two.py"],
                                 repo_branch="proj-a@main", will_push=False,
                                 worktree_isolation=True), 1),
    ]
    assert cd.analyze_batch(tasks, cfg=cfg).parallel_safe


# ─── bug 1 (三修): registry NOT locatable at all → fail-close, never config ────
#
# 二修 made registry the PRIMARY isolation source, but the registry was only located
# when ``$DX_SPAWN_SH`` was set (to DERIVE the path) or ``$DX_PROJECT_REGISTRY`` was
# pinned. The 中枢's LIVE shell defaults to DX_SPAWN_SH UNSET → ``_registry_path`` returned
# ``(None, _)`` → ``_registry_isolation`` returned ``None`` → ``_effective_isolation`` fell
# back to config (live ``{"default":"worktree"}``) → a singlepane project mislabeled
# SAFE-PARALLEL in the dry-run preview table the owner reads. 三修: registry not locatable
# AT ALL = unverifiable isolation = fail-close (``ISOLATION_UNKNOWN``), NOT config fallback.


def test_registry_isolation_unlocatable_is_unknown_not_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 bug 1 三修 (unit / mirrors the brief's 实测): with BOTH ``DX_SPAWN_SH`` and
    ``DX_PROJECT_REGISTRY`` unset, the registry path can't even be DERIVED, so
    ``_registry_isolation`` must return ``ISOLATION_UNKNOWN`` (not ``None``) and
    ``_effective_isolation`` must propagate it (not fall back to config's masking
    ``worktree`` default). Pre-fix this returned ``None`` → ``"worktree"`` — the bug."""
    monkeypatch.delenv("DX_SPAWN_SH", raising=False)
    monkeypatch.delenv("DX_PROJECT_REGISTRY", raising=False)
    pa = _mkproject(tmp_path, "proj-a")
    assert cd._registry_isolation(str(pa)) == cd.ISOLATION_UNKNOWN
    # config defaults every project to worktree; the fix must NOT let that mask the
    # unverifiable isolation — _effective_isolation returns ISOLATION_UNKNOWN, not "worktree".
    cfg = _config.Config(worker_isolation={"default": "worktree"})
    assert cd._effective_isolation(cfg, str(pa), True) == cd.ISOLATION_UNKNOWN


def test_unlocatable_registry_same_project_pair_is_must_serial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 bug 1 三修 (integration / DoD #1): when the registry is NOT locatable (中枢's
    default DX_SPAWN_SH-unset shell), two same-slug tasks in a project that is REALLY
    singlepane (but unreadable because no registry can be found) → MUST-SERIAL via
    ``ISOLATION_UNKNOWN``. This proves the dry-run preview table no longer mislabels a
    singlepane same-project pair SAFE-PARALLEL. The pair is otherwise fully clean (disjoint
    files, no push, all shared dims "none") so the verdict is owed to the fail-close axis
    ALONE — pre-fix (config fallback → worktree) it read SAFE-PARALLEL."""
    monkeypatch.delenv("DX_SPAWN_SH", raising=False)
    monkeypatch.delenv("DX_PROJECT_REGISTRY", raising=False)
    pa = _mkproject(tmp_path, "proj-a")
    # The project's TRUE mode is singlepane, but the gate can't locate any registry to read
    # it; live config masks it to worktree. The fix must serialize on the unverifiability.
    cfg = _config.Config(worker_isolation={"default": "worktree"})
    tasks = [
        cd._parse_identity(_task(pa, "t-one", predicted_files=["src/one.py"],
                                 repo_branch="proj-a@main", will_push=False,
                                 worktree_isolation=True), 0),
        cd._parse_identity(_task(pa, "t-two", predicted_files=["src/two.py"],
                                 repo_branch="proj-a@main", will_push=False,
                                 worktree_isolation=True), 1),
    ]
    analysis = cd.analyze_batch(tasks, cfg=cfg)
    assert _verdict_for(analysis, "t-one", "t-two") == cd.MUST_SERIAL
    assert any("unresolved registry" in r for r in analysis.pairs[0].reasons)


# ─── bug 2: cross-slug shared git object store ────────────────────────────────


def test_shared_git_object_store_cross_slug_is_must_serial(tmp_path: Path) -> None:
    """🔴 bug 2 (p74 RED — cross-slug shared git object store): two DIFFERENT projects
    (distinct slugs) that share ONE underlying ``.git`` — here a main repo and a linked
    git-worktree of it — must be MUST-SERIAL. Their declared files are disjoint (anchored
    to different roots) and their ``repo_branch`` strings differ, so the existing axes
    read SAFE-PARALLEL; but the slug-keyed spawn lock takes two distinct lockdirs → no
    serialization → concurrent ``git fetch`` / ``worktree add`` race the shared object
    store (index.lock / packed-refs). Resolving the git common-dir catches it."""
    main_repo = _mkgitrepo(tmp_path, "main-repo", files=["src/one.py"])
    linked = tmp_path / "linked"
    _git(main_repo, "worktree", "add", "-q", str(linked))
    cfg = _config.Config(worker_isolation={"default": "worktree"})
    tasks = [
        cd._parse_identity(_task(main_repo, "t-main", predicted_files=["src/one.py"],
                                 repo_branch="main-repo@main", will_push=False), 0),
        cd._parse_identity(_task(linked, "t-link", predicted_files=["src/two.py"],
                                 repo_branch="linked@feature", will_push=False), 1),
    ]
    analysis = cd.analyze_batch(tasks, cfg=cfg)
    assert _verdict_for(analysis, "t-main", "t-link") == cd.MUST_SERIAL
    assert any("object store" in r for r in analysis.pairs[0].reasons)


def test_independent_git_repos_stay_safe_parallel(tmp_path: Path) -> None:
    """Precision guard for bug 2 (no over-serialization): two INDEPENDENT git repos
    (separate ``git init`` → distinct object stores) with disjoint files and distinct
    repo_branches stay SAFE-PARALLEL. The fix must key on a SHARED common-dir, never on
    'both are git repos'."""
    ra = _mkgitrepo(tmp_path, "repo-a", files=["src/a.py"])
    rb = _mkgitrepo(tmp_path, "repo-b", files=["src/b.py"])
    cfg = _config.Config(worker_isolation={"default": "worktree"})
    tasks = [
        cd._parse_identity(_task(ra, "t-a", predicted_files=["src/a.py"],
                                 repo_branch="repo-a@main", will_push=False), 0),
        cd._parse_identity(_task(rb, "t-b", predicted_files=["src/b.py"],
                                 repo_branch="repo-b@main", will_push=False), 1),
    ]
    assert cd.analyze_batch(tasks, cfg=cfg).parallel_safe


def test_git_common_dir_probe_failure_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 bug 2 fail-closed: if the git common-dir probe cannot RUN (git binary missing
    / unexpected error — as opposed to a clean 'not a git repository') the task is
    tainted so its pairs are MUST-SERIAL — never silently treated as 'no shared store'."""
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("git not found")

    monkeypatch.setattr(cd.subprocess, "run", _boom)
    cfg = _config.Config(worker_isolation={"default": "worktree"})
    tasks = [
        cd._parse_identity(_task(pa, "t-a", repo_branch="a@main"), 0),
        cd._parse_identity(_task(pb, "t-b", repo_branch="b@main"), 1),
    ]
    assert _verdict_for(cd.analyze_batch(tasks, cfg=cfg), "t-a", "t-b") == cd.MUST_SERIAL


# ─── malformed-input hardening (fail-closed, no raw traceback) ───────────────


@pytest.mark.parametrize("contents", ["[]", '"x"', "42", "null"])
def test_non_dict_registry_json_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str
) -> None:
    """🔴 fail-closed (no crash): a registry that is valid JSON but NOT a top-level object
    (list/scalar/null) makes ``data.get`` raise ``AttributeError``. The guard treats a
    readable-but-not-an-object registry as corrupt → ``ISOLATION_UNKNOWN`` (same class as
    the OSError/ValueError branch), never an uncaught exception."""
    pa = _mkproject(tmp_path, "proj-a")
    reg = tmp_path / "project-registry.json"
    reg.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("DX_PROJECT_REGISTRY", str(reg))
    # does NOT raise + fails closed to the unresolved sentinel
    assert cd._registry_isolation(str(pa)) == cd.ISOLATION_UNKNOWN


def test_run_with_non_dict_registry_returns_exit_fail_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 a full ``cd.run`` over a valid 2-task batch with an EXPLICIT ``[]`` (non-object)
    registry must NOT raise. With FIX 1 the gate fail-closes gracefully (the same-project
    pair → MUST-SERIAL, the dry-run still completes), so ``run`` returns an int — the KEY
    invariant is that no raw traceback escapes."""
    pa = _mkproject(tmp_path, "proj-a")
    reg = tmp_path / "project-registry.json"
    reg.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("DX_PROJECT_REGISTRY", str(reg))
    tasks = [
        _task(pa, "t-one", predicted_files=["src/one.py"], repo_branch="proj-a@main"),
        _task(pa, "t-two", predicted_files=["src/two.py"], repo_branch="proj-a@main"),
    ]
    p = _write_json(tmp_path, tasks)
    rc = cd.run(p, execute=False)  # must NOT raise
    assert isinstance(rc, int)
    # and the gate fail-closed on the unresolvable registry
    assert cd._registry_isolation(str(pa)) == cd.ISOLATION_UNKNOWN


def test_nul_byte_predicted_files_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 fail-closed (no crash): a ``predicted_files`` entry with an embedded NUL makes
    ``os.path.realpath`` raise ``ValueError('embedded null byte')``. The per-entry guard
    taints the task (``files_indeterminate`` + a ``field_issue``) so its pairs are
    MUST-SERIAL, and ``cd.run`` over such a batch returns an int rather than crashing."""
    pa = _mkproject(tmp_path, "proj-a")
    pb = _mkproject(tmp_path, "proj-b")
    bad = cd.build_conflict_profile(
        cd._parse_identity(
            _task(pa, "t-bad", predicted_files=["src/\x00bad.py"], repo_branch="proj-a@main"), 0
        )
    )
    # the bad entry tainted the profile (no raise)
    assert bad.files_indeterminate
    assert bad.field_issues

    # a 2-task batch including it is MUST-SERIAL (the indeterminate file set taints the pair)
    tasks = [
        cd._parse_identity(
            _task(pa, "t-bad", predicted_files=["src/\x00bad.py"], repo_branch="proj-a@main"), 0
        ),
        cd._parse_identity(_task(pb, "t-ok", predicted_files=["src/ok.py"], repo_branch="proj-b@main"), 1),
    ]
    assert _verdict_for(cd.analyze_batch(tasks), "t-bad", "t-ok") == cd.MUST_SERIAL

    # and ``cd.run`` over a tasks-json with the NUL entry does NOT raise
    p = _write_json(
        tmp_path,
        [
            _task(pa, "t-bad", predicted_files=["src/\x00bad.py"], repo_branch="proj-a@main"),
            _task(pb, "t-ok", predicted_files=["src/ok.py"], repo_branch="proj-b@main"),
        ],
    )
    rc = cd.run(p, execute=False)  # must NOT raise
    assert isinstance(rc, int)
