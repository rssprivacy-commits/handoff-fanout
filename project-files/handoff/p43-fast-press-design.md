# sw-coord-p43 — singlepane paste→Enter exposure-window reduction (design brief for dual-brain R1)

**System:** `handoff-fanout` is the dispatch/hand-off/supervision hub. A launchd watchdog
(`~/.local/bin/auto-continue.sh`, a DEPLOYED COPY of `install/auto-continue.sh`) spawns a new VS
Code window for a queued task, opens a `vscode://` URI that PASTES the prompt into the Claude Code
input (Anthropic's URI handler only pastes, never submits), then synthesizes an Enter keystroke to
auto-submit. For the **singlepane** path (coordinator + most worker spawns) the submit machinery is
`singlepane_submit_with_retry` → `singlepane_first_press_gated` (first press) / `singlepane_retry_gate(_settled)`
(retries) + `singlepane_probe_confirm` (verify a NEW `*.jsonl` carrying `🆔<task>` appeared).

**Owner directive (the task):** owner observed each spawn "pastes the instruction then idle-waits
15–27s before Enter". Owner's strategic judgment: *in a multi-window fleet the wait itself is the
danger* — during the wait the desktop gets stolen and Enter lands wrong. Owner wants the philosophy
flipped from "wait until fully certain, then press" → "press at minimal-safe, verify after, retry
fast", cutting the paste→Enter exposure window from ~27s to ~2–3s. Owner's named items:
- **该砍 (cut):** ① `screen-is-locked` probe (post-paste, 2–5s) → move pre-paste / once per tick;
  ② `settle-sleep` (~1.6s fixed pad) → trim; ③ the "~16s AX value-ready poll" (`SP-FIRST-PRESS-START`
  → `AUTO-SUBMIT`) = "the failure amplifier, replace it".
- **必须留 (keep):** the front-window atomic read (front app == Code ∧ window title ⊇ token) — the
  one-shot addressing safety; deleting it = Enter lands on owner's terminal (the wh-coord-10 disaster).
- **替「等粘贴落」:** short fixed delay then press + verify via the existing "new 🆔 jsonl appeared"
  signal + fast retry. Owner flagged the **p34 double-submit red line**: any multi-press/retry MUST,
  before each subsequent Enter, confirm the prior one didn't already submit (input cleared OR jsonl grew).

**Process:** this is fleet-critical, deploy-trapped code (p35 once shipped a fleet-wide regression by
rushing it). Mandated path: design → dual-brain (codex+gemini, zero-trust) → deploy-trap → canary.

---

## 1. Evidence (all freshly measured this session — not inherited claims)

Source: live `~/.claude-handoff/auto-continue.log` (PERF lines) + direct timing + live code read.

### 1a. The deterministic exposure-window costs (EVERY spawn)
| stage | cost | note |
|---|---|---|
| `settle-sleep` (singlepane branch = `sleep 1.5`, line 1976) | **~1.6s** | fixed pad, every spawn |
| `screen-is-locked` (Quartz `--status` subprocess via `run_with_timeout`, line 1986) | **1.1–4.9s** (median ~2.4s) | post-paste re-check; `HANDOFF_UNLOCK_CMD` IS configured (`mp-unlock --unlock`) so it takes the slow Quartz path, not ioreg |

These two are **~4–5s of dead wait on every spawn**, independent of render speed.

### 1b. The render-dependent cost (slow-opening windows only)
Worst case observed — `mp-probe-alert-0620` (the 27s case), full timeline:
```
17:50:56 code-n 6511ms          ← fresh VS Code window OPEN (BEFORE paste; not in the paste→Enter window)
17:50:57 open-uri 534ms         ← PASTE
17:50:58 settle-sleep 1670ms
17:51:02 screen-is-locked 3440ms
17:51:02 SP-SUBMIT-START (base_jsonls=347)
17:51:08 SP-FIRST-PRESS-START   ← 6s gap (see 1c)
17:51:24 AUTO-SUBMIT confirmed  ← 16s gap = the value⊇marker readiness wait until the cold webview rendered + ~6s confirm poll
```
A fast window (`xunyin-coord-51`, 71 jsonls): paste→confirmed = **9s** total, exposure ~5s. So the
value-readiness wait is short for fast windows and only balloons for slow-opening ones (mp's `code-n`
itself took 6.5s → a slow machine/window moment → slow webview render).

### 1c. A theory I had and KILLED by measurement (anti-narrow discipline)
I suspected `singlepane_probe_confirm` (O(N) per-file `grep -Fxq` over the base set) was the 6s
gap. **Measured directly on mp's 348-file dir: the set-diff is 0.7s**, and a single-pass
`grep -vxF -f` replacement (verified identical new-file set) is 0.1s — saving only ~0.6s. So the
probe is **NOT** a bottleneck and is **out of scope** (not worth touching the p34/p41-frozen
contract for 0.6s). The 6s gap was most likely transient load (concurrent mp-coord spawn writing).

### 1d. The decisive finding: post-p41 the singlepane submit is HEALTHY
p41 (2026-06-20 ~08:40) changed the AX value-gate marker from `🆔$task` to plain ASCII `$task`
(the emoji didn't survive the webview AX read). Verifying that fix on the live log **after the deploy
point (line 183564)**:
- `gate=wronginput` count = **0** (every pre-deploy `input-not-ready` was `gate=wronginput`; all such
  cases are pre-p41, latest 06-20 01:07, before the 08:40 deploy).
- **10/10 singlepane spawns since deploy = `attempt=1 outcome=confirmed`.** Zero retries, zero failures.

So owner's premise — "the AX value-ready poll is the failure amplifier" — was true **pre-p41**
(wronginput: input had text but the emoji-marker AX read failed → false wait). **Post-p41 it is
already fixed**: the value⊇marker gate now matches reliably and presses on attempt 1. The remaining
slow case (mp) waits on the *silent* not-ready states (`noelem`/`notinput`/`emptyinput`) — i.e. the
webview input is **genuinely not rendered/focused yet** during a cold heavy render.

### 1e. The open question that decides owner's item ③ ("A")
For the slow-render case, does a **blind/fast Enter** (front+title only, skip the value⊇marker check)
submit EARLIER, or just get swallowed?
- If the input is visually ready (would accept Enter) but the AX value-read *lags* → fast press wins.
- If the webview input is genuinely not rendered/focused (AX correctly reports not-ready) → a fast
  Enter is swallowed (or lands on the wrong element); the real submit still waits for the render, and
  dropping the value-gate only **removes a safety check** (the value⊇marker gate is the double-submit
  + wrong-window-text guard) for **no speed gain**.

The current first-press poll is SILENT on which state it waits in (`*) : ;;` at line 941), so the log
cannot yet answer this. **This is unproven and must be measured before removing the value-gate.**

---

## 2. Proposed change set (this round = C + D + diagnostic; A deferred — see §4)

All changes are **singlepane-only**; cold and warm paths are byte-unchanged (zero regression for them).

### Change D — trim the singlepane settle (owner item ②)
Current `install/auto-continue.sh:1973-1977`:
```bash
            if [ "$COLD_WINDOW" = "1" ]; then
                sleep "${HANDOFF_COLD_RENDER_SECS:-0.5}"
            else
                sleep 1.5
            fi
```
Proposed:
```bash
            if [ "$COLD_WINDOW" = "1" ]; then
                sleep "${HANDOFF_COLD_RENDER_SECS:-0.5}"
            elif [ "$SINGLEPANE_WINDOW" = "1" ]; then
                # sw-coord-p43: trim the fixed 1.5s singlepane pre-pad to a short tunable. The
                # readiness-gated first press already waits out the cold render (wall-clock poll +
                # value⊇marker gate), so a long fixed pad only adds dead time to paste→Enter. Floor
                # kept so the URI paste has landed before the first read.
                sleep "${HANDOFF_SP_SETTLE:-0.5}"
            else
                sleep 1.5
            fi
```
**Saves ~1.1s/spawn.** Risk: if 0.5s < paste-land time, the first read sees `emptyinput`/`notinput`
→ the existing readiness poll simply waits/retries (the value⊇marker gate never presses on empty) →
worst case one extra poll iteration, never a bad press.

### Change C — singlepane skips the post-paste lock RE-check (owner item ①)
Current `install/auto-continue.sh:1986-1987`:
```bash
            _perf_call "$TASK" "screen-is-locked" screen_is_locked; _SRC=$?
            if [ "$_SRC" != "1" ]; then
```
Proposed:
```bash
            # sw-coord-p43: SINGLEPANE skips the post-paste lock RE-check (the 1.1–4.9s Quartz
            # --status subprocess) — it sits ON the paste→Enter hot path. Three layers already cover
            # the keystroke-into-a-locked-screen hazard for SP:
            #   (1) the PRE-paste lock check (line ~1595) already gated unlock/defer for THIS tick;
            #   (2) caffeinate -d -i is HELD across the unlock→submit window (re-lock prevented);
            #   (3) the Enter is ATOMICALLY guarded by the front-window read inside
            #       singlepane_retry_gate — a locked screen makes "loginwindow" frontmost, so the
            #       gate returns "nofront" and NEVER presses (the same check owner mandated keeping).
            # So the explicit recheck is redundant cost for SP. COLD/WARM keep it unchanged.
            if [ "$SINGLEPANE_WINDOW" = "1" ]; then
                _SRC=1
            else
                _perf_call "$TASK" "screen-is-locked" screen_is_locked; _SRC=$?
            fi
            if [ "$_SRC" != "1" ]; then
```
**Saves ~2.4s/spawn (up to 4.9s).** Residual: if the screen re-locks in the now-~1s window between
the pre-paste check and Enter (very unlikely with caffeinate held + active osascript driving), layer
(3) withholds the press (front≠Code → "nofront" → honest failed ack); the explicit defer/relock
branch wouldn't fire but `_post_iter_cleanup` still runs. **Keystroke safety is preserved by the
front=Code gate, NOT lost.**

### Change "diagnostic" — log the silent first-press not-ready states (LOG-ONLY, behavior-preserving)
Current `singlepane_first_press_gated` `install/auto-continue.sh:929-944`:
```bash
        case "$out" in
            sent) break ;;
            nofront|nowin|mismatch)
                log "SP-FIRST-PRESS: gate=$out — nonce-first raise + keep polling"
                raise_task_window "$token" "$task" >/dev/null
                ;;
            *) : ;;   # noelem|notinput|emptyinput|wronginput → wait out the cold render
        esac
        sleep "$settle"
```
Proposed (add `_dbg_last=""` to the function's `local` list at line 904):
```bash
        case "$out" in
            sent) break ;;
            nofront|nowin|mismatch)
                log "SP-FIRST-PRESS: gate=$out — nonce-first raise + keep polling"
                raise_task_window "$token" "$task" >/dev/null
                ;;
            *)
                # sw-coord-p43 diagnostic (LOG-ONLY / behavior-preserving): surface the SILENT
                # not-ready states the first-press poll waits in, logged on state-change to avoid
                # spam. This is the evidence that decides whether owner's "fast press on front+title"
                # (drop the value⊇marker wait) would submit earlier (state would be a ready input) or
                # just be swallowed (noelem/notinput/emptyinput = input genuinely not rendered). No
                # behavior change — keystroke red line untouched (still only a positive marker read
                # presses, inside the gate's own atomic osascript).
                [ "$out" != "$_dbg_last" ] && log "SP-FIRST-PRESS: gate=$out (input not ready — waiting out render)"
                _dbg_last="$out"
                ;;
        esac
        sleep "$settle"
```
**Zero behavior change** — only adds a log line. Lets the next slow spawn reveal the wait-state
distribution → settles §1e empirically.

---

## 3. Combined effect & safety summary
- C+D remove **~3.5–6s of deterministic dead wait from every singlepane spawn's paste→Enter window**.
- Fast windows: ~5s exposure → ~1–2s (meets owner's goal).
- Slow windows (mp-class): ~21s → ~16s; the residual ~10s is the **webview render wait** (item ③/A),
  which C+D do not touch — that needs the §4 decision, which the diagnostic informs.
- All three changes are singlepane-scoped; cold/warm byte-unchanged.
- No safety property is weakened: the front=Code ∧ title⊇token atomic gate (owner's "必须留") is
  untouched; the value⊇marker double-submit/wrong-window guard is untouched this round; the keystroke
  red line (press only on a positive marker read, inside one atomic osascript) is untouched.

## 4. Why "A" (drop the value⊇marker wait / fast press) is DEFERRED, not done now
This is the honest, evidence-first position (CLAUDE.md: solve-first / anti-conservative-bias requires
an *earned, quantified* "not now", and challenge-flawed-premise requires surfacing contradicting data):
- Owner's premise ("the value-ready poll is the failure amplifier") was true **pre-p41**; **post-p41
  it is already fixed** (wronginput=0, 10/10 attempt-1). So the value-gate is NOT currently amplifying
  failures.
- The value⊇marker gate is a **real safety feature** (double-submit guard: a submitted prompt empties
  the input, so a markerless/empty read must NOT press; + wrong-window-text guard). Removing it trades
  safety for speed.
- The speed benefit is **unproven**: it only materializes if a blind Enter submits *before* the AX
  read goes ready. If the slow case is "input genuinely not rendered" (the likely post-p41 reality),
  a fast press is swallowed → zero gain, pure safety loss.
- Therefore: ship the diagnostic, collect the wait-state distribution on real slow spawns, and decide
  A on data + owner sign-off (removing a deliberate safety gate is an owner call). This is NOT
  conservative foot-dragging — C+D already deliver owner's reliable win now; A is gated on the missing
  evidence the diagnostic produces.

---

## 5. Questions for the auditors (codex + gemini, independent, zero-trust — read the live code at
`/Users/chenmingzhong/Projects/handoff-fanout/install/auto-continue.sh`)
1. **C correctness/safety:** Is it TRUE that on a locked macOS screen the frontmost process is
   `loginwindow` (not `Code`), so `singlepane_retry_gate`'s `fa is not "Code" → return "nofront"`
   reliably withholds the press? Any path where C's `_SRC=1` shortcut lets an Enter land on a locked
   screen or a wrong window? Any interaction with the pre-paste lock check (line ~1595), caffeinate,
   `_post_iter_cleanup`/relock, or `RELOCK_FAILED` that C breaks?
2. **D correctness:** Any case where trimming the singlepane settle to 0.5s causes a BAD press
   (vs merely an extra harmless poll iteration)? Does the readiness gate truly catch a not-yet-landed
   paste?
3. **Diagnostic:** Confirm it is strictly log-only (no behavior/exit-code change; `$(...)` stdout of
   `singlepane_first_press_gated` is still only `sent|<state>`; the added `log` writes to the log file,
   not stdout). `_dbg_last` scoping correct?
4. **Scope/regression:** Confirm cold and warm paths are byte-unchanged. Any singlepane edge case
   (legacy no-nonce, focus-contended, accessibility-missing, screen genuinely locked) that regresses?
5. **The A deferral:** Is deferring A (keep the value-gate, ship the diagnostic) the right call given
   the post-p41 evidence, or is there a way to get owner's exposure-window win for SLOW windows that I
   am missing? Challenge the reasoning.

Reply with findings classified P0/P1/P2 and end with exactly one line: `Verdict: GREEN` (ship C+D+diagnostic
as designed) or `Verdict: RED` (a P0/P1 must be fixed first).
