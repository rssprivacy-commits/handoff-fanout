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
import os
import re
import sys
import urllib.parse
from pathlib import Path

from handoff_fanout import atomic
from handoff_fanout import config as _config
from handoff_fanout import memory_baseline as _memory_baseline
from handoff_fanout import spawn_nonce as _spawn_nonce
from handoff_fanout import spawner_focus as _spawner_focus
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


# req1 (machine-enforced 大白话 purpose-echo): the cue is the DISTINCTIVE phrase the injection itself
# emits — not the bare noun ``大白话`` (which a literal --prompt may mention incidentally, e.g. "把架构
# 用大白话写给主人", and which must NOT false-skip the injection — the Codex-flagged FIX D bug). A
# prompt that ALREADY carries this exact phrase (a re-dispatch of an injected prompt, or a deliberate
# purpose-bearing literal) is left verbatim (no double-inject). NB the live dx-spawn prompt carries
# only a bare "echo your 🆔" cue (回显本窗口标识), NOT this phrase — it has no purpose, which is exactly
# the gap req1 closes, so it DOES get the injection.
_PURPOSE_ECHO_CUE = "用一句大白话说明你这个会话要做什么"


def _purpose_echo_instruction(task: str, *, brief: str | None) -> str:
    """The 大白话 purpose-echo instruction prepended to a worker prompt (req1). ``brief`` present ⇒
    tell the worker to read it before stating its purpose; absent (a literal --prompt) ⇒ just state
    it. Ends with ``然后 `` so the original body reads on naturally after the prefix logic runs."""
    read = f"读 `{brief}` 后用人话讲清" if brief is not None else "用人话讲清"
    return (
        f"🔴开张第一句先回显：🆔{task} ＋ 用一句大白话说明你这个会话要做什么"
        f"（{read}，别只回显 🆔）。然后 "
    )


def _build_prompt(task: str, *, role: str, brief: str | None, prompt: str | None) -> str:
    """The session prompt, prefixed with the 🆔 window-identity token (CLAUDE.md 派会话窗口标识).

    ``--prompt`` is used verbatim; ``--brief`` becomes a short instruction to read that file. The
    🆔``<task>`` prefix is prepended unless the caller already led with it, so the spawned session's
    first message carries the exact id the owner uses to find the window.

    req1 (machine-enforced, 2026-06-27): a ``worker`` dispatch ALSO gets the 大白话 purpose-echo
    instruction injected, so EVERY dispatched worker self-announces its task in plain language — not
    just the 🆔. Applied to BOTH the ``--brief`` AND the ``--prompt`` path, because the LIVE dispatch
    (``dx-spawn`` → ``handoff spawn``) always converts a brief into ``--prompt`` (a --brief-only
    injection would never reach a real worker). A literal --prompt already carrying ``_PURPOSE_ECHO_CUE``
    is left verbatim (no double-inject). Non-worker roles (``supervisor_succession`` — a coordinator
    relay with its own deliberate continuation prompt) are NEVER injected: req1 is scoped to workers,
    and this keeps the succession prompt byte-identical."""
    prefix = f"🆔{task}"
    inject = role == ROLE_WORKER
    if prompt is not None:
        if inject and _PURPOSE_ECHO_CUE not in prompt:
            instr = _purpose_echo_instruction(task, brief=None)
            if prompt.startswith(prefix):
                # insert right after the leading 🆔{task} (+ its separator) so the id is never
                # duplicated — the live dx-spawn prompt leads with "🆔{task} · …".
                rest = prompt[len(prefix) :].lstrip(" ·")
                body = f"{prefix} {instr}{rest}"
            else:
                body = f"{instr}{prompt}"
        else:
            body = prompt  # verbatim — non-worker role, or the cue is already present
    else:
        open_line = f"open `{brief}` and execute per its instructions."
        body = (_purpose_echo_instruction(task, brief=brief) + open_line) if inject else open_line
    return body if body.startswith(prefix) else f"{prefix} {body}"


def _build_uri(cfg: _config.Config, prompt_text: str) -> str:
    # Fully percent-encode (safe="") so a prompt containing & / # / ? can't corrupt the query.
    return cfg.uri_template.format(prompt=urllib.parse.quote(prompt_text, safe=""))


