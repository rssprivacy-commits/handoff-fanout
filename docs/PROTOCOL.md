# Handoff Protocol Specification

> Version: **schema_version 2** (manifest/handoff layout, matches `handoff_fanout.dump.SCHEMA_VERSION`); **retro-evidence schema 5.5.0** (matches `handoff_precheck.EVIDENCE_SCHEMA_VERSION`).
> Status: stable.
> Scope: **Part I (§1–§10)** is the original v1.0.0 base — root layout, single-task + fan-out queue, atomicity, watchdog, ACK. **Part II (§11–§18)** is the post-v1.0 governance gate layer (worktree isolation, spawn lock, the v5.4 retro-evidence gate, the codex audit gate, coordinator succession, the pre-push delivery-audit gate, the runtime return/reclaim loop, and Part II reference implementations). Part II is the source-of-truth the in-code docstrings point to.

This document specifies the on-disk layout, file formats, and state machine that the `handoff-fanout` tool reads and writes. Any tool conforming to this spec can be used as a producer (e.g. a custom `handoff dump` replacement) or a consumer (e.g. an IDE auto-spawn helper).

> **Live home vs. default.** `$HANDOFF_HOME` defaults to `~/.handoff` (`config.DEFAULT_HOME`); the deployed install overrides it to `~/.claude-handoff` via the env var, so all on-disk paths below resolve under `~/.claude-handoff/<project>/` in practice.

## 1. Root directory

```
$HANDOFF_HOME/                        # default: ~/.handoff/
├── STOP_AUTO                         # global pause sentinel (any pending UI consumer skips)
├── done                              # global permanent-stop sentinel
└── <project>/
    ├── STOP_AUTO                     # per-project pause sentinel
    ├── queue/                        # single-task queue files
    │   ├── <task>.md                 # human-readable handoff (the baton)
    │   ├── <task>.uri                # sidecar consumed by the IDE auto-spawn helper
    │   ├── <task>.done               # success terminal marker
    │   └── <task>.BLOCKED.md         # failure terminal marker
    ├── batches/                      # fan-out: one subdir per batch
    │   └── <batch-id>/
    │       ├── manifest.json
    │       ├── STOP                  # per-batch pause sentinel
    │       ├── <sub-task-id>.env     # role environment (sourced by sub-task tab)
    │       ├── <sub-task-id>.heartbeat
    │       ├── <sub-task-id>.done
    │       ├── <sub-task-id>.blocked
    │       ├── fan-in.env
    │       ├── _fanin_triggered      # last-one-out marker
    │       ├── _fan_in_started
    │       ├── _fan_in_heartbeat
    │       ├── _fan_in_done
    │       ├── _watchdog_triggered   # set when watchdog forces fan-in
    │       ├── _aborted              # set when STOP/STOP_AUTO observed mid-batch
    │       └── _corrupted            # set when manifest.json fails to parse
    ├── ack/                          # IDE-side ack of spawn
    │   ├── <task>.spawned            # launcher saw queue/<task>.uri and opened a tab
    │   ├── <task>.submitted          # tab actually received the prompt
    │   └── <task>.failed             # tab spawn aborted
    └── launched/
        └── <task>-<nano-ts>.txt      # launchd ledger; one file per spawn attempt
```

### 1.1 Sentinels (precedence order)

1. `$HANDOFF_HOME/done` — permanent stop, nothing spawns until removed.
2. `$HANDOFF_HOME/STOP_AUTO` — global pause; resumes when removed.
3. `$HANDOFF_HOME/<project>/STOP_AUTO` — project-scoped pause.
4. `$HANDOFF_HOME/<project>/batches/<batch-id>/STOP` — batch-scoped pause (sub-task tabs should drain).

Any sentinel set at a level causes all narrower levels to inherit the stop. `dump` always evaluates these via `any_stop_auto(project, batch_id)` before writing.

## 2. Identifiers

### 2.1 `task-id`, `sub-task-id`, `batch-id`

All three obey the same kebab-case regex: `^[a-z0-9][a-z0-9-]*[a-z0-9]$`, max 60 characters.

Examples: `fix-issue-42`, `refactor-auth-token-flow`, `b-2026-05-29-01`.

### 2.2 `project` slug

Same regex as task IDs. Default-inferred from `os.path.basename(workspace)` if not provided.

## 3. Single-task mode (`queue/<task>.md` + `.uri`)

### 3.1 `<task>.md` — the handoff baton

Markdown rendered from `templates.build_handoff_md`. Required header fields:

```markdown
# Handoff — project `<project>` / task `<task>`

**生成**: YYYY-MM-DD HH:MM:SS
**Project**: `<project>` (<absolute-workspace>)
**HEAD**: `<git-sha-short>` (<branch>)
**Status**: `active` | `done` | `blocked`

## 第一步: Baseline 验证 (新会话开局必跑)
...
```

Additional sections (in order): baseline verification, recent commits, optional test baseline, roadmap excerpt, next-task brief, reading list, optional inject blocks, STOP controls, AI self-check, launch instructions, concurrency note.

### 3.2 `<task>.uri` — IDE spawn sidecar

Two-line text file, UTF-8:

```
WORKSPACE=<absolute-workspace-path>
URI=<scheme>://<host>/<path>?prompt=<urlencoded-short-prompt>
```

`URI` is built from `Config.uri_template` (default `vscode://anthropic.claude-code/open?prompt={prompt}`). Consumers (e.g. a launchd watcher) parse both lines, change directory to `WORKSPACE`, then invoke the URI.

