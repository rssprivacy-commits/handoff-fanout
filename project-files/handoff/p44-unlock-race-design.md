# p44 — launcher unlock concurrency race: surgical fix design

**Author**: sw-coord-p44 (handoff-fanout coordinator)
**Date**: 2026-06-20
**File**: `install/auto-continue.sh` (deploy-trapped launcher COPY)
**Scope**: fleet-critical, run-in-place after `install.sh --sync-launcher`
**Owner ruling**: solve-first (fix the race; keep the current 7 unlock-enabled projects).

## 0. North star
handoff-fanout = unattended cross-desktop multi-session AI-org dispatch. This fix is a **reliability** fix on the auto-unlock path: a concurrent launcher tick must never have its `Enter` keystroke slammed by another tick's re-lock, and a non-unlock-enabled project must never piggyback another project's auto-unlocked window. Failure mode is *recoverable* (a deferred/failed spawn, not a credential leak) — but with fleet-wide unlock now enabled on 7 projects the race probability is amplified.

## 1. The race (root cause — verified by reading the deployed + repo source)

The per-tick spawn loop (`install/auto-continue.sh`, `for URI_FILE in "$QUEUE"/*.uri`) gates the GUI path on screen-lock state at the top of each task iteration:

```
screen_is_locked; _LRC=$?                     # ~1606
[ "$_LRC" = "2" ] → defer "lock-unknown"; continue
if [ "$_LRC" = "0" ]; then                    # LOCKED branch — entered ONLY when locked
    ... unlock_enabled / cooldown / unlock_cmd / relock_cmd gates ...
    acquire_unlock_lock || defer "unlock-busy"; continue     # ~1627  ← MUTEX taken HERE
    UNLOCK_LOCK_HELD=1
    ... caffeinate, re-probe, unlock CLI, verify, UNLOCKED_BY_US=1 ...
    # mutex HELD across to _post_iter_cleanup (which do_relock's + releases)
fi
# Atomic claim → open → Enter                  # ~1671  ← reached for BOTH branches
```

`GLOBAL_UNLOCK_LOCK` (`$HANDOFF_ROOT/.unlock.lock`) is acquired **only inside the `_LRC=0` (locked) branch**. When `_LRC=1` (screen already unlocked) the whole branch is skipped and the code falls straight through to the atomic claim / `open` / `Enter` **without ever touching the mutex**.

**Race**:
1. Run A (e.g. `erp`) tick: screen LOCKED (`_LRC=0`) → acquires `.unlock.lock`, auto-unlocks (`UNLOCKED_BY_US=1`), holds mutex+caffeinate, proceeds to claim/open/Enter.
2. Run B (e.g. `hf`) — a concurrent launchd tick — starts during A's unlocked window: probes `screen_is_locked` → now `_LRC=1` (A unlocked it) → **skips the locked branch entirely** (skips both the per-project `unlock.enabled` gate AND the mutex) → proceeds to claim/open/Enter concurrently.
3. A finishes its iteration → `_post_iter_cleanup` → `UNLOCKED_BY_US=1` → `do_relock` re-locks the screen.
4. B's `Enter` now lands on the **lock screen** → swallowed / mis-targeted.

Two defects, one root cause (mutex scope too narrow):
- **(codex P1 / gemini P0) relock race** — B's Enter hits A's re-lock.
- **(codex P1) policy boundary break** — a project with no `unlock.enabled` can piggyback another project's auto-unlocked window (it never re-checks `unlock_enabled_for_project` on the fall-through path).

Both `erp` and `hf` already carry this race today; fleet-wide enablement (7 projects) amplifies it.

## 2. Fix — REVISED after R1 (codex+gemini BOTH RED on the original "defer-if-held" minimum)

### R1 verdict (audits/p44-unlock-race-design-r1.md, dual-brain GREEN-degraded=false)
Both brains confirmed the original `defer-if-mutex-held` guard closes the *active-holder* race, but **RED** because it is **advisory, not a critical section**: a residual TOCTOU remains — between the guard's `unlock_lock_active` check and the atomic claim, holder A can `do_relock` + `release_unlock_lock`, so B passes the (now-free) guard and fires open/Enter onto a freshly-relocked screen. They split on the fix:
- **codex**: insufficient — needs "a real global GUI/unlock critical section, acquired **before the authoritative lock-state decision** and held through open/submit/cleanup."
- **gemini**: scope is right but **add a `screen_is_locked` recheck before the claim**.

