"""``handoff spawn`` — fresh-spawn intent producer (design §13 A' / Phase 6a).

Project-agnostic. NO v5.4 retro-mandate gate (a missing ``--retro-evidence`` never exits 4) and
NO roadmap injection. ONE exception (Step1 G4 收口): ``--role supervisor_succession`` — the only
role that closes a predecessor coordinator window — is NOT a manual CLI path; it demands the
one-time ``--succession-token`` that a retro-gated ``handoff audit-close --coordinator --status
active`` issues (see :mod:`handoff_fanout.succession_authority`), so a coordinator relay can
never bypass the retro mandate through this producer. Produces exactly the artifacts the
watchdog (``install/auto-continue.sh``) already consumes for a fresh window — so the consumer
side needs no change:

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
rejected candidate B, would plant a legal bypass next to the retro mandate).

DELIBERATE dump-mirror duplication (single consolidated note — p6a-fix1 SHOULD): two pieces are
authored here against ``dump``'s contracts rather than shared via a ``dump`` refactor, because the
"dump unchanged" red line forbids editing ``dump`` to delegate:
  1. the singlepane workspace/sidecar JSON shapes mirror ``dump.maybe_write_singlepane_sidecar``'s
     (the SAME watchdog contract) + an additive ``isolation`` field (the watchdog's ``json_get``
     reads only known keys, so the extra field is tolerated);
  2. ``_active_singlepane_worker`` (MUST 3, design §5.4 concurrency REJECT) mirrors ``dump``'s
     file-based active-pane signal (sidecar + non-terminal ``.uri``), sharing only the standalone
     ``spawn_lock`` module.
A follow-up PURE refactor extracting both into a shared module that ``dump`` and ``spawn`` import
is the recommended single consolidation point.

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
from handoff_fanout import memory_baseline as _memory_baseline
from handoff_fanout import spawn_nonce as _spawn_nonce
from handoff_fanout import succession_authority as _authority
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

# close_policy enum the watchdog's try_autoclose acts on — anything else is unactionable.
CLOSE_KEEP = "keep"
CLOSE_PREDECESSOR = "close_predecessor"
CLOSE_POLICIES = (CLOSE_KEEP, CLOSE_PREDECESSOR)


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
    ``title_for``) + the 3 single-pane UX keys ONLY, never the coordinator's own settings/inject
    config block (per-project gating must stay in the repo's own ``.vscode``). Key set is locked by
    the singlepane sidecar tests on the ``dump`` side and mirrored here.

    §五·2 red-top (semantic-merge gap closed 2026-06-10, dual-brain codex MUST + gemini MUST):
    ``role=supervisor_succession`` IS the next coordinator window (design §3/§6: 中枢/继任 =
    singlepane + close_predecessor), and owner law says EVERY coordinator window must be red-topped
    regardless of spawn path — so derive ``is_coordinator`` from the role (single source of truth,
    no extra flag that could contradict it) and apply the same 🧭中枢· prefix + red titleBar as
    ``worktree.inject_vscode_workspace``/``dx-spawn --coordinator``. The prefix WRAPS the
    nonce-bound title, so the watchdog's substring nonce gate is untouched. The red-top VISUAL keys
    are not the coordinator inject-config block the THIN rule bans — gating still lives in the
    target repo's ``.vscode``. A ``role=worker`` spawn stays byte-identical (zero regression)."""
    title = (
        _spawn_nonce.title_for(project=project, task_id=task, role=role, nonce=nonce)
        + " [singlepane]${separator}${activeEditorShort}"
    )
    settings: dict[str, object] = {
        "window.title": title,
        "workbench.activityBar.location": "hidden",
        "workbench.startupEditor": "none",
        "claudeCode.preferredLocation": "panel",
    }
    if role == ROLE_SUCCESSION:
        settings["window.title"] = _worktree._COORDINATOR_TITLE_PREFIX + title
        settings["workbench.colorCustomizations"] = dict(_worktree._COORDINATOR_RED_TITLEBAR)
    # Step2 B 轨二: session-identity env signal — LAST key (byte-precise golden diff).
    # ``role`` here IS the session role (worker / supervisor_succession), no override.
    settings[_worktree.SESSION_ENV_SETTINGS_KEY] = _worktree.session_env_osx(role=role, task=task)
    return json.dumps(
        {
            "folders": [{"path": str(src)}],
            "settings": settings,
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


def _active_singlepane_worker(
    cfg: _config.Config, project: str, *, exclude_task: str
) -> str | None:
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

    ``supervisor_succession`` is exempt from the active-worker REJECT only (it REPLACES its
    predecessor window, design §6 — mirrors ``dump.singlepane_worker_guard``); it is NOT
    exempt from the lock (t41b-fix1): the watchdog's §6 pending-intent gate scans
    ``queue/*.uri`` under this same lock and counts on every spawn-side publisher holding
    it — an unlocked succession publish could land its .uri between the gate's scan and
    its close decision."""
    try:
        with project_spawn_lock(project, root=cfg.home):
            if role == ROLE_WORKER:
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
            f"singlepane project {project!r}: {role} spawn for {task!r} REJECTED — "
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


# Bounded wait for the project spawn lock on the WORKTREE path (Phase 7 fix). Parallel
# worktree workers are LEGITIMATE (design §2.2) — but truly concurrent spawns mutate the
# SAME source repo (`git fetch` tracking refs / `git worktree add -b` writing upstream
# config into .git/config), and git's own lock files turn that into spurious
# "could not lock config file" fail-closes. So unlike singlepane (§5.4 immediate
# REJECT), worktree spawns QUEUE on the same project `.spawn.lock` — aligned with the
# wait's order of magnitude with the lock TTL (a crashed holder is stale-broken anyway).
_WORKTREE_LOCK_WAIT = 120.0


def _spawn_worktree(
    *, cfg, project, task, role, src, nonce, close_policy, predecessor_nonce, prompt_text, queue_dir
) -> int:
    """Serialize create+publish under the project spawn lock (one critical section).

    Besides the git-level serialization above, holding the lock across the sidecar/.uri
    publish keeps the watchdog's try_autoclose contract honest: its critical section
    reads sidecars under this same lock assuming every sidecar WRITER holds it too
    (R2 lock-order TOCTOU fix) — which was true for dump's and spawn's singlepane
    paths but not, before this, for the worktree path."""
    try:
        with project_spawn_lock(project, root=cfg.home, wait=_WORKTREE_LOCK_WAIT):
            return _produce_worktree(
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
            f"worktree project {project!r}: spawn for {task!r} REJECTED — the project "
            f"spawn lock stayed held past the {_WORKTREE_LOCK_WAIT:.0f}s wait ({e}); "
            "retry after the concurrent spawn/autoclose settles"
        )
        return EXIT_FAIL_CLOSED


def _produce_worktree(
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
        # §五·2 red-top: a succession spawn IS the next coordinator window — derive from
        # role (single source of truth; see _singlepane_workspace_json's docstring).
        is_coordinator=(role == ROLE_SUCCESSION),
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
    succession_token: str | None = None,
) -> int:
    """Orchestrate one fresh spawn. Returns ``0`` on success, ``2`` fail-closed (never raises for a
    semantic error; never returns the retro RETRY code 4).

    Step1 G4 收口: ``role=supervisor_succession`` (the only role that closes a predecessor
    coordinator window) is NOT a manual CLI path — it requires the one-time
    ``--succession-token`` that ``handoff audit-close --coordinator --status active`` issues
    after its retro gate passes (see :mod:`handoff_fanout.succession_authority`). A worker
    spawn never takes a token."""
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
    # SHOULD (p6a-fix1): close_policy is an enum the watchdog acts on; an unknown value in
    # the sidecar would be silently unactionable downstream — reject it here instead.
    if close_policy is not None and close_policy not in CLOSE_POLICIES:
        _err(f"--close-policy must be one of {'/'.join(CLOSE_POLICIES)}: {close_policy!r}")
        return EXIT_FAIL_CLOSED
    # SHOULD (p6a-fix1): a succession's whole purpose is closing its predecessor window;
    # without that window's nonce it cannot be identified → the intent is unactionable.
    if role == ROLE_SUCCESSION and predecessor_nonce is None:
        _err(
            "role=supervisor_succession requires --predecessor-nonce (the nonce of the "
            "predecessor window it closes)"
        )
        return EXIT_FAIL_CLOSED
    # ── G4 收口 (Step1 / tribrain MUST#1): succession is retro-gated, never manual ──
    if succession_token is not None and role != ROLE_SUCCESSION:
        _err("--succession-token is only valid with --role supervisor_succession")
        return EXIT_FAIL_CLOSED
    if role == ROLE_SUCCESSION and succession_token is None:
        _err(
            "role=supervisor_succession is not a manual CLI path (G4 retro-mandate 收口): "
            "close the coordinator leg via `handoff audit-close --coordinator "
            "--status active` — after its retro gate passes it issues the one-time "
            "succession authority this spawn requires (--succession-token <path>)"
        )
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
        close_policy = CLOSE_PREDECESSOR if role == ROLE_SUCCESSION else CLOSE_KEEP
    elif (close_policy == CLOSE_PREDECESSOR) != (role == ROLE_SUCCESSION):
        # codex SHOULD (redtop-succ verification round): an EXPLICIT close policy that
        # contradicts the role is unactionable metadata — the watchdog/extension act on
        # role (succession closes its predecessor; a worker is never closed), so a
        # "succession+keep" or "worker+close_predecessor" sidecar would lie about what
        # the consumers will actually do. Reject the combo fail-closed rather than bake
        # the contradiction into the intent.
        _err(
            f"--close-policy {close_policy!r} contradicts --role {role!r} "
            "(succession⇔close_predecessor, worker⇔keep) — refusing the intent"
        )
        return EXIT_FAIL_CLOSED

    # ── G4 收口: validate + CONSUME the one-time succession authority LAST, just
    # before production (so an earlier arg/config rejection never burns a token).
    # A consumed token is gone even if the produce step then fails — conservative:
    # re-issue via a fresh retro-gated audit-close rather than leave authority reusable.
    if role == ROLE_SUCCESSION:
        # Step2 C.2: bind the consume to THIS spawn's task — the token authorizes one
        # designated successor (the audit-close --task), not any succession in-project.
        ok, reason = _authority.consume_token(
            Path(succession_token).expanduser(),
            home=cfg.home,
            project=project,
            expected_task=task,
        )
        if not ok:
            _err(
                f"succession authority rejected: {reason} — obtain a fresh one-time "
                "token via `handoff audit-close --coordinator --status active` "
                f"(TTL {_authority.TOKEN_TTL_SECONDS}s, single use)"
            )
            return EXIT_FAIL_CLOSED

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
        rc = _spawn_worktree(**common)
    else:
        rc = _spawn_singlepane(**common)
    # Step2 契约 A (G3): a succession spawn IS a coordinator dispatch — record the
    # dispatch-time memory snapshot baseline its own future relay compares against.
    # AFTER the publish succeeded so a failed/rolled-back intent never leaves a
    # baseline behind; best-effort inside (a baseline failure never fails the spawn).
    if rc == EXIT_OK and role == ROLE_SUCCESSION:
        _memory_baseline.write_baseline(
            home=cfg.home, project=project, coordinator_task=task, workspace=src
        )
    return rc


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="handoff spawn",
        description="Fresh-spawn intent producer (no retro gate / no roadmap; design §13 A'). "
        "EXCEPTION (G4 收口): --role supervisor_succession requires the one-time "
        "--succession-token issued by the retro-gated `handoff audit-close --coordinator "
        "--status active` — a bare manual succession spawn is rejected.",
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
    p.add_argument(
        "--succession-token",
        default=None,
        dest="succession_token",
        help="one-time succession authority issued by `handoff audit-close --coordinator "
        "--status active` (required for --role supervisor_succession; G4 收口 — a bare "
        "manual succession spawn is rejected)",
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
        succession_token=args.succession_token,
    )


if __name__ == "__main__":
    sys.exit(main())