def _write_uri(
    queue_dir: Path,
    task: str,
    *,
    workspace: Path,
    uri: str,
    is_coordinator: bool,
    spawner_focus: str | None = None,
) -> None:
    """Publish the launchd trigger LAST. ``WORKSPACE`` drives the watchdog's COLD vs SINGLEPANE
    routing (a worktree dir under ``*/worktrees/*`` ⇒ COLD; the real repo ⇒ SINGLEPANE).

    ``is_coordinator`` (place-role-explicit-contract 2026-06-29): the engine KNOWS the role at spawn
    time, so it stamps an explicit ``ROLE=coord`` (coordinator window) / ``ROLE=worker`` line into the
    manifest. The launcher reads ``ROLE=`` directly to tile the just-spawned window (coord→right-half,
    worker→free-quadrant) — mode-agnostic (worktree + singlepane + cold-start, one code path), no
    UI-sniffing. Always emitted (mandatory third line) so every engine-written .uri carries the role.

    ``spawner_focus`` (direct-jump-spawn 2026-06-13 / mp-locate-return 2026-06-14): the validated
    absolute .handoff.code-workspace path of the SPAWNING window (the active coordinator) — from the
    CLI/env OR env-independent self-identification (cwd worktree / singlepane focus marker). Written as
    an additive ``SPAWNER_FOCUS=`` line the watchdog reads → exports → ``code-router.sh`` runs the
    one-step ``focus-jump`` to the spawner's desktop before opening this worker (so the worker is born
    on the spawner's Space). Omitted when absent → the SPAWNER_FOCUS line is suppressed (向后兼容 for
    that optional line)."""
    role_token = "coord" if is_coordinator else "worker"
    body = f"WORKSPACE={workspace}\nURI={uri}\nROLE={role_token}\n"
    if spawner_focus:
        body += f"SPAWNER_FOCUS={spawner_focus}\n"
    atomic.atomic_replace(queue_dir / f"{task}.uri", body)


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
    wave_id: str | None = None,
) -> None:
    """``queue/<task>.singlepane`` — COMPACT single-line JSON (the watchdog's line-oriented
    ``json_get`` reader + the autoclose ``role``/``predecessor_nonce`` extraction depend on the flat
    one-line shape; do NOT ``indent=`` here). Mirrors ``dump.maybe_write_singlepane_sidecar``'s keys
    + the additive ``isolation`` field. ``wave_id`` (§6c C5) is written ONLY when the dispatch
    belongs to a wave — a non-wave sidecar stays byte-identical to the pre-§6c shape."""
    payload = {
        "workspace": str(workspace),
        "role": role,
        "close_policy": close_policy,
        "spawn_nonce": spawn_nonce,
        "isolation": isolation,
        "predecessor_nonce": predecessor_nonce,
    }
    if wave_id is not None:
        payload["wave_id"] = wave_id  # additive; json_get readers ignore unknown keys
    atomic.atomic_replace(
        queue_dir / f"{task}.singlepane",
        json.dumps(payload),
        # ← no indent= on purpose: single-line contract for the bash json_get reader
    )


def _singlepane_workspace_json(
    *, src: Path, project: str, task: str, role: str, nonce: str, focus_path: str
) -> str:
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
    # direct-jump-spawn: also carry this window's own focus path (the out-of-tree
    # .handoff.code-workspace realpath) so a session here can self-report when it spawns.
    settings[_worktree.SESSION_ENV_SETTINGS_KEY] = _worktree.session_env_osx(
        role=role, task=task, window_focus_path=focus_path
    )
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
    *, cfg, project, task, role, src, nonce, close_policy, predecessor_nonce, prompt_text,
    queue_dir, wave_id=None, spawner_focus=None,
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
                wave_id=wave_id,
                spawner_focus=spawner_focus,
            )
    except LockHeld as e:
        _err(
            f"singlepane project {project!r}: {role} spawn for {task!r} REJECTED — "
            f"a concurrent spawn holds the project lock ({e}); retry after it settles"
        )
        return EXIT_FAIL_CLOSED