### Arbitration (coordinator read the submit-time gates, zero-trust)
The residual's worst case is NOT a bad keystroke: [auto-continue.sh:2006-2035](../../install/auto-continue.sh) shows the **front=Code atomic gate** (singlepane) and the **screen-is-locked recheck** (cold/warm) already prevent any Enter on a lock screen — for singlepane it degrades to an honest `failed` ack (the p43-accepted trade-off), for cold/warm to a clean `re-locked-before-submit` defer. So the residual is recoverable, not a safety hole. But both brains are RED on incompleteness, and per solve-first I close it at the source.

**gemini's recheck costs 1.1–4.9s on EVERY spawn** (re-adds exactly the Quartz `--status` probe p43 deliberately removed from the hot path) and *still* leaves a sub-window. **codex's critical section, implemented correctly, adds ZERO probe cost** and closes the race completely.

### Chosen: codex's full critical section — acquire the mutex BEFORE the single lock probe, fold the old re-probe
Acquire `GLOBAL_UNLOCK_LOCK` for **every** spawn *before* the authoritative `screen_is_locked`, and hold it across claim→open→Enter→relock (release in `_post_iter_cleanup`, as today). Because the lock-state decision is made **under** the mutex, no concurrent tick can relock under our Enter and no project can piggyback — **no separate recheck needed**. The single existing probe just moves under the lock; the old "re-probe under the mutex" (`_RC`) is **folded away**, so the locked path goes from 2 probes → 1 and the unlocked path stays at 1 → **no p43 hot-path regression** (probe count same-or-fewer).

**Cost**: overlapping spawns serialize (a `spawn-busy` defer + next-tick retry). Accepted because (i) fleet spawns are ~30 min apart per coordinator → overlap is rare and the owner won't perceive it; (ii) serializing concurrent spawns also fixes the UI focus-contention gemini itself flagged (two focus-jumps fighting); (iii) a crashed holder cannot wedge the fleet — `acquire_unlock_lock`'s 180s stale-break already bounds it, and the locked path *already* held the mutex across the full spawn ("globally serial", old line 1668) so the risk profile is unchanged, just slightly more frequent.

### 2.1 New read-only helper (mirrors `acquire_unlock_lock`'s liveness/staleness rule)

```bash
# True (rc 0) iff the global unlock mutex is held by ANOTHER live run — i.e. some
# other launcher tick is mid unlock→spawn→relock critical section. Read-only: never
# breaks the lock (that's acquire_unlock_lock's job). A stale lock (dead/absent pid
# AND mtime >180s — the SAME break rule as acquire_unlock_lock) is treated as NOT
# held, so a crashed holder can never wedge the fleet forever. Our own pid → not
# "another run" (defensive; this path never holds the mutex).
unlock_lock_active() {
    [ -d "$GLOBAL_UNLOCK_LOCK" ] || return 1
    local pid; pid=$(cat "$GLOBAL_UNLOCK_LOCK/pid" 2>/dev/null)
    case "$pid" in ''|*[!0-9]*) pid="" ;; esac
    if [ -n "$pid" ]; then
        [ "$pid" = "$$" ] && return 1                    # our own → not another run
        kill -0 "$pid" 2>/dev/null && return 0           # live holder → active
    fi
    local mt; mt=$(_u_mtime "$GLOBAL_UNLOCK_LOCK"); local now; now=$(/bin/date +%s)
    if [ -n "$mt" ] && [ "$((now - mt))" -le 180 ]; then return 0; fi   # fresh → active
    return 1                                              # stale → not held
}
```

The held/stale boundary is **identical** to `acquire_unlock_lock` (live pid → busy; else fresh-mtime ≤180s → busy; else stale → break). `unlock_lock_active` returns "active" in exactly the cases `acquire_unlock_lock` returns "busy" (rc 1), so the two never disagree about whether a lock is live.

### 2.2 Guard inserted after the lock-gating block, before the atomic claim (`~1670`)

```bash
        fi   # end of the `if [ "$_LRC" = "0" ]` locked branch

        # Concurrency guard (sw-coord-p44): close the fleet-wide unlock race. If we
        # did NOT enter the locked branch (screen already unlocked, _LRC=1 — so
        # UNLOCK_LOCK_HELD=0) but ANOTHER run holds .unlock.lock, it is mid
        # unlock→spawn→RELOCK; spawning now would (a) let our Enter land on its
        # re-lock screen and (b) let a non-unlock-enabled project piggyback its
        # auto-unlocked window. Defer fail-closed until that run finishes (next tick
        # finds the screen re-locked → we go through our own gated unlock). When WE
        # hold the mutex (UNLOCK_LOCK_HELD=1) this is a no-op — we ARE the section.
        if [ "$UNLOCK_LOCK_HELD" != "1" ] && unlock_lock_active; then
            defer_uri "$PROJ_DIR" "$QUEUE" "$TASK" "unlock-in-progress-elsewhere"
            continue
        fi

        # Atomic claim
        TS=$(date +%s%N)
        ...
```

