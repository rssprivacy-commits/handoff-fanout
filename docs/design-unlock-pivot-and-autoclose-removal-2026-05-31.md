# Design — Unlock-pivot (scrap headless) + Autoclose removal

> Status: **PLAN / for codex R1 audit before any code.** Owner rulings on record
> (2026-05-31): ① locked-screen auto-continue must keep the **visible GUI tab**
> for human audit — NOT a headless blind-box; borrow MindPersist's proven
> auto-unlock. ② Uninstall the autoclose tab-closing feature ("be careful, don't
> break other functionality"). ③ Consolidate all three changes into one
> plan → codex audit → one PR.
>
> Author: Claude Code · 2026-05-31 · Repos touched: `handoff-fanout` (primary),
> `mindpersist` (expose unlock CLI), erp-system memory + capability-registry.

---

## 0. Why (premise)

The auto-continue relay exists to **develop ERP with the owner reviewing every
session** — what task ran, what problems it hit, what it delivered, quality,
bugs, suggestions. Two earlier mechanisms fight that purpose:

1. **Headless fallback** (PR #1 / `feat/headless-fallback`): runs the locked-screen
   session via `claude -p` with **no visible tab** → a blind-box the owner can't
   review. **Rejected by owner.**
2. **Autoclose**: closes the old VS Code tab after handoff → destroys the very
   transcript the owner audits. **Rejected by owner.** (Currently default-OFF and
   never triggered, but the code + installed extension are a standing risk.)

The correct lock-screen answer: **auto-UNLOCK the screen, then run the normal,
visible GUI path** (MindPersist already does exactly this in production for
WeChat sending). The tab stays open and reviewable. No blind-box, no tab loss.

---

## 1. Root mechanism (verified on box, 2026-05-31)

MindPersist `src/agent/idle.py` has a production unlock state machine:
- `is_screen_locked()` — Quartz `CGSSessionScreenIsLocked`.
- `unlock_screen()` — `caffeinate -u` wake → screenshot+OCR confirm lock UI →
  click password field → **CGEvent HID keyboard injection** of the login
  password (read from Keychain `mindpersist-login-password`, **confirmed present**)
  → Enter → verify; 3 attempts, AppleScript fallback.
- `lock_screen()` — Ctrl+Cmd+Q.
- **Key insight**: the lock blocks **AppleScript** keystrokes (what dead-stalled
  the GUI relay), but **CGEvent HID injection works at the login window**. That
  is the entire reason headless was unnecessary.
- MP venv has `Quartz` (pyobjc). `idle.py` has **no CLI entrypoint yet** (work item).

---

## 2. End state (what the relay does after this change)

Per `.uri` in the launcher loop:

| screen state | action |
|---|---|
| unlocked | GUI path (unchanged): `code -r` + `open URI` + osascript Enter |
| locked | **auto-unlock (MP)** → if unlocked → GUI path → (optional) re-lock after submit |
| unlock fails (3 attempts) | **defer**: keep `.uri`, write `<task>.deferred` marker, notify once/6h, resume on a later tick / manual unlock — **never** a dead silent stall |
| lock state UNKNOWN (ioreg failed) | defer (fail-closed) |

No headless. No autoclose. Tabs accumulate and stay open for the owner to review;
the owner closes them manually after auditing.

---

## 3. Workstream A — scrap headless (rework PR #1, keep the good parts)

**Remove** (headless-only):
- `src/handoff_fanout/headless_runner.py` (whole module) + `handoff headless-run`
  subcommand in `cli.py` + `handoff-headless-run` entry in `pyproject.toml`.
- `install/launchd/com.dharmaxis.handoff-headless.plist`.
- `install/install.sh --headless` block + the `headless-req` enumeration.
- `tests/test_headless_runner.py`.
- `auto-continue.sh`: the headless dispatch (`dispatch_headless`, `headless-req`
  write, `HANDOFF_HEADLESS_ENABLED`/`headless_enabled_for_project`, the
  launcher-start `--sweep-only` call), `docs/headless-validation-evidence.md`.

**Keep** (independent / reused by the unlock path):
- `dump.py` §3.7 **`atomic_replace`** for single-task `.md`/`.uri` — a real
  crash-safety fix, unrelated to headless. KEEP.
- `auto-continue.sh` `screen_is_locked()` incl. the **key-absent→UNLOCKED** fix +
  `HANDOFF_IOREG_CMD` + `HANDOFF_LOCK_CHECK_CMD` — the lock probe is now the
  router for the unlock path. KEEP.
- The **defer** machinery (`defer_uri`, `<task>.deferred` marker) — repurposed as
  the **unlock-failure fallback**. KEEP.
- `tests/test_headless_routing.py` — **rework**, not delete: keep the lock-probe
  parsing tests (ioreg key-absent / =Yes / failure), drop the headless-dispatch
  assertions, add unlock-path assertions.

> PR #1 disposition: do NOT merge as-is. Either rework on the same branch to the
> end state, or close #1 and open a fresh PR carrying only the kept parts +
> the new unlock path. (Recommend: rework on the branch; one clean PR.)

## 4. Workstream B — MindPersist unlock as a reusable capability

**MP side (mindpersist repo):**
- Add a thin CLI entrypoint exposing the existing functions (no logic rewrite):
  `src/agent/unlock_cli.py` with `main()` supporting `--unlock` / `--lock` /
  `--status`, calling `idle.unlock_screen()` / `lock_screen()` / `is_screen_locked()`.
  Register `mp-unlock = "agent.unlock_cli:main"` (or `python -m agent.unlock_cli`).
  Exit codes: 0 unlocked/success, 1 still-locked/failed, 2 no-password/error.
- Add `## 对外提供能力` section to MP `CLAUDE.md` documenting `mp-unlock`.
- Register in `~/.claude/projects/-Users-chenmingzhong/memory/capability-registry.md`
  under task scenario "解锁屏幕 / 锁屏自动化".

**handoff-fanout side:**
- `auto-continue.sh`: new env `HANDOFF_UNLOCK_CMD` (default = MP venv python +
  `-m agent.unlock_cli --unlock`, path discovered or configured) and
  `HANDOFF_RELOCK_CMD` (default = `… --lock`). Both **overridable for tests**
  (stub prints success/fail). A missing/failing unlock CMD ⇒ treat as
  unlock-failed ⇒ defer (never crash the loop).
- Flow in the locked branch: call `HANDOFF_UNLOCK_CMD`; re-probe `screen_is_locked`;
  if unlocked → proceed to the existing GUI Step 1/2/3; else → `defer_uri`.
- **Re-lock**: after the GUI submit (or after the whole spawn loop), if we
  unlocked this run, call `HANDOFF_RELOCK_CMD`. (待裁决 B1 below.)

**Dependency hygiene:** handoff-fanout does NOT import pyobjc; it shells out to
MP's venv python. The MP venv path is configured in `HANDOFF_UNLOCK_CMD` (no
hard cross-repo import). Single source of truth = MP's `idle.py`.

## 5. Workstream C — uninstall autoclose (surgical)

**Remove** (autoclose-only, verified):
- `extension/` handoff-helper — verified **only** does autoclose (description,
  `contributes:{}`, sole URI path `/autoclose`). `code --uninstall-extension
  dharmaxis.handoff-helper` + drop the extension build/install block from
  `install.sh` (keep an uninstall convenience). Decide whether to delete the
  `extension/` dir from the repo (待裁决 C1).
- `auto-continue.sh`: `autoclose_enabled_for_project()`, `try_autoclose()`,
  `KNOWN_SCHEMA_VERSIONS`, the `for PROJ_DIR … try_autoclose` loop,
  `HANDOFF_AUTOCLOSE_ENABLED`, the `AUTOCLOSED` counter (keep `OVERDUE_MARKED`).
- `tests/test_handoff_autoclose.py`: **split** — drop the autoclose (A-01..A-12)
  cases, KEEP the overdue-scanner (§7.9) cases (move them to a
  `test_overdue_scanner.py` if cleaner).

**KEEP — load-bearing, NOT autoclose's (red-line: do not touch):**
- `dump.py _write_old_ready` + `OLD_READY_SCHEMA_VERSION`. `old_ready` carries
  `codex_audit_hash`/`codex_audit_mode`/`next_session_forced_task` (Phase C/D
  audit gate) + `retro_evidence_hash` (v5.4 retro mandate), all read by the **§0
  new-session predecessor audit** (`templates.py` §0). Removing it breaks the
  audit + retro mandate.
- `auto-continue.sh` helpers the **overdue scanner ACTUALLY needs** (verified by
  R1): `now_iso_utc`, `json_get`, `iso_now_past_deadline`, `follow_up_satisfied`,
  `scan_overdue_kind`, `scan_overdue_overrides`. KEEP these.
- ⚠️ **R1 P2-9 correction**: `mtime_sec`, `sha256_file`, `release_lock`,
  `clean_stale_lock` are **autoclose-ONLY** (not shared) — they may be removed
  with autoclose. (My `defer_uri` uses its own self-contained `_mtime_epoch`, not
  `mtime_sec`, so removing `mtime_sec` is safe.) Confirm with a grep that no
  overdue/other caller remains before deleting.
- The **overdue scanner** (`scan_overdue_kind`, `scan_overdue_overrides`,
  `follow_up_satisfied`, `iso_now_past_deadline`) — separate feature (retro/codex
  bypass debt). KEEP.

**Verification that nothing else broke:** after the cut, grep the repo for
`autoclose`, `try_autoclose`, `AUTOCLOSE`, `handoff-helper`, `old_ready` and
confirm every remaining hit is either (a) the kept `old_ready` producers/readers
or (b) historical doc/changelog text — no live dangling caller.

---

## 6. 待裁决 (owner decisions embedded for review)

- **B1 — re-lock after spawn?** Recommend **yes** (re-lock after each submit,
  matches MP, screen returns to locked while the visible session runs; owner
  unlocks to review). Alternative: leave unlocked overnight (less secure).
- **B2 — security acceptance.** Auto-unlock means the screen briefly unlocks while
  you're away to run a visible bypassPermissions ERP session. Same physical-risk
  class as MP's WeChat sender (already accepted) + the existing 5/14 autonomous
  commit law. Owner to consciously accept.
- **C1 — delete `extension/` from the repo** vs keep it dormant (uninstalled from
  VS Code, dropped from installer, but source retained). Recommend **keep source,
  drop install** (smaller diff, reversible) — or delete if you want it gone.
- **OUT OF SCOPE (note, not reopening):** GUI auto-continue already auto-commits to
  `main` unattended (5/14 law). This plan does not change that; it only makes the
  relay survive a locked screen *visibly*. Raise separately if you want a
  branch/PR-gated review model.

---

## 7. Test plan

- Lock probe parsing (kept): ioreg key-absent→unlocked, `=Yes`→locked, ioreg
  fail→unknown (existing 3 tests).
- Unlock routing (new, `HANDOFF_LOCK_CHECK_CMD` + `HANDOFF_UNLOCK_CMD` stubs):
  locked+unlock-success → GUI path taken (open invoked); locked+unlock-fail →
  defer marker + no GUI; unknown → defer; unlocked → GUI directly.
- Re-lock invoked only when we unlocked this run (stub `HANDOFF_RELOCK_CMD`).
- Overdue scanner cases — unchanged green (proves autoclose removal didn't touch them).
- Accessibility tests — unchanged green (still pin lock=unlocked).
- MP `unlock_cli` — unit test arg parsing + exit codes with `is_screen_locked`
  monkeypatched (no real screen lock in CI).
- Full suite green; ruff 0.15.5 clean.

## 8. Audit + rollout

- **codex R1** (this plan) → fix gaps → **R2** (implementation) → for financial/
  state nothing changes (no ERP code), so R3/R4 light.
- DEFAULT OFF posture for the unlock path too: gated by a per-project
  `unlock.enabled` sentinel (mirrors the old opt-in), so the relay only
  auto-unlocks where the owner enabled it. Until enabled: locked → defer (today's
  safe pause). Owner enables for ERP after reviewing.
- Memory: update `lesson-handoff-fanout-headless-fallback-design` (headless
  superseded by unlock-pivot) + new lesson for the unlock capability.

---

## 9. codex R1 audit (2026-05-31) — findings + dispositions

codex confirmed the big boundaries (extension is autoclose-only; `old_ready`,
overdue scanner, §0 audit, retro/Phase-C/D gates must stay). All findings
folded into the plan below; implementation MUST satisfy these.

**P0**
- **P0-1 unlock opt-in not wired.** Add `unlock_enabled_for_project()` — **per-project
  `unlock.enabled` only, NO global default-on**. locked + not-opted-in ⇒
  `defer_uri reason=unlock-not-enabled`. Tests: default-off / per-project-on.
- **P0-2 cross-process unlock mutex.** A 2nd launcher tick could inject the password
  into an already-unlocked desktop / wrong window. Add a global
  `$HANDOFF_ROOT/.unlock.lock` (mkdir-atomic, stale TTL); hold it across
  unlock→claim→submit; re-probe `screen_is_locked` after acquiring. MP CLI also
  re-checks "still locked" at the last moment before injecting. Concurrency test.
- **P0-3 wrong-password retry storm.** A bad/expired Keychain password would be
  retried every 60s launchd tick → macOS account lockout. Add an unlock-failure
  **cooldown marker** (`first_epoch/last_epoch/count/next_retry_epoch`); after N
  consecutive failures stop auto-unlock → manual-unlock-only defer until the
  marker is cleared / Keychain updated.

**P1**
- **P1-4 stale LOCK_STATE + GUI guards.** LOCK_STATE is computed once at start and
  the VS Code / `code` guards only run when initial state = unlocked. After an
  unlock the GUI prerequisites were skipped. Fix: after unlock, **re-probe** and
  run the GUI prerequisites (VS Code running + `code` CLI) **before** claiming the
  `.uri`; fail ⇒ defer (don't claim). Re-evaluate lock state per task (or process
  one unlocked task per run).
- **P1-5 unlock/relock timeout + verify + relock-failure.** Add
  `HANDOFF_UNLOCK_TIMEOUT` / `HANDOFF_RELOCK_TIMEOUT` (macOS has no
  `/usr/bin/timeout` → bash bg+poll+kill or a Python wrapper). After re-lock,
  verify `screen_is_locked`; on failure write a HIGH-priority marker + notify and
  stop further spawns (never leave the Mac unlocked unattended silently).
- **P1-6 caffeinate + re-probe before Enter.** Hold a short `caffeinate -d -i`
  across unlock→`code -r`→`open URI`→Enter; **re-probe `screen_is_locked`
  immediately before the osascript Enter** — if re-locked, restore/defer (else the
  old "tab opened, never submitted" stall returns).
- **P1-7 headless launchd job teardown.** Removing source ≠ unloading the loaded
  job. Migration: `launchctl bootout gui/$UID/com.dharmaxis.handoff-headless` (and
  unload), delete the user LaunchAgents plist, clean `headless.enabled` /
  `headless-req/*.req` / `headless/*.pid`, verify
  `launchctl print gui/$UID/com.dharmaxis.handoff-headless` is gone.
- **P1-8 test split keeps old_ready + Phase C.** `test_handoff_autoclose.py` mixes
  autoclose (A), overdue (V), AND the **D3 old_ready writer** regression; Phase C
  also depends on old_ready fields. Split into `test_overdue_scanner.py` +
  `test_old_ready.py`; delete only the A series; KEEP D3 + Phase C
  old_ready/forced-follow-up tests.

**P2**
- **P2-9 keep/cut helper list corrected** — done inline in §5: `mtime_sec`,
  `sha256_file`, `release_lock`, `clean_stale_lock` are autoclose-only (removable);
  overdue needs only `now_iso_utc/json_get/iso_now_past_deadline/follow_up_satisfied/scan_overdue_*`.
- **P2-10 purge live autoclose steering.** Remove the installer's default extension
  install block + help text (`install.sh`), the `autoclose.enabled` / helper-URI
  note in `install/examples/config.json`, and reword the dump warning + docstrings
  so `old_ready` is described as **§0/Phase C/D audit metadata**, not an autoclose
  artifact. Keep `code --uninstall-extension dharmaxis.handoff-helper` as migration.

**New 待裁决 from R1:**
- **B3 — unlock failure threshold N** before switching to manual-only (recommend
  N=2: a single fat-finger/transient retries once, then stops to protect the
  account). Owner to confirm N.