def _produce_singlepane(
    *, cfg, project, task, role, src, nonce, close_policy, predecessor_nonce, prompt_text,
    queue_dir, wave_id=None, spawner_focus=None,
) -> int:
    sp_dir = cfg.home / project / "singlepane"
    ws_file = sp_dir / f"{task}.handoff.code-workspace"
    # direct-jump-spawn: this singlepane window's own focus path (realpath = VS Code's stored
    # configURIPath after norm) → injected into its terminal env so it can self-report when spawning.
    own_focus = os.path.realpath(str(ws_file))
    uri = _build_uri(cfg, prompt_text)
    try:
        atomic.atomic_replace(
            ws_file,
            _singlepane_workspace_json(
                src=src, project=project, task=task, role=role, nonce=nonce, focus_path=own_focus
            ),
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
            wave_id=wave_id,
        )
        # WORKSPACE=real repo ⇒ SINGLEPANE path; spawner_focus drives the watchdog one-step focus-jump.
        # ROLE=coord iff this is a coordinator succession (single source of truth = the spawn role).
        _write_uri(
            queue_dir, task, workspace=src, uri=uri,
            is_coordinator=(role == ROLE_SUCCESSION), spawner_focus=spawner_focus,
        )
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
    *, cfg, project, task, role, src, nonce, close_policy, predecessor_nonce, prompt_text,
    queue_dir, wave_id=None, spawner_focus=None,
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
                wave_id=wave_id,
                spawner_focus=spawner_focus,
            )
    except LockHeld as e:
        _err(
            f"worktree project {project!r}: spawn for {task!r} REJECTED — the project "
            f"spawn lock stayed held past the {_WORKTREE_LOCK_WAIT:.0f}s wait ({e}); "
            "retry after the concurrent spawn/autoclose settles"
        )
        return EXIT_FAIL_CLOSED


