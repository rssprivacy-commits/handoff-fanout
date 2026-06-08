"""Per-user / per-project configuration loaded from ``$HANDOFF_HOME/config.json``.

The original ERP scripts hardcoded a number of project-specific values:
the V3.6 redlines, ``docker compose exec ... alembic current`` for baseline
detection, the path of a particular roadmap file, the VS Code URI template.
This module pulls those out so the same engine can drive any project.

All fields have sensible defaults, so an empty / missing config file is fine
and the tool works out-of-the-box for ad-hoc use.

Resolution order:
  1. ``$HANDOFF_HOME/config.json`` if it exists and parses.
  2. Built-in defaults (no inject_blocks, no baseline hooks, no roadmap).

The home directory itself is resolved from ``$HANDOFF_HOME`` (env var) or
defaults to ``~/.handoff``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOME = "~/.handoff"
CONFIG_FILENAME = "config.json"
DEFAULT_URI_TEMPLATE = "vscode://anthropic.claude-code/open?prompt={prompt}"
DEFAULT_WORKSPACE_ROOT = "~/Projects"


@dataclass
class HookSpec:
    """A baseline-extension hook.

    ``name`` is the key under which the captured output is stored in the
    ``baseline`` dict returned by ``dump.detect_baseline``. ``command`` is
    passed straight to ``subprocess.run``. ``regex`` is an optional pattern
    whose first match group becomes the stored value (useful for trimming
    long output down to a version string).
    """

    name: str
    command: list[str]
    regex: str | None = None
    # Project slugs this baseline hook applies to. EMPTY = all projects (legacy /
    # byte-identical). A project-SPECIFIC hook (e.g. ERP's ``docker compose exec api
    # alembic current``, which only makes sense in the ERP repo) MUST list its project
    # here so it does not run for — and leak its output into — sibling projects' dumps,
    # because a single ``$HANDOFF_HOME/config.json`` is shared by every project. Mirrors
    # ``PreflightSpec.projects``.
    projects: tuple[str, ...] = ()


@dataclass
class RoadmapSpec:
    """Optional roadmap file whose phase sections are excerpted into prompts."""

    path: str | None = None
    section_regex: str = r"#### Phase[^\n]*\n(.*?)(?=\n#### |\Z)"
    max_sections: int = 2
    max_chars_per_section: int = 1200
    fallback_tail_chars: int = 3000
    # Project slugs this roadmap applies to. EMPTY = all projects (legacy). A
    # project-SPECIFIC roadmap (e.g. ERP's accounting roadmap) MUST list its project so
    # its phase excerpts are not injected into sibling projects' handoff prompts. Mirrors
    # ``PreflightSpec.projects`` / ``HookSpec.projects``.
    projects: tuple[str, ...] = ()


@dataclass
class PreflightSpec:
    """A project-scoped pre-dump gate command (generic / progress-agnostic).

    ``handoff dump`` runs each spec whose ``statuses`` includes the dump status
    as a HARD pre-req before producing the closure artifact. A non-zero exit
    (or a command that times out / cannot launch) FAILS CLOSED and blocks the
    dump. The engine never interprets what the command checks — it only runs
    what the project configured (e.g. ERP's ``progress_pending.py --gate``).

    Absent config ⇒ empty list ⇒ zero impact on projects that don't opt in.
    """

    name: str
    command: list[str]
    timeout: float = 30.0
    statuses: tuple[str, ...] = ("active", "done")
    # What to do when the command cannot be LAUNCHED or times out (infra failure,
    # NOT the gate's verdict). ``"block"`` (default) fails closed — right for a
    # security / de-id gate. ``"warn"`` fails open with a LOUD message — right for
    # a reminder / drift gate that must not brick every closure if its interpreter
    # path breaks (I8: dev-convenience checks fail-open). A command that RUNS and
    # exits non-zero is the gate's verdict and ALWAYS blocks regardless of this.
    on_error: str = "block"
    # Project slugs this gate applies to. EMPTY = all projects. Because a single
    # ``$HANDOFF_HOME/config.json`` is shared by every project under that home,
    # a project-specific gate (e.g. ERP's progress gate, which runs a script
    # bound to the ERP repo) MUST list its project here so it does not run for —
    # and block — sibling projects' dumps (dharmaxis / rakeforge / ...).
    projects: tuple[str, ...] = ()


@dataclass
class Config:
    home: Path = field(default_factory=lambda: Path(DEFAULT_HOME).expanduser())
    inject_blocks: list[str] = field(default_factory=list)
    # Per-project inject blocks (additive, default empty = no-op for legacy configs).
    # ``inject_blocks`` are TRULY GLOBAL (every project's prompt). A block that is
    # project-SPECIFIC (e.g. ERP's accounting red lines) belongs under its slug here so
    # it is NOT leaked into sibling projects' agent sessions. The effective set for a
    # dump is ``inject_blocks_for(project)`` = global ``inject_blocks`` + this slug's list.
    # Chosen as an additive dict (over making ``inject_blocks`` entries str|object) so the
    # existing ``inject_blocks`` field semantics — and every caller/test that constructs
    # ``Config(inject_blocks=[...])`` — are byte-identical untouched.
    project_inject_blocks: dict[str, list[str]] = field(default_factory=dict)
    baseline_hooks: list[HookSpec] = field(default_factory=list)
    dump_preflight_commands: list[PreflightSpec] = field(default_factory=list)
    roadmap: RoadmapSpec = field(default_factory=RoadmapSpec)
    uri_template: str = DEFAULT_URI_TEMPLATE
    workspace_root: Path = field(default_factory=lambda: Path(DEFAULT_WORKSPACE_ROOT).expanduser())
    # ── Single-pane (non-worktree) spawn windows (opt-in / default OFF) ────────────
    # Projects listed here get a default single-EDITOR-PANE VS Code window on spawn WITHOUT
    # git-worktree isolation: the dump generates an out-of-tree ``.handoff.code-workspace``
    # (folders→the real project dir; window.title carries the task) + a ``queue/<task>.singlepane``
    # sidecar; the watchdog opens that file so the handoff-helper extension collapses the side
    # bars on load. Fail-OPEN parse (a degenerate value → empty list = no project opts in = the
    # safe default — unlike the security mandate, an accidental empty here just means "no single
    # pane", never a leak). Distinct from ``worktree_projects`` (which gives single-pane AND git
    # isolation but imposes the merge-back protocol — too heavy for a non-technical owner's Node app).
    singlepane_projects: list[str] = field(default_factory=list)
    # Opt-in codex-audit repo-identity allowlist (Phase D P1 hardening). When the
    # ``audit_code_repos`` KEY is present, the audit gate accepts a cross-repo
    # ``code_repo`` ONLY if its realpath matches one of these (realpath-normalized)
    # absolute paths — closing the wrong-repo selector. ``audit_allowlist_configured``
    # records key PRESENCE so a key present-but-empty (all entries mis-written /
    # filtered) fails CLOSED instead of silently degrading to unrestricted. Key
    # absent → unconfigured → no restriction (the cross-repo anchor's friction +
    # single-user trust disclaimer still apply).
    audit_code_repos: list[str] = field(default_factory=list)
    audit_allowlist_configured: bool = False
    # Opt-in root-commit-SHA lineage allowlist (Phase D P1 hardening / owner ruling).
    # Stronger than ``audit_code_repos``: binds a cross-repo ``code_repo`` to its
    # root-commit LINEAGE (path-independent — a repo that moved still passes; a
    # different repo reusing an allowed path is rejected). HONEST SCOPING (like the
    # owner_ack non-crypto disclaimer): a root SHA names a lineage family, NOT a unique
    # repo — a fork/clone sharing the allowlisted first commit shares the identity
    # (acceptable single-user: such a fork IS a copy of that lineage). When the
    # ``audit_code_repo_roots`` KEY is present the gate accepts a cross-repo
    # ``code_repo`` ONLY if EVERY root reachable from its HEAD is listed (subset, so a
    # merge of unrelated history carrying one allowed root is rejected). Independent of
    # the path allowlist: both configured → BOTH must pass (never weakens). Key
    # present-but-empty fails CLOSED; key absent → unconfigured → no root restriction.
    audit_code_repo_roots: list[str] = field(default_factory=list)
    audit_code_roots_configured: bool = False
    # Opt-in project-scoped mandate roll-out (cross-project blast-radius control).
    # The retro/audit mandates live in GLOBAL env (``HANDOFF_RETRO_MANDATE`` /
    # ``HANDOFF_AUDIT_MANDATE``), but a single shared ``$HANDOFF_HOME/config.json``
    # drives every project under that home. When ``mandate_projects`` is a NON-EMPTY
    # list of slugs, only those projects enforce the env mandate on a no-evidence dump;
    # unlisted siblings take the legacy (no-gate) path. This lets the global dump entry
    # route to the engine without bricking not-yet-migrated projects' auto-continue
    # chains. FAIL-CLOSED (codex R2-P1): every degenerate shape — key absent, ``[]``,
    # a bare string typo, or all-invalid entries — leaves ``mandate_projects_configured``
    # False ⇒ enforce everywhere (an accidental empty must never silently disable the
    # mandate). See ``_parse_mandate_projects``. An explicit ``--retro-evidence`` or
    # ``HANDOFF_RETRO_BYPASS`` always runs the gate regardless of this list.
    mandate_projects: list[str] = field(default_factory=list)
    mandate_projects_configured: bool = False
    # ── Per-session git worktree isolation (opt-in / default OFF) ──────────────
    # Each spawned session works in its own ``git worktree`` instead of the shared
    # main tree, so one session's ``git stash`` / ``reset --hard`` / pytest can't
    # clobber another's. See ``worktree.py`` + design-per-session-worktree-isolation.
    # ``worktree_mode``: "off" (default / byte-identical) | "report" (log what WOULD
    # happen, mutate nothing) | "on" (create worktrees). Env ``HANDOFF_WORKTREE_ISOLATION``
    # + sentinels override (resolved in ``worktree.resolve_mode``); this is the config
    # floor. ``worktree_projects`` is a per-project allow-list that flips mode to "on"
    # for listed projects (fail-OPEN — an accidental empty must NOT enable a tree-mutating
    # feature globally, unlike the security mandate list). ``worktree_link_files`` are
    # gitignored paths symlinked into a fresh worktree so the session has them
    # (``.env`` DB creds, ``.claude`` settings); ``.venv`` is linked separately (it can
    # defeat isolation for editable-self-installed projects — see design §8.2 R1-X3).
    worktree_mode: str = "off"
    worktrees_root: Path | None = None  # default: home/<project>/worktrees
    worktree_branch_prefix: str = "handoff/"
    worktree_link_files: list[str] = field(default_factory=lambda: [".env", ".claude"])
    worktree_link_venv: bool = True
    worktree_default_branch: str | None = None  # explicit integration-branch override
    worktree_projects: list[str] = field(default_factory=list)

    def inject_blocks_for(self, project: str) -> list[str]:
        """Effective inject blocks for ``project`` = global + this project's blocks.

        Global ``inject_blocks`` apply to EVERY project; ``project_inject_blocks[project]``
        only to that one. Order: global first, then project-specific (so a project's own
        blocks read last in the prompt). A project with no specific blocks gets only the
        global set — byte-identical to the pre-gating behaviour when ``inject_blocks`` held
        the (then-global) blocks.
        """
        return list(self.inject_blocks) + list(self.project_inject_blocks.get(project, []))

    def queue_dir(self, project: str) -> Path:
        return self.home / project / "queue"

    def batches_dir(self, project: str) -> Path:
        return self.home / project / "batches"

    def ack_dir(self, project: str) -> Path:
        return self.home / project / "ack"

    def launched_dir(self, project: str) -> Path:
        return self.home / project / "launched"


def home_dir() -> Path:
    """Return the active handoff home, honouring ``$HANDOFF_HOME``."""
    return Path(os.environ.get("HANDOFF_HOME", DEFAULT_HOME)).expanduser()


def load(home: Path | None = None) -> Config:
    """Load config from ``home/config.json`` (or defaults if absent/invalid)."""
    if home is None:
        home = home_dir()
    cfg_path = home / CONFIG_FILENAME
    if not cfg_path.exists():
        return Config(home=home)
    try:
        raw = cfg_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return Config(home=home)
    return _from_dict(data, home=home)


def _from_dict(data: dict, home: Path) -> Config:
    roadmap_data = data.get("roadmap", {}) or {}
    roadmap = RoadmapSpec(
        path=roadmap_data.get("path"),
        section_regex=roadmap_data.get("section_regex", RoadmapSpec.section_regex),
        max_sections=int(roadmap_data.get("max_sections", RoadmapSpec.max_sections)),
        max_chars_per_section=int(
            roadmap_data.get("max_chars_per_section", RoadmapSpec.max_chars_per_section)
        ),
        fallback_tail_chars=int(
            roadmap_data.get("fallback_tail_chars", RoadmapSpec.fallback_tail_chars)
        ),
        projects=tuple(str(s) for s in (roadmap_data.get("projects", ()) or ()) if s),
    )
    hooks_raw = data.get("baseline_hooks", []) or []
    baseline_hooks = [
        HookSpec(
            name=h["name"],
            command=list(h["command"]),
            regex=h.get("regex"),
            projects=tuple(str(s) for s in (h.get("projects", ()) or ()) if s),
        )
        for h in hooks_raw
    ]
    preflight_raw = data.get("dump_preflight_commands", []) or []
    dump_preflight_commands = [
        PreflightSpec(
            name=str(p.get("name", "preflight")),
            command=[str(c) for c in p["command"]],
            timeout=float(p.get("timeout", 30.0)),
            statuses=tuple(str(s) for s in p.get("statuses", ("active", "done"))),
            on_error=("warn" if str(p.get("on_error", "block")) == "warn" else "block"),
            projects=tuple(str(s) for s in (p.get("projects", ()) or ())),
        )
        for p in preflight_raw
        if isinstance(p, dict) and p.get("command")
    ]
    workspace_root_raw = data.get("workspace_root", DEFAULT_WORKSPACE_ROOT)
    return Config(
        home=home,
        inject_blocks=list(data.get("inject_blocks", []) or []),
        project_inject_blocks=_parse_project_inject_blocks(data),
        baseline_hooks=baseline_hooks,
        dump_preflight_commands=dump_preflight_commands,
        roadmap=roadmap,
        uri_template=data.get("uri_template", DEFAULT_URI_TEMPLATE),
        workspace_root=Path(workspace_root_raw).expanduser(),
        # Guard ``isinstance(list)`` FIRST (like worktree_projects): a bare-string typo
        # ``"wilde-hexe"`` must NOT iterate into chars ``['w','i',...]`` (the mandate-parser
        # footgun). Any non-list → [] = no project opts in (fail-open, safe default).
        singlepane_projects=(
            [
                str(p)
                for p in data.get("singlepane_projects")
                if isinstance(p, str) and p
            ]
            if isinstance(data.get("singlepane_projects"), list)
            else []
        ),
        audit_code_repos=[
            str(r) for r in (data.get("audit_code_repos", []) or []) if isinstance(r, str) and r
        ],
        audit_allowlist_configured="audit_code_repos" in data,
        audit_code_repo_roots=[
            str(r)
            for r in (data.get("audit_code_repo_roots", []) or [])
            if isinstance(r, str) and r
        ],
        audit_code_roots_configured="audit_code_repo_roots" in data,
        **_parse_mandate_projects(data),
        **_parse_worktree(data),
    )


def _parse_project_inject_blocks(data: dict) -> dict[str, list[str]]:
    """Parse ``project_inject_blocks`` (``{slug: [block, ...]}``), defensively.

    Only a dict maps to gated blocks; any non-dict (absent / typo / list) yields ``{}``
    (= no project-specific blocks = byte-identical legacy behaviour). Within it, only
    string slugs with a list of non-empty string blocks survive — a degenerate entry is
    dropped, never crashes the load (config parse failures silently fall back to defaults,
    so a hard raise here could strip a project's blocks repo-wide). FAIL-SAFE: a bad shape
    simply means "no extra blocks", it can never inject the WRONG project's blocks.
    """
    raw = data.get("project_inject_blocks")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for slug, blocks in raw.items():
        if not isinstance(slug, str) or not slug:
            continue
        if not isinstance(blocks, list):
            continue
        cleaned = [str(b) for b in blocks if isinstance(b, str) and b.strip()]
        if cleaned:
            out[slug] = cleaned
    return out


def _parse_worktree(data: dict) -> dict:
    """Parse the worktree-isolation config block (all optional, safe defaults).

    ``worktree_mode`` is clamped to the known enum (anything unknown → "off", the
    fail-safe). ``worktree_projects`` is a permissive list of slug strings (fail-OPEN:
    a degenerate value just yields an empty list = no project flipped on — never the
    security-mandate's fail-closed semantics, since enabling a tree-mutating feature on
    an accidental empty would be the wrong default). ``worktrees_root`` expands ``~``.
    """
    mode_raw = str(data.get("worktree_mode", "off")).strip().lower()
    mode = mode_raw if mode_raw in ("off", "report", "on") else "off"
    root_raw = data.get("worktrees_root")
    worktrees_root = Path(str(root_raw)).expanduser() if root_raw else None
    link_files_raw = data.get("worktree_link_files")
    if isinstance(link_files_raw, list):
        link_files = [str(f) for f in link_files_raw if isinstance(f, str) and f]
    else:
        link_files = [".env", ".claude"]
    projects_raw = data.get("worktree_projects")
    projects = (
        [str(p) for p in projects_raw if isinstance(p, str) and p]
        if isinstance(projects_raw, list)
        else []
    )
    default_branch_raw = data.get("worktree_default_branch")
    default_branch = (
        str(default_branch_raw)
        if isinstance(default_branch_raw, str) and default_branch_raw
        else None
    )
    return {
        "worktree_mode": mode,
        "worktrees_root": worktrees_root,
        "worktree_branch_prefix": str(data.get("worktree_branch_prefix", "handoff/")),
        "worktree_link_files": link_files,
        "worktree_link_venv": bool(data.get("worktree_link_venv", True)),
        "worktree_default_branch": default_branch,
        "worktree_projects": projects,
    }


def _parse_mandate_projects(data: dict) -> dict:
    """Fail-CLOSED parse of ``mandate_projects`` (codex R2-P1 footgun fix).

    Scoping is honored ONLY when the value is a NON-EMPTY list of non-empty strings.
    Every degenerate shape → ``configured=False`` → enforce everywhere (never silently
    disables the mandate):
      * key absent                       → enforce everywhere
      * ``[]`` (empty list)              → enforce everywhere (NOT opt-out — a security
                                           mandate must fail closed on an accidental empty)
      * ``"erp-system"`` (string, typo)  → enforce everywhere (do NOT iterate chars)
      * ``["", null, 123]`` (all invalid)→ enforce everywhere
    To genuinely disable enforcement, unset the env mandate — not via this key.
    """
    raw = data.get("mandate_projects")
    projects = [str(p) for p in raw if isinstance(p, str) and p] if isinstance(raw, list) else []
    return {
        "mandate_projects": projects,
        "mandate_projects_configured": bool(projects),
    }
