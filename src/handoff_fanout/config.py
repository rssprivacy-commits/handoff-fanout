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


@dataclass
class RoadmapSpec:
    """Optional roadmap file whose phase sections are excerpted into prompts."""

    path: str | None = None
    section_regex: str = r"#### Phase[^\n]*\n(.*?)(?=\n#### |\Z)"
    max_sections: int = 2
    max_chars_per_section: int = 1200
    fallback_tail_chars: int = 3000


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
    baseline_hooks: list[HookSpec] = field(default_factory=list)
    dump_preflight_commands: list[PreflightSpec] = field(default_factory=list)
    roadmap: RoadmapSpec = field(default_factory=RoadmapSpec)
    uri_template: str = DEFAULT_URI_TEMPLATE
    workspace_root: Path = field(default_factory=lambda: Path(DEFAULT_WORKSPACE_ROOT).expanduser())
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
    )
    hooks_raw = data.get("baseline_hooks", []) or []
    baseline_hooks = [
        HookSpec(
            name=h["name"],
            command=list(h["command"]),
            regex=h.get("regex"),
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
        baseline_hooks=baseline_hooks,
        dump_preflight_commands=dump_preflight_commands,
        roadmap=roadmap,
        uri_template=data.get("uri_template", DEFAULT_URI_TEMPLATE),
        workspace_root=Path(workspace_root_raw).expanduser(),
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
    )