def _produce_worktree(
    *, cfg, project, task, role, src, nonce, close_policy, predecessor_nonce, prompt_text,
    queue_dir, wave_id=None, spawner_focus=None,
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
            wave_id=wave_id,
        )
        # WORKSPACE=worktree dir ⇒ COLD path; spawner_focus drives the watchdog one-step focus-jump.
        # ROLE=coord iff this is a coordinator succession (single source of truth = the spawn role) —
        # mirrors create_worktree(is_coordinator=...) on this path.
        _write_uri(
            queue_dir, task, workspace=wt, uri=uri,
            is_coordinator=(role == ROLE_SUCCESSION), spawner_focus=spawner_focus,
        )
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
    wave_id: str | None = None,
    spawner_focus_path: str | None = None,
    self_task: str | None = None,
    origin: str = _spawner_focus.ORIGIN_COORDINATOR,
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
    # §6c C5: a wave member's sidecar carries its wave_id so the reclaim producer can
    # detect attempted late-adds against the frozen manifest. Worker-only: a wave is a
    # parallel WORKER dispatch batch; a succession relay is never a wave member.
    if wave_id is not None and (not _SLUG_RE.match(wave_id) or len(wave_id) > 60):
        _err(f"--wave-id must be kebab-case (a-z 0-9 -), ≤60: {wave_id!r}")
        return EXIT_FAIL_CLOSED
    if wave_id is not None and role != ROLE_WORKER:
        _err("--wave-id is only valid for --role worker (a wave is a worker batch)")
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
    prompt_text = _build_prompt(task, role=role, brief=brief, prompt=prompt)
    queue_dir = cfg.queue_dir(project)

    # direct-jump-spawn (2026-06-13): validate the optional spawner focus path — the ACTIVE
    # coordinator's own .handoff.code-workspace (passed by dx-spawn from its $HANDOFF_WINDOW_FOCUS_PATH
    # env). The strict realpath/suffix/allowed-root gate lives in the shared ``spawner_focus`` module
    # (single security-boundary source — ``dump`` calls the SAME helper for its env path). FAIL-OPEN:
    # an invalid/foreign value returns None and is DROPPED (worker still spawns, just no desktop jump)
    # — never fail a spawn over a UX hint.
    spawner_focus = _spawner_focus.validate_spawner_focus(spawner_focus_path, cfg=cfg)
    if spawner_focus_path and spawner_focus is None:
        _err(
            "--spawner-focus-path dropped (not an existing in-tree/.tmp "
            f".handoff.code-workspace): {spawner_focus_path!r} — worker spawns "
            "without the desktop jump (fail-open)"
        )

    # mp-locate-return (2026-06-14 / sw-coord-p22): when the CLI/env didn't supply a valid focus path,
    # SELF-IDENTIFY the SPAWNING coordinator's own .handoff.code-workspace (env-independent) and emit it
    # as SPAWNER_FOCUS so the watchdog runs the EXISTING one-step focus-jump — Tier 1 worktree (cwd at
    # `handoff spawn` time is the coordinator's worktree) + Tier 2 singlepane (the session-keyed focus
    # marker, Stage 3 — the singlepane succession path spawns from the shared repo cwd). Validated through
    # the SAME gate. FAIL-OPEN: None when unresolvable → SPAWNER_FOCUS omitted, existing goto stands.
    if spawner_focus is None:
        spawner_focus = _spawner_focus.resolve_spawner_focus_path(
            os.getcwd(),
            cfg=cfg,
            home=cfg.home,
            project=project,
            self_task=self_task,
        )

    # spawn-unification Step 1 (2026-06-22): neither the CLI/env hint nor the symmetric resolver
    # found an anchor → the worker .uri carries NO SPAWNER_FOCUS and code-router.sh falls back to the
    # static desktop map (the wrong-desktop root cause). Step 1 records the miss so it stops being
    # silent; Step 4 (below) flips that miss to fail-closed for an enforce-phase coordinator dispatch.
    if spawner_focus is None:
        _spawner_focus.log_anchor_miss(
            home=cfg.home,
            project=project,
            task=task,
            cwd=os.getcwd(),
            isolation=isolation,
            reason="spawn:anchor-unresolved",
        )
        # spawn-unification Step 4: build the decision from the ALREADY-resolved (None) anchor — PURE
        # decision, no second resolution. DEFAULT = warn (config enforce lists empty) → required is
        # False → NEITHER branch fires → byte-identical Step-1 fail-open omit. A system-origin无锚
        # pass-through is audit-logged inside make_anchor_decision.
        decision = _spawner_focus.make_anchor_decision(
            None,
            cfg=cfg,
            home=cfg.home,
            project=project,
            origin=origin,
            cwd=os.getcwd(),
            callsite="spawn",
        )
        if decision.required and decision.enforcement == _spawner_focus.ENFORCE_BLOCK:
            _err(
                f"cannot resolve the coordinator workspace that dispatched you (anchor "
                f"{decision.miss_reason}). A singlepane coordinator must pass --self-task <its own "
                "task id>; a worktree coordinator must dispatch from its own cwd; if there is "
                "genuinely no coordinator desktop (manual / owner / cron / bootstrap) pass an "
                "explicit --origin {interactive|system}."
            )
            return EXIT_FAIL_CLOSED
        if decision.required and decision.enforcement == _spawner_focus.ENFORCE_DRY_RUN:
            # Shadow phase: record the would-block but DON'T block (design §4.1 phase 2).
            _spawner_focus.log_block_intent(
                home=cfg.home,
                project=project,
                task=task,
                cwd=os.getcwd(),
                origin=decision.origin,
                enforcement=decision.enforcement,
                reason="spawn:anchor-unresolved",
            )

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
        wave_id=wave_id,
        spawner_focus=spawner_focus,
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
        "--wave-id",
        default=None,
        dest="wave_id",
        help="wave id this worker dispatch belongs to (§6c C5; the coordinator freezes "
        "the membership manifest afterwards via `handoff worktree wave-freeze`)",
    )
    p.add_argument(
        "--spawner-focus-path",
        default=None,
        dest="spawner_focus_path",
        help="direct-jump-spawn: the SPAWNING window's own .handoff.code-workspace abs path "
        "(the active coordinator's $HANDOFF_WINDOW_FOCUS_PATH). Written to the .uri so the "
        "watchdog/code-router natively jumps to the spawner's desktop before opening the worker. "
        "Validated + fail-open (an invalid value is dropped; the worker still spawns).",
    )
    p.add_argument(
        "--succession-token",
        default=None,
        dest="succession_token",
        help="one-time succession authority issued by `handoff audit-close --coordinator "
        "--status active` (required for --role supervisor_succession; G4 收口 — a bare "
        "manual succession spawn is rejected)",
    )
    p.add_argument(
        "--self-task",
        default=None,
        dest="self_task",
        help="mp-locate-return §1: the SPAWNING coordinator's OWN task id (self-reported, "
        "env-independent). When --spawner-focus-path is absent, singlepane Tier-2 derives the "
        "coordinator's workspace via derive_singlepane_focus(home, project, self_task) so the worker "
        "focus-jumps to its desktop. Worktree coordinators use cwd (Tier-1, no --self-task). Fail-open.",
    )
    p.add_argument(
        "--origin",
        default=_spawner_focus.ORIGIN_COORDINATOR,
        choices=list(_spawner_focus.ORIGINS),
        help="spawn-unification Step 4: who is dispatching (default coordinator — an automated "
        "中枢→worker dispatch that MUST resolve an anchor). PER-INVOCATION, never inherited: leniency "
        "(allow-no-anchor) comes only from non-inheritable signals — 'system' ⟺ project ∈ config "
        "spawner_anchor_system_allow, 'interactive' ⟺ a real front TTY without HANDOFF_UNATTENDED, "
        "'test' ⟺ in-process pytest. Under an enforce-phase project a coordinator anchor-miss is "
        "fail-closed; default = warn (config lists empty → zero behavior change).",
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
        wave_id=args.wave_id,
        spawner_focus_path=args.spawner_focus_path,
        self_task=args.self_task,
        origin=args.origin,
    )


if __name__ == "__main__":
    sys.exit(main())
