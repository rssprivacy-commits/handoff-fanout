"""Per-session git worktree isolation (opt-in / default OFF).

handoff-fanout auto-continue historically spawned every next session into the
**same repo's same working tree + same `.git/index`** — single-writer git state
shared by N concurrent sessions. The result (2026-06-03 incident): a multi-tab
window where one session's bare ``git stash`` / ``git reset --hard`` clobbered
another's uncommitted edits, plus pytest cross-talk and ``.git/index.lock``
contention. This module gives each spawned session its own ``git worktree`` —
independent working tree + index + HEAD over one shared object store — so those
collisions are structurally impossible.

Design + dual-brain (codex + Gemini) audit:
``docs/design-per-session-worktree-isolation-2026-06-03.md`` (esp. §8 R1 results).

Honest boundaries (NOT solved here): Docker-DB cross-talk (all worktrees still
share one Postgres), alembic migration-chain forks (repo-level), and batch
fan-out (kept on the shared tree in v1). Worktree isolates the **git tree** layer
only.

Mode resolution (``resolve_mode``): default OFF; opt-in via env
``HANDOFF_WORKTREE_ISOLATION`` (off/report/on), sentinels, or config. ``report``
computes + logs what WOULD happen and mutates nothing (truly read-only).
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from handoff_fanout import config as _config
from handoff_fanout import spawn_nonce as _spawn_nonce

# A worktree whose ``queue/<task>.heartbeat`` was touched within this window is
# treated as a LIVE session — GC never reclaims it out from under a running tab.
HEARTBEAT_LIVE_SEC = 600

# Fixed engine-generated VS Code workspace file injected into a spawn worktree (option-C /
# 2026-06-03 worktree-spawn-bug fix). FIXED (not ``<project>.code-workspace``) so it (a) is
# exact-matchable in ``is_dirty`` — discounting a broad ``*.code-workspace`` suffix would treat a
# user's untracked ``my-wip.code-workspace`` as clean → GC data loss (R2 Gemini P0-2) — and (b)
# is collision-unlikely with a user-tracked file (R2 Gemini P0-4). auto-continue.sh greps for this
# exact name (only under ``*/worktrees/*``) to open it as the spawn window.
WORKTREE_VSCODE_FILE = ".handoff.code-workspace"

MODE_OFF = "off"
MODE_REPORT = "report"
MODE_ON = "on"

# Outcome statuses for create_worktree.
ST_OFF = "off"  # feature disabled — spawn on the shared tree (byte-identical legacy)
ST_REPORT = "report"  # report-only — computed, nothing mutated, spawn on shared tree
ST_CREATED = "created"  # worktree created — spawn isolated
ST_DEGRADED = "degraded"  # environmental unavailability — spawn on shared tree + loud warn
ST_BLOCKED = "blocked"  # unsafe (unpublished work / dirty collision) — caller must BLOCK


# ─── worker worktree lifecycle state machine (design §5.2/§5.3) ───────────────


class WorktreeState(enum.Enum):
    """Lifecycle of a per-session worker worktree (design §5.2).

    ``creating → active → awaiting-merge → merged | abandoned``. Only the TERMINAL
    states (``MERGED`` = work handed back / merged, ``ABANDONED`` = discarded) are
    ever eligible for orphan reclaim. ``CREATING`` (mid-spawn), ``ACTIVE`` (a session
    is working in it), and ``AWAITING_MERGE`` (closed but the business merge-back
    layer hasn't merged yet) are NEVER reclaimed by low-level GC — touching them
    would race a live session or destroy un-merged work (§5.3). The string values are
    persisted in sidecars/markers, so they must stay stable.
    """

    CREATING = "creating"
    ACTIVE = "active"
    AWAITING_MERGE = "awaiting-merge"
    MERGED = "merged"
    ABANDONED = "abandoned"


# Terminal states whose worktree the low-level reclaimer may drop (work is already
# handed back or explicitly discarded). Kept as a set so the gate reads declaratively
# and a future state addition can't silently become "reclaimable" by omission.
_RECLAIMABLE_STATES = frozenset({WorktreeState.MERGED, WorktreeState.ABANDONED})


def is_reclaimable_orphan(
    *, proc_alive: bool, in_pending_queue: bool, state: WorktreeState
) -> bool:
    """True iff a worktree is a reclaimable orphan — ALL three conditions hold (§5.3).

    Orphan = ① owner session process is dead (judged by transcript-mtime, NOT
    heartbeat — design §5.3) ∧ ② its ``task_id`` is not in the pending/inflight queue
    ∧ ③ state ∈ {``MERGED``, ``ABANDONED``} (work handed back / discarded). Any single
    condition failing → NOT reclaimable: a live process, a queued task, or an
    ``ACTIVE``/``AWAITING_MERGE`` worktree all belong to a running session or the
    business merge-back layer and must be left untouched (fail-closed — the cost of a
    false-positive reclaim is destroyed work, so the AND is intentional).
    """
    return (not proc_alive) and (not in_pending_queue) and (state in _RECLAIMABLE_STATES)


# ─── git helpers ─────────────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path, timeout: float = 15.0) -> tuple[int, str, str]:
    """Run ``git <args>`` in ``cwd``; return ``(rc, stdout, stderr)`` (stripped).

    rc 127 (git missing) / timeout are surfaced as a non-zero rc so callers
    fail-safe rather than raise.
    """
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


def is_git_repo(workspace: Path) -> bool:
    rc, out, _ = _git(["rev-parse", "--is-inside-work-tree"], workspace)
    return rc == 0 and out == "true"


def has_remote(workspace: Path, remote: str = "origin") -> bool:
    rc, out, _ = _git(["remote"], workspace)
    return rc == 0 and remote in out.split()


def head_sha(workspace: Path) -> str | None:
    rc, out, _ = _git(["rev-parse", "HEAD"], workspace)
    return out if rc == 0 and out else None


def head_sha_of_ref(workspace: Path, ref: str) -> str | None:
    """Resolve ``ref`` (e.g. ``refs/remotes/origin/main``) to its commit SHA."""
    rc, out, _ = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], workspace)
    return out if rc == 0 and out else None


def _ref_exists(workspace: Path, ref: str) -> bool:
    rc, _, _ = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], workspace)
    return rc == 0


def is_ancestor(workspace: Path, ancestor: str, descendant: str) -> bool:
    """True iff ``ancestor`` commit is reachable from ``descendant`` (⊆ history)."""
    rc, _, _ = _git(["merge-base", "--is-ancestor", ancestor, descendant], workspace)
    return rc == 0


def branch_head(workspace: Path, branch: str) -> str | None:
    """SHA of local ``refs/heads/<branch>``, or None if it does not resolve."""
    rc, out, _ = _git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], workspace
    )
    return out if rc == 0 and out else None


def is_dirty(workspace: Path, ignore: set[str] | tuple[str, ...] = ()) -> bool:
    """True iff the working tree has uncommitted (staged or unstaged) changes.

    ``ignore`` is a set of top-level path names to DISCOUNT — but ONLY when they are
    **untracked** (``??``). The engine symlinks ``.claude`` / ``.venv`` (and copies
    ``.env``); a fresh worktree shows those as untracked because the project's
    ``.gitignore`` uses directory patterns (``.venv/``) that don't match the *symlink*
    the engine creates, so without this filter every worktree reads "dirty" and GC's
    fail-safe never reclaims it (R-ON: real-machine ON test).

    REDLINE (codex R-ON P1): only untracked link-named entries are discounted. ANY
    tracked change — ``M .env`` / ``D .claude/settings.json`` / a rename — is genuine
    WIP regardless of name and still → dirty, so real work is never silently made
    destroyable. (Untracked entries never carry a ``->`` rename, so the P2 weird-name
    parse is moot once we gate on ``??``.)
    """
    rc, out, _ = _git(["status", "--porcelain"], workspace)
    if rc != 0:
        return True
    if not out.strip():
        return False
    # NB (option-C): the loop ALWAYS runs (even with an empty ``ignore``) because the
    # engine-injected UX artifacts (.vscode / *.code-workspace) must be discounted
    # unconditionally — a worktree carrying only those is clean. Any non-artifact /
    # non-ignored change still falls through to dirty below.
    ignore = set(ignore)
    for line in out.splitlines():
        if not line.strip():
            continue
        code = line[:2]
        path = line[3:].strip().strip('"')
        # Discount ONLY an untracked entry whose FULL path is an engine link name
        # (codex dual-brain P0): a prefix/first-component match would discount
        # ``?? src/new.py`` if a tracked dir name (``src``) were ever configured as a
        # link → real WIP silently destroyable. A linked dir is a *symlink* (git shows
        # ``?? .claude`` exactly, never its contents), so exact-match is sufficient.
        # Also discount the engine-injected VS Code spawn-UX artifacts (option-C / 2026-06-03):
        # the ``.vscode`` symlink and the FIXED ``WORKTREE_VSCODE_FILE`` are deterministically
        # engine-created, never user WIP. EXACT match (R2 Gemini P0-2: a broad ``*.code-workspace``
        # suffix would silently treat a user's untracked ``my-wip.code-workspace`` as clean → GC
        # could then reclaim real WIP). Same REDLINE: ``??``-gated only — a tracked change to either
        # still falls through to dirty.
        if code == "??" and (path in ignore or path == ".vscode" or path == WORKTREE_VSCODE_FILE):
            continue
        return True  # tracked change, OR an untracked non-link path → genuinely dirty
    return False  # every change was an untracked engine-linked file → clean


def _link_names(cfg: _config.Config) -> set[str]:
    """The engine-managed link names to discount when checking worktree dirtiness."""
    names = set(cfg.worktree_link_files)
    if cfg.worktree_link_venv:
        names.add(".venv")
    return names


# ─── mode resolution ─────────────────────────────────────────────────────────


def _env_mode(env: dict[str, str]) -> str | None:
    """Map ``HANDOFF_WORKTREE_ISOLATION`` to a mode, or None if unset/unknown."""
    raw = env.get("HANDOFF_WORKTREE_ISOLATION")
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in ("on", "1", "true", "yes"):
        return MODE_ON
    if v == "report":
        return MODE_REPORT
    if v in ("off", "0", "false", "no", ""):
        return MODE_OFF
    return None  # unknown token → no opinion (fall through to sentinels/config)


def resolve_mode(cfg: _config.Config, project: str, env: dict[str, str] | None = None) -> str:
    """Resolve the effective worktree mode for ``project`` (default ``off``).

    Precedence (first decisive wins):
      1. env ``HANDOFF_WORKTREE_ISOLATION`` (on/report/off) — the master switch.
      2. sentinels ``$HANDOFF_HOME/[<project>/]worktree.enabled`` → on.
      3. config ``worktree_projects`` lists ``project`` → on.
      4. sentinels ``$HANDOFF_HOME/[<project>/]worktree.report`` → report.
      5. config ``worktree_mode``.
      6. off.

    EXPLICIT ON (the ``enabled`` sentinel AND the ``worktree_projects`` config list)
    OUTRANKS the ``report`` sentinel (dual-brain P1: a global ``worktree.report``
    touched to pilot an off project must NOT silently demote a production project that
    is ON via config into a shared-tree report — that re-opens the very collision
    class isolation defends). A project-scoped ``worktree.report`` is the clean way to
    pilot ONE project without flipping the GLOBAL env/``worktree_mode``.
    """
    if env is None:
        env = dict(os.environ)
    em = _env_mode(env)
    if em is not None:
        return em
    # — explicit ON (sentinel + config) first —
    if (cfg.home / "worktree.enabled").exists() or (
        cfg.home / project / "worktree.enabled"
    ).exists():
        return MODE_ON
    if project in cfg.worktree_projects:
        return MODE_ON
    # — report (observe) only after explicit ON has had its say —
    if (cfg.home / "worktree.report").exists() or (cfg.home / project / "worktree.report").exists():
        return MODE_REPORT
    if cfg.worktree_mode in (MODE_OFF, MODE_REPORT, MODE_ON):
        return cfg.worktree_mode
    return MODE_OFF


# ─── integration-branch resolution (R1-X2) ───────────────────────────────────

# Branch-name prefixes that must NEVER be chosen as the integration branch — a
# worktree session's own HEAD is on one of these, so `rev-parse --abbrev-ref HEAD`
# would otherwise pick a task branch.
_TASK_BRANCH_PREFIXES = ("handoff/", "task/")


def _looks_like_task_branch(name: str, cfg: _config.Config | None = None) -> bool:
    """True if ``name`` is a per-session task branch (never an integration branch).

    Includes the configured ``worktree_branch_prefix`` (dual-brain Gemini P2) so a
    project that customized it (e.g. ``feat/``) doesn't mis-pick a ``feat/xxx`` task
    branch as the integration branch.
    """
    prefixes = _TASK_BRANCH_PREFIXES
    if cfg is not None and cfg.worktree_branch_prefix:
        prefixes = (*prefixes, cfg.worktree_branch_prefix)
    return any(name.startswith(p) for p in prefixes)


def resolve_integration_branch(
    workspace: Path, cfg: _config.Config, *, allow_network: bool = True
) -> str | None:
    """Resolve the integration branch name (e.g. ``"main"``), or None if unknown.

    Order (R1-X2 — never infer from a ``handoff/*`` / ``task/*`` branch, which is a
    worktree session's own HEAD):
      1. config ``worktree_default_branch``.
      2. ``git symbolic-ref refs/remotes/origin/HEAD``.
      3. ``git remote show origin`` "HEAD branch" (network; skipped if not allow_network).
      4. ``origin/main`` → main, ``origin/master`` → master.
      5. local ``main`` → main, ``master`` → master.
    Returns a bare branch name (no ``origin/`` prefix). None ⇒ caller degrades.
    """
    if cfg.worktree_default_branch:
        return cfg.worktree_default_branch

    rc, out, _ = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], workspace)
    if rc == 0 and out.startswith("refs/remotes/origin/"):
        name = out[len("refs/remotes/origin/") :]
        if name and not _looks_like_task_branch(name, cfg):
            return name

    if allow_network and has_remote(workspace):
        rc, out, _ = _git(["remote", "show", "origin"], workspace, timeout=20.0)
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("HEAD branch:"):
                    name = line.split(":", 1)[1].strip()
                    if name and name != "(unknown)" and not _looks_like_task_branch(name, cfg):
                        return name

    for cand in ("main", "master"):
        if _ref_exists(workspace, f"refs/remotes/origin/{cand}"):
            return cand
    for cand in ("main", "master"):
        if _ref_exists(workspace, f"refs/heads/{cand}"):
            return cand
    return None


# ─── paths ───────────────────────────────────────────────────────────────────


def worktrees_root(cfg: _config.Config, project: str) -> Path:
    """Root dir holding this project's per-task worktrees."""
    if cfg.worktrees_root is not None:
        return cfg.worktrees_root / project
    return cfg.home / project / "worktrees"


def worktree_path(cfg: _config.Config, project: str, task: str) -> Path:
    return worktrees_root(cfg, project) / task


def branch_name(cfg: _config.Config, task: str) -> str:
    return f"{cfg.worktree_branch_prefix}{task}"


# ─── result ──────────────────────────────────────────────────────────────────


@dataclass
class WorktreeResult:
    """Outcome of a worktree spawn attempt.

    ``spawn_workspace`` is what the successor session works in: the new worktree
    (``created``), or the shared ``source_workspace`` (``off`` / ``report`` /
    ``degraded``). ``blocked`` carries the reason the caller must surface + abort.
    """

    status: str
    spawn_workspace: Path
    branch: str | None = None
    base_sha: str | None = None
    integration_branch: str | None = None
    reason: str | None = None
    linked: list[str] = field(default_factory=list)
    # Non-fatal advisories surfaced to the successor (e.g. a dirty source whose
    # uncommitted changes are NOT propagated to the worktree base — R2 codex P0-B).
    warnings: list[str] = field(default_factory=list)
    # report-only: the command that WOULD run (for the log), without running it.
    planned_cmd: str | None = None
    # option-C spawn-UX (2026-06-03 worktree-spawn-bug fix): the generated
    # ``<project>.code-workspace`` in the worktree. auto-continue.sh opens THIS (not the
    # bare folder) so the window has an identifiable title + inherited ``.vscode`` →
    # fixes "新窗口认不出项目" + the bare-folder cold-start that swallowed the auto-submit Enter.
    vscode_workspace_file: str | None = None
    # p6a-fix1 MUST 2: True iff ``created`` ADOPTED an existing clean+published worktree
    # (the dual-brain P1 idempotent-reuse branch) rather than creating one THIS call. A
    # caller rolling back a later publish failure must only remove a worktree it actually
    # created (``reused=False``) — a reused one may belong to another live session /
    # the previous relay leg, and removing it is data loss.
    reused: bool = False

    @property
    def is_blocked(self) -> bool:
        return self.status == ST_BLOCKED

    @property
    def is_worktree(self) -> bool:
        return self.status == ST_CREATED


# ─── worktree classification (R1-C3) ─────────────────────────────────────────


def classify_worktree(
    wt_path: Path,
    branch: str,
    integration_branch: str,
    repo_workspace: Path,
    ignore_names: set[str] | tuple[str, ...] = (),
) -> dict:
    """Classify an existing worktree's safety for removal/reuse.

    Returns ``{exists, dirty, branch_head, published}`` where:
      * ``dirty``     — uncommitted changes, DISCOUNTING the engine-linked convenience
        files (``ignore_names``) that always read as untracked symlinks (R-ON).
      * ``published`` — branch HEAD ⊆ ``origin/<integration_branch>`` (no unmerged
        local commits). A clean-but-``not published`` worktree holds committed,
        unpushed work → must be RETAINED, never force-removed (R1-C3 / Gemini P1-4).
    """
    info: dict = {
        "exists": wt_path.exists(),
        "dirty": False,
        "branch_head": None,
        "published": False,
    }
    if not wt_path.exists():
        return info
    info["dirty"] = is_dirty(wt_path, ignore=ignore_names)
    bh = head_sha(wt_path)
    info["branch_head"] = bh
    if bh:
        ref = f"refs/remotes/origin/{integration_branch}"
        if _ref_exists(repo_workspace, ref):
            info["published"] = is_ancestor(repo_workspace, bh, ref)
    return info


def safe_to_recreate(info: dict) -> bool:
    """True iff an existing worktree is clean AND fully published (safe to drop)."""
    return bool(info.get("exists") and not info.get("dirty") and info.get("published"))


# ─── file linking (R1-G1 / R1-X3) ────────────────────────────────────────────


def link_files(source_workspace: Path, wt_path: Path, cfg: _config.Config) -> list[str]:
    """Symlink gitignored-but-essential files from the source tree into the worktree.

    A fresh ``git worktree`` checkout contains only tracked files; ``.env`` (DB
    creds → all E2E/alembic crash → instant BLOCK) and ``.claude/`` (session
    settings) live ungitted in the source tree (R1-G1 / Gemini P0-2). ``.venv`` is
    linked separately because a shared venv can run the MAIN checkout for editable
    self-installs (R1-X3 / codex P1) — opt-out via ``worktree_link_venv: false``.

    Regular **files** are COPIED (R2 Gemini P1-5): an absolute symlink to
    ``/Users/.../.env`` breaks the instant a worktree is bind-mounted into a Docker
    container (the host path doesn't exist there), so a real file is portable. **Dirs**
    (``.claude``, ``.venv``) are symlinked (copying a venv is absurd; a dir symlink
    survives the host-side toolchain). Never overwrites a tracked file of the same
    name; best-effort (a failed link/copy is skipped).
    """
    names = list(cfg.worktree_link_files)
    if cfg.worktree_link_venv:
        names.append(".venv")
    linked: list[str] = []
    for name in names:
        src = source_workspace / name
        dst = wt_path / name
        if not src.exists():
            continue
        if dst.exists() or dst.is_symlink():
            continue
        try:
            if src.is_dir():
                dst.symlink_to(src.resolve())
            else:
                shutil.copy2(src, dst)
            linked.append(name)
        except OSError:
            continue
    return linked


# ─── create ──────────────────────────────────────────────────────────────────


def _degrade(source_workspace: Path, reason: str) -> WorktreeResult:
    return WorktreeResult(status=ST_DEGRADED, spawn_workspace=source_workspace, reason=reason)


def _block(source_workspace: Path, reason: str, **extra) -> WorktreeResult:
    return WorktreeResult(
        status=ST_BLOCKED, spawn_workspace=source_workspace, reason=reason, **extra
    )


# 监管中枢窗口红顶防误关 (§五·2 / 2026-06-09 owner立法). Byte-identical to the proven-rendering
# spec in ``dx-spawn-session.sh --coordinator`` so handoff-fanout's worktree spawn path and the
# plain cross-project spawn path render an IDENTICAL red block + title prefix.
_COORDINATOR_TITLE_PREFIX = "🧭中枢·"
_COORDINATOR_RED_TITLEBAR = {
    "titleBar.activeBackground": "#8B0000",
    "titleBar.activeForeground": "#FFFFFF",
    "titleBar.inactiveBackground": "#5A0000",
    "titleBar.inactiveForeground": "#E0E0E0",
}


def _warn_coordinator_unredtopped(ws_file: Path, why: str) -> None:
    """禁止静默降级铁律 (codex+gemini round-2 P1): a coordinator window that can't be red-topped must
    NOT slip out silently — emit a visible stderr warning so the owner/center knows a 中枢 window
    will open without its 防误关 marker. Still never bricks the dump (UX polish is best-effort)."""
    sys.stderr.write(
        f"[worktree][WARN] ⚠️ 🧭中枢 红顶未能应用到 {ws_file} ({why}); "
        f"该窗口将无红顶标记 — 留意误关 (§五·2 / 禁止静默降级)\n"
    )


def _ensure_coordinator_redtop(ws_file: Path, project: str, task: str) -> None:
    """Idempotently patch the §五·2 red-top into an EXISTING ``.handoff.code-workspace`` when a
    coordinator worktree is REUSED (codex+gemini 双脑共识 finding 2026-06-09): the fresh-create path
    injects red-top directly, but a reused worktree whose file predates this patch — or was first
    created without ``--coordinator`` — would otherwise open WITHOUT red, silently breaking the
    absolute invariant "只要是中枢窗口就必须红顶".

    - title: add the 🧭中枢· prefix; if it was deleted (missing / non-str) install a marked,
      identifiable fallback (round-2 gemini P2 — keep the text marker, not just the color).
    - colors: **MERGE** the red titleBar keys into any existing ``colorCustomizations`` rather than
      replacing the whole dict — a user's unrelated colors (editor.background, …) must survive
      (round-2 gemini P0 — never destroy user content).
    - rewrite only when something actually changed (no needless born-dirty churn / no double-prefix).
    - on a genuinely un-patchable file (unparseable / no ``settings`` dict / write OSError) leave the
      content untouched BUT WARN (round-2 codex+gemini P1: 降级不静默) — never brick the dump."""
    try:
        data = json.loads(ws_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _warn_coordinator_unredtopped(ws_file, "unreadable / non-JSON")
        return
    settings = data.get("settings") if isinstance(data, dict) else None
    if not isinstance(settings, dict):
        _warn_coordinator_unredtopped(ws_file, "no settings object")
        return
    changed = False
    title = settings.get("window.title")
    if not isinstance(title, str):
        settings["window.title"] = (
            f"{_COORDINATOR_TITLE_PREFIX}{project} · {task} "
            "[worktree]${separator}${activeEditorShort}"
        )
        changed = True
    elif not title.startswith(_COORDINATOR_TITLE_PREFIX):
        settings["window.title"] = _COORDINATOR_TITLE_PREFIX + title
        changed = True
    colors = settings.get("workbench.colorCustomizations")
    if not isinstance(colors, dict):
        settings["workbench.colorCustomizations"] = dict(_COORDINATOR_RED_TITLEBAR)
        changed = True
    else:  # MERGE — preserve the user's other colors (round-2 gemini P0).
        for k, v in _COORDINATOR_RED_TITLEBAR.items():
            if colors.get(k) != v:
                colors[k] = v
                changed = True
    if changed:
        try:
            ws_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            _warn_coordinator_unredtopped(ws_file, "write failed")


def inject_vscode_workspace(
    source_workspace: Path,
    wt: Path,
    project: str,
    task: str,
    *,
    spawn_nonce: str | None = None,
    role: str = "worker",
    is_coordinator: bool = False,
) -> str | None:
    """Make a fresh worktree open as an *identifiable VS Code workspace* (option-C / 2026-06-03
    worktree-spawn-bug fix — dual-brain codex+Gemini).

    ``spawn_nonce`` (spawn-window-unify Phase 6a / design §4): when supplied, the ``window.title``
    binds ``project·task·role·nonce`` via :func:`spawn_nonce.title_for` so the watchdog can
    ATOMICALLY prove the front window is the exact one launched (the unguessable nonce, not just a
    guessable task token). When ``None`` (the legacy ``dump``→``create_worktree`` callers) the title
    is BYTE-IDENTICAL to the pre-Phase-6a form — this kwarg is purely additive; ``dump`` passes
    nothing and is unaffected.

    ``is_coordinator`` (§五·2 / 2026-06-09 owner立法): when the spawned session is a supervisor
    center (中枢), tint the window red + prefix the title ``🧭中枢·`` so the owner can't misclose it
    among many windows. A non-中枢 task is byte-identical to the pre-2026-06-09 engine (zero
    regression). See the ``if is_coordinator`` branch below. Composes with ``spawn_nonce``: the
    red-top prefix wraps WHATEVER title was computed (nonce-bound or legacy), so nonce gating and
    coordinator marking are orthogonal.

    A bare ``git worktree`` folder, opened via ``code -r <dir>``, (a) titles the window only
    by the dir basename (``stage1-10c`` — unrecognizable as the project) and (b) has no
    ``.vscode`` context, so VS Code cold-starts/re-indexes the Claude extension far past the
    launcher's ``sleep`` → the synthetic auto-submit Enter lands before the input is ready and
    is swallowed (the observed "粘贴了但没按 Enter"). This injects:

    (a) a ``.vscode`` symlink to the source tree → inherits the project's formatter / linter /
        launch config (so the spawned session also produces project-conforming code);
    (b) a ``<project>.code-workspace`` whose ``window.title`` natively shows the project name +
        task → auto-continue.sh opens this file (not the bare dir), fixing both the title and
        (because VS Code treats it as a real workspace) much of the cold-start.

    Best-effort: any OSError is swallowed (returns None) — UX polish must never brick a dump.
    Returns the ``.code-workspace`` path (str) or None.
    """
    try:
        vs_src = source_workspace / ".vscode"
        vs_dst = wt / ".vscode"
        if vs_src.is_dir() and not (vs_dst.exists() or vs_dst.is_symlink()):
            vs_dst.symlink_to(vs_src.resolve())
        # FIXED engine name (R2 Gemini P0-2/P0-4): NOT ``<project>.code-workspace``. A
        # project-named file (a) could collide with a user-tracked ``<project>.code-workspace``
        # → overwriting it makes the worktree born-``M``-dirty + never reusable, and (b) forced
        # ``is_dirty`` to discount by the broad ``*.code-workspace`` suffix, which would silently
        # treat a user's untracked ``my-wip.code-workspace`` as clean → GC data loss. A fixed,
        # collision-unlikely name is exact-matchable in ``is_dirty`` + skipped if the user already
        # has one.
        ws_file = wt / WORKTREE_VSCODE_FILE
        if ws_file.exists():
            if spawn_nonce is None:
                # legacy (``dump``) callers: BYTE-IDENTICAL pre-Phase-6a behavior —
                # respect a pre-existing (tracked/user) file; never overwrite.
                # EXCEPTION (§五·2): a coordinator MUST be red-topped even on the reuse path —
                # idempotently patch the red-top in (a pre-patch / non-coordinator-first file
                # would else open without red). A non-coordinator stays a byte-identical no-op.
                if is_coordinator:
                    _ensure_coordinator_redtop(ws_file, project, task)
                return str(ws_file)
            # p6a-fix1 MUST 1: a REUSED worktree still holds the previous spawn's workspace
            # file, whose title carries the STALE nonce — but the delivery contract is
            # "title carries THIS spawn's nonce" (design §4: the nonce is the unguessable
            # landing gate; the task token is not). The engine's OWN generated file is
            # safely rewritten below with the current title; a USER-tracked file is never
            # overwritten — then the title cannot carry this nonce, so return None and let
            # the spawn caller fail closed instead of producing a title↔sidecar mismatch.
            # (A coordinator spawn hits the same fail-closed: red-top is moot when the nonce
            # contract itself cannot be honored.)
            # ``ls-files --error-unmatch`` rc: 0=tracked (user content), 1=untracked (the
            # engine wrote it post-checkout; is_dirty discounts it). Any other rc (no git /
            # timeout) is INDETERMINATE → treat as user content (never risk an overwrite).
            rc_tracked, _, _ = _git(["ls-files", "--error-unmatch", "--", WORKTREE_VSCODE_FILE], wt)
            if rc_tracked != 1:
                return None
            # fall through: rewrite the engine-generated file with the current nonce title
            # (the is_coordinator branch below re-applies the red-top on this rewrite).
        # Phase 6a: bind the unguessable nonce into the title when a fresh-spawn supplies one
        # (project·task·role·nonce via title_for + the [worktree] marker). Legacy ``dump`` callers
        # pass spawn_nonce=None → the BYTE-IDENTICAL pre-Phase-6a title (project · task [worktree]…).
        if spawn_nonce:
            window_title = (
                _spawn_nonce.title_for(project=project, task_id=task, role=role, nonce=spawn_nonce)
                + " [worktree]${separator}${activeEditorShort}"
            )
        else:
            window_title = f"{project} · {task} [worktree]${{separator}}${{activeEditorShort}}"
        settings = {
            # ${...} are VS Code window-title variables (literal here; VS Code expands them).
            "window.title": window_title,
            # SINGLE-PANE cold spawn (2026-06-06 / dual-brain codex+Gemini + owner ruling).
            # A fresh worktree window otherwise opens multi-pane (activity bar + Explorer +
            # an EMPTY Claude SIDEBAR "Message input"); that empty input grabs keyboard focus,
            # so the synthetic Enter races the URI-pasted CENTER prompt for focus (~40% miss
            # under load → readiness-gate honestly withholds → owner presses Enter manually).
            # Fix = collapse to a single editor pane so there is NO focus competitor.
            # Declarative half (zero-keystroke, the robust core both brains endorsed):
            #   - activityBar.location:hidden → removes the activity bar AND its Claude
            #     sidebar view, i.e. the empty "Message input" competitor. (The Explorer alone
            #     is an AXOutline, not an AXTextArea "Message input", so it never competes.)
            #   - startupEditor:none → no Welcome tab grabbing initial focus.
            # Runtime half (auto-continue.sh close_sidebars_if_front_window_contains) idempotently
            # closes BOTH side bars BEFORE the URI for a literal one-pane look. preferredLocation
            # stays "panel" (enum is only sidebar|panel; the URI opens Claude in the editor anyway).
            "workbench.activityBar.location": "hidden",
            "workbench.startupEditor": "none",
            "claudeCode.preferredLocation": "panel",
        }
        if is_coordinator:
            # 监管中枢窗口红顶防误关 (§五·2 / 2026-06-09 owner立法 / handoff-fanout 派窗路径普适化).
            # Byte-parity with ``dx-spawn-session.sh --coordinator`` so the two spawn paths (plain
            # cross-project spawn vs handoff-fanout worktree) render an IDENTICAL red block + 🧭中枢
            # title — owner辨中枢、防误关 across whichever path emitted the window. Colors > text:
            # a non-technical owner scanning many windows can't visually ignore a red title bar.
            # ADDITIVE & ORDER-PRESERVING: ``window.title`` keeps project+task (still identifiable;
            # when spawn_nonce is set the nonce-bound title is wrapped, keeping the watchdog's
            # substring nonce gate intact) and the singlepane fields are untouched, so a non-中枢
            # task is byte-identical to the pre-2026-06-09 engine (validation gate #2). NOT
            # ``ensure_ascii=False`` — that would change the non-coordinator bytes too; VS Code's
            # JSON parser decodes the 🧭中枢 \uXXXX escapes back to the glyphs (osascript-confirmed
            # in the spawn e2e).
            settings["window.title"] = _COORDINATOR_TITLE_PREFIX + settings["window.title"]
            settings["workbench.colorCustomizations"] = dict(_COORDINATOR_RED_TITLEBAR)
        ws_file.write_text(
            json.dumps({"folders": [{"path": "."}], "settings": settings}, indent=2),
            encoding="utf-8",
        )
        return str(ws_file)
    except OSError:
        return None


def create_worktree(
    *,
    source_workspace: Path,
    project: str,
    task: str,
    cfg: _config.Config,
    mode: str,
    env: dict[str, str] | None = None,
    spawn_nonce: str | None = None,
    role: str = "worker",
    is_coordinator: bool = False,
) -> WorktreeResult:
    """Create (or report/degrade/block) a per-session worktree for ``task``.

    Returns a ``WorktreeResult``; the caller substitutes ``spawn_workspace`` for the
    successor session's artifacts and aborts the dump iff ``is_blocked``.

    Distinguishes **environmental unavailability** (degrade to shared tree + warn:
    not a git repo / no remote / unresolved integration branch / ``worktree add``
    failure) from **unsafe state** (BLOCK, never silently proceed: source HEAD not
    published to the integration branch / a dirty same-task worktree collision) —
    R1-C2 / R1-R3.

    ``spawn_nonce``/``role`` (Phase 6a / design §4): forwarded to
    :func:`inject_vscode_workspace` so the worktree's ``.handoff.code-workspace`` title carries the
    unguessable nonce. Purely additive — ``dump`` passes neither and gets the legacy title.
    """
    if env is None:
        env = dict(os.environ)
    wt = worktree_path(cfg, project, task)
    br = branch_name(cfg, task)

    if mode == MODE_OFF:
        return WorktreeResult(status=ST_OFF, spawn_workspace=source_workspace)

    # Resolve integration branch from LOCAL metadata only for report (R1-X4: no network).
    allow_network = mode == MODE_ON
    int_branch = resolve_integration_branch(source_workspace, cfg, allow_network=allow_network)

    if mode == MODE_REPORT:
        # Pure compute — mutate NOTHING (no fetch, mkdir, symlink, ref update).
        planned = (
            f"git -C {source_workspace} worktree add -b {br} {wt} "
            f"origin/{int_branch or '<UNRESOLVED>'}"
        )
        return WorktreeResult(
            status=ST_REPORT,
            spawn_workspace=source_workspace,
            branch=br,
            integration_branch=int_branch,
            planned_cmd=planned,
            reason=None if int_branch else "integration branch unresolved (report)",
        )

    # ── mode == on ──────────────────────────────────────────────────────────
    if not is_git_repo(source_workspace):
        return _degrade(source_workspace, "not a git repository")
    if not has_remote(source_workspace):
        # v1: published-integration-ref merge-back needs a bare remote (design §8.4).
        return _degrade(source_workspace, "no git remote (no-remote merge-back unsupported in v1)")
    if int_branch is None:
        return _degrade(source_workspace, "could not resolve integration branch")

    source_head = head_sha(source_workspace)
    if source_head is None:
        return _degrade(source_workspace, "could not resolve source HEAD")

    warnings: list[str] = []
    # R2 codex P0-B: a dirty source worktree's uncommitted changes are NOT carried
    # into the successor (which branches from origin/<int>). Unlike the shared-tree
    # path (where the next session inherits the tree), they stay only in the source
    # worktree (retained, never destroyed). WARN rather than BLOCK: benign hook
    # auto-edits (AGENTS.md / state files) routinely leave the tree dirty, and a hard
    # block would brick every real dump.
    if is_dirty(source_workspace):
        warnings.append(
            "source worktree had uncommitted changes at dump time — they are NOT in "
            "the successor's base (preserved in the source worktree; commit + publish "
            "them first if the successor should build on them)"
        )

    # Refresh the integration tracking ref so the contained-check sees the session's
    # just-pushed closure commit. Use an explicit refspec (R2 codex P1-D) so the
    # tracking ref is actually updated, and check rc. A push already updates
    # refs/remotes/origin/<int> directly, so a fetch failure (offline) still leaves
    # the ref authoritative — a stale ref only risks a SAFE false-BLOCK (the session
    # re-dumps), never a wrong-base spawn, so we warn + proceed rather than degrade.
    origin_ref = f"refs/remotes/origin/{int_branch}"
    rc_fetch, _, ferr = _git(
        ["fetch", "origin", f"+refs/heads/{int_branch}:refs/remotes/origin/{int_branch}"],
        source_workspace,
        timeout=30.0,
    )
    if rc_fetch != 0:
        warnings.append(f"fetch origin/{int_branch} failed ({ferr[:80]}); using local tracking ref")
    if not _ref_exists(source_workspace, origin_ref):
        return _degrade(source_workspace, f"origin/{int_branch} not found after fetch")

    # R1-C2: source HEAD must be published to origin/<int> or the successor would
    # branch from stale code. BLOCK (don't degrade — the shared tree's local <int>
    # is just as stale) so the closing session publishes + re-dumps.
    if not is_ancestor(source_workspace, source_head, origin_ref):
        return _block(
            source_workspace,
            f"source HEAD {source_head[:8]} not published to origin/{int_branch}; "
            f"push it (e.g. `git push origin HEAD:{int_branch}`) before dumping the successor",
            integration_branch=int_branch,
            base_sha=source_head,
        )

    # R1-G2: local <int> ahead of origin (owner committed in the main tree, unpushed)
    # → branching from origin/<int> would silently drop those commits. BLOCK.
    if _ref_exists(source_workspace, f"refs/heads/{int_branch}"):
        local_int = f"refs/heads/{int_branch}"
        if not is_ancestor(source_workspace, local_int, origin_ref):
            return _block(
                source_workspace,
                f"local {int_branch} is ahead of origin/{int_branch} (unpushed commits); "
                f"push or reconcile before isolating the successor",
                integration_branch=int_branch,
            )

    base_sha = head_sha(source_workspace)  # informational; worktree branches off origin_ref

    # Collision: a worktree dir and/or branch already exists for this task (retry/
    # re-dump / concurrent dump). Classify BOTH the worktree AND the branch before
    # touching either — a clean-looking absence of a worktree must NOT license
    # deleting a branch that still holds unpublished commits (R2 P0-A, both brains).
    existing = classify_worktree(wt, br, int_branch, source_workspace, _link_names(cfg))
    br_head = branch_head(source_workspace, br)  # SHA or None
    branch_exists = br_head is not None
    if existing["exists"] or branch_exists:
        if existing["dirty"]:
            # Unsafe: a same-task worktree with uncommitted work. Retain + BLOCK —
            # do NOT degrade to the shared tree (R1-R3: that re-opens the unsafe class).
            return _block(
                source_workspace,
                f"existing worktree {wt} has uncommitted changes; retained — resolve manually",
                integration_branch=int_branch,
            )
        if existing["exists"] and not existing["published"]:
            return _block(
                source_workspace,
                f"existing worktree {wt} holds unpublished commits; retained — "
                f"publish or remove manually",
                integration_branch=int_branch,
            )
        # R2 P0-A: the BRANCH may carry unpublished commits even with no worktree dir
        # (owner deleted the dir, or a same-name branch lingers). Never `branch -D`
        # such a ref — that destroys the last pointer to committed-but-unpushed work.
        if branch_exists and not is_ancestor(source_workspace, br_head, origin_ref):
            return _block(
                source_workspace,
                f"branch {br} holds unpublished commits ({br_head[:8]} not in "
                f"origin/{int_branch}); publish or delete it manually",
                integration_branch=int_branch,
            )
        # REUSE (dual-brain P1 race): a clean + published worktree already AT the
        # target base (origin/<int> HEAD) is exactly what we'd recreate — reuse it
        # instead of remove+recreate. This makes a same-task concurrent dump
        # idempotent (the loser adopts the winner's worktree rather than clobbering it
        # or writing a relay-stalling BLOCKED), and avoids needless churn on retry.
        origin_head = head_sha_of_ref(source_workspace, origin_ref)
        if (
            existing["exists"]
            and not existing["dirty"]
            and existing["published"]
            and existing["branch_head"]
            and origin_head
            and existing["branch_head"] == origin_head
        ):
            linked = link_files(source_workspace, wt, cfg)
            vws = inject_vscode_workspace(
                source_workspace,
                wt,
                project,
                task,
                spawn_nonce=spawn_nonce,
                role=role,
                is_coordinator=is_coordinator,
            )
            return WorktreeResult(
                status=ST_CREATED,
                spawn_workspace=wt,
                branch=br,
                base_sha=existing["branch_head"],
                integration_branch=int_branch,
                linked=linked,
                warnings=[
                    *warnings,
                    "reused an existing clean+published worktree at the same base",
                ],
                vscode_workspace_file=vws,
                reused=True,  # p6a-fix1 MUST 2: adopted, NOT created — never rollback-remove
            )
        # Otherwise the worktree is stale (base advanced) → drop + recreate.
        if existing["exists"]:
            rc_rm, _, _ = _git(["worktree", "remove", "--force", str(wt)], source_workspace)
            if rc_rm != 0:
                return _block(
                    source_workspace,
                    f"could not remove stale worktree {wt}; retained — resolve manually",
                    integration_branch=int_branch,
                )
        if branch_exists:
            _git(["branch", "-D", br], source_workspace)  # safe: published verified above

    try:
        worktrees_root(cfg, project).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _degrade(source_workspace, f"worktrees_root unwritable: {e}")

    rc, _out, err = _git(
        ["worktree", "add", "-b", br, str(wt), origin_ref], source_workspace, timeout=60.0
    )
    if rc != 0:
        # R2 P1-E: distinguish a concurrent same-task collision (another dump won the
        # race + created the path/branch) from a genuine environmental failure. A
        # collision must BLOCK (a duplicate session on the shared tree re-opens the
        # unsafe class), not degrade.
        low = err.lower()
        _git(["worktree", "prune"], source_workspace)
        if (
            "already exists" in low
            or "already used by worktree" in low
            or "already checked out" in low
        ):
            return _block(
                source_workspace,
                f"worktree/branch for {task} already exists (concurrent dump?); "
                f"retry after the other session settles — not degrading to shared tree",
                integration_branch=int_branch,
            )
        return _degrade(source_workspace, f"git worktree add failed: {err[:200]}")

    linked = link_files(source_workspace, wt, cfg)
    vws = inject_vscode_workspace(
        source_workspace,
        wt,
        project,
        task,
        spawn_nonce=spawn_nonce,
        role=role,
        is_coordinator=is_coordinator,
    )
    created_base = head_sha(wt) or base_sha
    return WorktreeResult(
        status=ST_CREATED,
        spawn_workspace=wt,
        branch=br,
        base_sha=created_base,
        integration_branch=int_branch,
        linked=linked,
        warnings=warnings,
        vscode_workspace_file=vws,
    )


# ─── branch-conflict reclaim / fail-closed (design §5.2 / R2r2-R1) ────────────


class WorktreeConflict(Exception):
    """A worktree/branch collision that is NOT a reclaimable orphan — fail-closed.

    ``git worktree add`` collided with an existing ``<prefix><task>`` branch/worktree
    that is still live / in-flight / awaiting-merge, so reclaiming it would clobber a
    running session's work. The caller must surface this (ack + reason) and let the
    supervisor pick a fresh ``task_id`` or intervene — never silently reuse a dirty /
    active orphan, and never hang (design §5.2).
    """


class WorktreeAddError(Exception):
    """``git worktree add`` failed for a NON-collision (environmental) reason — e.g.
    an unresolvable base ref, an unwritable parent, or a rebuild that still fails.

    Distinct from :class:`WorktreeConflict` so the caller can degrade (fall back to the
    shared tree) rather than treat it as a fatal name clash.
    """


# Substrings git emits when ``worktree add`` collides with an existing branch / worktree
# / path. Matched case-insensitively. Anything NOT matching is an environmental failure.
_WORKTREE_CONFLICT_MARKERS = (
    "already exists",
    "already used by worktree",
    "already checked out",
    "already registered",
    "is already used by",
)


def _is_worktree_conflict(stderr: str) -> bool:
    low = stderr.lower()
    return any(m in low for m in _WORKTREE_CONFLICT_MARKERS)


def add_worktree_or_reclaim_orphan(
    *,
    source_workspace: Path,
    wt: Path,
    branch: str,
    base_ref: str,
    proc_alive: bool,
    in_pending_queue: bool,
    state: WorktreeState,
    timeout: float = 60.0,
) -> None:
    """``git worktree add -b <branch> <wt> <base_ref>``; on a branch/worktree-exists
    collision, reclaim the old one IFF it is a confirmed orphan (§5.3) then rebuild,
    else fail-closed (design §5.2 / R2r2-R1).

    Outcomes:
      * add succeeds (no collision)                       → returns None.
      * collision AND ``is_reclaimable_orphan(...)``      → drop the orphan worktree +
        branch, rebuild from ``base_ref``; returns None.
      * collision AND NOT a reclaimable orphan            → raise :class:`WorktreeConflict`
        (live / queued / awaiting-merge — real work; never silently reused).
      * NON-collision (environmental) failure / rebuild   → raise :class:`WorktreeAddError`
        (caller degrades to the shared tree instead of treating it as a name clash).

    The orphan reclaim force-removes the worktree + deletes the branch — SAFE only
    because :func:`is_reclaimable_orphan` proved all three conditions (dead process,
    not queued, terminal state): there is no live writer and the work was already
    handed back / discarded. This function deliberately takes the three orphan
    conditions as inputs (rather than computing them) — determining process liveness
    (transcript-mtime) and persisted state is the spawn-intent / business merge-back
    layer's job; this is the §5.2 state-machine interface they call.

    🔴 RED-TOP INVARIANT (§五·2 / dual-brain Q5 consensus 2026-06-10 fold-audit): this
    helper does the git *add/reclaim/rebuild* ONLY — it deliberately does NOT touch the
    VS Code workspace, so it never calls :func:`inject_vscode_workspace`. It is currently
    a PARALLEL primitive NOT yet wired into :func:`create_worktree` (which still adds the
    worktree directly + calls inject right after). If a future refactor routes
    ``create_worktree`` through this helper as the real add primitive, the caller MUST
    still call ``inject_vscode_workspace(..., is_coordinator=...)`` after a successful
    add/rebuild — otherwise a reclaimed-and-rebuilt coordinator (中枢) worktree would open
    WITHOUT its red title bar, silently breaking the absolute invariant "只要是中枢窗口就
    必须红顶". Keep inject at the caller/orchestration layer, never bury it past this add.
    """

    def _add() -> tuple[int, str, str]:
        return _git(
            ["worktree", "add", "-b", branch, str(wt), base_ref], source_workspace, timeout=timeout
        )

    rc, _out, err = _add()
    if rc == 0:
        return
    if not _is_worktree_conflict(err):
        raise WorktreeAddError(f"git worktree add failed (non-collision): {err[:200]}")

    # Branch/worktree already exists. Reclaim ONLY a confirmed orphan; otherwise
    # fail-closed (a live / queued / awaiting-merge collision is genuine work).
    if not is_reclaimable_orphan(
        proc_alive=proc_alive, in_pending_queue=in_pending_queue, state=state
    ):
        raise WorktreeConflict(
            f"worktree/branch {branch!r} exists and is NOT a reclaimable orphan "
            f"(proc_alive={proc_alive}, in_pending_queue={in_pending_queue}, "
            f"state={state.value}); fail-closed — pick a fresh task_id or resolve manually"
        )

    # Confirmed orphan → drop the old worktree + branch, then rebuild. The worktree dir
    # may be gone (branch-only collision) → remove/prune are best-effort; ``branch -D``
    # is what clears a lingering ref. A force remove is safe here (orphan proven dead +
    # terminal). Clear any non-registered leftover dir so the rebuild's path is free.
    _git(["worktree", "remove", "--force", str(wt)], source_workspace)
    _git(["worktree", "prune"], source_workspace)
    _git(["branch", "-D", branch], source_workspace)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)

    rc2, _out2, err2 = _add()
    if rc2 != 0:
        raise WorktreeAddError(f"rebuild after reclaiming orphan {branch!r} failed: {err2[:200]}")


# ─── removal / GC ────────────────────────────────────────────────────────────


def remove_worktree(
    repo_workspace: Path,
    wt_path: Path,
    branch: str,
    integration_branch: str,
    ignore_names: set[str] | tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Remove a worktree IFF safe (clean + published). Returns ``(removed, reason)``.

    Fail-safe (the redline this whole feature defends): a worktree with uncommitted
    OR committed-but-unpublished work is RETAINED, never destroyed. Only a clean,
    fully-published worktree is removed; its branch is deleted only when published.
    ``ignore_names`` discounts the engine-linked convenience files (R-ON).
    """
    if not wt_path.exists():
        return False, "worktree path does not exist"
    info = classify_worktree(wt_path, branch, integration_branch, repo_workspace, ignore_names)
    if info["dirty"]:
        return False, "retained: uncommitted changes"
    if not info["published"]:
        return False, "retained: committed but unpublished (would lose work)"
    rc, _, err = _git(["worktree", "remove", str(wt_path)], repo_workspace)
    if rc != 0:
        rc, _, err = _git(["worktree", "remove", "--force", str(wt_path)], repo_workspace)
    # R2 P1-F: only claim success (and delete the branch + drop the sidecar) when the
    # remove actually succeeded — otherwise the caller must keep the recovery pointer.
    if rc != 0:
        return False, f"retained: worktree remove failed ({err[:80]})"
    # Delete the branch only when fully published (info.published already True).
    if branch:
        _git(["branch", "-d", branch], repo_workspace)
    _git(["worktree", "prune"], repo_workspace)
    return True, "removed (clean + published)"


def list_worktrees(repo_workspace: Path) -> list[dict]:
    """Parse ``git worktree list --porcelain`` into ``[{path, head, branch}, ...]``."""
    rc, out, _ = _git(["worktree", "list", "--porcelain"], repo_workspace)
    if rc != 0:
        return []
    items: list[dict] = []
    cur: dict = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur:
                items.append(cur)
            cur = {"path": line[len("worktree ") :], "head": None, "branch": None}
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD ") :]
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch ") :]
    if cur:
        items.append(cur)
    return items


# ─── terminal-task GC (handoff worktree {list,gc}) ───────────────────────────

_BLOCKED_SUFFIX = ".BLOCKED.md"


def _heartbeat_fresh(cfg: _config.Config, project: str, task: str) -> bool:
    """True iff ``queue/<task>.heartbeat`` was touched within ``HEARTBEAT_LIVE_SEC``.

    A fresh heartbeat = a LIVE session still working in the worktree → GC must never
    reclaim it. Absent / stale = the session closed (the handoff closure kills its
    heartbeat) → the worktree is a GC candidate.
    """
    hb = cfg.queue_dir(project) / f"{task}.heartbeat"
    try:
        return (time.time() - hb.stat().st_mtime) < HEARTBEAT_LIVE_SEC
    except OSError:
        return False


def find_reclaimable(cfg: _config.Config, project: str) -> list[dict]:
    """One record per ``.worktree`` sidecar whose session is no longer live.

    R2 P0-C (Gemini): the serial relay never writes ``A.done`` (task A closes by
    dumping B with ``--status active``), so gating on ``.done``/``.BLOCKED`` leaks
    every happy-path worktree. The real reclaim signal is **the session is gone** —
    proven by an absent/stale heartbeat — combined with the fail-safe (clean +
    published) check at removal time. A LIVE heartbeat → skip (never pull a running
    tab's rug). Record carries ``live`` + ``branch_published`` (for the orphan-branch
    case where the worktree dir was manually removed).
    """
    ack = cfg.ack_dir(project)
    if not ack.is_dir():
        return []
    out: list[dict] = []
    for sc in sorted(ack.glob("*.worktree")):
        task = sc.stem
        try:
            info = json.loads(sc.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {}
        wt_path = Path(info["path"]) if info.get("path") else None
        recorded_source = Path(info["source_workspace"]) if info.get("source_workspace") else None
        branch = info.get("branch")
        intb = info.get("integration_branch")
        # P0 (dual-brain Gemini): the recorded source is the PREDECESSOR's worktree
        # (the chain dumps B from A's worktree). Once A is GC'd, that path is gone and
        # the old code marked B "unresolved" → gc skipped B → the whole descendant
        # chain leaked forever. All worktrees share ONE git repo, so fall back to the
        # main repo for the git analysis/removal — any valid checkout of the repo works.
        main_repo = cfg.workspace_root / project
        source = recorded_source if (recorded_source and recorded_source.exists()) else None
        if source is None and main_repo.exists():
            source = main_repo
        # Discount the files THIS worktree actually linked (codex P0: not current
        # config, which may have drifted), falling back to current config.
        ignore = set(info.get("linked") or _link_names(cfg))
        live = _heartbeat_fresh(cfg, project, task)
        if wt_path is None or source is None:
            classification = {"exists": bool(wt_path and wt_path.exists()), "unresolved": True}
            branch_pub = None
        elif not wt_path.exists():
            # Dir gone — but the branch may linger (Gemini P1-6). Resolve its
            # publication so gc can delete a published orphan branch but RETAIN an
            # unpublished one.
            bh = branch_head(source, branch) if branch else None
            origin_ref = f"refs/remotes/origin/{intb}"
            branch_pub = (
                is_ancestor(source, bh, origin_ref)
                if (bh and _ref_exists(source, origin_ref))
                else (False if bh else None)
            )
            classification = {"exists": False}
        else:
            classification = classify_worktree(wt_path, branch or "", intb or "", source, ignore)
            branch_pub = classification.get("published")
        out.append(
            {
                "task": task,
                "sidecar": sc,
                "path": wt_path,
                "source": source,
                "branch": branch,
                "integration_branch": intb,
                "classification": classification,
                "live": live,
                "branch_published": branch_pub,
                "ignore": ignore,
            }
        )
    return out


def gc(cfg: _config.Config, project: str | None, *, execute: bool) -> int:
    """Reclaim worktrees whose session has closed. Dry-run by default (R1-X5).

    Skips LIVE worktrees (fresh heartbeat). Removes ONLY clean + published worktrees
    (``remove_worktree`` enforces the fail-safe); dirty / unpublished are RETAINED.
    A worktree dir gone but a *published* branch lingering → the orphan branch is
    deleted + sidecar dropped; an *unpublished* orphan branch is RETAINED (keep the
    last pointer to its commits). The ``.worktree`` sidecar is dropped only once the
    worktree is actually removed (R2 P1-F).
    """
    projects = [project] if project else _iter_projects(cfg)
    total = 0
    reclaimed = 0
    for proj in projects:
        for rec in find_reclaimable(cfg, proj):
            total += 1
            cls = rec["classification"]
            wt_path = rec["path"]
            task = rec["task"]
            if rec["live"]:
                print(f"  skip {proj}/{task}: live session (fresh heartbeat)")
                continue
            if cls.get("unresolved"):
                print(f"  ? {proj}/{task}: sidecar unresolved (source missing) — skip")
                continue
            if not cls.get("exists"):
                # Worktree dir gone. Clean a PUBLISHED orphan branch; retain an
                # unpublished one (its commits would be lost otherwise).
                if rec["branch_published"] is False:
                    print(
                        f"  retain {proj}/{task}: orphan branch {rec['branch']} unpublished — kept"
                    )
                    continue
                if execute:
                    if rec["branch"] and rec["branch_published"]:
                        _git(["branch", "-d", rec["branch"]], rec["source"])
                        _git(["worktree", "prune"], rec["source"])
                    rec["sidecar"].unlink(missing_ok=True)
                print(
                    f"  {'rm' if execute else 'would rm'} {proj}/{task}: stale sidecar (worktree gone)"
                )
                reclaimed += 1
                continue
            if not execute:
                verdict = (
                    "removable (clean+published)"
                    if (not cls.get("dirty") and cls.get("published"))
                    else "RETAIN: " + ("dirty" if cls.get("dirty") else "unpublished")
                )
                print(f"  would gc {proj}/{task} @ {wt_path}: {verdict}")
                continue
            removed, reason = remove_worktree(
                rec["source"],
                wt_path,
                rec["branch"] or "",
                rec["integration_branch"] or "",
                rec["ignore"],
            )
            print(f"  {'rm' if removed else 'retain'} {proj}/{task}: {reason}")
            if removed:
                rec["sidecar"].unlink(missing_ok=True)
                reclaimed += 1
        src = cfg.workspace_root / proj
        if execute and src.exists():
            _git(["worktree", "prune"], src)
    if total == 0:
        print("[worktree gc] nothing to reclaim — no closed-session worktrees.")
    else:
        verb = "reclaimed" if execute else "would reclaim"
        print(f"[worktree gc] {verb} {reclaimed}/{total} worktree(s).")
        if not execute:
            print("[worktree gc] dry-run — re-run with --execute to apply.")
    return 0


def _iter_projects(cfg: _config.Config) -> list[str]:
    if not cfg.home.exists():
        return []
    skip = {"locks", "_recovery"}
    return sorted(p.name for p in cfg.home.iterdir() if p.is_dir() and p.name not in skip)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="handoff worktree",
        description="Inspect / reclaim per-session git worktrees.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="List recorded worktrees + classification.")
    p_list.add_argument("--project", default=None)
    p_gc = sub.add_parser("gc", help="Reclaim terminal-task worktrees (dry-run default).")
    p_gc.add_argument("--project", default=None)
    p_gc.add_argument("--execute", action="store_true", help="Actually remove (default: dry-run).")
    args = ap.parse_args(argv)
    cfg = _config.load()

    if args.cmd == "list":
        projects = [args.project] if args.project else _iter_projects(cfg)
        any_found = False
        for proj in projects:
            for rec in find_reclaimable(cfg, proj):
                any_found = True
                cls = rec["classification"]
                tag = (
                    "clean+published"
                    if (not cls.get("dirty") and cls.get("published"))
                    else "dirty"
                    if cls.get("dirty")
                    else "unpublished/unresolved"
                )
                print(f"  {proj}/{rec['task']}  {rec['path']}  [{tag}]")
        if not any_found:
            print("[worktree list] no terminal-task worktrees recorded.")
        return 0

    if args.cmd == "gc":
        return gc(cfg, args.project, execute=args.execute)
    return 2


if __name__ == "__main__":
    sys.exit(main())