`<task>.uri` is removed automatically when the task terminates as `done` or `blocked`.

### 3.3 Terminal markers

* `<task>.done` — task closed cleanly; consumer may garbage-collect.
* `<task>.BLOCKED.md` — task failed; format from `templates.build_blocked_md`:

```markdown
# BLOCKED — project `<project>` / task `<task>`

Generated: <timestamp>
HEAD: <git-sha>

## Reason
<free text>
```

## 4. Fan-out mode (`batches/<batch-id>/`)

### 4.1 `manifest.json`

```jsonc
{
  "schema_version": 2,
  "batch_id": "b-2026-05-29-01",
  "fan_in_task": "post-batch-merge",       // the task ID the fan-in tab will drive
  "next_after_fanin": "deploy-staging",    // optional successor task
  "split_rationale": "string for audit",   // optional
  "amdahl_estimate": {                     // optional, used by heartbeat for calibration
    "estimated_speedup": 2.4,
    "serial_minutes": 90
  },
  "sub_tasks": [
    {
      "id": "extract-models",              // kebab-case
      "brief": "Extract DB models into models/",
      "estimated_minutes": 20,
      "file_ownership": [
        {"type": "prefix", "path": "src/models/"},
        {"type": "exact",  "path": "tests/test_models.py"}
      ]
    }
  ]
}
```

### 4.2 file_ownership specs

| `type` | Meaning | Constraint |
|---|---|---|
| `exact` | A single file path | Relative to workspace; `..` rejected. |
| `prefix` | A directory tree (recursive) | Path must end with `/`. |
| `glob`  | A workspace-rooted glob | Standard `Path.glob` semantics. |

Paths are resolved against the workspace and **must stay inside it**. Pairwise file_ownership intersection across all sub-tasks must be empty (Gate A — physical collision check; enforced by `validate_ownership_no_overlap`).

### 4.3 Spawn-storm guards

| Constant | Default | Purpose |
|---|---|---|
| `SUB_TASK_N_MAX` | `3` | Hard cap on sub-tasks per batch (529-rate-limit defense). |
| `GLOBAL_ACTIVE_LIMIT` | `5` | Total live `.uri` files (no `.done`/`.BLOCKED.md`) across all projects. |
| `STAGGER_SPAWN_SECONDS` | `30` | Delay between successive sub-task `.uri` writes inside one batch. |

A `--open-batch` invocation that would exceed either cap returns a non-zero exit and writes nothing.

### 4.4 Role environments

Each sub-task tab and the fan-in tab source a per-role env before any Bash op:

```bash
# .env contents
export HANDOFF_ROLE=sub-task     # or fan-in / main
export HANDOFF_BATCH_ID=<batch-id>
export HANDOFF_SUB_TASK_ID=<sub-task-id>   # sub-task only
export PATH="<git-guard-dir>:$PATH"
```

`HANDOFF_ROLE` is what the PATH-injected `git` wrapper and the pre-commit hook check. Sub-tasks are blocked from `commit`, `push`, `rebase`, `cherry-pick`, `reset`, `revert`, `tag`, `am`, `format-patch`, and `merge`. The fan-in tab is the **only** committer.

### 4.5 Lifecycle markers

| File | Set by | Meaning |
|---|---|---|
| `<sub-task-id>.heartbeat` | sub-task tab (every 60s) | Tab is alive. |
| `<sub-task-id>.done` | sub-task tab on success | Sub-task completed. |
| `<sub-task-id>.blocked` | sub-task tab on failure | Sub-task aborted (must include reason). |
| `_fanin_triggered` | last sub-task to terminate | Fan-in handoff has been written. |
| `_fan_in_started` | fan-in tab on entry | Fan-in is running. |
| `_fan_in_heartbeat` | fan-in tab (every 60s) | Fan-in still alive. |
| `_fan_in_done` | fan-in tab on success | Batch fully consolidated. |
| `_watchdog_triggered` | `handoff watchdog` | Forced fan-in due to stale heartbeats / timeout. |
| `_aborted` | dump on STOP detection | Batch was paused mid-spawn. |
| `_corrupted` | dump on manifest parse failure | Recovery required. |

All markers are written atomically (`atomic.atomic_create` for empty files, `atomic.write_with_fsync` for content) so consumers never observe a torn write.

## 5. State machine

```
                  ┌──────────────────┐
   dump(active) ─▶│   queue/<t>.md   │
                  │   queue/<t>.uri  │◀── consumed by IDE
                  └────────┬─────────┘
                           │
                           │ tab works, then dumps successor
                           ▼
                  ┌──────────────────┐
                  │  next task .md   │
                  │  next task .uri  │
                  └──────────────────┘

                  ┌──────────────────┐
   dump(done)  ─▶│  queue/<t>.done  │  (terminal; .uri removed)
                  └──────────────────┘

                  ┌──────────────────────┐
   dump(blocked) ▶│  queue/<t>.BLOCKED.md │ (terminal; .uri removed)
                  └──────────────────────┘
```

Fan-out:

