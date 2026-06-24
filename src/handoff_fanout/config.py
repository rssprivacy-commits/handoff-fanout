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
import sys
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
    # ── Unified spawn-window mechanism (default ON / killable) ────────────────────
    # Global kill-switch for the unified spawn-window mechanism (dedicated
    # ``.handoff.code-workspace`` + ``code -n`` + spawn_nonce title gate). Default ON;
    # ``false`` routes spawns down the legacy fallback path (design §8). Named
    # ``unified_spawn_enabled`` (NOT ``singlepane.enabled``) to avoid confusion with the
    # *isolation* mode also called "singlepane" (design §2.1 naming fix).
    unified_spawn_enabled: bool = True
    # Per-project worker isolation mode — an EXPLICIT, auditable choice, NEVER guessed
    # (design §2.2 R2r2-S). ``{slug: "worktree" | "singlepane"}``. A project absent from
    # this map ⇒ ``worker_isolation_for`` returns ``None`` ⇒ the CALLER must fail closed
    # (no silent default to either mode — picking wrong = parallel clobber OR broken
    # isolation). Only the two known modes survive parsing; a typo'd / non-string value is
    # dropped (→ None → caller fail-closed) so a config slip can't route a spawn down an
    # unrecognized path. Distinct from the legacy ``worktree_projects`` /
    # ``singlepane_projects`` lists (which this consolidates as spawns migrate).
    worker_isolation: dict[str, str] = field(default_factory=dict)
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
    # ── spawn-unification Step 4: per-project anchor fail-closed roll-out ──────────
    # Three-phase, per-project enforcement of «a coordinator dispatch whose spawner
    # anchor cannot be resolved is REFUSED, not silently landed on the wrong desktop»
    # (design Step 4 §4.1). DEFAULT = warn (zero behavior change): a project absent
    # from BOTH lists takes the legacy fail-open omit path. ``spawner_anchor_dry_run_projects``
    # runs the full new decision but only RECORDS a would-block (LOG_BLOCK_INTENT) — the
    # ≥24h shadow buffer before flipping real block. ``spawner_anchor_enforce_projects`` is
    # the real fail-closed phase. A project in BOTH → enforce wins (take the stricter, codex
    # R2). ``spawner_anchor_system_allow`` is the SINGLE source of a system-origin无锚 exemption
    # (cron/bootstrap headless entrypoints) — a config list (NOT inheritable, no token; design
    # §2.2 v3). Unlike ``mandate_projects`` (a security mandate that fails CLOSED on an empty
    # list = enforce-everywhere), these default OPEN (warn): an accidental empty must NOT flip
    # every project to fail-closed mid-roll-out (禁静默 behavior-change). ``*_configured`` mirrors
    # ``mandate_projects_configured`` (records whether ANY project is explicitly enforced) so the
    # «空列表 vs 缺键» distinction is observable. The fail-CLOSED direction lives in
    # ``config_trusted``: a present-but-corrupt config (parse/schema failure) must NEVER silently
    # degrade an enforce-able dispatch to warn (codex R2 «一个 config 坏 = 防线静默消失»).
    spawner_anchor_dry_run_projects: list[str] = field(default_factory=list)
    spawner_anchor_enforce_projects: list[str] = field(default_factory=list)
    spawner_anchor_enforce_configured: bool = False
    spawner_anchor_system_allow: list[str] = field(default_factory=list)
    # ── B1 (learning-loop component 6 / L1): retrieval-pull ENFORCE roll-out ──────
    # A COORDINATOR ACTIVE handoff whose retro evidence carries NO
    # ``predecessor_lesson_backref`` AND NO ``no_novel_lesson_attested`` disposition is
    # REFUSED — the closing coordinator must read its predecessor's lesson + record a
    # backref, or honestly attest there was no novel lesson. DEFAULT-OFF (empty list =
    # warn/no-op fleet-wide, like the Step 4 anchor lists — NOT ``mandate_projects``: an
    # accidental empty must never flip every chain to fail-closed). Owner flips a project
    # (or ``"*"``) in. Emergency one-key rollback = a kill-switch sentinel file (see
    # ``dump._retrieval_pull_enforce_enabled``), so a fleet-wide misfire is disabled WITHOUT
    # a config edit. Fail-SAFE-OFF: a corrupt config / unreadable evidence never blocks.
    retrieval_pull_enforce_projects: list[str] = field(default_factory=list)
    # ``True`` when this Config was parsed from a trusted source (absent file = clean defaults,
    # or a present file that read + parsed). A present-but-untrustworthy config (unreadable /
    # corrupt JSON) sets this ``False`` via ``_fail_closed_config`` so the Step 4 anchor decision
    # fails CLOSED instead of reading the (now-empty) enforce list as "warn everywhere" (design
    # §4.1 config fail-safe). Distinct from ``unified_spawn_enabled``: an EXPLICIT owner kill
    # (``unified_spawn_enabled: false``) parses fine and stays ``config_trusted=True``.
    config_trusted: bool = True
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
    # ── §6c worker worktree reclaim (contract v4, frozen 2026-06-10) ────────────
    # ``reclaim_ack_timeout``: seconds the producer waits (cross-tick, never a sleep)
    # for the extension's done/failed ack before releasing the lock with
    # ``reclaim_failed(reason=ack-timeout)`` (C6; configurable per gemini SHOULD —
    # remote/SSH high-latency hosts raise it). ``canonical_remote``: explicit remote
    # whose ``refs/remotes/<remote>/<int>`` is the merged-judgment truth (C1 gemini M1:
    # never hardcode origin under fork workflows; resolution order config >
    # ``branch.<int>.remote`` > origin). ``reclaim_probe_idle_sec``: a transcript
    # *.jsonl mtime younger than this ⇒ LIVE session, never close (C6).
    # ``reclaim_probe_disabled``: explicit owner opt-out of the live probe (gemini
    # SHOULD: the probe depends on Claude's local dir layout; disabling is the owner
    # accepting force-close risk — never a silent fallback).
    reclaim_ack_timeout: float = 30.0
    canonical_remote: str | None = None
    reclaim_probe_idle_sec: float = 600.0
    reclaim_probe_disabled: bool = False

    def inject_blocks_for(self, project: str) -> list[str]:
        """Effective inject blocks for ``project`` = global + this project's blocks.

        Global ``inject_blocks`` apply to EVERY project; ``project_inject_blocks[project]``
        only to that one. Order: global first, then project-specific (so a project's own
        blocks read last in the prompt). A project with no specific blocks gets only the
        global set — byte-identical to the pre-gating behaviour when ``inject_blocks`` held
        the (then-global) blocks.
        """
        return list(self.inject_blocks) + list(self.project_inject_blocks.get(project, []))

    def worker_isolation_for(self, project: str) -> str | None:
        """Explicit worker isolation mode for ``project``, or ``None`` if unset.

        ``None`` means the caller MUST fail closed (design §2.2 / §8: never guess an
        isolation mode — the wrong one is a parallel-clobber or broken-isolation bug).

        EXPLICIT-only by design: this is the accessor for the dump-time concurrency guard
        (``singlepane_worker_guard``), which must NOT change its firing under a config
        migration. The unified EFFECTIVE accessor (with legacy fallback) is
        ``resolve_isolation`` — keep these two distinct.
        """
        return self.worker_isolation.get(project)

    def resolve_isolation(self, project: str) -> str | None:
        """EFFECTIVE worker isolation mode for ``project`` (Step 6 unified accessor).

        Precedence (design §2.2 / Step 6 «配置合一»):
          a. explicit ``worker_isolation[project]`` (an auditable per-project choice);
          b. else the reserved global ``worker_isolation["default"]`` key;
          c. else the **legacy fallback (DEPRECATED, transition-only)** — membership in
             ``singlepane_projects`` → ``"singlepane"``, elif ``worktree_projects`` →
             ``"worktree"`` — so a config that has NOT migrated yet (the current live
             shape: populated ``singlepane_projects``, empty ``worker_isolation``) is
             byte-behavior-identical;
          d. else ``None`` (caller MUST fail closed — never guess a mode).

        Only ``_VALID_ISOLATION_MODES`` reach (a)/(b): the parse drops typos and the
        reserved ``"default"`` value is validated the same way, so an invalid mode falls
        THROUGH to the next tier rather than routing a spawn down an unrecognized path.
        Distinct from ``worker_isolation_for`` (explicit-only, no legacy fallback): the
        concurrency guard keeps explicit-only semantics so it does not start firing as
        chains migrate from the legacy lists. The legacy tier is the migration ramp and is
        expected to retire once every project carries an explicit ``worker_isolation``.
        """
        explicit = self.worker_isolation.get(project)
        if explicit is not None:
            return explicit
        default = self.worker_isolation.get("default")
        if default is not None:
            return default
        if project in self.singlepane_projects:
            return "singlepane"
        if project in self.worktree_projects:
            return "worktree"
        return None

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
    """Load config from ``home/config.json``.

    Sentinel fail-closed contract (design §7 / Task 5.2). Three distinct outcomes:

    * **ABSENT** (no file) → feature defaults. This is the clean out-of-the-box state:
      "unset" means "use the default", so the unified spawn-window mechanism stays ON.
    * **PRESENT + readable + valid JSON** → parsed verbatim.
    * **PRESENT but UNTRUSTWORTHY** (unreadable / permission-denied / corrupt JSON) → we
      cannot trust ANY read, so the unified-spawn kill-switch fails CLOSED
      (``unified_spawn_enabled=False``) + a LOUD warn — we must NOT silently drive the new
      spawn mechanism off a config we couldn't parse (禁止静默降级). Every OTHER field's
      empty-default is already its safe floor (no opt-in lists, worktree off), so a corrupt
      config can never silently opt a project into anything either.

    ``read_text`` (not ``exists()`` + read) collapses the absent check and the read into one
    operation: ``FileNotFoundError`` ⇒ absent (defaults ON); any other ``OSError`` ⇒
    present-but-unreadable (fail closed) — no TOCTOU between the two.
    """
    if home is None:
        home = home_dir()
    cfg_path = home / CONFIG_FILENAME
    try:
        raw = cfg_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config(home=home)  # absent → out-of-the-box defaults (unified spawn ON)
    except OSError as e:
        return _fail_closed_config(home, cfg_path, e)  # present but unreadable
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return _fail_closed_config(home, cfg_path, e)  # present but corrupt
    return _from_dict(data, home=home)


