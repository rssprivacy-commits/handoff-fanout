# sw-coord-p43 — implementation audit (R2) of the singlepane paste→Enter exposure-window fix

This is the **implementation** audit of the change whose **design** you (codex+gemini) already
passed GREEN/GREEN in R1 (`audits/p43-fast-press-design-r1.md`). R1 raised 3 P2 refinements which
were folded in; verify they were folded correctly and the implemented diff is sound. Zero-trust:
read the live working-tree file `/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh`
and the test `/Users/chenmingzhong/Projects/handoff-fanout/tests/test_singlepane_submit_retry.py`.

## Recap of intent (singlepane-only; cold & warm paths byte-unchanged)
Reduce the singlepane paste→Enter exposure window. Three changes:
- **D** — trim the fixed 1.5s singlepane settle to a sanitized `HANDOFF_SP_SETTLE` (default 0.5s).
- **C** — singlepane SKIPS the post-paste `screen_is_locked` Quartz re-check (1.1–4.9s on the hot
  path); keystroke-into-locked-screen safety is held by the existing front=Code atomic gate inside
  `singlepane_retry_gate` (a locked screen makes a non-Code process frontmost → "nofront" → no press).
- **diagnostic** — LOG-ONLY: surface the silent first-press not-ready states (`noelem`/`notinput`/
  `emptyinput`/`wronginput`) so the next slow spawn reveals whether a future "fast press" would help.
- **A is NOT in this change** (dropping the value⊇marker gate) — deferred pending the diagnostic data
  (both of you endorsed this in R1).

## R1 P2 refinements folded in (verify each)
1. (codex P2-1) C's comment narrowed: caffeinate is NOT universal — it's started only in the
   auto-unlock branch (line ~1622); the front=Code gate is the universal lock net. The comment now
   says so.
2. (codex P2-2) C's rare re-lock retry-downgrade is surfaced explicitly in the comment (NOT silent):
   on a re-lock inside the ~1s window, SP yields an honest `failed` ack without the .uri-restore+defer
   the COLD/WARM branch does — no bad-Enter path, owner re-presses manually.
3. (codex P2-3) D's `HANDOFF_SP_SETTLE` is now sanitized (`case ... *[!0-9.]*|*.*.*) ...=0.5`) instead
   of the comment falsely claiming a floor.
4. (gemini P2) Diagnostic confirmed log-only: `log()` (line 91) writes only to `$LOG`, never stdout,
   so the `$(...)` capture of `singlepane_first_press_gated` stays `sent|<state>`.

## Regression guard added (verify it actually guards)
`test_singlepane_skips_post_paste_lock_recheck` — a successful singlepane spawn must (a) still
submit, (b) produce NO `PERF[...]: screen-is-locked` line, (c) invoke the lock probe exactly once.
**Proven disable-fix→FAIL**: reverting change C made the test FAIL (line 449), restored byte-identical.

