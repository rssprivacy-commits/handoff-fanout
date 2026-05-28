# Handoff Protocol Specification

> Version: **schema_version 2** (matches `handoff_fanout.dump.SCHEMA_VERSION`)
> Status: stable for v1.0.0

This document specifies the on-disk layout, file formats, and state machine that the `handoff-fanout` tool reads and writes. Any tool conforming to this spec can be used as a producer (e.g. a custom `handoff dump` replacement) or a consumer (e.g. an IDE auto-spawn helper).

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