def _fail_closed_config(home: Path, cfg_path: Path, err: Exception) -> Config:
    """A present-but-untrustworthy config: warn loudly, fail the unified-spawn switch CLOSED.

    Returns an all-defaults ``Config`` EXCEPT ``unified_spawn_enabled=False`` (don't force
    the new mechanism off a config we couldn't read/parse). The warn is non-silent so the
    owner learns their spawn config is broken and the engine degraded to the legacy path.
    """
    print(
        f"⚠️  config at {cfg_path} unreadable/corrupt ({type(err).__name__}: {err}); "
        "failing CLOSED — unified_spawn_enabled=False (legacy fallback). The unified "
        "spawn-window mechanism will NOT auto-run off an untrusted config; fix "
        "config.json to re-enable.",
        file=sys.stderr,
    )
    # config_trusted=False so the Step 4 anchor decision fails CLOSED for an enforce-able
    # dispatch rather than reading the (now-empty) enforce list as "warn" (design §4.1 fail-safe).
    return Config(home=home, unified_spawn_enabled=False, config_trusted=False)


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
            [str(p) for p in data.get("singlepane_projects") if isinstance(p, str) and p]
            if isinstance(data.get("singlepane_projects"), list)
            else []
        ),
        unified_spawn_enabled=_parse_unified_spawn_enabled(data),
        worker_isolation=_parse_worker_isolation(data),
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
        **_parse_anchor_projects(data),
        # B1: default-OFF retrieval-pull enforce roll-out list (reuses the slug-list parser;
        # absent/typo/non-list → [] = no project enforced = warn/no-op, the safe default).
        retrieval_pull_enforce_projects=_anchor_slug_list(
            data, "retrieval_pull_enforce_projects"
        ),
        **_parse_worktree(data),
        **_parse_reclaim(data),
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


