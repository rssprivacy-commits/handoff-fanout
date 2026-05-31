# Runbook — Step 2c on-box lock-screen validation (owner-physical)

> **Status:** OWNER-PHYSICAL. This step cannot be done by an agent or by unit
> tests (they stub the lock probe). It must be run at the keyboard: configure the
> unlock env, opt in a **throwaway non-financial** project, physically lock the
> screen, and watch one real spawn + every failure path.
>
> Gates Step 2d (enable ERP). Do **not** point this at `erp-system` — validate on
> a scratch project first. Companion: `runbook-unlock-pivot-rollout.md` §2c.
>
> Verified prerequisites (2026-05-31, on box):
> - 2a wrapper `~/.local/bin/mp-unlock` exists; `--status` → 0/1 (not 2).
> - 2b runtime launcher `~/.local/bin/auto-continue.sh` == canonical `89935cf…`
>   (drift guard quiet); contains the audited unlock path; **default-OFF**.
> - launchd job `com.dharmaxis.auto-continue` plist at
>   `~/Library/LaunchAgents/com.dharmaxis.auto-continue.plist`; current
>   `EnvironmentVariables` has `HANDOFF_AUDIT_MANDATE=1` + `HANDOFF_RETRO_MANDATE=1`
>   but **no** `HANDOFF_UNLOCK_CMD/RELOCK_CMD` yet (this step adds them).
> - Keychain item `mindpersist-login-password` present (unlock won't rc=2 on a
>   missing password).

---

## 0. Brakes (keep these in your back pocket — always available)

```bash
touch ~/.claude-handoff/STOP_AUTO                      # pause ALL auto-continue
rm -f ~/.claude-handoff/_scratch_unlock/unlock.enabled # disable scratch unlock
rm -f ~/.claude-handoff/<proj>/.unlock-cooldown        # clear a stuck cooldown
rm -f ~/.claude-handoff/.relock-failed                 # clear the durable halt (AFTER you manually re-lock)
```

Live monitor in a second terminal the whole time:

```bash
tail -f ~/.claude-handoff/auto-continue.log
```

---

## 1. Configure the unlock env on the launchd job (one-time)

Add the two unlock commands to the job's `EnvironmentVariables`, then reload it.
The launcher word-splits these (intentional argv protocol), so include the flag
in the string.

```bash
PLIST=~/Library/LaunchAgents/com.dharmaxis.auto-continue.plist
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:HANDOFF_UNLOCK_CMD string '$HOME/.local/bin/mp-unlock --unlock'" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:HANDOFF_UNLOCK_CMD '$HOME/.local/bin/mp-unlock --unlock'" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:HANDOFF_RELOCK_CMD string '$HOME/.local/bin/mp-unlock --lock'" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:HANDOFF_RELOCK_CMD '$HOME/.local/bin/mp-unlock --lock'" "$PLIST"

# reload the agent so it picks up the new env
launchctl bootout "gui/$(id -u)/com.dharmaxis.auto-continue" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# verify the env is now present
launchctl print "gui/$(id -u)/com.dharmaxis.auto-continue" | grep -E "HANDOFF_(UN|RE)LOCK_CMD"
```

✅ Expect both `HANDOFF_UNLOCK_CMD` and `HANDOFF_RELOCK_CMD` to appear.

> Note: this only ARMS the launcher with the commands. Auto-unlock still fires
> ONLY for a project that has its own `unlock.enabled` sentinel (per-project
> opt-in, A1). No project is opted in yet, so nothing unlocks until step 2.

---

## 2. Opt in a THROWAWAY scratch project (not ERP)

The scratch project dir, workspace, and a **staged** probe are pre-built by the
agent (see "Agent pre-build" note below). You only flip the per-project opt-in:

```bash
SCRATCH=~/.claude-handoff/_scratch_unlock
touch "$SCRATCH/unlock.enabled"          # per-project opt-in — scratch ONLY (deliberate arming)
```

> ⚠️ **Why the probe is *staged*, not queued, and why you must use a DELAYED write
> in §3:** the launchd job watches `~/.claude-handoff/` and fires near-instantly on
> any `*.uri` write. If you create `probe.uri` while the screen is UNLOCKED, the
> launcher consumes it on the **unlocked** path before you can lock — testing the
> wrong path and wasting the probe. So the probe lives as `probe.uri.staged` (not a
> `*.uri`, never consumed) and you promote it to `probe.uri` with a **delayed
> background write** so the tick that picks it up happens AFTER you've locked.

Agent pre-build creates (no unlock fires — there is no `*.uri` in the queue and
`unlock.enabled` is left for you):
- `~/.claude-handoff/_scratch_unlock/{queue,ack}/`
- workspace `~/tmp-unlock-scratch/`
- `~/.claude-handoff/_scratch_unlock/queue/probe.uri.staged` (the probe content)

---

## 3. The happy-path test (physical) — DELAYED write + lock

1. Confirm `tail -f …/auto-continue.log` is running in another terminal, and that
   §1 env + §2 `unlock.enabled` are in place.
2. Arm the delayed write, then **lock within the delay window**:
   ```bash
   SCRATCH=~/.claude-handoff/_scratch_unlock
   ( sleep 25; cp "$SCRATCH/queue/probe.uri.staged" "$SCRATCH/queue/probe.uri" ) &
   # …now press Ctrl+Cmd+Q to LOCK within 25s and step away ~90s.
   ```
   The `.uri` lands ~25s later (screen already locked) → the tick that consumes it
   runs on the LOCKED path. If you miss the 25s window, `rm $SCRATCH/queue/probe.uri`
   and retry.
