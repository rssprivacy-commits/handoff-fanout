"""Per-project gating of the three ERP-specific injection vectors (inject_blocks,
roadmap, baseline_hooks) — the leak fix.

Root cause being guarded: a SINGLE shared ``$HANDOFF_HOME/config.json`` drives every
project, and ``inject_blocks`` / ``roadmap`` / ``baseline_hooks`` were injected into EVERY
project's handoff prompt with no project gate — so an accounting system's red lines and
roadmap leaked into an unrelated e-commerce agent session (observed in the wild). These
tests pin: (a) the owning project still gets byte-identical content; (b) sibling projects
get NONE of it; (c) the new keys are backward-compatible + fail-safe.
"""

from __future__ import annotations

from pathlib import Path

from handoff_fanout import config as _config
from handoff_fanout import dump, templates

# A config shaped like the post-ratify ERP config: the accounting red lines + roadmap +
# alembic baseline hook are all scoped to ``erp-system`` only.
ERP_RED_LINE = (
    "## V3.6 红线 (不可破)\n"
    "- paid_amount 裸写 / confirmed 触发账务 / 绕过 journal_service.post_entry()"
)
GLOBAL_BLOCK = "## Global (all projects)\n- never force-push a shared branch"


def _erp_gated_config(home: Path, roadmap_path: Path) -> _config.Config:
    return _config._from_dict(
        {
            "inject_blocks": [GLOBAL_BLOCK],
            "project_inject_blocks": {"erp-system": [ERP_RED_LINE]},
            "roadmap": {"path": str(roadmap_path), "projects": ["erp-system"]},
            "baseline_hooks": [
                {
                    "name": "alembic_ver",
                    "command": ["echo", "v999_head (head)"],
                    "regex": r"\b(v\d+_\w+)\s*\(head\)",
                    "projects": ["erp-system"],
                }
            ],
        },
        home=home,
    )


def test_inject_blocks_for_scopes_per_project(tmp_path: Path) -> None:
    cfg = _erp_gated_config(tmp_path, tmp_path / "rm.md")
    # ERP gets global + its own red line; wilde-hexe gets ONLY the global block.
    assert cfg.inject_blocks_for("erp-system") == [GLOBAL_BLOCK, ERP_RED_LINE]
    assert cfg.inject_blocks_for("wilde-hexe") == [GLOBAL_BLOCK]
    assert "journal_service" not in "\n".join(cfg.inject_blocks_for("wilde-hexe"))


def test_roadmap_gated_by_project(tmp_path: Path) -> None:
    rm = tmp_path / "rm.md"
    rm.write_text(
        "#### Phase 2a wo_cost_service\n- _recalc_pool_state (V3.6 事件源派生)\n", encoding="utf-8"
    )
    cfg = _erp_gated_config(tmp_path, rm)
    assert "_recalc_pool_state" in dump.get_roadmap_excerpt(cfg, "erp-system")
    # sibling: NO ERP roadmap content — a neutral placeholder instead.
    wh = dump.get_roadmap_excerpt(cfg, "wilde-hexe")
    assert "_recalc_pool_state" not in wh
    assert "V3.6" not in wh


def test_baseline_hook_gated_by_project(tmp_path: Path) -> None:
    cfg = _erp_gated_config(tmp_path, tmp_path / "rm.md")
    ws = tmp_path  # any dir; the hook is `echo`, no repo needed
    erp_baseline = dump.detect_baseline(ws, cfg=cfg, project="erp-system")
    wh_baseline = dump.detect_baseline(ws, cfg=cfg, project="wilde-hexe")
    assert erp_baseline.get("alembic_ver") == "v999_head"
    assert "alembic_ver" not in wh_baseline  # sibling never ran the ERP hook


