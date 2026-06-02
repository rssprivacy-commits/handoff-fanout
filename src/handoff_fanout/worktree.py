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
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from handoff_fanout import config as _config

MODE_OFF = "off"
MODE_REPORT = "report"
MODE_ON = "on"

# Outcome statuses for create_worktree.
ST_OFF = "off"  # feature disabled — spawn on the shared tree (byte-identical legacy)
ST_REPORT = "report"  # report-only — computed, nothing mutated, spawn on shared tree
ST_CREATED = "created"  # worktree created — spawn isolated
ST_DEGRADED = "degraded"  # environmental unavailability — spawn on shared tree + loud warn
ST_BLOCKED = "blocked"  # unsafe (unpublished work / dirty collision) — caller must BLOCK


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


def _ref_exists(workspace: Path, ref: str) -> bool:
    rc, _, _ = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], workspace)
    return rc == 0


def is_ancestor(workspace: Path, ancestor: str, descendant: str) -> bool:
    """True iff ``ancestor`` commit is reachable from ``descendant`` (⊆ history)."""
    rc, _, _ = _git(["merge-base", "--is-ancestor", ancestor, descendant], workspace)
    return rc == 0


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
      2. sentinels ``$HANDOFF_HOME/worktree.enabled`` (all projects) /
         ``$HANDOFF_HOME/<project>/worktree.enabled`` (one project) → on.
      3. config ``worktree_projects`` lists ``project`` → on.
      4. config ``worktree_mode``.
      5. off.
    """
    if env is None:
        env = dict(os.environ)
    em = _env_mode(env)
    if em is not None:
        return em
    if (cfg.home / "worktree.enabled").exists() or (
        cfg.home / project / "worktree.enabled"
    ).exists():
        return MODE_ON
    if project in cfg.worktree_projects:
        return MODE_ON
    if cfg.worktree_mode in (MODE_OFF, MODE_REPORT, MODE_ON):
        return cfg.worktree_mode
    return MODE_OFF


# ─── integration-branch resolution (R1-X2) ───────────────────────────────────

# Branch-name prefixes that must NEVER be chosen as the integration branch — a
# worktree session's own HEAD is on one of these, so `rev-parse --abbrev-ref HEAD`
# would otherwise pick a task branch.
_TASK_BRANCH_PREFIXES = ("handoff/", "task/")


def _looks_like_task_branch(name: str) -> bool:
    return any(name.startswith(p) for p in _TASK_BRANCH_PREFIXES)


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
        if name and not _looks_like_task_branch(name):
            return name

    if allow_network and has_remote(workspace):
        rc, out, _ = _git(["remote", "show", "origin"], workspace, timeout=20.0)
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("HEAD branch:"):
                    name = line.split(":", 1)[1].strip()
                    if name and name != "(unknown)" and not _looks_like_task_branch(name):
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
    # report-only: the command that WOULD run (for the log), without running it.
    planned_cmd: str | None = None

    @property
    def is_blocked(self) -> bool:
        return self.status == ST_BLOCKED

    @property
    def is_worktree(self) -> bool:
        return self.status == ST_CREATED


# ─── worktree classification (R1-C3) ─────────────────────────────────────────


def classify_worktree(
    wt_path: Path, branch: str, integration_branch: str, repo_workspace: Path
) -> dict:
    """Classify an existing worktree's safety for removal/reuse.

    Returns ``{exists, dirty, branch_head, published}`` where:
      * ``dirty``     — uncommitted changes (``git status --porcelain`` non-empty).
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
    rc, out, _ = _git(["status", "--porcelain"], wt_path)
    info["dirty"] = bool(rc != 0 or out.strip())
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

    Never overwrites an existing path in the worktree (a tracked file of the same
    name wins). Best-effort: a failed link is skipped (logged by the caller).
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
            dst.symlink_to(src.resolve())
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