```
   dump(--open-batch manifest.json)
     │
     ├─▶ batches/<b>/manifest.json
     ├─▶ batches/<b>/<st1>.env
     ├─▶ queue/<st1>.md   queue/<st1>.uri    (then sleep STAGGER_SPAWN_SECONDS)
     ├─▶ batches/<b>/<st2>.env
     ├─▶ queue/<st2>.md   queue/<st2>.uri    (...)
     └─▶ batches/<b>/fan-in.env  (pre-staged for the eventual fan-in tab)

   each sub-task tab on completion:
     touch batches/<b>/<stN>.done
     check: are all sub-tasks .done or .blocked?
       yes & all done  → write batches/<b>/_fanin_triggered + dump(fan_in_task)
       yes & any blocked → write _fanin_triggered + dump fan-in with degraded mode
       no              → exit (let the next sub-task be last-one-out)

   fan-in tab:
     atomic_create _fan_in_started
     loop: touch _fan_in_heartbeat every 60s
     do step 2-8 (audit / commit / regression / metrics / dump next)
     atomic_create _fan_in_done
```

## 6. Atomicity guarantees

* **`atomic_create(path)`** — `open(O_CREAT | O_EXCL | O_WRONLY)` then `fsync(file) + fsync(dir)`. Two concurrent creators see at most one success; the other gets `FileExistsError`.
* **`write_with_fsync(path, content)`** — write to a sibling tempfile, `fsync(tempfile) + os.replace(tempfile, path) + fsync(dir)`. Readers always see either the previous content or the complete new content, never a partial write.
* **`acquire_dir_lock(dir)`** — `flock(dir_fd, LOCK_EX)`, released on context exit. Used by `safe-commit` to make `git add → commit` atomic across processes.

## 7. Watchdog scan modes

`handoff watchdog` is meant to run from `launchd` (macOS) or `cron`/`systemd-timer` (Linux) on a 60s tick. It scans all batches under all projects and may take one of these actions:

| Mode | Condition | Action |
|---|---|---|
| `last-one-out` | All sub-tasks have `.done`/`.blocked` but no `_fanin_triggered` | Write `_fanin_triggered`, dump fan-in. |
| `stale-heartbeat` | Sub-task `.heartbeat` mtime > 3 min ago | Mark `<sub-task-id>.blocked` with reason `stale-heartbeat`, then `last-one-out` check. |
| `fan-in-stalled` | `_fan_in_heartbeat` mtime > 3 min ago | Re-dump fan-in handoff (idempotent). |
| `orphan-sub-task` | `ack/<task>.spawned` exists but no `queue/<task>.{md,done,BLOCKED.md}` | Write `queue/<task>.BLOCKED.md` (orphan template). |

The watchdog is idempotent: running it twice in the same second produces the same end state.

## 8. ACK protocol (IDE-side spawn helper)

The IDE auto-spawn helper (typically a launchd `WatchPaths` script) MUST:

1. Observe a new `queue/<task>.uri` file.
2. Read `WORKSPACE=` and `URI=` lines.
3. `cd` to `WORKSPACE`, then invoke the URI (`open <URI>` on macOS).
4. Touch `ack/<task>.spawned` on success.
5. Touch `ack/<task>.failed` on failure (with a reason file beside it, optional).
6. Once the tab has actually received the prompt, the tab (or the helper) MAY touch `ack/<task>.submitted` for observability.

Consumers without ACK semantics still work — ACK files exist purely to support the watchdog's `orphan-sub-task` detection.

## 9. Versioning

This is `schema_version: 2`. Older versions are not produced by the current tool; consumers MAY reject any manifest with a different schema number.

Forward-incompatible changes will bump `SCHEMA_VERSION`. Forward-compatible additions (new optional manifest fields, new sentinel files) will not.

## 10. Reference implementations

* `handoff_fanout.dump` — producer (single-task + fan-out).
* `handoff_fanout.watchdog` — periodic scanner.
* `handoff_fanout.heartbeat` — fan-in tab daemon + metrics.
* `handoff_fanout.safe_commit` — cross-process commit wrapper (Layer 3).
* `handoff_fanout.git_guard` — PATH-injected `git` shell wrapper (Layer 1).
* `tests/test_handoff_orphan.py` + `tests/test_handoff_hijack.py` — black-box conformance tests; any alternative implementation should pass these.

---

## Part II — governance gate layer (post-v1.0)

Part I describes how a baton is produced, spawned, and watched. Part II describes the **gates that govern whether a session may close out and a baton may advance**, plus the runtime machinery that isolates and reclaims worktree workers. **Where each gate actually bites depends on scoping** (§13.3): for a project listed in `mandate_projects` the retro/audit gates fire at *dump* time; for an unlisted project (e.g. handoff-fanout itself) a no-evidence dump takes the legacy path and the always-on enforcement is instead the **pre-push** hook (§16, gates *publishing*) + the new-session §0 self-audit + an explicit `--retro-evidence`. These layers were added after v1.0.0 and are what the in-code docstrings in `codex_audit.py`, `templates.py`, and `handoff_precheck.py` reference as their source of truth. Full prose walkthroughs with `file:line` evidence live in `project-files/handoff/architecture-2026-06-15/` (the comprehensive architecture snapshot); this Part is the normative summary.

## 11. Per-session git-worktree isolation

Worktree isolation is **opt-in (default OFF)**: `config.worktree_mode` is `"off"` by default and flips to `"on"` only via env `HANDOFF_WORKTREE_ISOLATION`, a sentinel, or the per-project `config.worktree_projects` allow-list (`worktree.resolve_mode`; `"report"` logs what would happen and mutates nothing). When **on**, `handoff dump` / `spawn` does **not** spawn the next tab in the shared checkout — it creates an out-of-tree git worktree so each session gets its own working copy of the source repo, eliminating cross-session git-index races.