- `UNLOCK_LOCK_HELD != "1"` → only the fall-through (`_LRC=1`) path is guarded; the run that legitimately holds the mutex proceeds (it IS the critical section).
- `continue` (no `_post_iter_cleanup`): on the fall-through path **no** per-iteration state was acquired — `CAFF_PID=""`, `UNLOCKED_BY_US=0`, `UNLOCK_LOCK_HELD=0`, `MAY_NEED_RELOCK=0` were all reset at the top of the iteration (lines ~1602-1605), so cleanup would be a pure no-op. This matches the established pre-acquisition defer pattern (e.g. `_LRC=2 → defer; continue` at ~1609).
- Reuses `defer_uri` (durable `.deferred` marker + watchdog "N waiting" surfacing + rate-limited notification; marker auto-cleared when the `.uri` is finally consumed at ~1680). Transient: resolves on the next tick once the other run relocks/releases.

## 3. Why this is complete & safe

| Scenario | Behavior with fix |
|---|---|
| Normal: screen unlocked, no concurrent unlock | `.unlock.lock` absent → `unlock_lock_active` rc1 → no defer → spawn as before (**zero behavioral change**) |
| A auto-unlocked (holds mutex), B (any project) tick | B sees `_LRC=1` + mutex held → **defer** `unlock-in-progress-elsewhere`; B's next tick (after A relocks) finds locked → own gated unlock |
| A auto-unlocked, B is NOT unlock-enabled | Same defer → **piggyback closed** |
| The run that holds the mutex (legitimate locked-path spawn) | `UNLOCK_LOCK_HELD=1` → guard skipped → proceeds (unchanged) |
| Crashed holder leaves stale `.unlock.lock` | `unlock_lock_active` treats stale (dead pid & mtime >180s) as not-held → fleet not wedged; the EXIT/TERM trap (`_on_terminate`) already relocks+releases on clean kill |
| Owner manually locks mid-spawn (residual) | Pre-existing, inherent, **out of scope** — not introduced or worsened by this fix; mutex cannot prevent owner-initiated locks |

- **fail-closed**: when uncertain we defer (keep the `.uri`), never blind-spawn.
- **`set -u` safe**: every var is `local`-initialized; `GLOBAL_UNLOCK_LOCK`/`_u_mtime` are pre-defined globals; conditions don't trip nounset.
- **single spawn region**: the `for URI_FILE` loop (~1567 → claim ~1671) is the **only** path to open/Enter (`.relock-failed` halt at lines 191/1532 is upstream and untouched). One insertion point covers all spawn paths.
- **deploy-trap discipline**: change to `install/auto-continue.sh` → commit with `HANDOFF_INSTALL_SH=/nonexistent`, verify live launcher SHA unchanged, gate GREEN, then deliberate `install.sh --sync-launcher`; rollback = p43 known-good `73d8f5c4` (`/tmp/auto-continue-rollback-p43.sh`).

## 4. Tests (regression guards — fail if the fix is reverted)

Extend `tests/test_unlock_routing.py` (shell-out harness with stateful stubs):

1. **`test_unlocked_defers_when_another_run_holds_unlock_mutex`** — seed unlocked `.uri`; pre-create `$HANDOFF_ROOT/.unlock.lock` dir holding a **live** pid (a real sleeping process) with fresh mtime → assert spawn **defers** with `unlock-in-progress-elsewhere`, `open` NOT invoked, `.uri` kept. (Disabling the guard → GUI opens → test FAILS.)
2. **`test_unlocked_proceeds_when_unlock_mutex_is_stale`** — seed unlocked `.uri`; pre-create `.unlock.lock` with a **dead** pid (e.g. a reaped pid) and mtime forced >180s old → assert spawn **proceeds** (GUI `open` invoked, `.uri` consumed). Proves a crashed holder can't wedge the fleet and the staleness rule matches `acquire_unlock_lock`.

Existing 16 unlock-routing tests unchanged (none pre-create `.unlock.lock` → guard is a no-op for them). Full suite expected **1686 → 1688 passed / 3 skipped / 0 failed**.

## 5. Pipeline
design (this doc) → **R1 dual-brain (codex+gemini, zero-trust, brief embeds code + design)** → implement + 2 tests → full suite GREEN → **R2 dual-brain (brief embeds diff)** → deploy-trap (`--sync-launcher`) → canary (watch `auto-continue.log` for `DEFER: ... reason=unlock-in-progress-elsewhere` appearing only during real concurrent unlocks, and zero regressions in normal spawns).