def test_handoff_md_no_leak_into_sibling(tmp_path: Path) -> None:
    """End-to-end: the rendered wilde-hexe handoff contains NONE of the ERP markers."""
    rm = tmp_path / "rm.md"
    rm.write_text("#### Phase 2a\n- paid_amount _recalc journal_service alembic\n", encoding="utf-8")
    cfg = _erp_gated_config(tmp_path, rm)

    def _render(project: str) -> str:
        baseline = dump.detect_baseline(tmp_path, cfg=cfg, project=project)
        return templates.build_handoff_md(
            task="t1",
            project=project,
            workspace=tmp_path,
            next_brief="do the thing",
            status="active",
            tests=None,
            baseline=baseline,
            roadmap_excerpt=dump.get_roadmap_excerpt(cfg, project),
            inject_blocks=cfg.inject_blocks_for(project),
            handoff_home=cfg.home,
            handoff_md_path=tmp_path / "t1.md",
        )

    erp_md = _render("erp-system")
    wh_md = _render("wilde-hexe")

    # ERP keeps its content (byte-presence golden).
    assert "journal_service" in erp_md
    assert "v999_head" in erp_md
    # wilde-hexe leaks NOTHING ERP-specific.
    for marker in ("paid_amount", "journal_service", "_recalc", "v999_head", "V3.6 红线"):
        assert marker not in wh_md, f"LEAK: {marker!r} found in wilde-hexe handoff"
    # but the truly-global block IS present for both.
    assert "force-push a shared branch" in erp_md
    assert "force-push a shared branch" in wh_md


def test_backward_compat_ungated_applies_to_all(tmp_path: Path) -> None:
    """A legacy config (no `projects`, no project_inject_blocks) injects into every project
    identically — byte-identical to pre-gating behaviour."""
    rm = tmp_path / "rm.md"
    rm.write_text("#### Phase 1\n- legacy roadmap line\n", encoding="utf-8")
    cfg = _config._from_dict(
        {
            "inject_blocks": ["## legacy global block"],
            "roadmap": {"path": str(rm)},  # no `projects` => all
            "baseline_hooks": [{"name": "pyver", "command": ["echo", "ok"]}],  # no `projects`
        },
        home=tmp_path,
    )
    for project in ("erp-system", "wilde-hexe", "anything"):
        assert cfg.inject_blocks_for(project) == ["## legacy global block"]
        assert "legacy roadmap line" in dump.get_roadmap_excerpt(cfg, project)
        assert dump.detect_baseline(tmp_path, cfg=cfg, project=project).get("pyver") == "ok"


def test_project_inject_blocks_degenerate_shapes_fail_safe(tmp_path: Path) -> None:
    # Any non-dict / degenerate shape => {} (no extra blocks). It can NEVER inject the
    # WRONG project's blocks; worst case is "no project-specific block".
    for bad in ("erp-system", ["erp-system"], 123, None):
        cfg = _config._from_dict({"project_inject_blocks": bad}, home=tmp_path)
        assert cfg.project_inject_blocks == {}
        assert cfg.inject_blocks_for("erp-system") == []
    # within a dict, only str-slug -> list-of-nonempty-str survives
    cfg = _config._from_dict(
        {
            "project_inject_blocks": {
                "erp-system": ["good", "", "  ", 5],
                "": ["dropped-empty-slug"],
                "bad-val": "not-a-list",
            }
        },
        home=tmp_path,
    )
    assert cfg.project_inject_blocks == {"erp-system": ["good"]}


# ---------------------------------------------------------------------------
# A2 — singlepane self-continuation carries ``--self-task <this-task>``.
#
# Root cause: ``build_handoff_md`` only injected ``{wt_args}`` (worktree's
# ``--project/--workspace``); singlepane got nothing, so the §-1 self-continuation
# commands could not resolve the spawner anchor (Tier-2) and fell back to the static
# desktop table. Fix: dump computes ``self_task_args = " --self-task <task>"`` iff
# ``cfg.resolve_isolation(project) == "singlepane"`` and threads it into the three §-1
# command blocks (precheck / dump / audit-run+audit-disposition+audit-close).
#
# Invariant pinned here: ANY non-singlepane mode (worktree / multiwindow / default /
# unconfigured) → ``""`` → byte-identical handoff.md (worktree path is golden-locked).
# ---------------------------------------------------------------------------

# The three §-1 command tokens each generated handoff carries; with the fix, a singlepane
# self-continuation appends ``--self-task <task>`` to each.
_SELF_TASK_BLOCK_PREFIXES = (
    "handoff precheck",
    "handoff dump",
    "handoff audit-run",
    "handoff audit-disposition",
    "handoff audit-close",
)