* **Location:** `worktrees_root` (default `$HANDOFF_HOME/<project>/worktrees/<task>/`).
* **Workspace file:** the dump writes a `.handoff.code-workspace` (VS Code multi-root) pointing the new window at the worktree, and the spawn URI opens that workspace.
* **Coordinator marker:** a `--coordinator` dump injects `workbench.colorCustomizations` (red title bar `#8B0000`/`#5A0000`) + a `🧭中枢·` window-title prefix into the workspace JSON, so a supervisor window is visually unmistakable (`worktree.inject_vscode_workspace`). Non-coordinator dumps are byte-identical to the un-marked form.
* **Reclaim:** when a worktree task terminates, the worktree directory becomes *eligible* for reclaim GC (`handoff worktree` subcommands + §17). The reclaim **close/GC path is wired but has not completed end-to-end** (see §17 + `GAP-ANALYSIS §F#7`); in practice stale worktrees are pruned manually / owner-in-loop.

## 12. Project spawn lock (`<project>/.spawn.lock`)

`spawn_lock.project_spawn_lock` is a project-scoped mutex covering the spawn-intent decision and the worktree-create critical section. macOS lacks `flock` on all filesystems, so the lock is an **atomic `mkdir`** of `$HANDOFF_HOME/<project>/.spawn.lock`.

* **TTL break:** a crashed holder must not deadlock the project. A waiter computes `age = now − mtime(lockdir)`; once `age ≥ ttl` (default 120 s) it breaks the stale lock and re-acquires. Breaking is a **bounded** retry loop (`max_stale_breaks`, default 5) — when two waiters race to break the same stale lock exactly one wins the re-`mkdir`; the loser's `FileExistsError` is a normal race outcome, never a crash.
* **Held-lock heartbeat (gap C2):** while held, a daemon thread refreshes the lockdir mtime via `os.utime(lockdir, None)` every `ttl/4`. This stops a *live* holder in a long critical section (a slow-fs / slow-remote `create_worktree` whose `git fetch` + `git worktree add` exceed `ttl`) from being mis-judged stale and broken out from under itself.
* **🔴 Empty-dir invariant:** three independent actors (`spawn_lock`, `reclaim`, the bash `try_autoclose`) all `rmdir` this lock as an **empty** directory. No file may ever be written *inside* `.spawn.lock` — fencing data goes in sibling files. The heartbeat refreshes mtime only; it writes nothing inside.
* **`wait` budget:** default `wait=0.0` (non-blocking, immediate `LockHeld`) for the singlepane reject path + watchdog skip-on-contention. Parallel worktree workers pass a positive `wait` so legitimate concurrency *queues* on the shared source repo rather than rejecting.

## 13. The v5.4 retro-evidence gate

> Source-of-truth for the in-code refs `handoff_precheck.py` / `templates.py` §0 (formerly cited as `v5.4-retro-mandate-draft.md`).

A session must prove it closed the loop on its own work before it is allowed to dump the next baton. The proof is a JSON evidence artifact; the dump gate verifies it.

### 13.1 The artifact — `precheck/<task>.retro.evidence.json`

Written by `handoff precheck` (`handoff_precheck` CLI) after a task closes:

```jsonc
{
  "schema_version": "5.5.0",          // EVIDENCE_SCHEMA_VERSION; gate also accepts "v5.4.1" (migration window)
  "evidence_kind": "retro",            // EVIDENCE_KIND_RETRO ("fan_in_aggregate" for batch fan-in)
  "task_id": "<task>",
  "project": "<project>",
  "workspace": "<absolute-workspace>",
  "mode": "normal",                    // or "forensic_retro"
  "head_at_precheck": "<git-sha>",     // bound at precheck; gate checks freshness vs live HEAD
  "head_at_precheck_timestamp": "<iso>",
  "session_commits": ["<sha>", ...],   // commits this session owns (for HEAD re-align proof)
  "session_id": "<id>", "session_id_kind": "claude-uuid",
  "phase0": { "memory": {"status": "✅"}, "tests": {...}, "audit": {...},
              "commit": {...}, "code_review": {...} },
  "phase1": { "codex": {...}, "claude_md": {...}, "l2_memory": {...},
              "tests": {...}, "prs": {...} },
  "evidence_hash": "<canonical-sha256>" // optional: "nonce", "codex_audit" (added by audit-close)
}
```

> Exact field names matter — a producer that emits `kind`/`task`/`head` instead of `evidence_kind`/`task_id`/`head_at_precheck` is rejected. Authority: `handoff_precheck.build_*` (the builder) + `retro_gate` (the reader).

* **Phase 0 keys** (5): `memory`, `tests`, `audit`, `commit`, `code_review`.
* **Phase 1 keys** (5): `codex`, `claude_md`, `l2_memory`, `tests`, `prs`.
* **status enum:** `✅` (this task actually changed it) / `⚠️` (warning, non-blocking) / `❌` (omitted → gate rejects) / `skip` (explicit, requires a `reason`). All 10 items must carry a known status.

### 13.2 The gate — `retro_gate.check_retro_gate` (invoked by `dump`)

