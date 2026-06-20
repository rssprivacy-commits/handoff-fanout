"""Tests for ``handoff coord-dispatch`` (coord_dispatch.py).

Covers the machine-judged concurrency-conflict gate (file overlap / same-repo
push / shared-resource intersection / fail-closed on missing-or-unknown fields /
unexpandable glob = potential conflict), the brief skeleton's welded-in hard
boundaries, and the dry-run-default vs --execute behavior. The --execute tests
use a FAKE ``DX_SPAWN_SH`` that records its argv — never a real worker window.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    rc = cd.run(p, execute=True)
    assert rc == cd.EXIT_OK
    log = fake_log.read_text()
    # both tasks dispatched, each with --project / --brief / --task-id
    assert "--task-id t-a" in log and "--task-id t-b" in log
    assert "--project " + str(pa) in log
    assert "--brief " in log
    # brief files persisted for the spawned sessions to read
    assert (home / "_dispatch_briefs" / "t-a.md").is_file()
    assert (home / "_dispatch_briefs" / "t-b.md").is_file()


def test_execute_refuses_unsafe_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    rc = cd.run(p, execute=True)
    assert rc == cd.EXIT_FAIL
    assert not fake_log.exists(), "an unsafe batch must never reach the spawn engine"


def test_execute_without_dx_spawn_env_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pa = _mkproject(tmp_path, "proj-a")
    monkeypatch.delenv("DX_SPAWN_SH", raising=False)
    monkeypatch.setenv("HANDOFF_HOME", str(tmp_path / "home"))
    p = _write_json(tmp_path, [_task(pa, "solo")])
    assert cd.run(p, execute=True) == cd.EXIT_FAIL


def test_execute_stops_on_first_dispatch_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing dx-spawn (rc!=0) → fail-closed: report + stop, exit 2."""
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
    assert cd.run(p, execute=True) == cd.EXIT_FAIL


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
