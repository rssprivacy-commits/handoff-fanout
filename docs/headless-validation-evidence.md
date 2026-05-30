# Headless fallback — pre-enable validation evidence (§2.2 / §2.3 gate)

> Status: **PENDING — headless stays OFF until every row below is ✅.**
>
> The implementation (`handoff_fanout.headless_runner` + `auto-continue.sh`
> lock routing + the `com.dharmaxis.handoff-headless` launchd job) has landed
> and is unit-tested, but the design (`design-headless-fallback-display-off.md`
> §2.2/§2.3) makes ENABLING headless conditional on on-box empirical evidence
> that **cannot** be settled by unit tests or design prose — it must be captured
> from the **launchd** context with the screen **actually locked**.
>
> Until this file's gate rows are all ✅ AND the owner rules to enable, no
> project should carry a `headless.enabled` sentinel. With the sentinel absent,
> `auto-continue.sh` never writes a `.req`, so the runner job stays idle.

## Why these can't be unit-tested

The unit suite stubs the lock probe, `claude`, and `caffeinate`, and runs as a
foreground process. The failure mode this whole feature targets is **unattended,
launchd-context, screen-locked** — exactly the conditions the harness can't
reproduce. So the four process-mechanics spikes and the lock-probe-under-launchd
check are gated here, not in `tests/`.

## Gate rows — capture each from the headless launchd job, screen LOCKED

| # | Check | How to capture | Result |
|---|---|---|---|
| 1 | `ioreg` lock probe returns `= Yes` from the **launchd** context (not Terminal) under **normal lock** | stub a runner that logs `screen_is_locked` rc to a file; lock; inspect | ☐ PENDING |
| 2 | …same under **fast-user-switching** | FUS to another user, then inspect the logged rc | ☐ PENDING |
| 3 | …same under **screensaver-lock** | start screensaver w/ immediate lock; inspect | ☐ PENDING |
| 4 | A stub runner spawned by the headless job actually **starts + writes its ack** while locked | drop a `.req`, lock, confirm `ack/<task>.submitted-headless` (stub) appears | ☐ PENDING |
| 5 | `caffeinate -i` is visible in `pmset -g assertions` for the run's lifetime | `pmset -g assertions \| grep -i caffeinate` mid-run | ☐ PENDING |
| 6 | **QueueDirectories re-launch**: drop `.req` B while the runner is mid-`.req` A → launchd re-launches so B is drained (no stuck req) | two reqs, one slow stub, confirm both eventually drain | ☐ PENDING |
| 7 | **killpg tree reap**: child spawns a bash grandchild → on STOP and on timeout `os.killpg` leaves **zero** survivors | stub `sh -c 'sleep 300 & sleep 300'`; STOP / timeout; `pgrep` the tree | ☐ PENDING |
| 7b | **setsid-escape probe**: a grandchild that itself `setsid`s escapes the group — confirm whether any real claude tool-call class does this; if so add a `pgrep -P` recursive fallback before enable | stub a `setsid`-ing grandchild; verify it survives, then decide | ☐ PENDING |
| 8 | **runner-SIGKILL orphan reconcile**: `kill -9` the runner mid-run → janitor on next launch validates start-time + clears the stale pidfile (and, if a verified group remains, reaps it) | kill -9 runner; relaunch; inspect pidfile + survivors | ☐ PENDING |
| 9 | **start_new_session vs launchd**: the child is NOT prematurely reaped by launchd job exit, IS reaped by runner/janitor | observe child across runner exit | ☐ PENDING |
| 10 | **Mandate-inside-headless (§2.3)**: a real headless task whose end calls `handoff dump` with mandate ON + no `--retro-evidence` exits nonzero AND the runner turns it into a visible `BLOCKED.md` (not silent death) | run one real task that under-dumps; confirm `BLOCKED.md` | ☐ PENDING |
| 10b | **Flag-set validation**: the default headless claude cmd (`claude-rc.py`, which already injects `--dangerously-skip-permissions`) PLUS the runner-appended `--permission-mode bypassPermissions --model … -p` must be a flag set the CLI accepts (the two permission flags could conflict). Confirm `claude -p` actually starts + reads the stdin prompt with the exact default argv; if it rejects the combo, drop `--permission-mode` via `HANDOFF_CLAUDE_HEADLESS_FLAGS` | run the default argv with a trivial prompt; confirm it starts | ☐ PENDING |

Any row failing ⇒ headless stays OFF; the GUI path + fail-closed defer remain the
safe default (a locked machine simply PAUSES the relay until unlock, with the
`<task>.deferred` markers showing what's waiting).

## Owner decisions still open before enable (design §6)

- **(b)** permission posture confirmed `bypassPermissions` + worktree precheck
  (mechanism shipped: dirty-tree refusal always on; `HANDOFF_PROTECTED_BRANCHES`
  defaults EMPTY so it does not break main-based handoff — owner sets the list if
  a protected branch should be refused).
- **(c)** model: `HANDOFF_HEADLESS_MODEL` (default `opus`).
- **(d)** timeout: `HANDOFF_HEADLESS_TIMEOUT` (default 2700s / 45 min).
- **(e)** concurrency: `HANDOFF_MAX_HEADLESS` (default 1).

## Audit debt (codex was unavailable at impl time)

The codex CLI was not installed on the impl box (machine-verified
"binary not installed" runtime failures), so the implementation carries an
**owed codex R2 audit**. An adversarial self-review stood in (and caught the
critical key-absent lock-probe relay-stall bug, since fixed). Before enable:
re-run a real codex R2 against the implementation, or the owner accepts the
self-audit on record.

## Enable procedure (after all rows ✅ + owner ruling)

1. `bash install/install.sh --headless` (generates + loads the launchd job,
   creating each project's `headless-req/`; asserts the plist carries both
   mandate envs).
2. Per project to enable: `touch ~/.claude-handoff/<project>/headless.enabled`
   (or global `~/.claude-handoff/headless.enabled`, or `HANDOFF_HEADLESS_ENABLED=1`).
3. Verify with a real overnight task; watch `headless-runner.log` +
   `<project>/headless/<task>.log`.