Activation triggers (`dump.py`): an explicit `--retro-evidence FILE`, `HANDOFF_RETRO_BYPASS=1`, `HANDOFF_RETRO_MANDATE=1`, or `HANDOFF_AUDIT_MANDATE=1` (the latter drives the codex audit gate, §14). Two exemptions skip the gate *before* triggers/scoping are evaluated at all: fan-out/fan-in **batch** dumps (`dump.py:261`) and a **terminal `done`/`blocked` dump with no explicit evidence** (`dump.py:276` — no successor to gate). Past those: the `mandate_projects` scoping (§13.3) applies **only to the bare env-mandate path** — an *active* dump with *no* explicit `--retro-evidence` **and** no `HANDOFF_RETRO_BYPASS`, where an unlisted project takes the legacy (no-gate) path. An explicit `--retro-evidence` always validates (it defeats both the terminal-status skip and the project-scope skip); `HANDOFF_RETRO_BYPASS=1` runs the gate for an active successor dump regardless of project listing (`dump.py:276-292`). On activation it verifies evidence presence + canonical-hash match + `head_at_precheck` freshness vs live HEAD, with a per-task attempt counter (hard-reject after 2) and a three-tier lock.

**Exit codes** (stderr carries the matching prefix; AI dispatches on the subcode):