3. Watch the log. Expected sequence for `project=_scratch_unlock task=probe`:
   - `UNLOCK-OK: project=_scratch_unlock task=probe (rc=0)`
   - `SUCCESS: spawned Claude tab …`
   - `AUTO-SUBMIT: pressed Enter …`  *(or `ABORT-SUBMIT … re-locked`/`Accessibility`
     if a precondition failed — see §5)*
4. Unlock and verify, in order:

```bash
# screen actually re-locked itself after the spawn (do this BEFORE you unlock if
# you can — or note whether you found it locked):
~/.local/bin/mp-unlock --status; echo "0=unlocked 1=locked → expect it had re-locked"

ls ~/.claude-handoff/.unlock.lock 2>/dev/null && echo "❌ mutex leaked" || echo "✅ mutex released"
pgrep -fl caffeinate || echo "✅ no orphan caffeinate"
ls ~/.claude-handoff/.relock-failed 2>/dev/null && echo "❌ relock-failed present" || echo "✅ no relock-failed"
ls ~/.claude-handoff/_scratch_unlock/ack/probe.* 2>/dev/null   # spawned/submitted ack
cat ~/.claude-handoff/_scratch_unlock/queue/probe.deferred 2>/dev/null && echo "(deferred — investigate)" || echo "✅ not deferred"
```

✅ **Happy-path PASS** = `UNLOCK-OK` → tab spawned → submitted → screen re-locked +
mutex released + no orphan caffeinate + no `.relock-failed` + not deferred.

---

## 4. Restore between drills

After each failure drill below, restore state before the next:

```bash
rm -f ~/.claude-handoff/_scratch_unlock/.unlock-cooldown
rm -f ~/.claude-handoff/.relock-failed
# re-queue the probe (.uri is consumed on a successful claim):
#   recreate $SCRATCH/queue/probe.uri as in §2
```

---

## 5. Failure-path drills (verify EACH behaves as designed)

These prove the safety machinery. Run one at a time, lock the screen, observe, restore.

### 5a. Wrong password → cooldown after threshold (N=2)
Temporarily set a wrong Keychain password, then lock + watch:
```bash
# save the real one first (note it / keep it), then:
security add-generic-password -U -s mindpersist-login-password -a "$USER" -w "WRONG_ON_PURPOSE"
```
- Lock screen, wait 2 ticks. Expect (log): `unlock-failed-rc1` defer → after the
  2nd consecutive fail: `UNLOCK-COOLDOWN … pause auto-unlock`, a notification, and
  `~/.claude-handoff/_scratch_unlock/.unlock-cooldown` written.
- **Restore the real password immediately**, then `rm …/.unlock-cooldown`.

### 5b. Config error rc=2 → permanent manual-only
Point the unlock cmd at a broken wrapper so the CLI returns 2:
```bash
launchctl setenv HANDOFF_UNLOCK_CMD "$HOME/.local/bin/mp-unlock --bogus-flag"   # arg error → exit 2
# (or temporarily edit the plist env; reload as in §1)
```
- Lock, one tick. Expect: `UNLOCK-CONFIG-ERROR … manual-only`, notification, and
  `.unlock-cooldown` with `last_rc=2` + a ~100-year `next_retry_epoch`.
- Restore the correct `HANDOFF_UNLOCK_CMD`, reload, `rm …/.unlock-cooldown`.

### 5c. Broken relock → `.relock-failed` + DURABLE halt
Make re-lock fail so the launcher must halt:
```bash
launchctl setenv HANDOFF_RELOCK_CMD "/usr/bin/false"   # relock "succeeds" exit-wise but screen stays unlocked
```
- Lock, one tick. Expect: unlock+spawn succeed, then `RELOCK-FAIL: screen not
  re-locked; halting further spawns`, a loud notification, `~/.claude-handoff/.relock-failed`
  written, and `HALT: relock failed`.
- **Verify the durable halt (A3 fix):** with `.relock-failed` still present, queue
  another probe `.uri` and wait a tick → the log shows
  `HALT: .relock-failed present — skipping all spawns` and **no** new spawn.
- Restore: fix `HANDOFF_RELOCK_CMD`, **manually re-lock once to confirm it works**,
  then `rm ~/.claude-handoff/.relock-failed`.

---

## 6. Cleanup (after all drills pass)

```bash
rm -rf ~/.claude-handoff/_scratch_unlock ~/tmp-unlock-scratch
rm -f ~/.claude-handoff/.relock-failed ~/.claude-handoff/STOP_AUTO
# leave HANDOFF_UNLOCK_CMD/RELOCK_CMD in the plist if you intend to proceed to 2d;
# remove them if you want to fully disarm:
#   /usr/libexec/PlistBuddy -c "Delete :EnvironmentVariables:HANDOFF_UNLOCK_CMD" "$PLIST"
#   /usr/libexec/PlistBuddy -c "Delete :EnvironmentVariables:HANDOFF_RELOCK_CMD" "$PLIST"  (then reload)
```

---

## 7. Exit criterion → unlock Step 2d

✅ 2c PASSES when, on a real locked screen:
- happy path: unlock → visible GUI tab → submit → **re-lock**, mutex released, no
  orphan caffeinate, no `.relock-failed`;
- 5a wrong password → cooldown after N=2;
- 5b rc=2 → permanent manual-only;
- 5c broken relock → `.relock-failed` + durable cross-run halt, confirmed to block
  the next tick.

Only then proceed to **Step 2d** (enable ERP: configure the same env durably +
`touch ~/.claude-handoff/erp-system/unlock.enabled`) — the highest-blast step,
your conscious go (runbook §2d + §6 B2 security acceptance).