def _render_handoff_for(cfg: _config.Config, project: str, task: str, tmp_path: Path) -> str:
    """Render exactly as ``dump`` does, deriving ``self_task_args`` from the effective
    isolation mode (the same one-liner that lives at both real call sites)."""
    self_task_args = (
        f" --self-task {task}" if cfg.resolve_isolation(project) == "singlepane" else ""
    )
    return templates.build_handoff_md(
        task=task,
        project=project,
        workspace=tmp_path,
        next_brief="do the thing",
        status="active",
        tests=None,
        baseline={"git_head": "abc123"},
        roadmap_excerpt="(none)",
        inject_blocks=[],
        handoff_home=cfg.home,
        handoff_md_path=tmp_path / f"{task}.md",
        self_task_args=self_task_args,
    )


def test_singlepane_handoff_carries_self_task(tmp_path: Path) -> None:
    """A singlepane project's §-1 precheck / dump / audit-close blocks each carry
    ``--self-task <this-session's-task>`` (value = THIS task, not next-task)."""
    cfg = _config._from_dict(
        {"worker_isolation": {"wilde-hexe": "singlepane"}}, home=tmp_path
    )
    md = _render_handoff_for(cfg, "wilde-hexe", "sw-coord-p64", tmp_path)
    # The self-task token threads its OWN task id (the handoff being generated).
    assert " --self-task sw-coord-p64" in md
    # Each §-1 command line that carries --task <next-task-id> now also carries --self-task.
    for line in md.splitlines():
        if any(line.lstrip().startswith(p) for p in _SELF_TASK_BLOCK_PREFIXES):
            if "--task <next-task-id>" in line:
                assert "--self-task sw-coord-p64" in line, f"missing --self-task on: {line!r}"


def test_worktree_handoff_has_no_self_task(tmp_path: Path) -> None:
    """A worktree project (legacy fallback) → not singlepane → NO --self-task anywhere."""
    cfg = _config._from_dict({"worktree_projects": ["erp-system"]}, home=tmp_path)
    md = _render_handoff_for(cfg, "erp-system", "erp-t1", tmp_path)
    assert "--self-task" not in md


def test_default_and_unconfigured_handoff_have_no_self_task(tmp_path: Path) -> None:
    """default-key=multiwindow and a wholly-unconfigured project → not singlepane →
    NO --self-task (only singlepane opts in)."""
    cfg = _config._from_dict(
        {"worker_isolation": {"default": "multiwindow"}}, home=tmp_path
    )
    assert "--self-task" not in _render_handoff_for(cfg, "anything", "t1", tmp_path)

    cfg_empty = _config._from_dict({}, home=tmp_path)
    # resolve_isolation -> None (fail-closed) -> not "singlepane" -> no self-task.
    assert "--self-task" not in _render_handoff_for(cfg_empty, "unconfigured", "t1", tmp_path)


def test_worktree_byte_identity_with_empty_self_task(tmp_path: Path) -> None:
    """The golden lock: passing ``self_task_args=""`` (the non-singlepane value) renders
    BYTE-IDENTICAL to omitting the new kwarg entirely (the pre-A2 caller signature). This
    is what guarantees the worktree handoff is unchanged."""

    # Explicit literal kwargs (no heterogeneous ``**dict`` unpack — that defeats Pyright's
    # per-argument narrowing). ``self_task_args`` is the ONLY varying input: None ==> omit
    # the kwarg entirely (exercise the default ""); "" ==> pass it explicitly.
    def _render_worktree(self_task_args: str | None) -> str:
        if self_task_args is None:
            return templates.build_handoff_md(
                task="erp-t1",
                project="erp-system",
                workspace=tmp_path,
                next_brief="do the thing",
                status="active",
                tests=None,
                baseline={"git_head": "abc123"},
                roadmap_excerpt="(none)",
                inject_blocks=[],
                handoff_home=tmp_path,
                handoff_md_path=tmp_path / "erp-t1.md",
                worktree_info={"status": "created"},
            )
        return templates.build_handoff_md(
            task="erp-t1",
            project="erp-system",
            workspace=tmp_path,
            next_brief="do the thing",
            status="active",
            tests=None,
            baseline={"git_head": "abc123"},
            roadmap_excerpt="(none)",
            inject_blocks=[],
            handoff_home=tmp_path,
            handoff_md_path=tmp_path / "erp-t1.md",
            worktree_info={"status": "created"},
            self_task_args=self_task_args,
        )

    legacy = _render_worktree(None)  # no self_task_args (default "")
    explicit_empty = _render_worktree("")
    assert legacy == explicit_empty
    # And the worktree --project/--workspace injection is still present (not regressed).
    assert "--project erp-system" in legacy
    assert "--self-task" not in legacy