| exit | prefix | meaning | response |
|---|---|---|---|
| 0 | `OK:` | gate passed | wait for the IDE to spawn the next tab |
| 1 | `ERR-FATAL:` | tamper / unrecoverable (retry can't help) | stop |
| 2 | `ERR-BLOCKED:` | attempt #2 hard-reject / head-stale-fatal | stop retrying → BLOCKED flow |
| 3 | `ERR-LOCKED:` | precheck/dump/attempt lock contention | yield + exit (a parallel tab is dumping) |
| 4 | `ERR-RETRY:` | evidence missing / hash mismatch / schema unknown | fix + re-dump once (attempt < 2) |
| 6 | `ERR-BYPASS:` | bypass field missing / follow-up overdue | add the trail fields + re-dump |

> Exit 5 is intentionally unassigned (§7.1). Schema-version: an evidence file whose `schema_version` is not in `SUPPORTED_EVIDENCE_SCHEMA_VERSIONS` is a fatal-class `ERR-RETRY`.

### 13.3 `mandate_projects` scoping (🔴 repo-specific behaviour)

`config.json:mandate_projects` whitelists which projects the **dump-time** env mandate applies to. It is currently `["erp-system"]` — **handoff-fanout is not listed**. So for handoff-fanout a no-evidence, no-bypass `dump` takes the **legacy path** (`dump.py` returns before `check_retro_gate` runs → exit 0); the env mandate alone does **not** cause exit 4 for this repo. Dump-time enforcement for handoff-fanout therefore requires an **explicit `--retro-evidence`** (which coordinator `audit-close` always passes), and the always-on enforcement is the **pre-push hook** (§16) + the new-session §0 self-audit.

### 13.4 Emergency bypass

`HANDOFF_RETRO_BYPASS=1` requires an `ack/<task>.retro.override.json` carrying a `follow_up_retro_task_id` + ISO-8601 `follow_up_deadline`, else `ERR-BYPASS` (exit 6). Bypass is *deferred* retro, not *skipped* retro: an overdue follow-up is caught at the next dump.

### 13.5 The `closeout_obligations` status-vector (warn-mode · DEFAULT-OFF)

A **third** optional status-vector on the retro evidence (after `phase0` / `phase1`). It turns the soft text rule ⑬「交棒前先复盘」 — which conflated two different things — into a machine-checkable **scope-by-delivery** closeout contract:

* `sedimentation_always` — lesson + retro-evidence, done on **every** coordinator handoff (should be `✅`).
* `audit` — only when there were code changes.
* `doc_mapping` — only when instructions / architecture / config changed.
* `release` — only on user-visible delivery.
* `sync_pipeline` — only when artifacts changed.
* `postmortem` — only when this hop had an incident / regression.

Each item is either an artifact-pass (`✅`) or an explicit N/A. **The status vocabulary is reused from phase0/phase1** (`✅` / `⚠️` / `❌` / `skip`); because `skip` is in `STATUS_REQUIRING_REASON`, an N/A item (`skip`) **naturally requires a reason** — i.e. "N/A + why" is enforced for free. Authority: `handoff_precheck.CLOSEOUT_KEYS` + `_validate_closeout` (the single structural-validation point) + the `--closeout-status <key>=<status>[:reason]` CLI flag.

**Conditional-fold (the zero-regression basis).** Unlike phase0/phase1 (which are *always present* via `merge_phase_status`), `closeout_obligations` is **OPTIONAL** and uses the same conditional-fold pattern as `predecessor_lesson_backref` / `lesson_disposition`: when **not** supplied the key does not appear in the payload and the evidence (and its `evidence_hash`) is **byte-for-byte identical** to a pre-closeout payload. When supplied it is validated (malformed → `ValueError`, garbage never enters the hashed payload; an unknown key is rejected — stricter than `merge_phase_status`'s drop-unknown leniency, because the keys are an enum) then folded into the hashed payload. `_attempt_realign` preserves a present vector verbatim across a sibling-HEAD refresh (same guarantee `codex_audit` / backref have); `dump._write_old_ready` surfaces a present vector into `old_ready` so the next session's §0 audit can read it.

**Warn-mode gate (never blocks).** `dump._run_closeout_obligations_gate` is **WARN-ONLY**: it ALWAYS returns `None` and NEVER returns a blocking exit code — it only prints a non-blocking stderr advisory (clearly marked `warn-mode advisory, non-blocking`) when a coordinator handoff is missing the vector, or when `sedimentation_always` is not `✅`. It is **DEFAULT-OFF** (empty `config.json:closeout_obligations_warn_projects` = no project warned fleet-wide; owner flips a project or `"*"` in). One-key rollback = an off-switch sentinel `$HANDOFF_HOME/<project>/.closeout-obligations-warn-off` (per-project) or `$HANDOFF_HOME/.closeout-obligations-warn-off` (fleet-wide). Fail-SAFE-OFF: an unreadable / malformed evidence, a non-dict payload, or any unexpected error → silent `None` (a warn-only gate must never crash the dump and thereby block a handoff). Crucially the vector is **NOT** added to `retro_gate._validate_phase_status` (the hard-blocking phase0/phase1 check) — its only structural validation is in `build_evidence` (precheck-side, fail-fast); the dump side carries warn-mode advisory only.

**🔴 Q3 — "who verifies the honesty of an N/A (`release:skip`)?" — chosen design (owner-ratified).** Warn-mode v1 does **NOT** verify N/A honesty. An independent consumer that scrutinizes a suspicious `release:skip` is **DEFERRED to enforce-mode**, where it will mirror retrieval-pull: the next coordinator's §0 audit reads the predecessor's closeout vector (surfaced into `old_ready`) and can challenge it. The warn-mode v1 signal is simply that the vector becomes a **visible artifact** (folded into the hashed evidence + surfaced into `old_ready` + an advisory when absent). This is the intentional "right size" (freeze Case B + owner-chosen warn-first + simplicity-first: do not build an independent consumer in v1).

### 13.6 The `closure_attestation` vector + ship-live closure gate (BLOCK-mode · DEFAULT-ON)

A **fourth** optional vector on the retro evidence (after `phase0` / `phase1` / `closeout_obligations`). It is the machine-checkable form of the「彻底闭环」core law — the one law that, before this, had **no machine gate** and so was systematically defeated by sessions reporting "建了 / 提交了 / 记一笔" in place of "真 live 生效", leaving every project's遗留 problems to accumulate (owner direct order 2026-06-27).

**The 闭环证书 (closure attestation).** A list bound into the hashed evidence; each entry binds **one claimed deliverable** to specific LIVE evidence:

* `deliverable` (non-empty str) — what was delivered.
* `kind` ∈ `{shipped, skip}` (`handoff_precheck.CLOSURE_KINDS`):
  * `shipped` → REQUIRES `deployed` (WHERE it went live: SHA / deploy path / merged-`main` commit) **and** `verified` (the behavior-verify: cmd → observed effect — not "我觉得好了").
  * `skip` → REQUIRES `reason` (this deliverable is N/A / nothing shipped) — the same anti-hollow「skip 须带理由」invariant phase status carries, so a false all-skip is an EXPLICIT, auditable lie.

Authority: `handoff_precheck.CLOSURE_KINDS` + `_validate_closure_attestation` (the single structural-validation point) + the `--closure-evidence <deliverable>=shipped:<deployed>::<verified>` / `…=skip:<reason>` CLI flag (on both `handoff-precheck` and `audit-close`), with a `--closure-evidence-file` JSON-array alternative (file wins).

**🔴 Why this does NOT repeat `field-verify-guard` (the undecidability wall, `lesson-sw-coord-p67`).** `field-verify` tried to decide, from a session's **prose + this-turn tool names**, whether a closure claim was *truly verified* — an **undecidable** question (text numbers may be read or observed; an interpreter command may read config or run a system; cross-turn verification is invisible). It died 7/7-bypassed + FP. This gate **never parses prose and never judges truth.** It checks only **existence + structural completeness + binding** — all decidable, exactly like `retro_gate` checks phase status and `deploy-audited` checks byte binding. The session *asserts* the binding in a structured field; the gate verifies the field is **present, well-formed, and hash-bound**, not that the assertion is true.

**The trigger is a STRUCTURED declaration, never prose.** The blocking requirement fires **iff** the session's own `closeout_obligations.release` status is `✅` (= "a user-visible delivery happened this hop" — §13.5's `release` key). When `release=✅` the gate REQUIRES a `closure_attestation` carrying ≥1 `shipped` entry; absent / all-`skip` → `ERR-RETRY` (`closure-attestation-missing` / `closure-attestation-all-skip`) → the same attempt-counter ladder as phase status (retry→retry→`ERR-BLOCKED`). Any present vector is also structurally re-validated (`closure-attestation-malformed`, defence-in-depth). When `release` is anything else (`skip` / absent / `⚠️` / `❌`) the binding is NOT required — a coordination / internal-refactor hop is never touched.

**Conditional-fold (the zero-regression basis).** Like the three optional vectors before it, `closure_attestation` is **OPTIONAL** and uses conditional-fold: omitted / empty → the key is absent and the payload (+ `evidence_hash`) is **byte-for-byte identical** to a pre-closure payload; supplied → validated (malformed → `ValueError`, garbage never hashed) then folded into the hashed payload. `_attempt_realign` preserves a present vector verbatim across a sibling-HEAD refresh; `dump._write_old_ready` surfaces it into `old_ready` so the next coordinator's §0 audit can challenge a suspicious `release=skip` (same successor-challenge posture as closeout).

**BLOCK-mode + DEFAULT-ON, but purely additive (`retro_gate._validate_closure_gate`).** UNLIKE closeout's warn-only gate, this **blocks** (`ERR-RETRY`→`ERR-BLOCKED`). And UNLIKE every roll-out list, it **DEFAULTS ON** (ship-live is owner law, not opt-in — `config.closure_attestation_mandate` defaults `True`). It is nonetheless **additive + safe**: it **rides the existing evidence-bearing gate path** — a legacy no-evidence dump still short-circuits to legacy before the gate (`dump._run_retro_gate`), so it never *forces* evidence where there was none; it fires only on the structured `release=✅` (narrow → no FP); it is skipped in `forensic_retro` (a recovering session can't attest a dead session's deploy); and it **fail-opens** (a bug in the gate is caught → never blocks). Off-switches (any one): env `HANDOFF_CLOSURE_OFF=1`, `config.json: closure_attestation_mandate: false`, sentinel `$HANDOFF_HOME/<project>/.closure-gate-off` (per-project) or `$HANDOFF_HOME/.closure-gate-off` (fleet-wide); a present-but-untrustworthy config (`config_trusted=False`) also disables it (a blocking gate must never run off an unparseable config). Resolution: `dump._closure_attestation_mandate_enabled`.

**🔴 Same-trust-domain positioning (`lesson-sw-coord-p58`).** The gate **raises the floor + makes hiding visible**, it does not stop a determined liar in the same trust domain: a session can still declare `release=skip` to dodge the binding. But that is now an **explicit, auditable, successor-challengeable** statement (surfaced into `old_ready`), not a silent default drift — which is exactly the failure mode (默认漂移: "记一笔当完成") this gate is built to treat. A deliberately-rejected stronger trigger — "non-empty `session_commits` ⇒ require closure" — was NOT adopted: it would FP on internal-only commits (refactors / tests / governance) and brick coordination hops. The `release=✅` coupling is the deterministic, FP-free trigger.

## 14. The codex audit gate

> Source-of-truth for the in-code refs `codex_audit.py` / `templates.py` §-1.5 (formerly cited as `codex-audit-gate-spec-draft.md` / `codex-audit-gate-design.md`).

A task that **changed code** must carry a passing codex-audit block before it can dump — **with the same scoping as §13.3**: dump-time G0–G9 enforcement applies to a `mandate_projects`-listed project, or to any dump that uses explicit `--retro-evidence` / `audit-close`; an unlisted project's bare no-evidence dump takes the legacy path (`dump.py:293`), with enforcement carried instead by the pre-push hook (§16) + §0. The audit→fix→re-audit loop produces machine artifacts; `retro_gate.evaluate_audit_gate` (gates G0–G9) verifies them in the same locked process as the dump (so HEAD can't drift between audit and dump).

