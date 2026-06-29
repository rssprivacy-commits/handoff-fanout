# req3 — auto-close audited worker windows (operator + design doc)

> 🔴 SAFETY-CRITICAL. Closing a window ENDS its session (low reversibility). One mis-close =
> the owner loses an un-reviewed delivery. The supreme rule is **宁可漏关，绝不误关** — every
> predicate is fail-safe; any uncertainty means DON'T close. DEFAULT-OFF + dry-run-default +
> kill-switch + durable log + /resume-recoverable closes.

Spec: `~/.claude-handoff/req3-build-spec-v3.md` (owner gate 2026-06-26: Option 2 — going-forward
auto-close on discharge + janitor backlog sweep).

## Pieces & where they live

| Piece | What | Location |
|---|---|---|
| **A** signal writer | `handoff audit-discharge` → writes the strong, git-corroborated `ack/<task>.audit_discharged` (forgeable only by the same-UID worker — see "single-user authorization residual" below) | `src/handoff_fanout/codex_audit.py` (git, editable) |
| **gate** | the 5-predicate fail-safe safety gate (pure, unit-tested) | `src/handoff_fanout/autoclose_gate.py` (git, editable) |
| **B** close-by-WID | `coord-close-windows.py --close-wid` (AI-title + §6(a) TOCTOU root-fix) + §6(b) stats fix | `~/.claude-handoff/supervisor-monitor/coord-close-windows.py` (non-git, deploy-audited) |
| **C** driver/sweep | `autoclose-audited-workers.py` (thin glue: gate → bind WID → invoke the close tool) — modes `--task` / `--sweep` (signal path) + `--reconcile` (git-terminal path) | `~/.claude-handoff/supervisor-monitor/autoclose-audited-workers.py` (non-git, deploy-audited) |
| **C′** git-terminal reconciler | `gate_task_git_terminal` + `reconcile_open_worker_windows` (signal-free: seed the gate from the OPEN worker window's own merged delivery) | `src/handoff_fanout/autoclose_gate.py` (git, editable) |
| **D** opt-in switch | `worker-autoclose.enabled` (DEFAULT-OFF, signal+reconcile execute) + `worker-autoclose-reconcile.enabled` (DEFAULT-OFF, reconcile mode) + kill-switch | sentinel files under `$HANDOFF_HOME` (see below) |
| **E** janitor scheduler | launchd plist DESIGN-ONLY → routed to dx (red-line #4) | this doc §E |

## The 5-predicate safety gate (a window auto-closes ⟺ ALL hold)

Implemented in `autoclose_gate.gate_task`. See its module docstring for the authoritative list.

1. **worker, NOT coordinator** — `queue/<task>.singlepane` `role == worker`. Coordinators (🧭 /
   sw-coord / -coord-) are NEVER auto-closed (the close tool also hard-protects them).
2. **non-forgeable audited-to-terminal signal (P0-1 + P0-2)** — a coordinator-written
   `ack/<task>.audit_discharged` (verdict GREEN) is honored ONLY when corroborated by git:
   `merge_sha` is a NON-TRIVIAL real ancestor of the integration branch (in-section `git fetch`
   then `merge-base --is-ancestor`), `merge_sha != spawn_base` (kills the vacuous-ancestor hole),
   recorded `worktree_head == merge_sha` (no drift), and the LIVE worktree HEAD == `merge_sha`
   (the worker-writable signal is evidence, never authority). A reclaimed worktree must carry a
   `reclaim_done` (§6c already proved merged + removed). Worker self-reports
   (`.worker_reported`/`.submitted`/`.done`) are NEVER a trigger; bare `merge-base --is-ancestor`
   is NEVER a standalone signal.
3. **not in-flight (P0-3)** — newest transcript's last *conversation* entry is a settled
   `assistant_turn` AND idle ≥ threshold (default 30 min). `running_tool` / `blocked_on_question`
   / `dangling_tool_result` / unmapped / idle < 0 / unreadable ⇒ REFUSE (the probe's fail-open
   `idle_long` is deliberately NOT reused).
4. **not dirty** — live worktree has no uncommitted/untracked changes (the close tool also never
   force-closes a save-prompt-blocked window).
5. **fail-safe** — git unreachable / transcript unreadable / identity unparseable / ANY exception
   ⇒ DON'T close.

### Known boundary (honest, not silent)

A worker window whose VS Code title has been **fully overwritten by a Claude-Code AI tab summary**
(`… — .handoff (Workspace)`, no recoverable `<project> · <task> · <nonce>`) **cannot be safely
bound to a task** from `winlist` (which exposes only title / window_number / desktop — no workspace
path). Such windows are **NOT auto-closed**; the driver logs them as `unbindable → manual close`.
This is a deliberate fail-safe, not a silent drop. Most worker windows DO retain the structured
nonce title and close normally.

### Known boundary — single-user authorization residual (owner-ACCEPTED, reclaim parity)

The discharge signal and the pinned record (`ack/<task>.audit_discharged`, `ack/<task>.old_ready`,
`ack/<task>.head.json`) all live under the **worker-writable** `ack/` dir, and the worker runs as the
**same Unix user** as the coordinator. So a worker CAN forge both sides of the `merge_sha ==
_pinned_sha` equality — point both at some already-merged commit `Q`, reset its own worktree to `Q`,
keep it clean — and self-authorize a close. The equality therefore proves *consistency*, **not**
*coordinator authorship*. This is an **authorization-purity** gap, and on a single-user same-UID
system it **cannot be cryptographically closed** without a coordinator-held secret (HMAC-signed
discharge) — explicitly deferred to a multi-user deployment, exactly mirroring the `owner_ack_token`
precedent (防篡改 + 摩擦, 非加密, 单用户接受). **owner ACCEPTED this at the ENABLE gate (Option 1 =
reclaim parity).**

🔴 **The SAFETY property holds regardless** (绝不误关 · 无未审丢失): to close, the gate requires the
**live worktree HEAD == merge_sha == a real ancestor of `origin/<int>` (merged)** AND the worktree
**clean**. So in EVERY forge path the window that closes has a worktree sitting at a clean, merged
commit — there is **no un-reviewed/un-merged work to lose**. (Un-reviewed work at a non-merged HEAD →
`worktree-live-head-drift` REFUSE; uncommitted changes → `dirty` REFUSE; reset-to-merged-Q →
the work is already overwritten.) The residual is purely "a worker can self-authorize *its own*
close", never "an un-reviewed delivery is destroyed".

## C′ — the git-terminal reconciler (signal-free auto-close that actually self-triggers)

### Why (the signal path is inert in practice)

The signal path (`gate_task` / `--task` / `--sweep`) only ever fires for a task that has an
`ack/<task>.audit_discharged` signal — and that signal is written **only** by a coordinator
hand-running `handoff audit-discharge`. Coordinators almost never run it (across the whole fleet
there has typically been a single discharge signal), so the auto-close feature is **inert**: a worker
that finished, got reviewed, and was merged keeps its window open forever because nothing ever wrote
its discharge signal.

### The fix — seed the gate from the OPEN window's own merged delivery

`gate_task_git_terminal` runs the **same** fail-safe gate but derives `merge_sha` from the task's
**own pinned delivery record** (`reclaim._pinned_sha` = `ack/<task>.old_ready` `commit_hash` >
`ack/<task>.head.json` `head_sha`) instead of reading a discharge signal. Everything else is reused
**unchanged**: the idle probe (`_evaluate_idle`) and the network corroboration (`_corroborate_git`).
The shared local checks (worker role + nonce + base anchor + non-vacuous + pinned binding) are factored
into `_corroborate_local_shared`, used by **both** paths — so the signal path stays byte-for-byte
identical (its full test suite passes unchanged).

`reconcile_open_worker_windows(cfg, project, windows, parse_title, …)` is a **pure** pass over the
open windows (injected `windows` + `parse_title`, zero winlist/close I/O): for each window that parses
to this project's worker (non-coordinator, non-empty task id) it runs `gate_task_git_terminal` and
collects only the `close_ok` decisions (each annotated with `.task` for WID binding). The driver
(`--reconcile`) does the winlist + close I/O; the gate carries all judgement.

### Why this is SAFE — "merged to origin/main" is the strongest possible terminal proof

The only thing the git-terminal path drops versus the signal path is the coordinator's **explicit
GREEN assertion**. It replaces it with the strictly stronger terminal fact: **the pinned commit is a
real ancestor of `origin/<int>` (= already merged).** On `main`, a merge requires a GREEN pre-push
`audit-check` (red-line #3) and a worker **cannot self-merge** — so "merged to origin/main" *implies*
"a coordinator audited it GREEN and merged it". All 5 fail-safe predicates still apply:
worker-not-coordinator, pinned-bound + merged + live-HEAD == pinned + clean, idle + settled, and any
exception ⇒ DON'T close.

🔴 **Abandoned work is correctly REFUSED.** A worker whose pinned commit is **not** an ancestor of the
integration head (never merged) → `not-merged` refusal. A worktree that drifted off the pinned commit
→ `worktree-live-head-drift`. Uncommitted changes → `dirty`. So a window only closes when its
worktree sits at a clean, merged commit — there is **no un-reviewed/un-merged work to lose** (the same
绝不误关 guarantee as the signal path).

### Known boundary — ancestry model only covers fast-forward / ancestor merges

The proof is `is-ancestor(pinned, origin/<int>)`. A worker whose branch was integrated by a
**squash** or a **rebase** (so the worker's *exact* pinned commit never appears on `main`) will fail
`not-merged` and be **left for MANUAL close** — fail-safe, never mis-closed. This matches the
fleet's `--ff-only` merge convention (worker tips land verbatim on `main`); non-ff integrations are a
deliberate, honest gap, not a silent drop. (AI-retitled windows with no recoverable identity are also
left for manual close, exactly as in the signal path.)

## D — the opt-in switch (DEFAULT-OFF) + kill-switch

`worker-autoclose.enabled` is **SEPARATE** from the v4 coordinator-autoclose switch
(`autoclose.enabled`, which stays OFF per owner). DEFAULT-OFF: without it, `--execute` is forced to
dry-run. Enable via **any one** of:

```bash
export HANDOFF_WORKER_AUTOCLOSE_ENABLED=1                       # process/global
touch "$HANDOFF_HOME/worker-autoclose.enabled"                 # fleet-wide
touch "$HANDOFF_HOME/<project>/worker-autoclose.enabled"       # one project
```

**`worker-autoclose-reconcile.enabled`** (NEW, DEFAULT-OFF) is a **SEPARATE** switch that gates the
git-terminal `--reconcile` mode (decoupled from `worker-autoclose.enabled`, so the reconciler can be
rolled out / revoked independently). OFF ⇒ `--reconcile` is a **no-op** (not even dry-run) — an hourly
`--reconcile` daemon can be deployed **inert** until this is flipped. A real reconcile close needs
**BOTH** this switch (to run) **and** `worker-autoclose.enabled` (for `--execute` to be honored —
defense in depth). Enable via **any one** of:

```bash
export HANDOFF_WORKER_AUTOCLOSE_RECONCILE=1                            # process/global
touch "$HANDOFF_HOME/worker-autoclose-reconcile.enabled"              # fleet-wide
touch "$HANDOFF_HOME/<project>/worker-autoclose-reconcile.enabled"    # one project
```

**Kill-switch** (emergency disable, no config edit — the driver then does nothing, not even dry-run):

```bash
touch "$HANDOFF_HOME/.worker-autoclose-off"                    # fleet-wide
touch "$HANDOFF_HOME/<project>/.worker-autoclose-off"          # one project
```

Durable decision log (every close / refuse+reason / unbindable): `$HANDOFF_HOME/<project>/autoclose-audited.log` (JSONL).

## D — going-forward trigger (coordinator SOP — least-invasive option)

The least-invasive wiring (chosen over engine-editing audit-close, which would breach
coordinator-no-self-edit and enlarge the blast radius) is a **coordinator SOP step**: after a
coordinator audits a worker to GREEN-terminal and merges its branch, it runs the four commands
below **in order**.

🔴 **Why `record-head` is step 0 (do NOT skip — without it the gate REFUSES every legit close).**
Predicate 2 is reclaim-parity: it honors `.audit_discharged` only when `merge_sha` **EQUALS the
task's pinned closing commit** (`reclaim._pinned_sha` = `ack/<task>.old_ready` `commit_hash` >
`ack/<task>.head.json` `head_sha`), not merely descends from the shared spawn base (that base-only
lower bound is the FORGE-D hole). If the worker never dumped an `old_ready` (e.g. it only
`touch`-ed `worker_reported`), **nothing populates the pinned record** → `_pinned_sha` returns
`None` → the gate fails closed and the window never auto-closes (fail-safe, but the feature is
INERT). `handoff worktree record-head` is the evidence channel that fills `head.json`.

🔴 **Independence (keep the FORGE-D defense intact).** `record-head` reads the **worktree/branch
HEAD at audit time** — an *independent* capture of the worker's actual delivery. The `merge_sha`
then equals the pinned SHA **by construction of the ff-only merge** (ff = no new commit, so
`main` advances to exactly the worker's branch tip), NOT by echoing the discharge input into the
pinned record. Never set `pinned = merge_sha` from the discharge — that would re-open FORGE D
(a worker could pin any commit it points `--merge-sha` at). Run `record-head` **before** the merge.

```bash
# 0) record the worker's HONEST closing commit (reads the worktree/branch HEAD → head.json).
#    Independence: this is captured from the worker's delivery, BEFORE the merge, not from the
#    coordinator's --merge-sha input.
handoff worktree record-head <task> --project <project>

# 1) merge the worker's branch fast-forward-only → main advances to EXACTLY the worker's tip,
#    so the resulting merge SHA == the pinned head recorded in step 0. PUSH it: the gate
#    proves "merged" by fetching origin/main and checking merge_sha is its ancestor — an
#    un-pushed local merge fails the gate ("not-merged", fail-safe). Push first.
git merge --ff-only handoff/<task>          # merged SHA = worker HEAD = recorded pinned
git push origin main                        # gate checks ancestry against origin/main

# 2) write the strong git-corroborated signal (the COORDINATOR does this — it has the merge SHA):
handoff audit-discharge <task> --project <project> --verdict GREEN --merge-sha <merged-sha>
#    optional: --worktree-head <sha>  --nonce <spawn-nonce>   (worktree-head defaults to merge-sha)

# 3) gate + (if worker-autoclose.enabled) close that one worker window — dry-run unless --execute:
~/.claude-handoff/supervisor-monitor/autoclose-audited-workers.py \
    --project <project> --task <task> [--execute]
```

Step 3 is safe to run unconditionally: it is dry-run unless BOTH `--execute` is passed AND the
opt-in switch is ON, and the gate refuses anything not provably audited-to-terminal + idle + clean.

> **Note — workers that dump a handoff already carry the pinned record.** A worker that ran
> `handoff dump`/`audit-close` at its own handoff writes `old_ready.commit_hash` (the retro-gated
> recorder, which `_pinned_sha` prefers); for those, step 0 is redundant but harmless (it only
> writes the `head.json` fallback channel). Run step 0 unconditionally so the flow is correct for
> both worker shapes (dumped vs. report-only).

## Janitor backlog sweep (manual or scheduled)

```bash
~/.claude-handoff/supervisor-monitor/autoclose-audited-workers.py \
    --project <project> --sweep [--execute]
```

Scans every `ack/<task>.audit_discharged` in the project, gates each, and closes the cleared ones
(WID-bound). DRY-RUN unless `--execute` + opt-in ON.

## Git-terminal reconcile (the self-triggering mode — manual or scheduled)

```bash
~/.claude-handoff/supervisor-monitor/autoclose-audited-workers.py \
    --project <project> --reconcile [--execute]
```

Gates EVERY **open** worker window from its own merged delivery (no discharge signal needed). No-op
unless `worker-autoclose-reconcile.enabled`; DRY-RUN unless additionally `worker-autoclose.enabled` +
`--execute`. This is the mode the hourly daemon should run so auto-close actually self-triggers (the
sweep/launchd wiring to `--reconcile` is routed to dx per red-line #4 — see §E).

## E — janitor launchd scheduler (DESIGN-ONLY → route to dx; do NOT install here)

Red-line #4: launchd plists live in cc-global / dharmaxis territory. This worker writes the DESIGN +
the exact invocation only; **the coordinator routes installation to dx**. The sweep LOGIC (C) runs
fine without launchd via the manual `--sweep` command above.

Proposed plist (one project shown; replicate per opted-in project, or wrap a multi-project sweep in
a small shell script the plist calls). `--execute` is intentionally included but is a NO-OP until the
project's `worker-autoclose.enabled` sentinel exists — defense in depth:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>dharmaxis.worker-autoclose-sweep.handoff-fanout</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/chenmingzhong/.claude-handoff/supervisor-monitor/autoclose-audited-workers.py</string>
    <string>--project</string>
    <string>handoff-fanout</string>
    <string>--sweep</string>
    <string>--execute</string>
  </array>
  <!-- needs handoff_fanout importable: run under the env/python that carries the editable install -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>HANDOFF_HOME</key>   <string>/Users/chenmingzhong/.claude-handoff</string>
  </dict>
  <key>StartInterval</key>    <integer>3600</integer>            <!-- hourly backlog sweep -->
  <key>RunAtLoad</key>        <false/>
  <key>StandardOutPath</key>  <string>/Users/chenmingzhong/.claude-handoff/handoff-fanout/autoclose-sweep.out.log</string>
  <key>StandardErrorPath</key><string>/Users/chenmingzhong/.claude-handoff/handoff-fanout/autoclose-sweep.err.log</string>
</dict>
</plist>
```

Install notes for dx (NOT done by this worker):
- The plist's `/usr/bin/python3` must be able to `import handoff_fanout` (editable install) and
  `import handoff_fanout.autoclose_gate`. If the system python lacks it, point `ProgramArguments[0]`
  at the python that carries the editable install (or wrap in a venv-activating shell script).
- Keep `worker-autoclose.enabled` OFF until owner flips ENABLE (the plist's `--execute` is inert
  until then). The kill-switch sentinel disables the sweep without unloading the plist.
- Hourly `StartInterval` is a starting point; raise it if winlist/goto contention is observed.

## Rollout / reversibility

dry-run default · opt-in switch (DEFAULT-OFF) · kill-switch sentinel · durable JSONL log · closed
sessions are /resume-recoverable. Process per spec §3: build → zero-trust review → v2.1 dual-brain
(DOUBLE-GREEN, workflows真跑 replica) → behavior-verify (dry-run + close 1–2 clearly-audited terminal
worker windows, confirm protected ones untouched) → owner gate for ENABLE → janitor launchd → dx.