# Recognised string spellings of the unified-spawn kill-switch (case-insensitive,
# whitespace-trimmed). Anything outside these → loud warn + feature default (ON).
_FALSEY_STR = frozenset({"false", "0", "no", "off", "n", "f", ""})
_TRUTHY_STR = frozenset({"true", "1", "yes", "on", "y", "t"})


def _parse_unified_spawn_enabled(data: dict) -> bool:
    """Parse the unified-spawn kill-switch WITHOUT the ``bool()`` footgun.

    ``bool("false")`` is ``True`` — so the old ``bool(data.get("unified_spawn_enabled",
    True))`` left the feature ENABLED when an owner typed the JSON string ``"false"`` to
    KILL it: a SILENT fail-OPEN of a safety kill-switch (禁止静默降级). Here:

    * key absent OR JSON ``null`` → feature default (ON) — "unset" means "use default".
    * a real JSON bool (``true`` / ``false``) → honoured verbatim.
    * a number (``0`` → off, non-zero → on).
    * a recognised string (``"false"`` / ``"0"`` / ``"off"`` / ``"true"`` / …) →
      interpreted, so the kill-switch actually works as the owner intended.
    * anything genuinely unrecognised (a typo like ``"banana"``, a list, …) → feature
      default (ON) but a LOUD stderr warn — never a silent mis-parse.
    """
    if "unified_spawn_enabled" not in data:
        return True
    raw = data.get("unified_spawn_enabled")
    if raw is None:  # explicit JSON null → treat like unset (use default)
        return True
    if isinstance(raw, bool):  # MUST precede int — bool is a subclass of int
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in _FALSEY_STR:
            return False
        if s in _TRUTHY_STR:
            return True
    print(
        f"⚠️  unified_spawn_enabled: unrecognized value {raw!r}; defaulting to enabled "
        "(True). Use a JSON boolean (true/false).",
        file=sys.stderr,
    )
    return True