### 14.1 Authority + artifacts

* The **machine artifact is the source of truth** — codex emits a structured `codex-findings.json`; its hash lives in a **sidecar manifest** (a JSON can't contain its own hash). Evidence stores only per-finding *dispositions*, each bound to an original finding id + hash.
* Runtime artifacts: `$HANDOFF_HOME/<project>/audit/<task>/<run>/`, referenced by canonical relative path (never absolute).
* CLI: `handoff audit-run` (records a run's findings + sidecar) → `handoff audit-disposition` (one per P0/P1) → `handoff audit-close` (folds the audit block into retro evidence + dumps, atomically).

### 14.2 The four audit modes

| mode | when |
|---|---|
| `full_codex_audit` | code changed |
| `empty_diff_attestation` | diff is empty (e.g. a coordinator hand-off with no code delta) |
| `docs_only_light_audit` | docs only (prompts / CLAUDE.md / schema / SQL do **not** count as docs) |
| `codex_unavailable_bypass` | codex genuinely unavailable |

### 14.3 The G0–G9 gates (`evaluate_audit_gate`)

G9 round-cap → G2 artifact integrity (missing = RETRY / tampered = FATAL) → G0 the last (clean) re-audit ran against the current HEAD → docs-only content-diff legitimacy → G3 every P0/P1 (union across all rounds, deduped by identity hash) has a disposition → G4 a P0/P1 may never be merely *deferred* → G5 a `fixed` disposition needs a real `fix_commit` AND the finding must be gone from the last run → G6 an `independent_reviewer_refuted` must come from a *different* session and be anti-forgery-bound → G7 owner-override binding → G8 disposition shape. `empty_diff_attestation` reduces to G0 (attested HEAD == live HEAD) + a machine re-compute.

### 14.4 Bypass = debt; owner-override = friction-bound, not crypto

* **Bypass** (`codex_unavailable_bypass`) needs a bypass file with `codex_failure_attempts` (≥3 machine-proven failures) + a `follow_up_audit_task_id`. The next dump's `--task` **must** equal that follow-up id (written into `old_ready.next_session_forced_task`; the new session's §0 verifies it). `audit-close` bypass auto-writes `ack/<task>.audit.override.json` (deadline = now + 1 day); an overdue debt is caught by the Phase C overdue scanner at the next dump.
* **owner-override** (exempting a finding the AI argues should not be fixed) needs an on-disk `ack/<task>.owner_ack.<finding_hash_short>.json` whose `owner_ack_token = sha256(task | finding_hash | nonce | approved_at)` recomputes (G7). **Honest bound:** this token is a *tamper-evident + friction* binding, **not** cryptographic — it stops one approval being silently replayed onto a different finding / used past expiry, but does **not** stop a process running *as the owner* from forging a self-consistent token (single-user-machine assumption; a private-key HMAC is the deferred multi-user upgrade).

> **Two distinct overdue mechanisms** (do not conflate): the open codex re-audit debts currently live in the **PUSH gate** `audits/bypasses/*.json` (one-shot, already-used), which the dump-time overdue scanner does **not** read. The scanner reads `ack/*.audit.override.json` / `*.audit_overdue.txt`. Both producers are wired LIVE, but the dump-time audit-overdue chain has never fired end-to-end for handoff-fanout (0 such markers on disk).

## 15. Coordinator succession, red-top, singlepane

A supervisor/coordinator window hands its role to a fresh window (coordinator→coordinator) rather than to a worker.

* **Red-top + singlepane:** a coordinator window is a single-pane VS Code window with a red title bar + `🧭中枢·<chain>·<coord-id>` prefix (§11). Defence-in-depth against accidental close: the visual marker lowers mis-close probability, and a mis-closed coordinator is recoverable from the durable hand-off brief + `/resume`.
* **One-shot succession token:** when the predecessor's sidecar carries a 16-hex `predecessor_nonce`, `audit-close` routes through a *suppressed* dump + an in-process succession spawn that opens the heir window; the token transitions ISSUED → CONSUMED so it can't be replayed. With no nonce it falls back to a legacy dump with a loud WARN.
* **autoclose (old window):** opt-in only (`HANDOFF_AUTOCLOSE_ENABLED` env / global sentinel / per-project sentinel), all currently un-armed — so by default the old coordinator window stays open and the owner closes it. This is a deliberate safety posture (mis-close losing context > the minor friction of a manual close).

## 16. Pre-push delivery-audit machine gate

The always-on enforcement point for handoff-fanout (given §13.3). Three hooks call `handoff audit-check` with different teeth: **`pre-push` blocks** pushing new commits to `main` unless a matching GREEN delivery-audit evidence file (keyed by head SHA, or by `git patch-id` + changed-file set) exists under `~/.claude-handoff/handoff-fanout/audits/`; **`post-merge` warns only**; **`post-commit` gates only the auto-deploy** of the two deploy-copy assets (§16 note below), not the commit itself. The pushed-commit requirement: The evidence is produced by the dual-brain runner (`--evidence-repo <repo> --evidence-range <base>..<head> --out audits/…`); a RED verdict can only be released by the owner via an interactive-tty `handoff audit-override`. **Docs-only commits are not exempt** — every push of a new commit to `main` needs GREEN evidence.

> **Launcher = deployment COPY.** `install/auto-continue.sh` and `install/dump-handoff.py` are copied to the live launcher by a post-commit hook. Editing them requires `export HANDOFF_INSTALL_SH=/nonexistent` before commit (to skip the auto-deploy), verifying the launcher SHA is unchanged, then a manual `install.sh --sync-launcher` after the gate passes. `install.sh` itself and `backup-handoff-state.sh` are **not** on the deploy manifest — editing them is safe.

## 17. Runtime return-leg + §6c worker-window reclaim

The always-on watchdog (`auto-continue.sh`, launchd/cron tick) drives two runtime loops beyond Part I's batch watchdog:

* **Focus return-leg:** when a spawn is armed with `HANDOFF_SPAWNER_FOCUS`, after the worker window opens on the coordinator's desktop the watchdog jumps the *view* back to the owner's origin desktop (`spawn-precapture` / `spawn-return` via the cross-repo `vscode-spaces.py`). Both helpers are wrapped in `run_with_timeout ${HANDOFF_RETURN_TIMEOUT:-20}` (gap C1) — an `rc=124` timeout disarms + WARNs rather than freezing the watchdog iteration.
* **§6c reclaim (PID dead-man):** a worker-worktree window writes `<task>.host_pid.json` (pid + nonce + project + task + ts) on activate. Reclaim is **A-poll pull**, not push: a coordinator's `<request>.reclaim_requested` sentinel is consumed by the watchdog/reclaim producer, which writes a `<task>.reclaim_pending.json` (post-gate authorization); the worker window's extension polls **its own** `<task>.reclaim_pending.json`, self-closes, and writes `<task>.reclaim_ack.json`; the producer then verifies the host PID is gone (`os.kill(pid, 0)` → ESRCH) and GC's the worktree. (Pull beats push because `open vscode://…` only reaches the *focused* window — a worker on another desktop never received a pushed reclaim; an extension reading its own pending file makes window-targeting intrinsic.) **Status:** wired and code-live, but the reclaim *close* path has never completed end-to-end (host_pid.json proves window activation, not reclaim completion); enabling it for real needs one `reclaim_requested → reclaim_pending → reclaim_ack → PID-ESRCH → GC` trace as evidence.

## 18. Reference implementations (Part II)

* `handoff_fanout.handoff_precheck` — retro-evidence builder + CLI (§13).
* `handoff_fanout.retro_gate` — the dump-time gate: `check_retro_gate` (§13) + `evaluate_audit_gate` G0–G9 (§14).
* `handoff_fanout.codex_audit` — audit run/disposition/close builders + sidecar manifest (§14).
* `handoff_fanout.spawn_lock` — project spawn mutex (§12).
* `handoff_fanout.worktree` — worktree isolation + coordinator red-top injection (§11).
* `handoff_fanout.reclaim` — §6c PID dead-man reclaim state machine (§17).
* `install/auto-continue.sh` — the launchd watchdog: return-leg + reclaim tick + overdue scanner (§16/§17).