## The implemented diff
```diff
@@ install/auto-continue.sh : singlepane_first_press_gated() local list @@
-    local token="$1" marker="$2" task="$3" ws="$4" base="$5" start deadline ready_secs out="" settle
+    local token="$1" marker="$2" task="$3" ws="$4" base="$5" start deadline ready_secs out="" settle _dbg_last=""

@@ install/auto-continue.sh : singlepane_first_press_gated() not-ready case arm @@
-            *) : ;;   # noelem|notinput|emptyinput|wronginput → wait out the cold render
+            *)
+                # sw-coord-p43 diagnostic (LOG-ONLY / behavior-preserving — log() writes only to
+                # $LOG, never stdout [verified line 91], so the $(...) capture stays sent|<state>):
+                # surface the SILENT not-ready states the first-press poll waits in, logged on
+                # state-change to avoid spam. ... Keystroke red line untouched (only a positive marker
+                # read presses). REMOVE once the wait-state distribution is collected.
+                [ "$out" != "$_dbg_last" ] && log "SP-FIRST-PRESS: gate=$out (input not ready — waiting out render)"
+                _dbg_last="$out"
+                ;;   # noelem|notinput|emptyinput|wronginput → wait out the cold render

@@ install/auto-continue.sh : dispatch settle (D) @@
             if [ "$COLD_WINDOW" = "1" ]; then
                 sleep "${HANDOFF_COLD_RENDER_SECS:-0.5}"   # 主人立法 2026-06-06: 粘完 0.5s 直接 Enter
+            elif [ "$SINGLEPANE_WINDOW" = "1" ]; then
+                # sw-coord-p43: trim the fixed 1.5s singlepane pre-pad to a short tunable. ... never a
+                # bad Enter (R1 dual-brain codex+gemini GREEN).
+                _sp_settle="${HANDOFF_SP_SETTLE:-0.5}"; case "$_sp_settle" in ''|*[!0-9.]*|*.*.*) _sp_settle=0.5 ;; esac
+                sleep "$_sp_settle"
             else
                 sleep 1.5
             fi

@@ install/auto-continue.sh : dispatch lock recheck (C) @@
-            _perf_call "$TASK" "screen-is-locked" screen_is_locked; _SRC=$?
+            # sw-coord-p43: SINGLEPANE skips the post-paste lock RE-check (1.1–4.9s Quartz --status).
+            # Keystroke-into-locked-screen held by the front=Code atomic read in singlepane_retry_gate
+            # (locked → loginwindow/ScreenSaverEngine/SecurityAgent frontmost ≠ Code → "nofront" → no
+            # press). caffeinate held ONLY when we auto-unlocked (line ~1622) → front=Code, not
+            # caffeinate, is the universal lock net. TRADE-OFF (R1 codex P2, surfaced): a rare re-lock
+            # in the ~1s window → honest failed ack WITHOUT .uri-restore+defer (no bad-Enter; manual
+            # re-press). COLD/WARM keep the recheck unchanged.
+            if [ "$SINGLEPANE_WINDOW" = "1" ]; then
+                _SRC=1
+            else
+                _perf_call "$TASK" "screen-is-locked" screen_is_locked; _SRC=$?
+            fi
             if [ "$_SRC" != "1" ]; then
```
(Abbreviated for readability — the FULL verbatim source is in the live working-tree files; read them
directly per zero-trust: `install/auto-continue.sh` lines ~903/~938/~1983/~2003 and
`tests/test_singlepane_submit_retry.py::test_singlepane_skips_post_paste_lock_recheck`.)

## Verify
1. **C — correctness & safety:** Is the `if [ "$SINGLEPANE_WINDOW" = "1" ]; then _SRC=1; else
   _perf_call ... screen_is_locked; _SRC=$?; fi` shortcut sound? Does the downstream `if [ "$_SRC"
   != "1" ]` / `elif ! accessibility_trusted` / `elif [ COLD ]||[ SP ]||is_frontmost_code` chain
   still behave correctly for SP (accessibility still checked; submit machinery still reached)? Any
   path where `_SRC=1` lets an Enter land on a locked screen or wrong window? Confirm cold/warm are
   byte-unchanged.
2. **D — correctness:** Is the `_sp_settle` sanitization correct (junk/multi-dot → 0.5)? `_sp_settle`
   is a bare (non-local) var in the main loop body under `set -u` — is that safe here (no leak/clash
   with another var of that name)? Any case where a trimmed settle causes a BAD press rather than a
   harmless extra poll iteration?
3. **diagnostic — strictly log-only:** Confirm the new `*)` arm cannot change the function's return
   value / exit code / `$(...)`-captured stdout. `_dbg_last` scoping (added to the `local` list)
   correct? Does the state-change-only logging risk missing a transition or mis-attributing one?
4. **regression guard:** Does the test truly fail if change C is reverted (i.e. is the
   `"screen-is-locked" not in log` + `lock_calls == 1` pair load-bearing)? Any flakiness (timing,
   shadow-probe contamination of `lock_sink`)?
5. **scope/blast-radius:** Anything outside singlepane affected? Any interaction with the focus-drift
   discriminator, `_post_iter_cleanup`/relock, `RELOCK_FAILED`, or the return-leg that this breaks?

Classify findings P0/P1/P2 and end with exactly one line: `Verdict: GREEN` (ship as implemented) or
`Verdict: RED` (a P0/P1 must be fixed first).