_VALID_ISOLATION_MODES = ("worktree", "singlepane", "multiwindow")


def _parse_worker_isolation(data: dict) -> dict[str, str]:
    """Parse ``worker_isolation`` (``{slug: "worktree"|"singlepane"|"multiwindow"}``), defensively.

    Mirrors ``_parse_project_inject_blocks``: only a dict maps to gated values; any
    non-dict (absent / typo / list) yields ``{}``. Within it, only string slugs whose
    value is one of the KNOWN isolation modes (``_VALID_ISOLATION_MODES``) survive — a
    typo'd mode (``"worktre"``) or a non-string value is dropped, so ``worker_isolation_for``
    returns ``None`` and the caller fails closed (design §2.2 no-guess). A bad shape can
    never route a spawn down an unrecognized isolation path. The reserved ``"default"``
    slug (Step 6) is an ordinary string key — it passes the slug check and feeds
    ``resolve_isolation``'s global-default tier.
    """
    raw = data.get("worker_isolation")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for slug, mode in raw.items():
        if not isinstance(slug, str) or not slug:
            continue
        if isinstance(mode, str) and mode in _VALID_ISOLATION_MODES:
            out[slug] = mode
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


def _parse_reclaim(data: dict) -> dict:
    """Parse the §6c reclaim config block (all optional, safe defaults).

    ``reclaim_ack_timeout`` is clamped to a sane positive range (a zero/negative or
    unparseable value would make every reclaim instantly ``ack-timeout`` — fail back to
    the 30s default instead). ``canonical_remote`` accepts only a non-empty string
    (anything else → None → dynamic resolution). ``reclaim_probe_disabled`` must be an
    explicit JSON ``true`` — the probe is a safety gate, so a typo can never disable it.
    """
    timeout_raw = data.get("reclaim_ack_timeout", 30.0)
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError):
        timeout = 30.0
    if not (0 < timeout <= 3600):
        timeout = 30.0
    remote_raw = data.get("canonical_remote")
    remote = str(remote_raw) if isinstance(remote_raw, str) and remote_raw.strip() else None
    idle_raw = data.get("reclaim_probe_idle_sec", 600.0)
    try:
        idle = float(idle_raw)
    except (TypeError, ValueError):
        idle = 600.0
    if idle <= 0:
        idle = 600.0
    return {
        "reclaim_ack_timeout": timeout,
        "canonical_remote": remote,
        "reclaim_probe_idle_sec": idle,
        "reclaim_probe_disabled": data.get("reclaim_probe_disabled") is True,
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


def _anchor_slug_list(data: dict, key: str) -> list[str]:
    """Parse a Step 4 anchor project list defensively (mirrors ``worktree_projects`` /
    ``singlepane_projects``): only a JSON list of non-empty strings survives; any non-list
    (absent / bare-string typo / object) → ``[]``. A bare string ``"hf"`` must NOT iterate
    into chars ``['h','f']`` (the mandate-parser footgun) — guard ``isinstance(list)`` first."""
    raw = data.get(key)
    return [str(p) for p in raw if isinstance(p, str) and p] if isinstance(raw, list) else []


def _parse_anchor_projects(data: dict) -> dict:
    """Parse the Step 4 anchor fail-closed roll-out lists (design §4.1).

    Default-OPEN (warn), the OPPOSITE of ``_parse_mandate_projects``: a project absent from
    both ``spawner_anchor_dry_run_projects`` and ``spawner_anchor_enforce_projects`` is warn
    (legacy fail-open) — so an accidental empty / absent key can NEVER flip a project to
    fail-closed mid-roll-out. ``spawner_anchor_enforce_configured`` mirrors
    ``mandate_projects_configured`` (records whether any project is explicitly enforced) so the
    «空列表 vs 缺键» distinction is testable. The fail-CLOSED direction (corrupt config) is
    carried by ``config_trusted`` (set in ``_fail_closed_config``), NOT here — a valid config
    with empty lists legitimately means "nothing enforced yet".
    """
    enforce = _anchor_slug_list(data, "spawner_anchor_enforce_projects")
    return {
        "spawner_anchor_dry_run_projects": _anchor_slug_list(data, "spawner_anchor_dry_run_projects"),
        "spawner_anchor_enforce_projects": enforce,
        "spawner_anchor_enforce_configured": bool(enforce),
        "spawner_anchor_system_allow": _anchor_slug_list(data, "spawner_anchor_system_allow"),
    }