def create_worktree(
    *,
    source_workspace: Path,
    project: str,
    task: str,
    cfg: _config.Config,
    mode: str,
    env: dict[str, str] | None = None,
) -> WorktreeResult:
    """Create (or report/degrade/block) a per-session worktree for ``task``.

    Returns a ``WorktreeResult``; the caller substitutes ``spawn_workspace`` for the
    successor session's artifacts and aborts the dump iff ``is_blocked``.

    Distinguishes **environmental unavailability** (degrade to shared tree + warn:
    not a git repo / no remote / unresolved integration branch / ``worktree add``
    failure) from **unsafe state** (BLOCK, never silently proceed: source HEAD not
    published to the integration branch / a dirty same-task worktree collision) —
    R1-C2 / R1-R3.
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

    # Best-effort refresh of the integration tracking ref so the contained-check
    # below sees the session's just-pushed closure commit (the push already updated
    # refs/remotes/origin/<int>, but fetch is idempotent + covers a tracking miss).
    _git(["fetch", "origin", int_branch], source_workspace, timeout=30.0)
    origin_ref = f"refs/remotes/origin/{int_branch}"
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

    # Collision: a worktree dir or branch already exists for this task (retry/re-dump).
    existing = classify_worktree(wt, br, int_branch, source_workspace)
    branch_exists = _ref_exists(source_workspace, f"refs/heads/{br}")
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
        # Clean + published (or only a stale branch ref) → safe to drop + recreate.
        if existing["exists"]:
            _git(["worktree", "remove", "--force", str(wt)], source_workspace)
        if branch_exists:
            _git(["branch", "-D", br], source_workspace)

    try:
        worktrees_root(cfg, project).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _degrade(source_workspace, f"worktrees_root unwritable: {e}")

    rc, _out, err = _git(
        ["worktree", "add", "-b", br, str(wt), origin_ref], source_workspace, timeout=60.0
    )
    if rc != 0:
        # Clean up a half-created worktree admin entry, then degrade.
        _git(["worktree", "prune"], source_workspace)
        return _degrade(source_workspace, f"git worktree add failed: {err[:200]}")

    linked = link_files(source_workspace, wt, cfg)
    created_base = head_sha(wt) or base_sha
    return WorktreeResult(
        status=ST_CREATED,
        spawn_workspace=wt,
        branch=br,
        base_sha=created_base,
        integration_branch=int_branch,
        linked=linked,
    )


# ─── removal / GC ────────────────────────────────────────────────────────────


def remove_worktree(
    repo_workspace: Path, wt_path: Path, branch: str, integration_branch: str
) -> tuple[bool, str]:
    """Remove a worktree IFF safe (clean + published). Returns ``(removed, reason)``.

    Fail-safe (the redline this whole feature defends): a worktree with uncommitted
    OR committed-but-unpublished work is RETAINED, never destroyed. Only a clean,
    fully-published worktree is removed; its branch is deleted only when published.
    """
    if not wt_path.exists():
        return False, "worktree path does not exist"
    info = classify_worktree(wt_path, branch, integration_branch, repo_workspace)
    if info["dirty"]:
        return False, "retained: uncommitted changes"
    if not info["published"]:
        return False, "retained: committed but unpublished (would lose work)"
    rc, _, err = _git(["worktree", "remove", str(wt_path)], repo_workspace)
    if rc != 0:
        _git(["worktree", "remove", "--force", str(wt_path)], repo_workspace)
    # Delete the branch only if it is fully published (info.published already True).
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


def _terminal_tasks(cfg: _config.Config, project: str) -> set[str]:
    qd = cfg.queue_dir(project)
    if not qd.is_dir():
        return set()
    done = {f.stem for f in qd.glob("*.done")}
    blocked = {f.name[: -len(_BLOCKED_SUFFIX)] for f in qd.glob(f"*{_BLOCKED_SUFFIX}")}
    return done | blocked


def find_reclaimable(cfg: _config.Config, project: str) -> list[dict]:
    """One record per terminal task that still has a ``.worktree`` sidecar.

    Record: ``{task, sidecar, path, source, branch, integration_branch,
    classification}``. ``classification`` is from ``classify_worktree`` (or a
    ``missing`` marker when the worktree dir is already gone).
    """
    ack = cfg.ack_dir(project)
    if not ack.is_dir():
        return []
    terminal = _terminal_tasks(cfg, project)
    out: list[dict] = []
    for sc in sorted(ack.glob("*.worktree")):
        task = sc.stem
        if task not in terminal:
            continue
        try:
            info = json.loads(sc.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {}
        wt_path = Path(info["path"]) if info.get("path") else None
        source = Path(info["source_workspace"]) if info.get("source_workspace") else None
        branch = info.get("branch")
        intb = info.get("integration_branch")
        if wt_path is None or source is None or not source.exists():
            classification = {"exists": bool(wt_path and wt_path.exists()), "unresolved": True}
        elif not wt_path.exists():
            classification = {"exists": False}
        else:
            classification = classify_worktree(wt_path, branch or "", intb or "", source)
        out.append(
            {
                "task": task,
                "sidecar": sc,
                "path": wt_path,
                "source": source,
                "branch": branch,
                "integration_branch": intb,
                "classification": classification,
            }
        )
    return out


def gc(cfg: _config.Config, project: str | None, *, execute: bool) -> int:
    """Reclaim worktrees of terminal tasks. Dry-run by default (R1-X5).

    Removes ONLY clean + published worktrees (``remove_worktree`` enforces the
    fail-safe); dirty / unpublished are RETAINED with a printed reason. The
    ``.worktree`` sidecar is dropped only once the worktree is actually removed (or
    was already gone). ``git worktree prune`` runs at the end to clear stale admin
    entries for any reclaimed dir.
    """
    projects = [project] if project else _iter_projects(cfg)
    total = 0
    reclaimed = 0
    for proj in projects:
        for rec in find_reclaimable(cfg, proj):
            total += 1
            cls = rec["classification"]
            wt_path = rec["path"]
            if cls.get("unresolved"):
                print(f"  ? {proj}/{rec['task']}: sidecar unresolved (source missing) — skip")
                continue
            if not cls.get("exists"):
                # worktree dir already gone → just drop the stale sidecar.
                if execute:
                    rec["sidecar"].unlink(missing_ok=True)
                print(
                    f"  {'rm' if execute else 'would rm'} {proj}/{rec['task']}: stale sidecar (worktree gone)"
                )
                reclaimed += 1
                continue
            if not execute:
                verdict = (
                    "removable (clean+published)"
                    if (not cls.get("dirty") and cls.get("published"))
                    else "RETAIN: " + ("dirty" if cls.get("dirty") else "unpublished")
                )
                print(f"  would gc {proj}/{rec['task']} @ {wt_path}: {verdict}")
                continue
            removed, reason = remove_worktree(
                rec["source"], wt_path, rec["branch"] or "", rec["integration_branch"] or ""
            )
            print(f"  {'rm' if removed else 'retain'} {proj}/{rec['task']}: {reason}")
            if removed:
                rec["sidecar"].unlink(missing_ok=True)
                reclaimed += 1
        # admin GC for any source repo (best-effort): prune stale worktree entries.
        src = cfg.workspace_root / proj
        if execute and src.exists():
            _git(["worktree", "prune"], src)
    if total == 0:
        print("[worktree gc] nothing to reclaim — no terminal tasks with worktrees.")
    else:
        verb = "reclaimed" if execute else "would reclaim"
        print(f"[worktree gc] {verb} {reclaimed}/{total} terminal-task worktree(s).")
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
