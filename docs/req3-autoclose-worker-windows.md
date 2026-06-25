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
| **A** signal writer | `handoff audit-discharge` → writes the non-forgeable `ack/<task>.audit_discharged` | `src/handoff_fanout/codex_audit.py` (git, editable) |
| **gate** | the 5-predicate fail-safe safety gate (pure, unit-tested) | `src/handoff_fanout/autoclose_gate.py` (git, editable) |
| **B** close-by-WID | `coord-close-windows.py --close-wid` (AI-title + §6(a) TOCTOU root-fix) + §6(b) stats fix | `~/.claude-handoff/supervisor-monitor/coord-close-windows.py` (non-git, deploy-audited) |
| **C** driver/sweep | `autoclose-audited-workers.py` (thin glue: gate → bind WID → invoke the close tool) | `~/.claude-handoff/supervisor-monitor/autoclose-audited-workers.py` (non-git, deploy-audited) |
| **D** opt-in switch | `worker-autoclose.enabled` (DEFAULT-OFF) + kill-switch | sentinel files under `$HANDOFF_HOME` (see below) |
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

## D — the opt-in switch (DEFAULT-OFF) + kill-switch

`worker-autoclose.enabled` is **SEPARATE** from the v4 coordinator-autoclose switch
(`autoclose.enabled`, which stays OFF per owner). DEFAULT-OFF: without it, `--execute` is forced to
dry-run. Enable via **any one** of:

```bash
export HANDOFF_WORKER_AUTOCLOSE_ENABLED=1                       # process/global
touch "$HANDOFF_HOME/worker-autoclose.enabled"                 # fleet-wide
touch "$HANDOFF_HOME/<project>/worker-autoclose.enabled"       # one project
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
coordinator audits a worker to GREEN-terminal and merges its branch, it runs two commands:

```bash
# 1) write the non-forgeable signal (the COORDINATOR does this — it has the merge SHA):
handoff audit-discharge <task> --project <project> --verdict GREEN --merge-sha <merged-sha>
#    optional: --worktree-head <sha>  --nonce <spawn-nonce>   (worktree-head defaults to merge-sha)

# 2) gate + (if worker-autoclose.enabled) close that one worker window — dry-run unless --execute:
~/.claude-handoff/supervisor-monitor/autoclose-audited-workers.py \
    --project <project> --task <task> [--execute]
```

Step 2 is safe to run unconditionally: it is dry-run unless BOTH `--execute` is passed AND the
opt-in switch is ON, and the gate refuses anything not provably audited-to-terminal + idle + clean.

## Janitor backlog sweep (manual or scheduled)

```bash
~/.claude-handoff/supervisor-monitor/autoclose-audited-workers.py \
    --project <project> --sweep [--execute]
```

Scans every `ack/<task>.audit_discharged` in the project, gates each, and closes the cleared ones
(WID-bound). DRY-RUN unless `--execute` + opt-in ON.

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
