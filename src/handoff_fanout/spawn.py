"""``handoff spawn`` — fresh-spawn intent producer (design §13 A' / Phase 6a).

Project-agnostic. NO v5.4 retro-mandate gate (a missing ``--retro-evidence`` never exits 4) and
NO roadmap injection. Produces exactly the artifacts the watchdog (``install/auto-continue.sh``)
already consumes for a fresh window — so the consumer side needs no change:

  * ``--isolation worktree``  → a per-session git worktree (via ``worktree.create_worktree``)
    whose ``.handoff.code-workspace`` ``window.title`` carries the unguessable ``spawn_nonce``;
    the ``.uri`` ``WORKSPACE`` is the worktree dir (under ``*/worktrees/*`` ⇒ the watchdog's
    COLD_WINDOW path opens that file + gates the submit).
  * ``--isolation singlepane`` → an OUT-OF-TREE ``.handoff.code-workspace`` (``folders``→the real
    repo, so the tree is never dirtied) + the ``queue/<task>.singlepane`` sidecar; the ``.uri``
    ``WORKSPACE`` is the real repo (not under ``*/worktrees/*``) ⇒ the watchdog's SINGLEPANE path
    reads the sidecar for the open target + the nonce submit gate.

Both modes write ``queue/<task>.singlepane`` (the watchdog's ``try_autoclose`` reads
``role``/``predecessor_nonce`` from it regardless of isolation) and publish ``queue/<task>.uri``
LAST (the launchd ``WatchPaths`` trigger — every sidecar must exist before it lands).

This module REUSES the real production primitives (``worktree.create_worktree`` /
``inject_vscode_workspace`` with its new ``spawn_nonce`` kwarg, ``spawn_nonce.title_for``,
``atomic.atomic_replace``). It NEVER invokes, imports, or alters ``dump`` (design §13: ``dump``
stays the retro-gated *old-session handoff* producer — routing a fresh spawn through it, the
rejected candidate B, would plant a legal bypass next to the retro mandate). The singlepane
workspace/sidecar JSON shapes deliberately mirror ``dump.maybe_write_singlepane_sidecar``'s (the
SAME watchdog contract) and add an ``isolation`` field (the watchdog's ``json_get`` reads only
known keys, so the extra field is tolerated). They are authored here against that contract rather
than shared via a ``dump`` refactor because the "dump unchanged" constraint forbids editing
``dump`` to delegate; a follow-up pure extraction so ``dump`` delegates here is recommended.

Fail-closed (design §13: never a partial/残缺 intent): an invalid identity / untrusted (corrupt)
config / missing workspace / an UNSAFE or UNAVAILABLE worktree state all return ``2`` and write
nothing publishable; a worktree created and then a later step failing is rolled back (no orphan).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import urllib.parse
from pathlib import Path

from handoff_fanout import atomic
from handoff_fanout import config as _config
from handoff_fanout import spawn_nonce as _spawn_nonce
from handoff_fanout import worktree as _worktree
from handoff_fanout.spawn_lock import LockHeld, project_spawn_lock

# Kebab-case identity (same shape as dump.TASK_ID_RE / handoff_precheck.TASK_ID_RE — the engine's
# established slug contract; kept local so spawn never imports dump).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
_NONCE_RE = re.compile(r"^[0-9a-f]+$")

EXIT_OK = 0
EXIT_FAIL_CLOSED = 2  # NEVER 4 — that is the retro RETRY code; the fresh-spawn path has no gate.

ROLE_WORKER = "worker"
ROLE_SUCCESSION = "supervisor_succession"

ISOLATION_WORKTREE = "worktree"
ISOLATION_SINGLEPANE = "singlepane"


def _err(msg: str) -> None:
    print(f"❌ [spawn] {msg}", file=sys.stderr)


# ─── prompt / uri ─────────────────────────────────────────────────────────────


def _build_prompt(task: str, *, brief: str | None, prompt: str | None) -> str:
    """The session prompt, prefixed with the 🆔 window-identity token (CLAUDE.md 派会话窗口标识).

    ``--prompt`` is used verbatim; ``--brief`` becomes a short instruction to read that file. The
    🆔``<task>`` prefix is prepended unless the caller already led with it, so the spawned session's
    first message carries the exact id the owner uses to find the window."""
    body = prompt if prompt is not None else f"open `{brief}` and execute per its instructions."
    prefix = f"🆔{task}"
    return body if body.startswith(prefix) else f"{prefix} {body}"


def _build_uri(cfg: _config.Config, prompt_text: str) -> str:
    # Fully percent-encode (safe="") so a prompt containing & / # / ? can't corrupt the query.
    return cfg.uri_template.format(prompt=urllib.parse.quote(prompt_text, safe=""))


def _write_uri(queue_dir: Path, task: str, *, workspace: Path, uri: str) -> None:
    """Publish the launchd trigger LAST. ``WORKSPACE`` drives the watchdog's COLD vs SINGLEPANE
    routing (a worktree dir under ``*/worktrees/*`` ⇒ COLD; the real repo ⇒ SINGLEPANE)."""
    atomic.atomic_replace(queue_dir / f"{task}.uri", f"WORKSPACE={workspace}\nURI={uri}\n")


# ─── sidecar / workspace-file writers (watchdog contract) ──────────────────────


def _write_sidecar(
    queue_dir: Path,
    task: str,
    *,
    workspace: Path | str,
    role: str,
    close_policy: str,
    spawn_nonce: str,
    isolation: str,
    predecessor_nonce: str | None,
) -> None:
    """``queue/<task>.singlepane`` — COMPACT single-line JSON (the watchdog's line-oriented
    ``json_get`` reader + the autoclose ``role``/``predecessor_nonce`` extraction depend on the flat
    one-line shape; do NOT ``indent=`` here). Mirrors ``dump.maybe_write_singlepane_sidecar``'s keys
    + the additive ``isolation`` field."""
    atomic.atomic_replace(
        queue_dir / f"{task}.singlepane",
        json.dumps(
            {
                "workspace": str(workspace),
                "role": role,
                "close_policy": close_policy,
                "spawn_nonce": spawn_nonce,
                "isolation": isolation,
                "predecessor_nonce": predecessor_nonce,
            }
        ),  # ← no indent= on purpose: single-line contract for the bash json_get reader
    )


def _singlepane_workspace_json(*, src: Path, project: str, task: str, role: str, nonce: str) -> str:
    """The OUT-OF-TREE ``.handoff.code-workspace`` content for a singlepane spawn (``folders``→the
    real repo). THIN settings — ``window.title`` (binding project·task·role·nonce via
    ``title_for``) + the 3 single-pane UX keys ONLY, never a coordinator/inject block (per-project
    gating must stay in the repo's own ``.vscode``). Key set is locked by the singlepane sidecar
    tests on the ``dump`` side and mirrored here."""
    title = (
        _spawn_nonce.title_for(project=project, task_id=task, role=role, nonce=nonce)
        + " [singlepane]${separator}${activeEditorShort}"
    )
    return json.dumps(
        {
            "folders": [{"path": str(src)}],
            "settings": {
                "window.title": title,
                "workbench.activityBar.location": "hidden",
                "workbench.startupEditor": "none",
                "claudeCode.preferredLocation": "panel",
            },
        },
        indent=2,
    )


# ─── rollback (fail-closed: never leave a partial intent) ──────────────────────


def _rollback(queue_dir: Path, task: str, *, ws_file: Path | None = None) -> None:
    for p in (queue_dir / f"{task}.uri", queue_dir / f"{task}.singlepane"):
        with contextlib.suppress(OSError):
            p.unlink(missing_ok=True)
    if ws_file is not None:
        with contextlib.suppress(OSError):
            ws_file.unlink(missing_ok=True)


def _remove_worktree_best_effort(
    cfg: _config.Config, src: Path, project: str, task: str, result: _worktree.WorktreeResult
) -> None:
    """Undo a worktree created moments ago when a later publish step failed. A FRESH worktree is
    clean + published (it was branched off ``origin/<int>``), so ``remove_worktree`` reclaims it;
    if anything raced it dirty, ``remove_worktree`` RETAINS it (never destroys work) and we warn."""
    wt_path = _worktree.worktree_path(cfg, project, task)
    branch = result.branch or _worktree.branch_name(cfg, task)
    int_branch = result.integration_branch or ""
    try:
        removed, reason = _worktree.remove_worktree(
            src, wt_path, branch, int_branch, _worktree._link_names(cfg)
        )
    except Exception as e:  # never let rollback raise over the original failure
        _err(
            f"rollback: remove_worktree raised ({e}); worktree {wt_path} retained — resolve manually"
        )
        return
    if not removed:
        _err(
            f"rollback could not remove worktree {wt_path}: {reason} (retained — resolve manually)"
        )


# ─── per-isolation orchestration ───────────────────────────────────────────────


def _active_singlepane_worker(cfg: _config.Config, project: str, *, exclude_task: str) -> str | None:
    """task_id of an existing ACTIVE singlepane worker for ``project`` other than
    ``exclude_task``, else ``None``.

    Active = a ``queue/<task>.singlepane`` sidecar whose ``<task>.uri`` is present and
    NON-terminal (no ``.done`` / ``.BLOCKED.md``) — the same deterministic file-based
    "pane held" signal ``dump``'s Task5.1 guard reads. ``exclude_task`` keeps a same-task
    re-spawn (retry) from rejecting itself. Deliberately authored here against that
    contract rather than imported from ``dump`` (see the module docstring's shared-module
    extraction note)."""
    queue = cfg.queue_dir(project)
    if not queue.exists():
        return None
    for sidecar in sorted(queue.glob("*.singlepane")):
        other = sidecar.stem
        if other == exclude_task:
            continue
        if not (queue / f"{other}.uri").exists():
            continue  # no pending spawn → pane not held by it
        if (queue / f"{other}.done").exists() or (queue / f"{other}.BLOCKED.md").exists():
            continue  # terminal → pane free
        return other
    return None


def _spawn_singlepane(
    *, cfg, project, task, role, src, nonce, close_policy, predecessor_nonce, prompt_text, queue_dir
) -> int:
    """Gate + produce. MUST 3 (p6a-fix1 / design §5.4): ``handoff spawn`` is a PUBLIC entry
    that bypasses ``dump``'s Task5.1 singlepane guard, so it must carry its own hard REJECT:
    two concurrent singlepane workers on one project = two windows landing in the SAME real
    repo (index.lock clashes / overwrites). The check + the whole produce run under the SAME
    project ``.spawn.lock`` dump/autoclose use (``spawn_lock`` is a standalone shared module;
    ``dump`` itself stays untouched), so a concurrent worker #2 cannot slip its artifacts in
    between our check and publish — fail-closed, never 'it shouldn't be concurrent'.
    ``supervisor_succession`` is exempt from the active-worker REJECT (it REPLACES its
    predecessor window, design §6 — mirrors ``dump.singlepane_worker_guard``)."""
    if role != ROLE_WORKER:
        return _produce_singlepane(
            cfg=cfg,
            project=project,
            task=task,
            role=role,
            src=src,
            nonce=nonce,
            close_policy=close_policy,
            predecessor_nonce=predecessor_nonce,
            prompt_text=prompt_text,
            queue_dir=queue_dir,
        )
    try:
        with project_spawn_lock(project, root=cfg.home):
            holder = _active_singlepane_worker(cfg, project, exclude_task=task)
            if holder is not None:
                _err(
                    f"singlepane project {project!r}: worker spawn for {task!r} REJECTED — "
                    f"pane held by active worker {holder!r}. A singlepane project may have "
                    "only ONE active worker; wait for it to finish (its task → done/blocked) "
                    "before spawning here (design §5.4: never spawn concurrently into the "
                    "same real repo)"
                )
                return EXIT_FAIL_CLOSED
            return _produce_singlepane(
                cfg=cfg,
                project=project,
                task=task,
                role=role,
                src=src,
                nonce=nonce,
                close_policy=close_policy,
                predecessor_nonce=predecessor_nonce,
                prompt_text=prompt_text,
                queue_dir=queue_dir,
            )
    except LockHeld as e:
        _err(
            f"singlepane project {project!r}: worker spawn for {task!r} REJECTED — "
            f"a concurrent spawn holds the project lock ({e}); retry after it settles"
        )
        return EXIT_FAIL_CLOSED


def _produce_singlepane(
    *, cfg, project, task, role, src, nonce, close_policy, predecessor_nonce, prompt_text, queue_dir
) -> int:
    sp_dir = cfg.home / project / "singlepane"
    ws_file = sp_dir / f"{task}.handoff.code-workspace"
    uri = _build_uri(cfg, prompt_text)
    try:
        atomic.atomic_replace(
            ws_file,
            _singlepane_workspace_json(src=src, project=project, task=task, role=role, nonce=nonce),
        )
        _write_sidecar(
            queue_dir,
            task,
            workspace=ws_file,
            role=role,
            close_policy=close_policy,
            spawn_nonce=nonce,
            isolation=ISOLATION_SINGLEPANE,
            predecessor_nonce=predecessor_nonce,
        )
        _write_uri(queue_dir, task, workspace=src, uri=uri)  # WORKSPACE=real repo ⇒ SINGLEPANE path
    except Exception as e:
        _err(f"singlepane spawn failed ({e}); rolling back partial intent")
        _rollback(queue_dir, task, ws_file=ws_file)
        return EXIT_FAIL_CLOSED
    print(f"[spawn] ✅ singlepane intent for {project}/{task} (nonce {nonce})")
    return EXIT_OK


def _spawn_worktree(
    *, cfg, project, task, role, src, nonce, close_policy, predecessor_nonce, prompt_text, queue_dir
) -> int:
    result = _worktree.create_worktree(
        source_workspace=src,
        project=project,
        task=task,
        cfg=cfg,
        mode=_worktree.MODE_ON,
        spawn_nonce=nonce,
        role=role,
    )
    if result.is_blocked:
        _err(f"worktree isolation BLOCKED (unsafe state): {result.reason}")
        return EXIT_FAIL_CLOSED
    if result.status != _worktree.ST_CREATED:
        # report / degraded / off: an EXPLICIT --isolation worktree could not be honored. NEVER
        # silently fall back to a shared-tree spawn (禁止静默降级) — fail closed with the reason.
        _err(
            f"worktree isolation unavailable ({result.status}): {result.reason or 'n/a'} — "
            "refusing to spawn (no silent downgrade to the shared tree)"
        )
        return EXIT_FAIL_CLOSED

    wt = result.spawn_workspace  # under */worktrees/* ⇒ the watchdog COLD_WINDOW path
    # The worktree's own .handoff.code-workspace (nonce title) is the open target the watchdog
    # `find`s; the sidecar's workspace field is informational on this path, but try_autoclose reads
    # role/predecessor_nonce from the sidecar so we still write it.
    cws = result.vscode_workspace_file
    if cws is None:
        # MUST 1 (p6a-fix1): inject_vscode_workspace could not bind THIS spawn's nonce into
        # the workspace title (a user-tracked .handoff.code-workspace it must not overwrite,
        # or the write failed). Publishing anyway would bake a title↔sidecar nonce mismatch
        # into the intent — fail closed instead (and reclaim the worktree only if WE made it).
        _err(
            "worktree workspace title cannot carry this spawn's nonce (pre-existing "
            "user-tracked .handoff.code-workspace, or workspace write failure) — "
            "refusing to produce a mismatched intent"
        )
        if not result.reused:
            _remove_worktree_best_effort(cfg, src, project, task, result)
        return EXIT_FAIL_CLOSED
    uri = _build_uri(cfg, prompt_text)
    try:
        _write_sidecar(
            queue_dir,
            task,
            workspace=cws,
            role=role,
            close_policy=close_policy,
            spawn_nonce=nonce,
            isolation=ISOLATION_WORKTREE,
            predecessor_nonce=predecessor_nonce,
        )
        _write_uri(queue_dir, task, workspace=wt, uri=uri)  # WORKSPACE=worktree dir ⇒ COLD path
    except Exception as e:
        _err(f"worktree publish failed ({e}); rolling back partial intent")
        _rollback(queue_dir, task)
        # MUST 2 (p6a-fix1): only remove a worktree THIS call created. A reused one
        # (create_worktree's idempotent-adoption branch) may belong to another live
        # session / the previous relay leg — removing it would be data loss; we roll
        # back only our own sidecar/.uri above and leave the worktree untouched.
        if not result.reused:
            _remove_worktree_best_effort(cfg, src, project, task, result)
        return EXIT_FAIL_CLOSED
    print(f"[spawn] ✅ worktree intent for {project}/{task} on {result.branch} (nonce {nonce})")
    return EXIT_OK


# ─── public entry ───────────────────────────────────────────────────────────────


def run_spawn(
    *,
    project: str,
    task: str,
    role: str,
    isolation: str,
    workspace: str | None = None,
    brief: str | None = None,
    prompt: str | None = None,
    close_policy: str | None = None,
    predecessor_nonce: str | None = None,
) -> int:
    """Orchestrate one fresh spawn. Returns ``0`` on success, ``2`` fail-closed (never raises for a
    semantic error; never returns the retro RETRY code 4)."""
    # ── identity + arg validation (fail-closed) ──
    if not _SLUG_RE.match(project) or len(project) > 60:
        _err(f"project must be kebab-case (a-z 0-9 -), ≤60: {project!r}")
        return EXIT_FAIL_CLOSED
    if not _SLUG_RE.match(task) or len(task) > 60:
        _err(f"task-id must be kebab-case (a-z 0-9 -), ≤60: {task!r}")
        return EXIT_FAIL_CLOSED
    if (brief is None) == (prompt is None):
        _err("provide EXACTLY ONE of --brief or --prompt")
        return EXIT_FAIL_CLOSED
    if predecessor_nonce is not None and not _NONCE_RE.match(predecessor_nonce):
        _err(f"--predecessor-nonce must be lowercase hex: {predecessor_nonce!r}")
        return EXIT_FAIL_CLOSED

    cfg = _config.load()
    # An untrusted (corrupt/unreadable) config fails the unified-spawn switch CLOSED in
    # config.load(); an explicit `unified_spawn_enabled: false` also lands here. Either way refuse
    # to produce a spawn intent off a config we can't trust / that disabled the mechanism.
    if not cfg.unified_spawn_enabled:
        _err("unified_spawn_enabled is False (config untrusted or disabled) — refusing to spawn")
        return EXIT_FAIL_CLOSED

    src = Path(workspace).expanduser() if workspace else (cfg.workspace_root / project)
    if not src.is_dir():
        _err(f"workspace does not exist or is not a directory: {src}")
        return EXIT_FAIL_CLOSED

    if close_policy is None:
        # worker keeps its own window; a succession's whole purpose is to close its predecessor.
        close_policy = "close_predecessor" if role == ROLE_SUCCESSION else "keep"

    nonce = _spawn_nonce.new_nonce()
    prompt_text = _build_prompt(task, brief=brief, prompt=prompt)
    queue_dir = cfg.queue_dir(project)

    common = dict(
        cfg=cfg,
        project=project,
        task=task,
        role=role,
        src=src,
        nonce=nonce,
        close_policy=close_policy,
        predecessor_nonce=predecessor_nonce,
        prompt_text=prompt_text,
        queue_dir=queue_dir,
    )
    if isolation == ISOLATION_WORKTREE:
        return _spawn_worktree(**common)
    return _spawn_singlepane(**common)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="handoff spawn",
        description="Fresh-spawn intent producer (no retro gate / no roadmap; design §13 A').",
    )
    p.add_argument("--project", required=True, help="project slug (kebab-case)")
    p.add_argument("--task-id", required=True, dest="task", help="task id (kebab-case)")
    p.add_argument("--role", choices=[ROLE_WORKER, ROLE_SUCCESSION], default=ROLE_WORKER)
    p.add_argument("--isolation", required=True, choices=[ISOLATION_WORKTREE, ISOLATION_SINGLEPANE])
    p.add_argument(
        "--workspace",
        default=None,
        help="source repo dir (default: <config workspace_root>/<project>)",
    )
    p.add_argument("--brief", default=None, help="path to a brief file the spawned session reads")
    p.add_argument("--prompt", default=None, help="literal prompt text for the spawned session")
    p.add_argument("--close-policy", default=None, dest="close_policy")
    p.add_argument(
        "--predecessor-nonce",
        default=None,
        dest="predecessor_nonce",
        help="nonce of the predecessor window a supervisor_succession closes",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return run_spawn(
        project=args.project,
        task=args.task,
        role=args.role,
        isolation=args.isolation,
        workspace=args.workspace,
        brief=args.brief,
        prompt=args.prompt,
        close_policy=args.close_policy,
        predecessor_nonce=args.predecessor_nonce,
    )


if __name__ == "__main__":
    sys.exit(main())
