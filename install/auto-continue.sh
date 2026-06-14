#!/bin/bash
# V3.10 子批 4 / 全项目自动接续 launcher (跨项目 Queue 模式)
#
# 触发: launchd com.dharmaxis.auto-continue
#   - WatchPaths: ~/.claude-handoff/ 目录递归监听
#   - StartInterval: 60 秒 fallback
#   - ThrottleInterval: 1 秒
#
# 责任: 遍历所有项目 queue 目录 → 每个 .uri 文件 → 用 code -r 激活该项目窗口 → open URI
#
# 全局 guards (任一命中所有项目跳过):
# - ~/.claude-handoff/STOP_AUTO 存在 → 退
# - ~/.claude-handoff/done 存在 → 退
# - pgrep "Visual Studio Code.app" 未运行 → 退
#
# Per-project guards (在项目子目录):
# - ~/.claude-handoff/<project>/STOP_AUTO → 跳过本项目
# - ~/.claude-handoff/<project>/done → 跳过本项目
#
# Per-task guards (在 queue/ 内):
# - <task>.done → 跳过本 task
# - <task>.BLOCKED.md → 跳过本 task (等主人介入)

set -u

# v5.4 / Phase 4d D-4 — every external dependency is overridable via env so
# tests can stub the side effects without touching the real VS Code, open(1)
# or osascript binaries on the developer's machine.
HANDOFF_ROOT="${HANDOFF_ROOT:-$HOME/.claude-handoff}"
LOG="$HANDOFF_ROOT/auto-continue.log"
HANDOFF_OPEN_CMD="${HANDOFF_OPEN_CMD:-/usr/bin/open}"
HANDOFF_OSASCRIPT_CMD="${HANDOFF_OSASCRIPT_CMD:-/usr/bin/osascript}"
HANDOFF_SHA256_CMD="${HANDOFF_SHA256_CMD:-/usr/bin/shasum}"
# tests set HANDOFF_SKIP_SPAWN=1 to exercise the overdue-scanner segment
# without depending on a live VS Code instance.
HANDOFF_SKIP_SPAWN="${HANDOFF_SKIP_SPAWN:-0}"
# v4 path-D autoclose (role-gated supervisor succession; spawn-window-unify Task 4.1).
# Default OFF — opt in via this env or an autoclose.enabled sentinel (global/per-project,
# 改进 #6). Safe to flip ON globally today: only a `supervisor_succession` spawn ever closes
# a window, and every current dump writes role="worker" → a no-op until a succession producer
# lands (dormant-but-ready). HANDOFF_SPAWN_LOCK_TTL mirrors handoff_fanout.spawn_lock.ttl=120.
HANDOFF_AUTOCLOSE_ENABLED="${HANDOFF_AUTOCLOSE_ENABLED:-0}"
HANDOFF_SPAWN_LOCK_TTL="${HANDOFF_SPAWN_LOCK_TTL:-120}"
# tests set HANDOFF_VSCODE_CHECK=0 to skip the `pgrep "Visual Studio Code"`
# global guard (no-op in CI / headless contexts).
HANDOFF_VSCODE_CHECK="${HANDOFF_VSCODE_CHECK:-1}"
# python3 is a hard dependency of this system (the dump/precheck CLIs are a
# Python package); the overdue scanner uses it for timezone-correct ISO-8601
# comparison. Overridable so tests can point at a specific interpreter.
# Prefer the ABSOLUTE /usr/bin/python3 over a bare PATH `python3` (2026-06-05): an
# interactive/dev shell may put a wrapper-shim first on PATH (e.g. the tob-modern-python
# uv-shim that intercepts bare `python3` and exits non-zero). The overdue scanner is a
# SAFETY mechanism (retro/audit mandate-debt tracking) whose iso_now_past_deadline
# fail-safes a parse error to "not overdue" — so a shimmed `python3` would SILENTLY
# no-op the gate (debt never flagged). The scanner only needs stdlib (datetime/json),
# satisfied by the system python3. Same hardening as the dump-handoff /usr/bin/python3
# absolute shebang. An explicit HANDOFF_PYTHON_CMD always wins (tests / power users).
if [ -z "${HANDOFF_PYTHON_CMD:-}" ]; then
    if [ -x /usr/bin/python3 ]; then HANDOFF_PYTHON_CMD=/usr/bin/python3; else HANDOFF_PYTHON_CMD=python3; fi
fi

# ── unlock-pivot (lock-screen → auto-unlock → visible GUI; design §4 / codex R1) ──
# The GUI submit (code -r / open / osascript Enter) needs an UNLOCKED screen —
# synthetic keystrokes are forbidden against the macOS lock screen. When locked +
# the project opted in, auto-unlock first (MindPersist's CGEvent password
# injection CLI), then run the visible GUI path so the owner can still audit the
# tab. Locked + not-opted-in / unlock-failed / unknown ⇒ defer (keep .uri, notify,
# resume on unlock) — never a silent dead-stall, never a blind-box. Default OFF.
HANDOFF_LOCK_CHECK_CMD="${HANDOFF_LOCK_CHECK_CMD:-}"    # tests stub: prints locked|unlocked|*
HANDOFF_IOREG_CMD="${HANDOFF_IOREG_CMD:-/usr/sbin/ioreg}"
HANDOFF_UNLOCK_CMD="${HANDOFF_UNLOCK_CMD:-}"            # e.g. "<mp>/.venv/bin/python -m src.agent.unlock_cli --unlock"
HANDOFF_RELOCK_CMD="${HANDOFF_RELOCK_CMD:-}"            # e.g. "<mp>/.venv/bin/python -m src.agent.unlock_cli --lock"
HANDOFF_UNLOCK_TIMEOUT="${HANDOFF_UNLOCK_TIMEOUT:-90}"  # wall-clock cap for the unlock CLI (P1-5)
HANDOFF_RELOCK_TIMEOUT="${HANDOFF_RELOCK_TIMEOUT:-20}"
HANDOFF_LOCKCHECK_TIMEOUT="${HANDOFF_LOCKCHECK_TIMEOUT:-15}"  # cap for the Quartz --status lock probe (P0 lock-probe fix)
HANDOFF_CAFFEINATE_CMD="${HANDOFF_CAFFEINATE_CMD:-caffeinate -d -i}"  # held across unlock→submit (P1-6); empty disables
HANDOFF_UNLOCK_FAIL_THRESHOLD="${HANDOFF_UNLOCK_FAIL_THRESHOLD:-2}"   # consecutive fails → manual-only (P0-3 / B3)
HANDOFF_UNLOCK_COOLDOWN="${HANDOFF_UNLOCK_COOLDOWN:-1800}"            # seconds to wait after threshold reached

CODE_BIN="${HANDOFF_CODE_BIN:-/usr/local/bin/code}"
[ ! -x "$CODE_BIN" ] && CODE_BIN="/opt/homebrew/bin/code"
# fallback: which code
[ ! -x "$CODE_BIN" ] && CODE_BIN=$(command -v code 2>/dev/null)

log() {
    mkdir -p "$HANDOFF_ROOT" 2>/dev/null
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
}

# ── per-segment spawn timing (2026-06-07 spawn-speedup task) ──────────────────────────────
# A single cold spawn can run tens of seconds because each osascript "System Events" query can
# BLOCK for seconds while a freshly-opened VS Code window renders (the AX tree of a mid-launch
# Code process is slow to enumerate), and the cost varies wildly run-to-run — so a single
# measurement misleads (observed: the SAME code, ~38s one spawn vs ~14s the next). These marks
# make every real spawn SELF-REPORT where the wall-clock went (one `PERF[...]` line per segment),
# so timing is measured from the real deployment path itself, never guessed. Cheap: one date(1)
# fork + one log line per segment; no osascript. `date +%s%N` is nanosecond-real on this box.
_PERF_LAST=0
# epoch milliseconds. ROBUST against a /bin/date without %N (R2 codex): %N IS supported on this box
# (macOS 26 → true ns), but if a date(1) ever returns the literal "…N" suffix, feeding it to `$(( ))`
# would error/spam stderr — and instrumentation must NEVER misbehave (it runs on every spawn, set -u is
# on). So validate: non-numeric → fall back to second precision ×1000.
_perf_ms() {
    local ns; ns=$(/bin/date +%s%N 2>/dev/null)
    case "$ns" in
        ''|*[!0-9]*) echo $(( $(/bin/date +%s) * 1000 )) ;;
        *) echo $(( ns / 1000000 )) ;;
    esac
}
_perf_reset() { _PERF_LAST=$(_perf_ms); }
_perf_mark() {  # _perf_mark <task> <label> — log ms since the previous mark/reset, advance the cursor
    local now; now=$(_perf_ms)
    log "PERF[$1]: $2 $((now - _PERF_LAST))ms"
    _PERF_LAST=$now
}
# Time a single command, log its PERF segment, and PRESERVE its exit code so it stays usable inside
# if/elif (the spawn flow uses screen_is_locked / accessibility_trusted / is_frontmost_code as guards —
# short-circuit semantics are kept: a wrapped guard only runs when its elif is reached, exactly as before).
_perf_call() {  # _perf_call <task> <label> <cmd> [args...]
    local task="$1" label="$2"; shift 2
    "$@"; local r=$?
    _perf_mark "$task" "$label"
    return $r
}

# Drift guard (甲 / 2026-06-05 owner ruling B+C — backstop to the post-commit auto-sync). The launchd
# copy ~/.local/bin/auto-continue.sh is a DEPLOYED COPY of the canonical SOURCE install/auto-continue.sh,
# normally kept current by the post-commit hook's `install.sh --sync-launcher`. If that auto-sync ever
# does NOT happen (hook uninstalled / sync failed / runtime hand-edited), the running copy ($0) diverges
# from the source and silently runs OLD logic. Compare the two LIVE and LOUDLY surface a mismatch:
#   - a prominent log line every run (durable nag) that names the exact remedy command, and
#   - a one-shot desktop notification, throttled per drift sha so editing this file doesn't spam.
# NEVER skips a spawn (owner 甲: a stale-but-running launcher beats a halted 接续 loop — a cold-submit
# blast radius is a manual Enter, not data). Fully non-fatal: a missing source / sha tool just skips it.
#
# Replaces the OLD guard, which compared $0 against the LAST-SYNCED sha file (.auto-continue.canonical.sha)
# — blind to "source moved ahead of runtime" (both stay equal until a sync), the exact bug owner hit. The
# sha file is still written by `--sync-launcher` and read by audit-mandate-preflight.sh, so it is kept.
HANDOFF_CANON_SRC="${HANDOFF_CANON_SRC:-$HOME/Projects/handoff-fanout/install/auto-continue.sh}"
if [ -f "$HANDOFF_CANON_SRC" ]; then
    _self_sha="$("$HANDOFF_SHA256_CMD" "$0" 2>/dev/null | awk '{print $1}')"
    _src_sha="$("$HANDOFF_SHA256_CMD" "$HANDOFF_CANON_SRC" 2>/dev/null | awk '{print $1}')"
    if [ -n "$_self_sha" ] && [ -n "$_src_sha" ] && [ "$_self_sha" != "$_src_sha" ]; then
        log "⚠⚠ DRIFT: running launcher ($_self_sha) != canonical source ($_src_sha @ $HANDOFF_CANON_SRC) — post-commit auto-sync did NOT deploy; 接续 continues on the current copy. Remedy: bash ~/Projects/handoff-fanout/install/install.sh --sync-launcher"
        _drift_marker="$HANDOFF_ROOT/.auto-continue.drift-notified.$_src_sha"
        if [ ! -f "$_drift_marker" ]; then
            "$HANDOFF_OSASCRIPT_CMD" -e 'display notification "运行副本落后于源码 — 跑 install.sh --sync-launcher" with title "⚠ auto-continue 漂移"' >/dev/null 2>&1 || true
            : > "$_drift_marker" 2>/dev/null || true
        fi
    fi
fi

# 全局 Guard 1: handoff root 存在
if [ ! -d "$HANDOFF_ROOT" ]; then
    exit 0
fi

# 全局 Guard 2: STOP_AUTO 紧急刹车
if [ -f "$HANDOFF_ROOT/STOP_AUTO" ]; then
    log "SKIP: global STOP_AUTO (全局暂停)"
    exit 0
fi

# 全局 Guard 3: done 永久停
if [ -f "$HANDOFF_ROOT/done" ]; then
    exit 0
fi

# 全局 Guard 4: VS Code 必须运行 (tests skip via HANDOFF_VSCODE_CHECK=0)
if [ "$HANDOFF_VSCODE_CHECK" = "1" ]; then
    if ! pgrep -f "Visual Studio Code.app" > /dev/null 2>&1; then
        log "SKIP: VS Code not running"
        exit 0
    fi
fi

# 全局 Guard 5 (full-sweep A3 / Gate0b P2): a PRIOR run failed to RE-LOCK the Mac
# after an auto-unlock and left a durable `.relock-failed` marker. That halt must
# persist ACROSS runs — the in-run `break 2` alone is not enough, because the next
# launchd tick finds the screen already unlocked, skips the unlock branch, and
# would happily resume spawning on an unattended unlocked Mac (red-line ②). Skip
# ALL spawns until the owner re-locks and clears the marker; the (read-only)
# overdue scanner further below still runs. Evaluated BEFORE the code-CLI guard so
# a missing `code` can't abort the run before that scanner (which needs no `code`).
# Documented in the runbook brakes table.
RELOCK_HALT=0
if [ -f "$HANDOFF_ROOT/.relock-failed" ]; then
    RELOCK_HALT=1
    log "HALT: .relock-failed present — skipping all spawns until re-locked + 'rm $HANDOFF_ROOT/.relock-failed'"
fi

# 全局 Guard 6: code CLI 必须可用 (workspace routing 核心). Skipped when only the
# overdue segment runs (HANDOFF_SKIP_SPAWN) or when spawns are halted (RELOCK_HALT)
# — neither touches `code -r`, so a missing `code` must not abort the overdue scan.
if [ "$HANDOFF_SKIP_SPAWN" != "1" ] && [ "$RELOCK_HALT" != "1" ]; then
    if [ -z "$CODE_BIN" ] || [ ! -x "$CODE_BIN" ]; then
        log "FATAL: code CLI not found (workspace routing unavailable)"
        exit 1
    fi
fi

SPAWNED=0
OVERDUE_MARKED=0
AUTOCLOSED=0
DEFERRED=0
shopt -s nullglob

# 2026-05-28 codex audit blind-spot #4 修复:
# 写 ack 文件给「spawn-new-session」skill 读, 让 AI 能验证 spawn 是否真发生
# (dump-handoff.py 写 ack/<task>.queued / 本脚本写 .spawned / .submitted / .failed)
write_ack() {
    local proj_dir="$1"; local task="$2"; local state="$3"; local detail="${4:-}"
    local ack_dir="$proj_dir/ack"
    mkdir -p "$ack_dir" 2>/dev/null
    printf '%s\n%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$detail" > "$ack_dir/$task.$state"
}

# 2026-05-28 codex audit blind-spot #4 修复:
# osascript Enter 前必须确认 frontmost app 是 Code, 否则按到错误窗口风险真实
# 返回 0 = frontmost 是 Code (可按 Enter), 非 0 = 别的 app (abort)
is_frontmost_code() {
    local front
    front=$("$HANDOFF_OSASCRIPT_CMD" -e 'tell application "System Events" to name of first application process whose frontmost is true' 2>/dev/null)
    [ "$front" = "Code" ]
}

# Truthful auto-submit verification (2026-06-03 worktree-spawn-bug fix / dual-brain codex+Gemini).
# osascript `keystroke return` exit 0 only proves the key event was SENT — NOT that the Claude
# session received + submitted the prompt. The prior code wrote `.submitted` on osascript exit 0,
# producing a FALSE-POSITIVE ack when a cold (worktree) window swallowed the Enter. A *real* submit
# makes the spawned session touch `queue/<task>.heartbeat` early in the handoff prompt — poll for it.
# Returns 0 (session genuinely started) / 1 (no heartbeat within the window → Enter didn't land).
verify_session_started() {
    local queue="$1" task="$2"
    local hb="$queue/$task.heartbeat"
    local secs="${HANDOFF_SUBMIT_VERIFY_SECS:-40}"
    local i=0
    while [ "$i" -lt "$secs" ]; do
        [ -f "$hb" ] && return 0
        sleep 1
        i=$((i + 1))
    done
    return 1
}

# Raise THIS task's VS Code window to frontmost before the synthetic Enter (2026-06-03 worktree
# multi-window fix). A fresh per-session worktree opens its OWN window that competes with the owner's
# other project windows; `is_frontmost_code` only proves the *app* is Code, not *which window* — so
# the Enter can land on a wrong project's window (observed: diagnostic Enter hit a family-business
# window). The engine-injected .code-workspace sets `window.title` to contain the task id, so AXRaise
# the window whose name contains it. Best-effort (always returns 0; a miss just falls back to the
# pre-existing frontmost-app guard). Cold worktree windows only — main-window tab spawns don't need it.
#
# focus-drift v2 hardening (2026-06-10 / dual-brain gemini MUST): ENUMERATE FIRST — the old code ran
# the app-level `activate` BEFORE the title match, so when no window matched the net effect was
# pulling VS Code's LAST-ACTIVE (= the owner's OLD) window to front: the exact reverse of the intent
# (the wh-coord-10 secondary lesion). Now a separate enumerate-only osascript (which deliberately
# contains NO app-activation keyword, so a recording stub can prove the no-op) looks for a window
# whose title carries one of the tokens; ONLY a hit runs activate→AXRaise (activate must precede
# AXRaise — a bare AXRaise only reorders windows INSIDE a backgrounded app). A miss does NOTHING.
# Tokens are tried in argv order (caller passes the singlepane spawn_nonce first when it has one,
# the task id as fallback); argv-passed (no AppleScript injection). Sets RAISE_MATCHED_TOKEN to the
# token that hit ("" = no hit, nothing raised) for the discriminator diagnostics. Always returns 0.
RAISE_MATCHED_TOKEN=""
raise_task_window() {
    local tok hit prev=""
    RAISE_MATCHED_TOKEN=""
    for tok in "$@"; do
        [ -z "$tok" ] && continue
        [ "$tok" = "$prev" ] && continue   # cold path passes (task, task) — don't enum twice
        prev="$tok"
        hit=$("$HANDOFF_OSASCRIPT_CMD" -e 'on run argv
            -- handoff-window-enum
            set token to item 1 of argv
            tell application "System Events"
                if not (exists process "Code") then return "nohit"
                tell process "Code"
                    repeat with w in windows
                        if name of w contains token then return "hit"
                    end repeat
                end tell
            end tell
            return "nohit"
        end run' "$tok" 2>>"$LOG")
        if [ "$hit" = "hit" ]; then
            RAISE_MATCHED_TOKEN="$tok"
            # TOCTOU (fdv2-fix1 SHOULD, documented as ACCEPTED): the window matched by the enum
            # above can close in the gap before this raise script runs — its `activate` still
            # fires app-level and pulls VS Code's last-active (possibly the owner's) window
            # front ONCE. Best-effort by design; folding the title match INTO this script would
            # put an app-activation keyword on the enumerate path and break the stubbed
            # provability of the miss case (the enum script deliberately carries none).
            "$HANDOFF_OSASCRIPT_CMD" -e 'on run argv
                -- handoff-window-raise
                set token to item 1 of argv
                tell application "Visual Studio Code" to activate
                delay 0.3
                tell application "System Events" to tell process "Code"
                    repeat with w in windows
                        if name of w contains token then
                            perform action "AXRaise" of w
                            exit repeat
                        end if
                    end repeat
                end tell
            end run' "$tok" 2>>"$LOG"
            return 0
        fi
    done
    return 0
}

# ONE System Events probe (focus-drift v2 / 2026-06-10). Prints, prefix-tagged so empty values and
# arbitrary window titles parse unambiguously:
#   PROBE:OK                         (FIRST line — fdv2-fix1 trust marker: the enumeration
#                                     COMPLETED. An osascript error / AX hang prints NOTHING
#                                     (stderr dropped), and callers MUST read a missing
#                                     PROBE:OK as a FAILED probe — never as "Code has no
#                                     windows". Conflating the two was fail-OPEN: a failed
#                                     snapshot made every front window test "fresh" and the
#                                     discriminator dispatched into the owner's old window.)
#   FRONT_APP:<frontmost app name>
#   FRONT_WIN:<frontmost Code window name — empty unless Code is frontmost>
#   WIN:<name>                       (one line per Code window, any order)
# Window names are newline-sanitized IN-SCRIPT (cleanName: linefeed/return → space) so one
# window is always exactly one WIN: line — a pathological filename-with-newline title would
# otherwise shear the line protocol that bash parses with `sed`/`grep -Fxq` (codex MUST-2).
# Three consumers: (a) the PRE-`code -n` snapshot the timeout discriminator checks membership
# against; (b) re-run at discriminator time for the FRESH front app/window; (c) the retry-tick
# probe (does the target window still exist / is it already front?). "PROBE:OK + zero WIN:
# lines" = a LEGAL empty snapshot (no Code process / no windows — a first-window spawn still
# dispatches); output without PROBE:OK = probe FAILURE → callers fail-closed.
probe_code_windows() {
    "$HANDOFF_OSASCRIPT_CMD" -e 'on run
        -- handoff-window-probe
        tell application "System Events"
            set frontApp to ""
            try
                set frontApp to name of first application process whose frontmost is true
            end try
            set frontWin to ""
            set winLines to ""
            if exists process "Code" then
                tell process "Code"
                    repeat with w in windows
                        set winLines to winLines & "WIN:" & my cleanName(name of w) & linefeed
                    end repeat
                    if frontApp is "Code" and (count of windows) > 0 then
                        set frontWin to my cleanName(name of front window)
                    end if
                end tell
            end if
            return "PROBE:OK" & linefeed & "FRONT_APP:" & frontApp & linefeed & "FRONT_WIN:" & frontWin & linefeed & winLines
        end tell
    end run
    on cleanName(t)
        -- codex MUST-2: replace linefeed/return with a space so one window == one line
        set text item delimiters of AppleScript to {linefeed, return}
        set parts to every text item of (t as text)
        set text item delimiters of AppleScript to " "
        set cleaned to parts as text
        set text item delimiters of AppleScript to ""
        return cleaned
    end cleanName' 2>/dev/null
}

# Does any newline-separated window name in <list> CONTAIN <token>? (substring per line —
# mirrors AppleScript `contains`). Empty list / empty token ⇒ 1.
_wins_contain() {
    [ -n "$1" ] && [ -n "$2" ] || return 1
    printf '%s\n' "$1" | /usr/bin/grep -Fq -- "$2"
}

# Window-level frontmost helpers (2026-06-03 code-r-clobber fix / dual-brain codex+Gemini).
# `is_frontmost_code` only proves the *app* is Code — insufficient for a cold worktree spawn that
# opens its OWN window competing with the owner's other Code windows. These resolve *which* window
# is frontmost by its title (the engine-injected .handoff.code-workspace sets window.title to carry
# the task id), so we can (a) wait for the fresh window to render + take focus BEFORE `open URI`
# (consensus: no hardcoded sleep), and (b) refuse the synthetic Enter unless THE task window is the
# frontmost one (consensus P1: a stray Enter must never land on a wrong window — terminal mid-command
# / finance UI). Returns "" when Code isn't frontmost or has no window → callers treat as not-ready.
frontmost_code_window_name() {
    "$HANDOFF_OSASCRIPT_CMD" -e 'tell application "System Events"
        set frontApp to name of first application process whose frontmost is true
        if frontApp is not "Code" then return ""
        tell process "Code"
            if (count of windows) is 0 then return ""
            return name of front window
        end tell
    end tell' 2>/dev/null
}

# 0 = the frontmost Code window's title contains <task> (THE task window has focus).
target_window_frontmost() {
    local task="$1" name
    name=$(frontmost_code_window_name)
    case "$name" in
        *"$task"*) return 0 ;;
        *) return 1 ;;
    esac
}

# Poll until THE task window is frontmost (cold spawn render+focus), up to <secs> (default 8). 0=ready.
# WALL-CLOCK budget (2026-06-07 spawn-speedup): each poll runs `target_window_frontmost`, an osascript
# that DURING a cold window render can block for SECONDS (System Events enumerating the AX tree of a
# mid-launch Code window). The old step-counting (attempts = secs×5, sleep 0.2) only counted the 0.2s
# sleeps and IGNORED that per-iter osascript cost, so a nominal "3s" overshot to ~16s wall-clock when the
# title never matched (measured: sp-deploy2 spawn 2026-06-07 — code-n→URI took +16s). A real /bin/date
# deadline keeps "<secs>s" honest (same fix already proven in cold_submit_with_retry). The DESIGNED
# behaviour is unchanged: on timeout the caller AXRaises the task window + opens the URI into the
# frontmost (= just-opened worktree) window anyway, and the Enter is still readiness-gated downstream —
# so restoring the intended budget does not weaken the multi-window focus guarantee, it only stops the
# overshoot. Poll-FIRST (below) guarantees ≥1 target_window_frontmost check before the deadline is honoured.
wait_target_window_frontmost() {
    local task="$1" secs="${2:-8}" deadline_ms
    # MILLISECOND deadline (R2 codex+Gemini): a /bin/date +%s second-clock truncates — captured at X.99s it
    # rolls to X+1 one tick later, shrinking a 3s budget to ~2s and risking ZERO polls for a tiny budget.
    # _perf_ms is ms-precise + robust. Poll-FIRST so target_window_frontmost ALWAYS runs at least once before
    # the deadline is honoured (preserves the pre-fix step-counter's "≥1 check" invariant even when secs=0 /
    # the budget is already elapsed) — a stray Enter is still gated downstream, but the URI should land on the
    # task window whenever it is reachable within the budget.
    deadline_ms=$(( $(_perf_ms) + secs * 1000 ))
    while :; do
        target_window_frontmost "$task" && return 0
        [ "$(_perf_ms)" -ge "$deadline_ms" ] && return 1
        sleep 0.2
    done
}

# Poll until the Code *app* is frontmost (warm reuse path), up to <secs> (default 3). 0=ready.
# Returns immediately when already frontmost → non-regressive vs the old fixed `sleep 0.4`.
wait_code_frontmost() {
    local i=0 attempts=$(( ${1:-3} * 5 ))
    while [ "$i" -lt "$attempts" ]; do
        is_frontmost_code && return 0
        sleep 0.2
        i=$((i + 1))
    done
    return 1
}

# Atomic submit (R2 dual-brain codex+Gemini / closes a TOCTOU gap): ONE osascript asserts (frontmost
# app is Code AND the front window title contains <token>) and ONLY then presses Enter — in the SAME
# process, so focus cannot drift between a separate check and a separate keystroke (a stray Enter must
# never land on a wrong window: a terminal mid-command / a finance UI). <token> = the task id (cold
# worktree window, whose .handoff.code-workspace title carries it) or the workspace display name
# (warm reuse, default VS Code title's rootName). Token is passed as argv (not string-interpolated)
# → no AppleScript injection. Returns 0 ONLY when Enter was sent to the matching window (echo "sent").
submit_enter_if_front_window_contains() {
    local token="$1" do_focus="${2:-0}" out focus_cmd="" script fkey
    # do_focus=1 (COLD worktree): the fresh window opens with the LEFT sidebar (Explorer) focused, so a
    # bare `keystroke return` lands on the sidebar — NOT the Claude input (owner-diagnosed on stage1-10d:
    # Claude renders in the side panel; AXRaise + default focus leaves the keyboard on the Explorer).
    # First run Claude Code's "Focus input" command via a dedicated keybinding (HANDOFF_FOCUS_KEY +
    # cmd/ctrl/alt — install/keybindings-claude-focus.json must bind it to claude-vscode.focus), THEN
    # re-assert the window and press Enter so it reaches the Claude input.
    if [ "$do_focus" = "1" ]; then
        fkey="${HANDOFF_FOCUS_KEY:-0}"
        case "$fkey" in [0-9A-Za-z]) : ;; *) fkey=0 ;; esac   # single alnum only (no AppleScript injection / R2 P2)
        focus_cmd="keystroke \"$fkey\" using {command down, control down, option down}
                        delay 0.3"
    fi
    script="on run argv
        set token to item 1 of argv
        tell application \"System Events\"
            set frontApp to name of first application process whose frontmost is true
            if frontApp is not \"Code\" then return \"nofront\"
            tell process \"Code\"
                if (count of windows) is 0 then return \"nowin\"
                if name of front window contains token then
                    $focus_cmd
                    -- R2 codex+Gemini P1: the focus chord + delay re-opened a focus-drift window, so
                    -- RE-ASSERT (app=Code is guaranteed by the outer tell; re-check the front window)
                    -- before the Enter — a stray Enter must never land on a window the owner just
                    -- switched to during the delay. (do_focus=0 / warm: focus_cmd empty → immediate.)
                    if (count of windows) > 0 and name of front window contains token then
                        keystroke return
                        return \"sent\"
                    end if
                    return \"mismatch\"
                end if
                return \"mismatch\"
            end tell
        end tell
    end run"
    out=$("$HANDOFF_OSASCRIPT_CMD" -e "$script" "$token" 2>>"$LOG") || return 2
    # rc: 0 = Enter sent to the matching window | 2 = osascript/keystroke genuinely errored
    # (accessibility revoked mid-run) | 1 = nofront/nowin/mismatch (focus drift, not accessibility).
    [ "$out" = "sent" ] && return 0
    return 1
}

# Collapse BOTH side bars on a COLD worktree window so it becomes a single editor pane (owner's "默认只有
# 中间栏"). 2026-06-06 / owner chose the NATIVE-EXTENSION path over keystrokes.
#
# EVOLUTION (why this is now a URI to our extension, not a keystroke):
#   v0 custom cmd+ctrl+alt+9 chord via `keystroke "9" using {option down}` — FAILED: osascript `keystroke` of a
#      DIGIT under option sends the option-MUTATED character (e.g. "≈"), not a clean key, so VS Code never matched
#      the binding. (Not a webview swallow — the Claude webview only eats Escape; VS Code forwards webview keydown.)
#   v1 built-in Cmd+B / Cmd+Alt+B toggles — FAILED: a toggle REOPENS an already-closed bar (state-fragile), and
#      the cold-spawn URI re-opens the chat side bar, so closing before it never survived.
#   v2 key code 25 (= physical "9") firing the cmd+ctrl+alt+9 keybinding's explicit closeSidebar/closeAuxiliaryBar
#      — worked, but still depended on a keybindings.json entry + osascript Accessibility + the right window being
#      frontmost for the key to land + could be eaten by a focused text input.
#   v3 (CURRENT) the handoff-helper VS Code extension calls VS Code's OWN closeSidebar + closeAuxiliaryBar
#      NATIVELY. The launcher just opens vscode://dharmaxis.handoff-helper/singlepane?task_id=<task>. Benefits:
#      no keystroke (cannot be eaten by a focused input / no character mutation), no keybindings.json dependency,
#      no toggle state (explicit idempotent close), layout-independent (primary + secondary), and a BUILT-IN guard
#      — the extension closes side bars ONLY when the active window's workspace is a `.handoff.code-workspace`
#      (a cold-spawn worktree), so it can never collapse a side bar on the owner's normal window (multi-window
#      red line). The CALLER fires it AFTER the prompt tab is open + submitted, so whatever the Claude URI
#      re-opened (the chat side bar) is closed LAST → single editor pane.
# 🔴 "dispatched" ≠ "actually closed" — only a real-machine VISUAL check confirms single-pane (lesson 2026-06-06:
# a prior false-positive log fooled both me and the dual-brain). The log below says DISPATCHED, never "closed".
# Returns: 0 = singlepane URI dispatched | 2 = open failed.
close_sidebars_if_front_window_contains() {
    local token="$1"
    # Native close via the handoff-helper extension (dharmaxis.handoff-helper). task_id is the kebab-case task id
    # (URL-safe). The extension guards on .handoff.code-workspace, so a stray dispatch onto a wrong window is a
    # no-op there — and we only call this on the cold worktree path, after the submit.
    if "$HANDOFF_OPEN_CMD" "vscode://dharmaxis.handoff-helper/singlepane?task_id=$token" 2>>"$LOG"; then
        log "COLD-SIDEBAR: DISPATCHED native closeSidebar+closeAuxiliaryBar via handoff-helper extension (singlepane URI, token=$token) — note: DISPATCHED ≠ confirmed-closed (verify visually)"
        return 0
    fi
    log "COLD-SIDEBAR: singlepane URI open FAILED (token=$token) — best-effort; readiness-gate still guarded the submit"
    return 2
}

# Worktree session transcript line count — the cold-submit "prompt actually submitted" signal
# (2026-06-03 cold-start-swallow fix). The Claude session writes its transcript (<sid>.jsonl) the
# instant it begins PROCESSING the submitted prompt (thinking + tool calls) → it GROWS within ~seconds
# of a real submit, far earlier than queue/<task>.heartbeat (which the AI only touches after reading the
# whole prompt + §0, tens of seconds later — too late to gate a retry without risking a double-submit).
# Project slug = the absolute workspace path with every '/' and '.' replaced by '-' (Claude Code
# convention). HANDOFF_TRANSCRIPT_ROOT overridable for tests. Echoes the newest .jsonl's line count (0 if none).
worktree_transcript_lines() {
    local ws="$1" slug pd total rp
    # Resolve symlinks first (2026-06-05 live-test finding): Claude Code derives its project dir from the
    # CANONICAL (symlink-resolved) cwd, so the slug MUST be computed from the resolved path. A symlinked
    # workspace root otherwise yields a wrong slug → the real transcript is never found → growth is missed
    # → false ABORT *and* blind retries that defeat the monotonic-SUM double-submit guard (observed: a /tmp
    # test worktree wrote to ~/.claude/projects/-private-tmp-… but the slug read -tmp-…). `cd && pwd -P`
    # resolves every symlink component; fall back to the raw path if the dir is gone. Real worktrees under
    # ~/.claude-handoff are not symlinked, so this is a no-op there (resolved path == raw path).
    rp=$(cd "$ws" 2>/dev/null && pwd -P) || rp=""
    [ -n "$rp" ] || rp="$ws"
    slug=$(printf '%s' "$rp" | sed 's#[/.]#-#g')
    pd="${HANDOFF_TRANSCRIPT_ROOT:-$HOME/.claude/projects}/$slug"
    [ -d "$pd" ] || { echo 0; return; }
    # SUM lines across ALL .jsonl (monotonic-increasing during a spawn). Using only the NEWEST file's
    # count is NON-monotonic (codex+Gemini R2 P0/P1): a reused worktree's old high-line transcript makes
    # a fresh new-session file (1 line) read as a DECREASE → growth missed → blind retry → DOUBLE-SUBMIT.
    # A new session file ADDS to the sum, so sum > base detects it. `find -exec cat` avoids the
    # nullglob/stdin-hang hazard of a bare `cat "$pd"/*.jsonl` (empty glob → cat reads stdin → hang).
    total=$(/usr/bin/find "$pd" -maxdepth 1 -name '*.jsonl' -exec cat {} + 2>/dev/null | wc -l | tr -d ' ')
    echo "${total:-0}"
}

# Cold worktree submit — READINESS-GATED single Enter (2026-06-06 主人立法 + root-cause investigation).
# ROOT CAUSE (probe + screenshots + failing logs, 2026-06-06): a fresh cold worktree window opens with keyboard
# focus on the LEFT-sidebar Claude panel (or Welcome); `open URI` creates the prompt in the CENTER editor Claude
# tab, which takes a VARIABLE time to render and grab focus from the sidebar. The OLD code pressed Enter at a FIXED
# delay (0.5s) — a gamble that LOSES under load (~40% miss in tests): the Enter fires while focus is still on the
# EMPTY sidebar input → nothing is submitted. A fixed time budget (5s / 8s / 0.5s — whatever) can NEVER reliably hit
# the moment the center tab grabs focus, because that moment varies with render load. So we STOP guessing the time:
# poll (READ-ONLY — proven not to move focus) until the FOCUSED element is the prompt-bearing Claude input (an
# AXTextArea "Message input" whose value CONTAINS the task token = OUR pasted prompt landed AND focus is ON it, not
# the empty/stale sidebar), and ONLY THEN press Enter — in the SAME osascript process (no event-loop yield between
# the value read and the keystroke; the TOCTOU window is microseconds — narrowed to the physical limit, not provably
# eliminated since `keystroke` targets whatever is focused at dispatch, codex). Fast when ready (sub-second), waits
# out a slow render, and HONESTLY withholds (never a blind Enter
# onto the sidebar / a wrong window) when readiness never arrives. This directly fixes the owner-observed "焦点跑侧栏".
# Return codes (HONEST per-state acks): 0 = submitted (Enter on the verified prompt input + transcript grew) /
# 3 = ALREADY-grew before our Enter (external/manual Enter started the session) → running, mark submitted (no
# duplicate re-trigger) but NOT script-verified / 2 = osascript keystroke errored / 5 = readiness never arrived →
# Enter WITHHELD (manual needed) / 1 = Enter sent on the verified input but transcript still did not grow (unexpected).
# $3 = a PRE-OPEN baseline captured by the caller BEFORE the settle (so a manual Enter during the settle is caught as
# already-grew rc=3, not missed → no `failed` mis-ack of a running session → no duplicate-window re-trigger).
cold_submit_with_retry() {
    local token="$1" ws="$2" base="${3:-}"
    local cur rc w verify ready_secs start deadline out laststate=""
    [ -z "$base" ] && base=$(worktree_transcript_lines "$ws")   # fallback if no pre-settle baseline was passed
    # both timeouts INTEGER only — a fractional value would break `[ -lt ]` (codex P2). Clamp junk → default, min 1.
    verify="${HANDOFF_COLD_VERIFY_SECS:-6}"; case "$verify" in ''|*[!0-9]*) verify=6 ;; esac; [ "$verify" -lt 1 ] && verify=1
    ready_secs="${HANDOFF_COLD_READY_SECS:-10}"; case "$ready_secs" in ''|*[!0-9]*) ready_secs=10 ;; esac; [ "$ready_secs" -lt 1 ] && ready_secs=1
    log "COLD-SUBMIT-START: token=$token base_lines=$base ws=$ws ready≤${ready_secs}s verify=${verify}s (readiness-gated single Enter / 主人立法 2026-06-06)"
    # READINESS-GATED atomic submit: assert (front window contains token) AND (focused element is a Claude "Message
    # input" AXTextArea with a NON-EMPTY value = the prompt landed + focus is on THE prompt input), then keystroke —
    # all one process. Echoes a diagnostic state so the poll can wait out the cold render. Token via argv (no injection).
    local script='on run argv
        set token to item 1 of argv
        tell application "System Events"
            set fa to name of first application process whose frontmost is true
            if fa is not "Code" then return "nofront"
            tell process "Code"
                if (count of windows) is 0 then return "nowin"
                -- EVERY AX read is missing-value-guarded BEFORE any string op (Gemini P0 2026-06-06): during the
                -- sidebar→center focus transition, focus passes through nodes whose name/role/description is
                -- `missing value`; an unguarded `contains`/`is not` on `missing value` THROWS (-1728) → osascript
                -- exits 1 → bash `|| return 2` would ABORT the whole poll exactly when it should keep waiting.
                set wname to ""
                try
                    set wname to name of front window
                end try
                if wname is missing value then set wname to ""
                if wname does not contain token then return "mismatch"
                set f to missing value
                try
                    set f to value of attribute "AXFocusedUIElement"
                end try
                if f is missing value then return "noelem"
                set r to ""
                try
                    set r to (role of f)
                end try
                if r is missing value then set r to ""
                if r is not "AXTextArea" then return "notinput"
                set d to ""
                try
                    set d to (description of f)
                end try
                if d is missing value then set d to ""
                if d does not contain "Message input" then return "notinput"
                set v to ""
                try
                    set v to (value of f)
                end try
                if v is missing value then return "emptyinput"
                if v is "" then return "emptyinput"
                -- codex P1 2026-06-06: a NON-EMPTY value alone is too weak — the empty left-sidebar Claude input is
                -- ALSO an AXTextArea "Message input", and could hold a stale draft. Require the focused input value
                -- to CONTAIN the task token (the handoff prompt embeds the task id) → proves it is OUR center prompt
                -- input, not a sidebar draft. (Falls through to wait/withhold when the right input is not focused.)
                if v does not contain token then return "wronginput"
                keystroke return
                return "sent"
            end tell
        end tell
    end run'
    # WALL-CLOCK deadline (each poll ≈ osascript ~0.5s + 0.25s sleep, so step-counting would overshoot the timeout
    # ~3× — a real clock keeps "ready ≤ ${ready_secs}s" honest). Found by live validation 2026-06-06.
    start=$(/bin/date +%s); deadline=$((start + ready_secs))
    while [ "$(/bin/date +%s)" -lt "$deadline" ]; do
        # already-grew (manual/early Enter started the session before ours) → running, do not claim it (rc 3)
        cur=$(worktree_transcript_lines "$ws")
        if [ "$cur" -gt "$base" ]; then
            log "COLD-SUBMIT: transcript already grew ${base}→${cur} before our Enter — external/manual Enter started the session (rc=3 submitted-external, NOT script-verified)"
            return 3
        fi
        out=$("$HANDOFF_OSASCRIPT_CMD" -e "$script" "$token" 2>>"$LOG") || return 2
        laststate="$out"
        [ "$out" = "sent" ] && break
        # mismatch = the task window is NOT frontmost (a concurrent window stole front) → AXRaise it back so the NEXT
        # poll finds it frontmost (owner-endorsed "不置顶就让它置顶再 Enter"; AXRaise PRESERVES the editor input focus —
        # proven live — so it never knocks focus onto the sidebar). Other not-ready states (noelem/notinput/empty/
        # wronginput) just wait for the center Claude tab to grab focus.
        [ "$out" = "mismatch" ] && raise_task_window "$token"
        sleep 0.25
    done
    if [ "$out" != "sent" ]; then
        log "COLD-SUBMIT: focus never settled on the prompt input within ${ready_secs}s (last=$laststate) — Enter WITHHELD, manual needed (rc=5)"
        return 5
    fi
    log "COLD-SUBMIT: focus VERIFIED on the prompt input → bare Enter sent (ready after ~$(( $(/bin/date +%s) - start ))s) token=$token — verifying transcript growth (${verify}s)"
    # Verify the Enter genuinely submitted: a real submit grows the worktree transcript within ~seconds. Only this
    # (not "osascript exit 0", which merely proves the KEY was sent) lets the ack be truthful — never false "submitted".
    w=0
    while [ "$w" -lt "$verify" ]; do
        cur=$(worktree_transcript_lines "$ws")
        if [ "$cur" -gt "$base" ]; then
            log "COLD-SUBMIT: transcript grew ${base}→${cur} after our Enter (submitted, auto-verified)"
            return 0
        fi
        sleep 1; w=$((w + 1))
    done
    log "COLD-SUBMIT: Enter sent on the VERIFIED prompt input but transcript NOT grown in ${verify}s — unexpected (rc=1)"
    return 1
}

# ─── SINGLEPANE bounded submit retry (sw-sp-enter-retry / 2026-06-10, dual-brain GREEN) ──────
# THE BUG (owner: "经常手动 Enter"): the singlepane path submitted through the WARM one-shot
# gate (ONE osascript title-nonce assertion + bare Enter, no retry) — but a singlepane spawn is
# a cold-rendering NEW window, so the Enter can fire while the URI paste has not landed
# (swallowed) → no second chance. cold_submit_with_retry's transcript line-GROWTH gate cannot
# be reused: a singlepane session writes into the SHARED project transcript dir
# (~/.claude/projects/<project-slug>/, cwd = the real repo) where a SIBLING session's growth
# would false-confirm. CONTRACT (dual-brain GREEN + coordinator arbitration):
#   confirm   = a NEW *.jsonl (∉ the pre-URI baseline FILE-SET) carrying the 🆔<task> marker.
#               mtime is BANNED — a resume / re-dispatch of the same task leaves OLD files
#               containing the same 🆔, which an mtime/content-only probe would false-confirm
#               (the false-positive MAIN path);
#   re-probe BEFORE every retry — already confirmed → ack submitted, NEVER press again;
#   retry gate = ONE osascript asserting Code frontmost ∧ front window title contains the
#               nonce token ∧ focused element is the Claude "Message input" ∧ its value still
#               contains 🆔<task> (= OUR prompt sits UNSUBMITTED in OUR input) → only then
#               keystroke return. Empty/markerless input → DO NOT press (a submitted prompt
#               empties the input — a second Enter there is the double-submit hazard), keep
#               polling the jsonl; front window without the nonce → nonce-first
#               raise_task_window, then retry;
#   re-read   = a cold heavy-render burst can flash a transient not-ready focus read
#               (noelem/notinput/emptyinput) while OUR 🆔 prompt is still physically in the box;
#               singlepane_retry_gate_settled re-reads HANDOFF_SP_REREAD_TRIES (default 3) times
#               with a HANDOFF_SP_REREAD_BACKOFF (default 0.4s) settle before conceding the
#               attempt — the press red line is untouched (only a positive marker read presses);
#   bounded   = retries ≤ HANDOFF_SP_RETRY_MAX (default 2) after the first Enter; confirm
#               poll window HANDOFF_SP_POLL_SECS × HANDOFF_SP_POLL_TRIES (default 2s×3) per
#               attempt; per-attempt re-read ≤ HANDOFF_SP_REREAD_TRIES. Exhausted → an HONEST
#               failed ack saying which step fell empty.
# Scope: SINGLEPANE_WINDOW=1 AND the URI `open` succeeded. The focus-contended defer/give-up
# paths happen BEFORE the URI dispatch and never reach this machinery; a visible-park window
# never receives an Enter.

# Newline list of the project transcript dir's existing *.jsonl paths (sorted, stable). The
# slug derives from the RESOLVED workspace path ('/'+'.' → '-', Claude Code convention) — the
# same resolution worktree_transcript_lines performs (kept duplicated ON PURPOSE: the cold
# path is byte-frozen; extracting a shared skeleton belongs to the 共享模块重构 backlog).
singlepane_list_jsonls() {
    local ws="$1" rp slug pd
    rp=$(cd "$ws" 2>/dev/null && pwd -P) || rp=""
    [ -n "$rp" ] || rp="$ws"
    slug=$(printf '%s' "$rp" | sed 's#[/.]#-#g')
    pd="${HANDOFF_TRANSCRIPT_ROOT:-$HOME/.claude/projects}/$slug"
    [ -d "$pd" ] || return 0
    /usr/bin/find "$pd" -maxdepth 1 -name '*.jsonl' 2>/dev/null | LC_ALL=C /usr/bin/sort
}

# 0 = CONFIRMED: some *.jsonl NOT in the baseline set carries the 🆔<task> marker. Sets
# SP_PROBE_STATE ∈ confirmed|new-jsonl-no-marker|no-new-jsonl (the SP-SUBMIT diagnostic enum).
# A baseline (pre-existing) file is NEVER a confirm source even when it greps the marker —
# that is exactly the resume/re-dispatch false-positive the new-file-set design exists to kill.
SP_PROBE_STATE=""
singlepane_probe_confirm() {
    local ws="$1" task="$2" base="$3" f cur found_new=0
    SP_PROBE_STATE="no-new-jsonl"
    cur=$(singlepane_list_jsonls "$ws")
    [ -n "$cur" ] || return 1
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        if [ -n "$base" ] && printf '%s\n' "$base" | /usr/bin/grep -Fxq -- "$f"; then
            continue
        fi
        found_new=1
        if /usr/bin/grep -qF -- "🆔$task" "$f" 2>/dev/null; then
            SP_PROBE_STATE="confirmed"
            return 0
        fi
    done <<EOF
$cur
EOF
    [ "$found_new" = "1" ] && SP_PROBE_STATE="new-jsonl-no-marker"
    return 1
}

# Retry-Enter gate — ONE osascript process (TOCTOU narrowed to the physical limit, as in
# cold_submit_with_retry, whose AX-guard pattern this mirrors: every AX read is
# missing-value-guarded BEFORE any string op — an unguarded op on `missing value` THROWS and
# bash would mis-read "osascript error" exactly when it should keep waiting). Echoes one of:
# sent|nofront|nowin|mismatch|noelem|notinput|emptyinput|wronginput. argv-passed (no
# AppleScript injection). Non-zero exit = osascript itself errored (accessibility revoked).
singlepane_retry_gate() {
    local token="$1" marker="$2"
    "$HANDOFF_OSASCRIPT_CMD" -e 'on run argv
        -- handoff-sp-retry-gate
        set token to item 1 of argv
        set marker to item 2 of argv
        tell application "System Events"
            set fa to name of first application process whose frontmost is true
            if fa is not "Code" then return "nofront"
            tell process "Code"
                if (count of windows) is 0 then return "nowin"
                set wname to ""
                try
                    set wname to name of front window
                end try
                if wname is missing value then set wname to ""
                if wname does not contain token then return "mismatch"
                set f to missing value
                try
                    set f to value of attribute "AXFocusedUIElement"
                end try
                if f is missing value then return "noelem"
                set r to ""
                try
                    set r to (role of f)
                end try
                if r is missing value then set r to ""
                if r is not "AXTextArea" then return "notinput"
                set d to ""
                try
                    set d to (description of f)
                end try
                if d is missing value then set d to ""
                if d does not contain "Message input" then return "notinput"
                set v to ""
                try
                    set v to (value of f)
                end try
                if v is missing value then return "emptyinput"
                if v is "" then return "emptyinput"
                if v does not contain marker then return "wronginput"
                keystroke return
                return "sent"
            end tell
        end tell
    end run' "$token" "$marker" 2>>"$LOG"
}

# Bounded focus RE-READ around singlepane_retry_gate (sw-sp-rc6-precision / cold-render precision).
# THE FALSE NEGATIVE (xunyin 2/2): during a cold heavy-render burst a SINGLE AXFocusedUIElement read
# can come back noelem/notinput/emptyinput even though the 🆔 prompt is physically sitting in the
# Claude input — the AX tree has not settled yet / focus has not landed on the freshly-rendered webview
# input (the owner's manual bare Return seconds later submits, proving the prompt was there all along).
# A single read judged that transient as "not ready" and, after a swallowed first Enter, gave up at
# rc=6. FIX: on a TRANSIENT not-ready read (noelem|notinput|emptyinput) re-read the focused value up to
# HANDOFF_SP_REREAD_TRIES times with a short HANDOFF_SP_REREAD_BACKOFF settle; ANY read that sees
# role=AXTextArea ∧ "Message input" ∧ value⊇marker presses INSIDE that same osascript process. THE PRESS
# RED LINE IS UNCHANGED — keystroke only ever fires after a positive marker read, and that read+press is
# singlepane_retry_gate's own atomic single process (re-reading just grants more chances to READ
# positive, NEVER a blind press). wronginput (a non-empty value WITHOUT our marker — provably not our
# prompt, possibly a sibling window's text) is NOT a render transient → returned immediately, never
# re-read into a press. nofront/nowin/mismatch are returned immediately too (the caller's nonce-first
# raise owns that recovery). Echoes the final gate outcome; a non-zero exit (osascript hard error /
# accessibility revoked) is propagated unchanged so the orchestrator's `|| return 2` still fires.
singlepane_retry_gate_settled() {
    local token="$1" marker="$2" tries backoff k out rc
    tries="${HANDOFF_SP_REREAD_TRIES:-3}"; case "$tries" in ''|*[!0-9]*) tries=3 ;; esac; [ "$tries" -lt 1 ] && tries=1
    backoff="${HANDOFF_SP_REREAD_BACKOFF:-0.4}"; case "$backoff" in ''|*[!0-9.]*|*.*.*) backoff=0.4 ;; esac
    k=0
    while [ "$k" -lt "$tries" ]; do
        k=$((k + 1))
        out=$(singlepane_retry_gate "$token" "$marker"); rc=$?
        [ "$rc" = "0" ] || return "$rc"   # osascript hard error → propagate (orchestrator returns 2)
        case "$out" in
            noelem|notinput|emptyinput)
                # transient cold-render not-ready — observe, settle, re-read (unless this was the last try)
                log "SP-SUBMIT: reread=$k/$tries gate=$out (AX not settled — re-reading focused value)"
                if [ "$k" -lt "$tries" ]; then
                    case "$backoff" in 0|0.0|0.00) : ;; *) sleep "$backoff" ;; esac
                fi
                ;;
            *)
                # sent | wronginput | nofront | nowin | mismatch → terminal (no re-read into a press)
                printf '%s\n' "$out"
                return 0
                ;;
        esac
    done
    printf '%s\n' "$out"
    return 0
}

# Orchestrator. Return codes (HONEST per-state acks, mirroring cold):
#   0 = our Enter + a NEW 🆔-marked jsonl (script-verified submit)
#   3 = a NEW 🆔-marked jsonl appeared WITHOUT our machinery pressing (external/manual Enter,
#       or confirm raced ahead of the press) → running, mark submitted but NOT script-verified
#   2 = osascript hard error (accessibility revoked mid-run)
#   6 = ambiguous-after-first-enter: Enter WAS pressed, the input then read empty/markerless,
#       and no marked jsonl arrived in the budget → NEVER press again (contract: 不盲按)
#   1 = Enter sent (marker-verified or first-shot) but no marked jsonl within the budget
#   5 = exhausted without EVER pressing (front never ours / input never ready) → manual Enter
SP_LAST_OUTCOME=""
singlepane_submit_with_retry() {
    local token="$1" task="$2" ws="$3" base="$4"
    local marker="🆔$task"
    local retry_max poll_secs poll_tries
    retry_max="${HANDOFF_SP_RETRY_MAX:-2}"; case "$retry_max" in ''|*[!0-9]*) retry_max=2 ;; esac
    poll_secs="${HANDOFF_SP_POLL_SECS:-2}"; case "$poll_secs" in ''|*[!0-9]*) poll_secs=2 ;; esac; [ "$poll_secs" -lt 1 ] && poll_secs=1
    poll_tries="${HANDOFF_SP_POLL_TRIES:-3}"; case "$poll_tries" in ''|*[!0-9]*) poll_tries=3 ;; esac; [ "$poll_tries" -lt 1 ] && poll_tries=1
    local attempt=0 max_attempts=$((1 + retry_max)) enter_pressed=0 out rc i base_n
    base_n=$(printf '%s' "$base" | /usr/bin/grep -c . || true)
    SP_LAST_OUTCOME=""
    log "SP-SUBMIT-START: token=$token task=$task retries≤$retry_max poll=${poll_secs}sx${poll_tries} base_jsonls=$base_n (new-file-set + 🆔 confirm, bounded Enter retry)"
    while [ "$attempt" -lt "$max_attempts" ]; do
        attempt=$((attempt + 1))
        # contract: RE-PROBE before any press — already confirmed → never press again
        if singlepane_probe_confirm "$ws" "$task" "$base"; then
            log "SP-SUBMIT: attempt=$attempt outcome=confirmed (pre-press probe — no further Enter)"
            [ "$enter_pressed" = "1" ] && return 0
            return 3
        fi
        if [ "$attempt" -eq 1 ]; then
            # first press = the pre-existing warm atomic title(nonce)-gated bare Enter —
            # status quo for the initial submit; the NEW machinery is everything after it.
            submit_enter_if_front_window_contains "$token"; rc=$?
            if [ "$rc" = "2" ]; then return 2; fi
            if [ "$rc" = "0" ]; then out="sent"; else out="mismatch"; fi
        else
            out=$(singlepane_retry_gate_settled "$token" "$marker") || return 2
        fi
        case "$out" in
            sent)
                enter_pressed=1
                i=0
                while [ "$i" -lt "$poll_tries" ]; do
                    sleep "$poll_secs"
                    if singlepane_probe_confirm "$ws" "$task" "$base"; then
                        log "SP-SUBMIT: attempt=$attempt outcome=confirmed"
                        return 0
                    fi
                    i=$((i + 1))
                done
                SP_LAST_OUTCOME="$SP_PROBE_STATE"
                log "SP-SUBMIT: attempt=$attempt outcome=$SP_PROBE_STATE (Enter sent, confirm poll exhausted)"
                ;;
            nofront|nowin|mismatch)
                SP_LAST_OUTCOME="front-mismatch"
                log "SP-SUBMIT: attempt=$attempt outcome=front-mismatch (gate=$out) — nonce-first raise + retry"
                raise_task_window "$token" "$task"
                sleep "$poll_secs"
                ;;
            *)
                # noelem|notinput|emptyinput|wronginput → DO NOT press; keep polling the jsonl
                # (a submitted prompt EMPTIES the input — pressing here is the double-submit
                # hazard; a markerless value is not provably our prompt input).
                SP_LAST_OUTCOME="input-not-ready"
                log "SP-SUBMIT: attempt=$attempt outcome=input-not-ready (gate=$out) — Enter withheld, polling jsonl"
                i=0
                while [ "$i" -lt "$poll_tries" ]; do
                    sleep "$poll_secs"
                    if singlepane_probe_confirm "$ws" "$task" "$base"; then
                        log "SP-SUBMIT: attempt=$attempt outcome=confirmed (during input-not-ready poll — no further Enter)"
                        [ "$enter_pressed" = "1" ] && return 0
                        return 3
                    fi
                    i=$((i + 1))
                done
                ;;
        esac
    done
    if [ "$enter_pressed" = "1" ]; then
        if [ "$SP_LAST_OUTCOME" = "input-not-ready" ]; then
            log "SP-SUBMIT: AMBIGUOUS after first Enter — input empty/markerless, no 🆔-marked jsonl in budget (attempts=$attempt) — never pressing again (rc=6)"
            return 6
        fi
        log "SP-SUBMIT: Enter sent but NO new 🆔-marked jsonl within budget (last=$SP_LAST_OUTCOME attempts=$attempt) (rc=1)"
        return 1
    fi
    log "SP-SUBMIT: exhausted without ever pressing (last=$SP_LAST_OUTCOME attempts=$attempt) — Enter WITHHELD (rc=5)"
    return 5
}

# Accessibility (UI-scripting) preflight. `keystroke` requires the process that
# ultimately drives System Events (launchd's osascript binary) to hold the
# Accessibility permission. Probe it NON-destructively via `UI elements enabled`
# — a pure query that sends no keys — routed through HANDOFF_OSASCRIPT_CMD so
# tests can stub it. Returns 0 = trusted.
accessibility_trusted() {
    local r
    r=$("$HANDOFF_OSASCRIPT_CMD" -e 'tell application "System Events" to return (UI elements enabled)' 2>/dev/null)
    [ "$r" = "true" ]
}

# Missing Accessibility used to surface only as a per-task WARN buried in the
# log, so auto-submit silently degraded to "tab opened, Enter never pressed".
# Raise ONE actionable notification instead — at most once per run, and once per
# 6h across runs (marker mtime) so a persistently-unfixed grant doesn't nag on
# every launchd tick. `display notification` itself needs no Accessibility, so
# it still fires when keystroke can't.
ACCESSIBILITY_WARNED=0
warn_accessibility_once() {
    [ "$ACCESSIBILITY_WARNED" = "1" ] && return 0
    ACCESSIBILITY_WARNED=1
    local marker="$HANDOFF_ROOT/.accessibility-warned"
    if [ -f "$marker" ]; then
        local mt now
        mt=$(/usr/bin/stat -f %m "$marker" 2>/dev/null || echo 0)
        now=$(/bin/date +%s)
        [ "$((now - mt))" -lt 21600 ] && return 0
    fi
    : > "$marker" 2>/dev/null || true
    log "ACCESSIBILITY-MISSING: 自动接续无法按 Enter (缺辅助功能权限). 新 tab 仍会打开但需手动按一次 Enter. 修复: System Settings → 隐私与安全性 → 辅助功能, 勾选运行 launchd 的 osascript/Terminal."
    "$HANDOFF_OSASCRIPT_CMD" -e 'display notification "自动接续无法按 Enter：缺辅助功能权限。tab 已打开，请手动按 Enter，并到 系统设置 → 隐私与安全性 → 辅助功能 授权。" with title "Handoff ⚠️ 辅助功能权限" sound name "Basso"' 2>>"$LOG" || true
}

# lock-probe P0 (no-unlock fallback): the launcher fell back to the ioreg lock
# probe because NEITHER an explicit HANDOFF_LOCK_CHECK_CMD NOR a derivable Quartz
# `--status` (from HANDOFF_UNLOCK_CMD) is configured. On modern macOS (≥ ~14,
# verified macOS 26) ioreg's CGSSessionScreenIsLocked is absent EVEN WHEN LOCKED,
# so this probe cannot distinguish locked from unlocked and may report a locked
# screen as "unlocked" → the GUI spawns behind the lock + Enter is a silent no-op.
# We CANNOT safely fix this without a reliable probe (forcing UNKNOWN here would
# also defer every genuinely-unlocked run → 100% stall). So make the risk LOUD
# (the original silent failure went undetected precisely because the log showed
# "success") and tell the operator to configure a probe. Once per run + per 6h.
LOCKPROBE_WARNED=0
warn_lockprobe_unreliable_once() {
    [ "$LOCKPROBE_WARNED" = "1" ] && return 0
    LOCKPROBE_WARNED=1
    local marker="$HANDOFF_ROOT/.lockprobe-unreliable-warned"
    if [ -f "$marker" ]; then
        local mt now
        mt=$(/usr/bin/stat -f %m "$marker" 2>/dev/null || echo 0)
        now=$(/bin/date +%s)
        [ "$((now - mt))" -lt 21600 ] && return 0
    fi
    : > "$marker" 2>/dev/null || true
    log "LOCKPROBE-UNRELIABLE-FALLBACK: 无可靠锁屏探针 (未配 HANDOFF_LOCK_CHECK_CMD / HANDOFF_UNLOCK_CMD --unlock), 退回 ioreg — 新版 macOS 锁屏时该探针可能误判'未锁', 锁屏下自动接续可能把 tab 开在锁屏背后且 Enter 无效. 修复: 配置 HANDOFF_UNLOCK_CMD='<mp-unlock> --unlock' (即启用 unlock-pivot) 或显式 HANDOFF_LOCK_CHECK_CMD."
    "$HANDOFF_OSASCRIPT_CMD" -e 'display notification "锁屏探针不可靠 (ioreg 回退)：锁屏下自动接续可能失效。请配置 unlock 命令或锁屏探针。" with title "Handoff ⚠️ 锁屏探针" sound name "Basso"' 2>>"$LOG" || true
}

# ─── unlock-pivot helpers (lock-aware GUI gating; defined before the loop) ───

# Effective Quartz lock-probe command. The MP unlock CLI exposes `--status`
# (exit 0=unlocked / 1=locked / 2=error) backed by Quartz
# CGSessionCopyCurrentDictionary — the RELIABLE lock probe on modern macOS.
# Derived from HANDOFF_UNLOCK_CMD by swapping --unlock→--status (mirrors
# effective_relock_cmd's --lock). Empty when no unlock cmd is configured.
effective_lockcheck_cmd() {
    case "$HANDOFF_UNLOCK_CMD" in
        *--unlock*) printf '%s' "${HANDOFF_UNLOCK_CMD/--unlock/--status}" ;;
        *) printf '' ;;
    esac
}

# Lock probe. exit 0=locked / 1=unlocked / 2=UNKNOWN. Probe priority:
#   1. HANDOFF_LOCK_CHECK_CMD — explicit stdout-contract override (prints
#      locked|unlocked|*); used by tests and power users.
#   2. Quartz via the MP unlock CLI's `--status` (exit-code contract) — the
#      RELIABLE default whenever the unlock feature is configured.
#   3. ioreg `CGSSessionScreenIsLocked` — LAST-RESORT fallback ONLY. ⚠️ On modern
#      macOS (verified macOS 26 / Tahoe, 2026-05-31 on-box 2c) this property is
#      ABSENT even when the screen is LOCKED, so this path reports "unlocked" for a
#      locked screen — a silent killer (the launcher would spawn the GUI behind the
#      lock screen, osascript Enter a no-op, session dead). Reached only when no
#      unlock cmd is configured (unlock feature unused); when it IS, path 2 wins.
screen_is_locked() {
    if [ -n "$HANDOFF_LOCK_CHECK_CMD" ]; then
        case "$("$HANDOFF_LOCK_CHECK_CMD" 2>/dev/null)" in
            locked) return 0 ;; unlocked) return 1 ;; *) return 2 ;;
        esac
    fi
    local qcmd; qcmd=$(effective_lockcheck_cmd)
    if [ -n "$qcmd" ]; then
        run_with_timeout "${HANDOFF_LOCKCHECK_TIMEOUT:-15}" $qcmd >/dev/null 2>&1
        case $? in 0) return 1 ;; 1) return 0 ;; *) return 2 ;; esac
    fi
    # Gate0b/lock-probe P0-2: unlock is CONFIGURED ($HANDOFF_UNLOCK_CMD non-empty)
    # but we could NOT derive a reliable Quartz `--status` probe from it (e.g. the
    # cmd has no `--unlock` token to swap, or a typo'd flag). Do NOT trust the
    # known-broken ioreg fallback here — that is exactly the macOS-26 silent killer
    # (locked screen read as "unlocked" → GUI spawned behind the lock). Return
    # UNKNOWN so the caller fails CLOSED (defer), never blind-spawns.
    if [ -n "$HANDOFF_UNLOCK_CMD" ]; then
        log "LOCKPROBE-UNRELIABLE: HANDOFF_UNLOCK_CMD set but no Quartz --status derivable (need a '--unlock' token or explicit HANDOFF_LOCK_CHECK_CMD); refusing ioreg fallback → UNKNOWN"
        return 2
    fi
    # No unlock feature in use → legacy ioreg fallback (best-effort; unreliable on
    # modern macOS, but only reached when the unlock path is entirely unconfigured).
    # Make the unreliability LOUD so a locked-screen mis-read can't fail silently
    # (the original silent failure hid behind "success" logs). Skipped under a stub
    # ioreg in tests via HANDOFF_LOCKPROBE_QUIET=1.
    [ "${HANDOFF_LOCKPROBE_QUIET:-0}" = "1" ] || warn_lockprobe_unreliable_once
    local out
    out=$("$HANDOFF_IOREG_CMD" -n Root -d1 2>/dev/null) || return 2
    [ -z "$out" ] && return 2
    printf '%s' "$out" | /usr/bin/grep -q '"CGSSessionScreenIsLocked" = Yes' && return 0
    return 1
}

# Unlock opt-in (R2 P0-1 / full-sweep A1: the per-project `<project>/unlock.enabled`
# sentinel is the ONLY enabler). Auto-unlock injects the Mac login password, so
# every project must be enabled deliberately via its own sentinel. The former
# global `HANDOFF_UNLOCK_ENABLED=1` env enabler was REMOVED: a single stray export
# (launchd EnvironmentVariables / a shell rc) would otherwise arm password
# injection for EVERY project at once (red-line ③ — per-project opt-in). There is
# intentionally no global / all-projects switch on the production path; tests opt
# in by writing the same per-project sentinel under a tmp HANDOFF_ROOT.
unlock_enabled_for_project() {
    local proj_dir="$1"
    [ -f "$proj_dir/unlock.enabled" ] && return 0
    return 1
}

# Portable epoch mtime (BSD/macOS vs GNU).
_u_mtime() { case "$(uname)" in Darwin) /usr/bin/stat -f %m "$1" 2>/dev/null ;; *) stat -c %Y "$1" 2>/dev/null ;; esac; }

# Run a command with a wall-clock timeout (macOS lacks /usr/bin/timeout): bg +
# poll + kill. Returns the command's exit code, or 124 on timeout (P1-5).
run_with_timeout() {
    local secs="$1"; shift
    [ "$#" -eq 0 ] && return 2
    "$@" &
    local pid=$! waited=0
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$waited" -ge "$secs" ]; then
            # R2 P1: reap immediate grandchildren too, not just the direct child.
            pkill -TERM -P "$pid" 2>/dev/null
            kill -TERM "$pid" 2>/dev/null; sleep 1
            pkill -KILL -P "$pid" 2>/dev/null
            kill -KILL "$pid" 2>/dev/null
            wait "$pid" 2>/dev/null; return 124
        fi
        sleep 1; waited=$((waited + 1))
    done
    wait "$pid"; return $?
}

# Durable defer marker (design §3.1): keep the .uri, record why/since/ticks so the
# 状态 shortcut + watchdog surface "N tasks waiting for unlock". Removed when the
# .uri is finally consumed. Self-contained.
defer_uri() {
    local proj_dir="$1" queue="$2" task="$3" reason="$4"
    local marker="$queue/$task.deferred"
    local now; now=$(/bin/date +%s)
    local first="$now" ticks=1
    if [ -f "$marker" ]; then
        local pf pt
        pf=$(sed -n 's/^first_epoch=//p' "$marker" 2>/dev/null | head -1)
        pt=$(sed -n 's/^ticks=//p' "$marker" 2>/dev/null | head -1)
        case "$pf" in ''|*[!0-9]*) pf="$now" ;; esac
        case "$pt" in ''|*[!0-9]*) pt=0 ;; esac
        first="$pf"; ticks=$((pt + 1))
    fi
    printf 'task=%s\nreason=%s\nfirst_epoch=%s\nlast_epoch=%s\nticks=%s\n' \
        "$task" "$reason" "$first" "$now" "$ticks" > "$marker"
    local nfile="$proj_dir/.deferred-notified"
    local do_notify=1
    if [ -f "$nfile" ]; then
        local mt; mt=$(_u_mtime "$nfile")
        [ -n "$mt" ] && [ "$((now - mt))" -lt 21600 ] && do_notify=0
    fi
    if [ "$do_notify" = "1" ]; then
        : > "$nfile" 2>/dev/null || true
        "$HANDOFF_OSASCRIPT_CMD" -e 'display notification "锁屏待接续 — 解锁或为该项目开启 unlock" with title "Handoff"' 2>>"$LOG" || true
    fi
    log "DEFER: project=$(basename "$proj_dir") task=$task reason=$reason ticks=$ticks"
    DEFERRED=$((DEFERRED + 1))
}

# Unlock-failure cooldown (P0-3): a wrong/expired Keychain password must NOT be
# retried every 60s tick (macOS account lockout). Count consecutive failures;
# once >= threshold set a long next_retry so auto-unlock pauses until the owner
# clears the marker / fixes Keychain.
_unlock_cd_marker() { echo "$1/.unlock-cooldown"; }
unlock_in_cooldown() {
    local m; m=$(_unlock_cd_marker "$1"); [ -f "$m" ] || return 1
    local nr; nr=$(sed -n 's/^next_retry_epoch=//p' "$m" 2>/dev/null | head -1)
    # A4 (full-sweep): a PRESENT-but-corrupt cooldown marker (missing/non-numeric
    # next_retry_epoch — e.g. a kill mid-write) must fail CLOSED. This marker gates
    # Mac login-password injection; an unparseable value formerly fell through to
    # "not in cooldown" (fail-OPEN) and re-attempted unlock. Treat it as in-cooldown
    # (pause auto-unlock until the owner clears it). Absent marker = genuinely not
    # in cooldown (handled by the -f test above).
    case "$nr" in ''|*[!0-9]*)
        log "UNLOCK-COOLDOWN-CORRUPT: $(basename "$1") — unparseable next_retry_epoch, failing closed (manual clear of $m)"
        return 0 ;;
    esac
    local now; now=$(/bin/date +%s)
    [ "$now" -lt "$nr" ]
}
unlock_fail_bump() {
    local proj_dir="$1" rc="$2"; local m; m=$(_unlock_cd_marker "$proj_dir")
    local now; now=$(/bin/date +%s); local cnt=0
    [ -f "$m" ] && cnt=$(sed -n 's/^count=//p' "$m" 2>/dev/null | head -1)
    case "$cnt" in ''|*[!0-9]*) cnt=0 ;; esac
    cnt=$((cnt + 1))
    local nr=$now
    # R2 P0: rc=2 from the unlock CLI = a config/env error (no Keychain password,
    # pyobjc missing) — auto-retry can NEVER fix it. Pause until the owner clears
    # the marker (manual-only), don't loop every cooldown window.
    if [ "$rc" = "2" ]; then
        nr=$((now + 3153600000))   # ~100y = effectively permanent / manual-clear
        "$HANDOFF_OSASCRIPT_CMD" -e "display notification \"自动解锁配置错误（无登录密码/环境缺失）— 已停用自动解锁，须人工修复后清除 .unlock-cooldown\" with title \"Handoff ⛔ 解锁配置\" sound name \"Basso\"" 2>>"$LOG" || true
        log "UNLOCK-CONFIG-ERROR: project=$(basename "$proj_dir") rc=2 — manual-only until marker cleared"
    elif [ "$cnt" -ge "$HANDOFF_UNLOCK_FAIL_THRESHOLD" ]; then
        nr=$((now + HANDOFF_UNLOCK_COOLDOWN))
        "$HANDOFF_OSASCRIPT_CMD" -e "display notification \"自动解锁连续失败 $cnt 次，已暂停自动解锁（密码错/Keychain 过期?），请人工处理\" with title \"Handoff ⚠️ 解锁\" sound name \"Basso\"" 2>>"$LOG" || true
        log "UNLOCK-COOLDOWN: project=$(basename "$proj_dir") count=$cnt rc=$rc — pause auto-unlock until $nr"
    fi
    printf 'count=%s\nlast_epoch=%s\nnext_retry_epoch=%s\nlast_rc=%s\n' "$cnt" "$now" "$nr" "$rc" > "$m"
}
unlock_fail_reset() { rm -f "$(_unlock_cd_marker "$1")" 2>/dev/null || true; }

# Global unlock mutex (P0-2): one unlock at a time across concurrent launchd ticks
# so a 2nd tick never injects the password into an already-unlocked / wrong window.
GLOBAL_UNLOCK_LOCK="$HANDOFF_ROOT/.unlock.lock"
acquire_unlock_lock() {
    if [ -d "$GLOBAL_UNLOCK_LOCK" ]; then
        local pid; pid=$(cat "$GLOBAL_UNLOCK_LOCK/pid" 2>/dev/null)
        case "$pid" in ''|*[!0-9]*) pid="" ;; esac
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then return 1; fi
        local mt; mt=$(_u_mtime "$GLOBAL_UNLOCK_LOCK"); local now; now=$(/bin/date +%s)
        if [ -n "$mt" ] && [ "$((now - mt))" -le 180 ]; then return 1; fi
        rm -f "$GLOBAL_UNLOCK_LOCK/pid" 2>/dev/null; rmdir "$GLOBAL_UNLOCK_LOCK" 2>/dev/null
    fi
    mkdir "$GLOBAL_UNLOCK_LOCK" 2>/dev/null || return 1
    echo "$$" > "$GLOBAL_UNLOCK_LOCK/pid" 2>/dev/null || true
    return 0
}
release_unlock_lock() {
    rm -f "$GLOBAL_UNLOCK_LOCK/pid" 2>/dev/null || true
    rmdir "$GLOBAL_UNLOCK_LOCK" 2>/dev/null || true
}

# Effective re-lock command: explicit HANDOFF_RELOCK_CMD, else derive from the
# unlock cmd by swapping --unlock→--lock (the MP unlock CLI supports both). Empty
# only if neither is available — in which case we must NOT have unlocked (guarded
# at the call site so we never strand the Mac unlocked).
effective_relock_cmd() {
    if [ -n "$HANDOFF_RELOCK_CMD" ]; then printf '%s' "$HANDOFF_RELOCK_CMD"; return 0; fi
    case "$HANDOFF_UNLOCK_CMD" in
        *--unlock*) printf '%s' "${HANDOFF_UNLOCK_CMD/--unlock/--lock}" ;;
        *) printf '' ;;
    esac
}

# Re-lock after a run WE unlocked (R2 P0-3 / P1-5): mandatory + verified. On any
# failure: loud notification + a durable .relock-failed marker + set RELOCK_FAILED
# so the loop stops launching further sessions (never leave the Mac silently
# unlocked + keep spawning).
RELOCK_FAILED=0
do_relock() {
    local cmd; cmd=$(effective_relock_cmd)
    if [ -z "$cmd" ]; then
        RELOCK_FAILED=1; : > "$HANDOFF_ROOT/.relock-failed" 2>/dev/null || true
        "$HANDOFF_OSASCRIPT_CMD" -e 'display notification "自动解锁后无重新锁屏命令 — 屏幕仍解锁，已停止后续接续，请人工锁屏" with title "Handoff ⛔ 无法重锁" sound name "Basso"' 2>>"$LOG" || true
        log "RELOCK-FAIL: no relock command — screen left UNLOCKED; halting further spawns"
        return 1
    fi
    run_with_timeout "$HANDOFF_RELOCK_TIMEOUT" $cmd >>"$LOG" 2>&1
    sleep 1
    if ! screen_is_locked; then
        RELOCK_FAILED=1; : > "$HANDOFF_ROOT/.relock-failed" 2>/dev/null || true
        "$HANDOFF_OSASCRIPT_CMD" -e 'display notification "自动接续后重新锁屏失败 — 屏幕可能仍解锁，已停止后续接续，请人工锁屏" with title "Handoff ⚠️ 重锁失败" sound name "Basso"' 2>>"$LOG" || true
        log "RELOCK-FAIL: screen not re-locked; halting further spawns"
        return 1
    fi
    return 0
}

# Per-iteration cleanup: stop the held caffeinate + re-lock if WE unlocked. Must
# run before every `continue` AFTER the unlock gating, and at iteration end.
CAFF_PID=""
UNLOCKED_BY_US=0
UNLOCK_LOCK_HELD=0
MAY_NEED_RELOCK=0   # Gate0b P1: set BEFORE the unlock CLI runs (race-window guard)
_post_iter_cleanup() {
    # Re-lock (while still holding the mutex + caffeinate) if WE unlocked, then
    # drop caffeinate, then release the global unlock mutex last (P0-2: the mutex
    # spans the whole unlock→submit→relock critical section).
    #
    # Two relock triggers (Gate0b P1 — close the signal race): the normal
    # UNLOCKED_BY_US flag, OR — covering a TERM/EXIT that lands AFTER the unlock
    # CLI injected+unlocked but BEFORE UNLOCKED_BY_US was set — MAY_NEED_RELOCK
    # (an unlock was attempted this iteration) while the screen is NOT currently
    # locked. `! screen_is_locked` is true for both unlocked AND unknown, so an
    # undecidable probe still fails CLOSED (attempt relock; do_relock verifies and
    # marks .relock-failed if it can't confirm a re-lock). If the attempt left the
    # screen still locked we never unlocked → no relock (and a synthetic keystroke
    # against a lock screen is forbidden anyway).
    if [ "$UNLOCKED_BY_US" = "1" ]; then
        do_relock
    elif [ "$MAY_NEED_RELOCK" = "1" ] && ! screen_is_locked; then
        do_relock
    fi
    UNLOCKED_BY_US=0
    MAY_NEED_RELOCK=0
    [ -n "$CAFF_PID" ] && kill "$CAFF_PID" 2>/dev/null
    CAFF_PID=""
    [ "$UNLOCK_LOCK_HELD" = "1" ] && { release_unlock_lock; UNLOCK_LOCK_HELD=0; }
}

# A2 (full-sweep): a signal/exit trap GUARANTEES we never leave the Mac unlocked,
# leak the global unlock mutex, or orphan caffeinate if the launcher is killed
# (launchd unload / SIGTERM / SIGINT / SIGHUP) AFTER we auto-unlocked but BEFORE
# the normal per-iteration cleanup ran. _post_iter_cleanup is idempotent: in the
# normal exit path UNLOCKED_BY_US/CAFF_PID/UNLOCK_LOCK_HELD are already reset so
# the EXIT trap is a no-op; only an interrupted critical section has work to undo
# (re-lock via do_relock, kill caffeinate, release the mutex). Red-line ②.
_on_terminate() {
    trap - EXIT HUP INT TERM   # disarm so the handler can't re-enter itself
    _post_iter_cleanup
    exit "${1:-143}"
}
trap '_post_iter_cleanup' EXIT
trap '_on_terminate 129' HUP
trap '_on_terminate 130' INT
trap '_on_terminate 143' TERM

# ── djs-jump-return (2026-06-14): return-to-origin after spawn (MP-style locate-act-return) ──
# A coordinator on desktop A spawns a worker while the owner works on desktop B. The worker is
# born on A (code-router's SPAWNER_FOCUS focus-jump), then — AFTER the whole prompt-inject + Enter
# sequence finishes on A — the view snaps back to B (owner barely notices, never gets dragged).
# The return MUST run AFTER URI+Enter: the inject needs the worker window frontmost on A. Part B
# ran it INSIDE code-router (BEFORE inject) → the prompt landed in the wrong window on B / AXRaise
# cancelled the return (the placement bug this closes — p19, both external brains missed it). The
# whole orchestration lives HERE now (watchdog-exclusive, so a human's hand-typed `code` is never
# affected). Every step is fail-open: any failure/timeout never blocks the spawn, never tears the
# desktop, never cascades — it degrades to "stay on the current desktop" (worker on A, no return =
# an acceptable downgrade, far better than a prompt in the wrong window). Default ON; disable via
# env HANDOFF_RETURN_AFTER_SPAWN∈{0,false,no,off} or the file ~/.vscode-spaces/return-after-spawn.off.
_return_enabled() {
    case "${HANDOFF_RETURN_AFTER_SPAWN:-}" in 0|false|no|FALSE|NO|off|OFF) return 1 ;; esac
    [ -f "$HOME/.vscode-spaces/return-after-spawn.off" ] && return 1
    return 0
}

# vscode-spaces.py (the return primitives) sits beside the router. Resolvable ONLY when
# HANDOFF_CODE_BIN points at the router — the one case an outbound focus-jump happens. Any other
# CODE_BIN (a plain `code`) has no sibling .py → return 1 → caller fail-opens (no return leg).
_return_spaces_py() {
    # ${VAR:-} default: HANDOFF_CODE_BIN may be UNSET (manual/debug invocation, a future test
    # harness, a plist regression) — under `set -u` (line 24) a bare $HANDOFF_CODE_BIN would abort
    # the WHOLE watchdog run on "unbound variable", violating the fail-open red line. The :- makes
    # an unset value cleanly disarm the return leg (return 1) instead. Mirrors line 80's pattern.
    [ -n "${HANDOFF_CODE_BIN:-}" ] || return 1
    local _p
    _p="$(dirname "$HANDOFF_CODE_BIN")/vscode-spaces.py"
    [ -f "$_p" ] || return 1
    printf '%s' "$_p"
}

# precapture — call BEFORE the outbound jump (`$CODE_BIN -n`). Snapshots the owner's desktop
# (origin = B) + the window set, so the post-inject return can anchor the worker window on A and
# goto back to B. Arms ONLY for a SPAWNER_FOCUS spawn (the direct-jump-spawn scenario this targets)
# with the feature on + the router resolvable; a no-op (stays disarmed) otherwise → byte-for-byte
# legacy behavior. Sets globals _RETURN_ARMED / _RETURN_ORIGIN / _RETURN_BEFORE / _RETURN_PY.
_return_precapture() {
    _RETURN_ARMED=0; _RETURN_ORIGIN=""; _RETURN_BEFORE=""; _RETURN_PY=""
    [ -n "$HANDOFF_SPAWNER_FOCUS" ] || return 0
    _return_enabled || return 0
    _RETURN_PY="$(_return_spaces_py)" || { _RETURN_PY=""; return 0; }
    local _pre
    _pre=$(/usr/bin/python3 "$_RETURN_PY" spawn-precapture 2>>"$LOG") || return 0
    _RETURN_ORIGIN=$(printf '%s\n' "$_pre" | /usr/bin/sed -n 's/^ORIGIN=//p')
    _RETURN_BEFORE=$(printf '%s\n' "$_pre" | /usr/bin/sed -n 's/^BEFORE=//p')
    _RETURN_ARMED=1
}

# return jump — call AFTER URI+Enter dispatch SUCCEEDS (and NOT on the screen-relock defer branch).
# Anchors the just-born worker window on the current desktop (A) then goto-s back to origin (B).
# SYNCHRONOUS by design: the frontmost/AXRaise/inject contention is already over here, so there is
# no desktop race; a background `&` would instead race the NEXT iteration's precapture (re-creating
# a desktop race — the same failure class as the bug). NOTE: --max-wait bounds ONLY the anchor-poll
# loop inside spawn-return; the subsequent goto_desktop is NOT bounded by it (its internal retries
# are bounded, but its osascript subprocess has no timeout — the SAME pre-existing hang exposure the
# outbound focus-jump/goto already carries, NOT newly introduced here). Happy path resolves ~0.2s
# (worker window provably present post-inject). Whether to wrap this call in run_with_timeout for
# consistency with the other slow calls in this file is deferred to codex 6-15 (the timing auditor).
# spawn-return always exits 0 (fail-open); `|| true` is belt-and-suspenders.
_return_jump_back() {
    [ "$_RETURN_ARMED" = "1" ] || return 0
    [ -n "$_RETURN_PY" ] || return 0
    # mp-locate-return P2-live-2: pass the identity token ($1 = the same _submit_token the submit guard
    # asserted in the front window's title) so spawn-return anchors on OUR worker (title carries the
    # token), not merely "a new window on A" — kills the presence-not-identity / empty-before misfire.
    local _anchor="${1:-}"
    # --max-wait default raised 2.0 → 8.0 (design refinement #2): MP full-chain cold-start title load can
    # exceed 2s; over-waiting only forgoes the "return to B" nicety (worker safe on A), tearing is worse.
    /usr/bin/python3 "$_RETURN_PY" spawn-return \
        --origin="${_RETURN_ORIGIN:--1}" --before="$_RETURN_BEFORE" \
        --anchor-token="$_anchor" \
        --max-wait="${HANDOFF_RETURN_MAX_WAIT:-8.0}" >>"$LOG" 2>&1 || true
    _RETURN_ARMED=0
}

# 遍历所有项目子目录 — main spawn loop (gated by HANDOFF_SKIP_SPAWN).
if [ "$HANDOFF_SKIP_SPAWN" = "1" ]; then
    log "SKIP-SPAWN: HANDOFF_SKIP_SPAWN=1 — skipping main spawn loop (test mode)"
fi
for PROJ_DIR in "$HANDOFF_ROOT"/*/; do
    PROJECT=$(basename "$PROJ_DIR")
    QUEUE="$PROJ_DIR/queue"
    LAUNCHED="$PROJ_DIR/launched"

    [ ! -d "$QUEUE" ] && continue
    [ "$HANDOFF_SKIP_SPAWN" = "1" ] && continue
    [ "$RELOCK_HALT" = "1" ] && continue   # A3: durable .relock-failed halt

    # Per-project Guard: 项目级 STOP_AUTO / done
    if [ -f "$PROJ_DIR/STOP_AUTO" ]; then
        continue
    fi
    if [ -f "$PROJ_DIR/done" ]; then
        continue
    fi

    # Per-project Guard: terminal.enabled → iTerm watchdog 接管本项目, VS Code 路径跳过 (B+C 共存)
    # 默认 OFF (sentinel 不存在则零行为变化)。避免 iTerm + VS Code 两边抢 spawn 同一 task。
    if [ -f "$PROJ_DIR/terminal.enabled" ]; then
        log "SKIP(terminal): $PROJECT 由 iTerm watchdog 接管 (terminal.enabled sentinel)"
        continue
    fi

    mkdir -p "$LAUNCHED"

    # fdv2-fix1 SHOULD (housekeeping): a .focus_contended marker normally dies on URI success,
    # give-up, or its own next retry tick — but a task terminated OUTSIDE the focus-retry path
    # (queue .done guard / a done|failed ack) with the .uri already gone leaves the marker
    # orphaned forever. One pass per project tick: clear markers whose task is finished and has
    # no .uri left to ever clear them. (nullglob is on — an empty glob skips the loop.)
    for _FCM in "$QUEUE"/*.focus_contended; do
        [ -f "$_FCM" ] || continue
        _FCT=$(basename "$_FCM" .focus_contended)
        [ -f "$QUEUE/$_FCT.uri" ] && continue   # live retry chain — not stale
        if [ -f "$QUEUE/$_FCT.done" ] || [ -f "$PROJ_DIR/ack/$_FCT.done" ] || [ -f "$PROJ_DIR/ack/$_FCT.failed" ]; then
            rm -f "$_FCM" 2>/dev/null
            log "FOCUS-HOUSEKEEPING: cleared stale focus_contended marker (task finished, no .uri). project=$PROJECT task=$_FCT"
        fi
    done

    # 遍历项目 queue 内 .uri 文件
    for URI_FILE in "$QUEUE"/*.uri; do
        [ ! -f "$URI_FILE" ] && continue

        TASK=$(basename "$URI_FILE" .uri)

        # Per-task Guards
        [ -f "$QUEUE/$TASK.done" ] && continue
        [ -f "$QUEUE/$TASK.BLOCKED.md" ] && continue

        # Parse URI file: 第一行 WORKSPACE= / 第二行 URI= / (可选)第三行 SPAWNER_FOCUS=
        WORKSPACE=$(grep -m1 '^WORKSPACE=' "$URI_FILE" 2>/dev/null | cut -d= -f2-)
        URI=$(grep -m1 '^URI=' "$URI_FILE" 2>/dev/null | cut -d= -f2-)
        # direct-jump-spawn (2026-06-13): the SPAWNING window's own .handoff.code-workspace abs path,
        # written by `handoff spawn --spawner-focus-path` (validated). EXPORT it (reset every iteration,
        # empty when absent) so the `$CODE_BIN`(=code-router.sh) that opens this worker can natively
        # JUMP to the spawner's desktop first → the worker is born on the active coordinator's Space.
        # Empty / unset → code-router falls back to its existing per-project goto (零行为变化).
        SPAWNER_FOCUS=$(grep -m1 '^SPAWNER_FOCUS=' "$URI_FILE" 2>/dev/null | cut -d= -f2-)
        export HANDOFF_SPAWNER_FOCUS="$SPAWNER_FOCUS"
        # djs-jump-return: reset the return-leg state for THIS task. Unconditional (the warm and
        # _skip_code_n=1 paths never call _return_precapture, so a previous task's arming must not
        # leak into _return_jump_back below). _RETURN_DISPATCHED gates the return jump POSITIVELY
        # (mp-locate-return P2-live-1): it is set ONLY where a submit actually SUCCEEDS (ack
        # `submitted`), so every NOT-truly-dispatched path (screen re-lock, accessibility missing,
        # Enter withheld / no transcript growth, frontmost-not-Code) correctly suppresses the return —
        # the owner is NEVER snapped back to B while a worker tab sits unsubmitted on A.
        _RETURN_ARMED=0; _RETURN_DISPATCHED=0

        if [ -z "$URI" ]; then
            log "WARN: empty URI in $URI_FILE (project=$PROJECT task=$TASK), skipping"
            continue
        fi

        # ── unlock-pivot gating (design §4): the GUI path needs an unlocked screen ──
        CAFF_PID=""
        UNLOCKED_BY_US=0
        UNLOCK_LOCK_HELD=0
        MAY_NEED_RELOCK=0
        screen_is_locked; _LRC=$?
        if [ "$_LRC" = "2" ]; then
            # UNKNOWN lock state ⇒ fail-closed: never GUI-submit blind.
            defer_uri "$PROJ_DIR" "$QUEUE" "$TASK" "lock-unknown"; continue
        fi
        if [ "$_LRC" = "0" ]; then
            # Locked → must auto-unlock first (UI keystrokes are forbidden locked).
            if ! unlock_enabled_for_project "$PROJ_DIR"; then
                defer_uri "$PROJ_DIR" "$QUEUE" "$TASK" "locked-unlock-not-enabled"; continue
            fi
            if unlock_in_cooldown "$PROJ_DIR"; then
                defer_uri "$PROJ_DIR" "$QUEUE" "$TASK" "unlock-cooldown"; continue
            fi
            if [ -z "$HANDOFF_UNLOCK_CMD" ]; then
                defer_uri "$PROJ_DIR" "$QUEUE" "$TASK" "unlock-cmd-unset"; continue
            fi
            # R2 P0-3: never unlock without a way to RE-lock — else we'd strand the
            # Mac unlocked. Require an effective relock cmd (explicit or derived).
            if [ -z "$(effective_relock_cmd)" ]; then
                defer_uri "$PROJ_DIR" "$QUEUE" "$TASK" "relock-cmd-unset"; continue
            fi
            if ! acquire_unlock_lock; then
                defer_uri "$PROJ_DIR" "$QUEUE" "$TASK" "unlock-busy"; continue
            fi
            UNLOCK_LOCK_HELD=1
            # Hold caffeinate across unlock→submit (P1-6: keep system awake so it
            # can't re-lock mid-window). Empty HANDOFF_CAFFEINATE_CMD disables.
            if [ -n "$HANDOFF_CAFFEINATE_CMD" ]; then $HANDOFF_CAFFEINATE_CMD >/dev/null 2>&1 & CAFF_PID=$!; fi
            # Re-probe under the mutex (P0-2): another tick may have unlocked
            # already. Three-state (lock-probe P0-1): only rc=1 (genuinely unlocked)
            # is safe to proceed without unlocking; rc=2 (UNKNOWN — e.g. a Quartz
            # `--status` timeout) must fail CLOSED, never blind-spawn behind a lock.
            screen_is_locked; _RC=$?
            if [ "$_RC" = "2" ]; then
                defer_uri "$PROJ_DIR" "$QUEUE" "$TASK" "lock-unknown-premutex"
                _post_iter_cleanup; continue
            fi
            if [ "$_RC" = "0" ]; then
                # Still locked → auto-unlock. Gate0b P1: from the instant we invoke
                # the unlock CLI a kill must be able to trigger a re-lock (the CLI may
                # unlock before we reach UNLOCKED_BY_US=1); cleanup relocks iff the
                # screen is actually unlocked.
                MAY_NEED_RELOCK=1
                run_with_timeout "$HANDOFF_UNLOCK_TIMEOUT" $HANDOFF_UNLOCK_CMD >>"$LOG" 2>&1; _URC=$?
                # Verify (lock-probe P0-1): ONLY rc=1 (confirmed unlocked) is success.
                # rc=0 (still locked) OR rc=2 (UNKNOWN) ⇒ fail CLOSED (defer + cooldown);
                # never proceed to GUI on an unconfirmed-unlocked screen.
                screen_is_locked; _VRC=$?
                if [ "$_VRC" != "1" ]; then
                    unlock_fail_bump "$PROJ_DIR" "$_URC"
                    defer_uri "$PROJ_DIR" "$QUEUE" "$TASK" "unlock-failed-rc$_URC-verify$_VRC"
                    _post_iter_cleanup   # relocks iff MAY_NEED_RELOCK && screen not locked
                    continue
                fi
                unlock_fail_reset "$PROJ_DIR"
                UNLOCKED_BY_US=1
                log "UNLOCK-OK: project=$PROJECT task=$TASK (rc=$_URC)"
            fi
            # _RC=1 ⇒ another tick already unlocked; proceed to spawn on the
            # genuinely-unlocked screen (mutex held; UNLOCKED_BY_US stays 0).
            # R2 P0-2: HOLD the mutex across claim→submit (released in
            # _post_iter_cleanup) so a 2nd tick can't see the unlocked screen and
            # race the GUI/Enter. The whole locked-path spawn is globally serial.
        fi

        # Atomic claim
        TS=$(date +%s%N)
        LAUNCHED_FILE="$LAUNCHED/$TASK-$TS.txt"
        if ! mv "$URI_FILE" "$LAUNCHED_FILE" 2>/dev/null; then
            log "SKIP: race lost for project=$PROJECT task=$TASK"
            _post_iter_cleanup
            continue
        fi
        # Consuming the .uri clears any prior defer marker for this task.
        rm -f "$QUEUE/$TASK.deferred" 2>/dev/null

        log "TRIGGER: project=$PROJECT task=$TASK workspace=$WORKSPACE"
        _perf_reset   # start the per-segment spawn clock (PERF[...] lines from here to COLD-SUBMIT)

        # Step 1: activate the project window (跨项目 routing 核心)
        # worktree spawn-UX fix (2026-06-03): ONLY a per-session worktree spawn (workspace under
        # */worktrees/*) is a fresh cold window needing the engine-injected .code-workspace open-target
        # (identifiable title + inherited .vscode) + a longer cold-start wait + heartbeat-verified
        # submit. A main-repo workspace stays on the proven warm-window fast path even if the owner
        # keeps a *.code-workspace in its root (R2 Gemini P1-3). `find` takes the pattern as an arg →
        # no shell-glob/nullglob hazard (an unmatched glob under `nullglob` would let `ls` list the CWD).
        OPEN_TARGET="$WORKSPACE"; COLD_WINDOW=0
        case "$WORKSPACE" in
            */worktrees/*)
                COLD_WINDOW=1
                _cws=$(/usr/bin/find "$WORKSPACE" -maxdepth 1 -name '.handoff.code-workspace' 2>/dev/null | /usr/bin/head -1)
                if [ -n "$_cws" ] && [ -f "$_cws" ]; then OPEN_TARGET="$_cws"; fi
                ;;
        esac
        # SINGLE-PANE (non-worktree / 2026-06-08 dual-brain codex+Gemini → owner ruling S): a project
        # opted into config `singlepane_projects` gets a DEFAULT single-editor-pane window WITHOUT
        # git-worktree isolation. The dump wrote a sidecar `$QUEUE/$TASK.singlepane` holding the path of
        # an OUT-OF-TREE generated `.handoff.code-workspace` (folders→the real project dir; window.title
        # carries the task). We open THAT (cold-style `code -n`, so the handoff-helper extension collapses
        # the side bars on load — it guards on the `.handoff.code-workspace` suffix) but keep WORKSPACE =
        # the real repo and SUBMIT via the WARM window-guarded single Enter (token=task), NOT the worktree
        # transcript-retry — the main tree's transcript is shared across windows so a sibling's growth could
        # falsely confirm a submit (codex). Worktree (COLD_WINDOW) wins if a project is somehow in both.
        SINGLEPANE_WINDOW=0
        SINGLEPANE_NONCE=""
        if [ "$COLD_WINDOW" != "1" ]; then
            _sp_sidecar="$QUEUE/$TASK.singlepane"
            if [ -f "$_sp_sidecar" ]; then
                # Phase 2 (spawn-window-unify R2 M1): the sidecar is JSON now
                # {workspace, role, close_policy, spawn_nonce, predecessor_nonce} — `cat` would hand the
                # `[ -f ]` test a JSON blob, not a path. Parse via $HANDOFF_PYTHON_CMD (already the
                # script's python; no jq dependency), printing workspace on line 1 + spawn_nonce on
                # line 2. A legacy plain-path sidecar (pre-migration) is tolerated: the raw text is
                # treated as the workspace path with an empty nonce (→ task-token submit gate, as before).
                _sp_parsed=$("$HANDOFF_PYTHON_CMD" - "$_sp_sidecar" <<'PY' 2>/dev/null
import json, sys
try:
    raw = open(sys.argv[1], encoding="utf-8").read()
except OSError:
    raise SystemExit(0)
try:
    d = json.loads(raw)
except ValueError:
    d = None
if isinstance(d, dict):
    print(d.get("workspace", ""))
    print(d.get("spawn_nonce", "") or "")
else:
    # legacy plain-path sidecar (or non-object JSON): raw text IS the workspace path, no nonce
    print(raw.strip())
    print("")
PY
)
                { IFS= read -r _sp_target; IFS= read -r SINGLEPANE_NONCE; } <<EOF
$_sp_parsed
EOF
                if [ -n "$_sp_target" ] && [ -f "$_sp_target" ]; then
                    OPEN_TARGET="$_sp_target"; SINGLEPANE_WINDOW=1
                else
                    log "SINGLEPANE: sidecar present but workspace file missing ($_sp_target) — warm fallback. task=$TASK"
                fi
            fi
        fi
        # code-r-clobber fix (2026-06-03 / dual-brain codex+Gemini / owner ruling: 分治).
        # The pre-existing `code -r` ("reuse window") FORCE-replaces the last-active window when
        # OPEN_TARGET isn't already open — so a background spawn for project B silently clobbered the
        # owner's focused window belonging to a *different* running project A (observed: a warm
        # `code -r /Private/ledger` at 18:47:17 replaced a focused erp worktree window, freezing that
        # session the same second). Drop `-r` on BOTH paths and split by window kind:
        #   cold (worktree): `-n` forces a NEW dedicated window — config-independent (works regardless
        #                    of window.openFoldersInNewWindow), never reuses/clobbers anything.
        #   warm (main repo): no flag = reuse the project window if already open, else new window;
        #                     under the default openFoldersInNewWindow it never replaces a folder-window.
        if [ -n "$WORKSPACE" ] && [ -d "$WORKSPACE" ]; then
            if [ "$COLD_WINDOW" = "1" ] || [ "$SINGLEPANE_WINDOW" = "1" ]; then
                # ── focus-drift fail-closed v2 (2026-06-10 / wh-coord-10; dual-brain: codex RED closed
                # by the snapshot discriminator, gemini raise-ordering absorbed; owner-approved) ──
                # Window token precedence: the unguessable singlepane spawn_nonce when the sidecar
                # carried one, the task id otherwise (worktree titles carry the task id only).
                _focus_token="$TASK"
                if [ "$SINGLEPANE_WINDOW" = "1" ] && [ -n "$SINGLEPANE_NONCE" ]; then
                    _focus_token="$SINGLEPANE_NONCE"
                fi
                # 2.1 PRE-`code -n` snapshot: ONE osascript enumerates every Code window name, so the
                # post-timeout discriminator can tell "fresh window whose title merely lags" (front
                # window ∉ snapshot → dispatch) from "the owner's OLD window still holds front"
                # (∈ snapshot → fail-closed). The same single call doubles as the retry-tick probe.
                # fdv2-fix1 (dual-brain re-audit MUST): the probe carries a PROBE:OK first-line trust
                # marker. "PROBE:OK + zero WIN: lines" = a LEGAL empty snapshot (Code genuinely has
                # no windows — a first-window spawn still dispatches). NO PROBE:OK (osascript error /
                # AX hang — a cold render is exactly when System Events stalls) = a FAILED probe:
                # _snap_ok=0 makes the 2.4 discriminator unconditionally fail-closed. Pre-fix both
                # shapes parsed as an empty snapshot, so a failed probe made EVERY front window test
                # "fresh" ("∉ empty snapshot") → the URI dispatched into the owner's old window =
                # fail-OPEN on the very contract whose core is "everything else fail-closed".
                _probe_out=$(probe_code_windows)
                _snap_ok=0
                case "$_probe_out" in PROBE:OK*) _snap_ok=1 ;; esac
                _snap_wins=$(printf '%s\n' "$_probe_out" | /usr/bin/sed -n 's/^WIN://p')
                _skip_code_n=0; _front_verified=0; _skip_primary_wait=0
                FOCUS_MARKER="$QUEUE/$TASK.focus_contended"
                if [ -f "$FOCUS_MARKER" ]; then
                    # 2.5 retry tick — minimize focus theft (codex Q2). Reuse the snapshot probe:
                    #   (0) probe FAILED → the window state is UNKNOWN: do NOT `code -n` (a blind
                    #       rebuild against a state we cannot see can stack a duplicate window —
                    #       the pre-fix code read a failed probe as "target gone → rebuild"); go
                    #       straight to the raise + discriminator, which fail-closes on _snap_ok=0
                    #       (fdv2-fix1 MUST);
                    #   (a) target window already FRONT → skip `code -n` (and the waits) entirely;
                    #   (b) target exists in the BACKGROUND → skip `code -n` (its focusing of an
                    #       existing workspace is only a SHOULD-level assumption, never a correctness
                    #       premise), go straight to the hardened raise + a short re-wait;
                    #   (c) target gone (owner closed it) → rebuild via the normal `code -n` path.
                    if [ "$_snap_ok" != "1" ]; then
                        _skip_code_n=1
                        _skip_primary_wait=1
                        log "FOCUS-RETRY: window probe FAILED — unknown window state: skipping code -n, raise + discriminator will fail-closed. project=$PROJECT task=$TASK"
                    else
                        _p_app=$(printf '%s\n' "$_probe_out" | /usr/bin/sed -n 's/^FRONT_APP://p' | /usr/bin/head -1)
                        _p_win=$(printf '%s\n' "$_probe_out" | /usr/bin/sed -n 's/^FRONT_WIN://p' | /usr/bin/head -1)
                        if _wins_contain "$_snap_wins" "$_focus_token" || _wins_contain "$_snap_wins" "$TASK"; then
                            _skip_code_n=1
                            if [ "$_p_app" = "Code" ]; then
                                case "$_p_win" in
                                    *"$_focus_token"*|*"$TASK"*) _front_verified=1 ;;
                                esac
                            fi
                            if [ "$_front_verified" = "1" ]; then
                                log "FOCUS-RETRY: target window already frontmost — skipping code -n + waits. project=$PROJECT task=$TASK"
                            else
                                _skip_primary_wait=1
                            fi
                        else
                            log "FOCUS-RETRY: target window gone (owner closed it?) — rebuilding via code -n. project=$PROJECT task=$TASK"
                        fi
                    fi
                fi
                if [ "$_skip_code_n" != "1" ]; then
                    # djs-jump-return: capture origin (owner's desktop B) + window snapshot BEFORE the
                    # outbound focus-jump, which happens INSIDE `$CODE_BIN`(=code-router) when this is a
                    # SPAWNER_FOCUS spawn. Must precede `$CODE_BIN -n`. No-op unless armed (feature on +
                    # router resolvable + SPAWNER_FOCUS set) → legacy byte-for-byte when disarmed.
                    _return_precapture
                    # `-n` forces a NEW dedicated window opening OPEN_TARGET (worktree's, or the singlepane
                    # generated .handoff.code-workspace) — never reuses/clobbers the owner's window.
                    "$CODE_BIN" -n "$OPEN_TARGET" 2>>"$LOG" || log "WARN: code -n $OPEN_TARGET failed (continue with open)"
                fi
                _perf_mark "$TASK" "code-n"
                # Wait for the fresh window to render + take focus (title carries the task id) BEFORE
                # `open URI`, so the Claude tab lands in THIS window — not a stale/other Code window.
                # `code -n` makes the new window frontmost almost immediately, so the title-match
                # normally hits in ~1-2s. TIMEOUT capped at 3s (2026-06-05 owner: "too long"). The wait is
                # a true /bin/date wall clock (was step-counting that overshot to ~16s — see
                # wait_target_window_frontmost). On timeout: AXRaise fallback + re-wait; if THAT also
                # fails, the 2.4 discriminator below decides dispatch vs fail-closed — the pre-fix
                # "open the URI anyway" assumed timeout ⇒ frontmost == the just-opened window, which a
                # 交棒 (dispatch typed in the OLD window's terminal, owner holding it front) inverts:
                # the URI pasted the prompt into the OLD window (wh-coord-10, log L94919-94929).
                _focus_ok=1
                if [ "$_front_verified" != "1" ]; then
                    _primary_ok=0
                    if [ "$_skip_primary_wait" = "1" ]; then
                        # (0) failed-probe ticks logged their decision above — only the (b)
                        # background-window tick gets this message (fdv2-fix1).
                        if [ "$_snap_ok" = "1" ]; then
                            log "FOCUS-RETRY: target window in background — skipping code -n, straight to raise. project=$PROJECT task=$TASK"
                        fi
                    elif wait_target_window_frontmost "$TASK" "${HANDOFF_WIN_FRONT_SECS:-3}"; then
                        _primary_ok=1
                    else
                        log "PERF[$TASK]: wait-frontmost TIMED OUT (${HANDOFF_WIN_FRONT_SECS:-3}s) → AXRaise fallback"
                    fi
                    if [ "$_primary_ok" != "1" ]; then
                        # fallback: AXRaise THE task window (nonce token first), then re-wait for IT
                        # (not merely the Code app — else the URI/Enter could still target a wrong
                        # window; R2 codex). Best-effort; a miss raises nothing (v2 hardening).
                        raise_task_window "$_focus_token" "$TASK"
                        wait_target_window_frontmost "$TASK" 2 || _focus_ok=0
                    fi
                fi
                _perf_mark "$TASK" "wait-frontmost"
                if [ "$_focus_ok" = "0" ]; then
                    # 2.4 timeout discriminator (the codex-RED closure): wait + raise + re-wait ALL
                    # failed. ONE fresh probe decides:
                    #   frontmost is Code AND its window name ∉ the pre-open snapshot → that IS the
                    #   window we just opened (title binding lags on a cold render) → dispatch (the
                    #   Enter stays nonce/readiness-gated downstream; zero cold-boot regression).
                    #   EVERYTHING else (front window ∈ snapshot = the owner's old window holds
                    #   front; front app not Code; window name unreadable) → fail-closed: never
                    #   paste the prompt into a window we cannot prove is ours.
                    # RESIDUAL RISK (documented per the dual-brain review): a window the owner opened
                    # BY HAND after the snapshot is not in it and gets mis-judged as ours → the URI
                    # lands there; the Enter nonce/readiness gate still withholds = no worse than the
                    # pre-fix behavior.
                    # fdv2-fix1 (dual-brain re-audit MUST): the ∉-snapshot test is only meaningful
                    # when the snapshot actually COMPLETED (_snap_ok=1). A failed probe yields an
                    # empty snapshot in which EVERY window tests "fresh" — pre-fix that dispatched
                    # into the owner's old window (fail-open); now it defers. (A failed
                    # discriminator-time probe needs no extra guard: it leaves _d_app empty ≠ Code.)
                    _d_out=$(probe_code_windows)
                    _d_app=$(printf '%s\n' "$_d_out" | /usr/bin/sed -n 's/^FRONT_APP://p' | /usr/bin/head -1)
                    _d_win=$(printf '%s\n' "$_d_out" | /usr/bin/sed -n 's/^FRONT_WIN://p' | /usr/bin/head -1)
                    _dispatch=0
                    if [ "$_snap_ok" = "1" ] && [ "$_d_app" = "Code" ] && [ -n "$_d_win" ] && \
                       ! printf '%s\n' "$_snap_wins" | /usr/bin/grep -Fxq -- "$_d_win"; then
                        _dispatch=1
                        log "FOCUS-DISCRIMINATOR: front Code window not in the pre-open snapshot → fresh window with a lagging title — dispatching. front_title=$_d_win project=$PROJECT task=$TASK"
                    fi
                    if [ "$_dispatch" != "1" ]; then
                        _matched_by="none"
                        if [ -n "$RAISE_MATCHED_TOKEN" ]; then
                            if [ "$RAISE_MATCHED_TOKEN" = "$TASK" ]; then _matched_by="task"; else _matched_by="nonce"; fi
                        fi
                        # 2.5 bounded retry: a DEDICATED consecutive counter (the generic .deferred
                        # marker is rm'd at every claim — L"Consuming the .uri" above — so its ticks
                        # never accumulate). Cleared when a URI is successfully dispatched; bumped
                        # once per fail-closed pass; ≥ HANDOFF_FOCUS_DEFER_MAX (default 5) → give up.
                        _fc_count=$(/usr/bin/sed -n 's/^count=//p' "$FOCUS_MARKER" 2>/dev/null | /usr/bin/head -1)
                        case "$_fc_count" in ''|*[!0-9]*) _fc_count=0 ;; esac
                        _fc_first=$(/usr/bin/sed -n 's/^first_epoch=//p' "$FOCUS_MARKER" 2>/dev/null | /usr/bin/head -1)
                        _fc_now=$(/bin/date +%s)
                        case "$_fc_first" in ''|*[!0-9]*) _fc_first="$_fc_now" ;; esac
                        _fc_count=$((_fc_count + 1))
                        _fc_max="${HANDOFF_FOCUS_DEFER_MAX:-5}"
                        case "$_fc_max" in ''|*[!0-9]*) _fc_max=5 ;; esac
                        log "FOCUS-CONTENDED: URI WITHHELD (fail-closed) matched_by=$_matched_by snap_ok=$_snap_ok front_app=$_d_app front_title=$_d_win count=$_fc_count/$_fc_max project=$PROJECT task=$TASK"
                        if [ "$_fc_count" -ge "$_fc_max" ]; then
                            # give up: CONSUME the .uri (no restore — no infinite retry); the intent
                            # text stays parked in launched/ for the owner's manual recovery.
                            rm -f "$FOCUS_MARKER" 2>/dev/null
                            write_ack "$PROJ_DIR" "$TASK" "failed" "focus-contended x$_fc_count: URI 未发, 新窗已留在桌面(visible park), 手动恢复: 点击新窗 → 在 Claude 输入框粘贴 queue 里 launched/$TASK-*.txt 的 prompt"
                            log "FOCUS-GIVE-UP: $_fc_count consecutive contended ticks — URI consumed, failed ack written. project=$PROJECT task=$TASK"
                            # owner notification through the SAME 6h throttle file defer_uri uses
                            _nfile="$PROJ_DIR/.deferred-notified"
                            _notify=1
                            if [ -f "$_nfile" ]; then
                                _nmt=$(_u_mtime "$_nfile")
                                [ -n "$_nmt" ] && [ "$((_fc_now - _nmt))" -lt 21600 ] && _notify=0
                            fi
                            if [ "$_notify" = "1" ]; then
                                : > "$_nfile" 2>/dev/null || true
                                "$HANDOFF_OSASCRIPT_CMD" -e 'display notification "接续窗口前台争夺多次未决 — URI 未发，新窗已留桌面，请点击新窗手动粘贴 prompt" with title "Handoff ⚠️ 前台争夺" sound name "Basso"' 2>>"$LOG" || true
                            fi
                            _post_iter_cleanup
                            continue
                        fi
                        printf 'count=%s\nfirst_epoch=%s\n' "$_fc_count" "$_fc_first" > "$FOCUS_MARKER"
                        # hand the intent back for the next tick (the L"re-locked" precedent below)
                        mv "$LAUNCHED_FILE" "$URI_FILE" 2>/dev/null
                        defer_uri "$PROJ_DIR" "$QUEUE" "$TASK" "focus-contended"
                        _post_iter_cleanup
                        continue
                    fi
                fi
                # SINGLE-PANE: the side-bar close moved to AFTER the URI + submit (see the cold submit block
                # below). Closing BEFORE the URI did not survive — the URI re-opens the Claude chat side bar.
            else
                "$CODE_BIN" "$OPEN_TARGET" 2>>"$LOG" || log "WARN: code $OPEN_TARGET failed (continue with open)"
                wait_code_frontmost "${HANDOFF_WIN_FRONT_SECS_WARM:-3}" || sleep 0.4  # frontmost or floor
                _perf_mark "$TASK" "wait-frontmost-warm"
            fi
        else
            log "WARN: WORKSPACE empty/invalid ($WORKSPACE), falling back to frontmost"
        fi

        # SP-SUBMIT baseline (sw-sp-enter-retry): the singlepane confirm signal is a NEW
        # transcript *.jsonl (∉ this set) carrying 🆔<task>. Captured BEFORE the URI dispatch
        # so ANY session file born after it counts as new; the set (not mtime) is the
        # discriminator — old files from a resume/re-dispatch carry the same 🆔 and must
        # never confirm.
        _SP_BASE_JSONLS=""
        [ "$SINGLEPANE_WINDOW" = "1" ] && _SP_BASE_JSONLS=$(singlepane_list_jsonls "$WORKSPACE")
        # Step 2: open URI in the activated workspace
        if "$HANDOFF_OPEN_CMD" "$URI"; then
            log "SUCCESS: spawned Claude tab in project=$PROJECT task=$TASK (archived: $TASK-$TS.txt)"
            write_ack "$PROJ_DIR" "$TASK" "spawned" "open URI success @ $TS"
            # a successfully dispatched URI resets the focus-contended streak (the counter is
            # CONSECUTIVE by contract — focus-drift v2 §2.5; no-op on warm / uncontended spawns)
            rm -f "$QUEUE/$TASK.focus_contended" 2>/dev/null
            SPAWNED=$((SPAWNED + 1))
            _perf_mark "$TASK" "open-uri"
            # PRE-SETTLE baseline (cold only): capture the worktree transcript line count BEFORE the 0.5s settle
            # sleep, so a manual/early Enter DURING that settle is detected as already-grew (rc=3) — not missed
            # (codex+Gemini dual-brain P0 2026-06-06: a base taken after the sleep would already include that growth →
            # the running session mis-acked `failed` → duplicate-window re-trigger). 0/empty when not yet started (normal).
            _COLD_BASE=""
            [ "$COLD_WINDOW" = "1" ] && _COLD_BASE=$(worktree_transcript_lines "$WORKSPACE")
            [ "$COLD_WINDOW" = "1" ] && _perf_mark "$TASK" "transcript-baseline"
            # Step 3: auto-submit (Claude Code URI handler 仅粘贴 prompt 不自动发送 / Anthropic 安全设计)
            # 2026-05-28 codex audit blind-spot #4 修复:
            # 等 sleep 1.5 后必须验证 frontmost app 是 Code 才按 Enter
            # 否则可能按到 finder / 别 app, 触发不可预期行为 (写入文件名 / 触发快捷键等)
            # 2026-06-05 主人诊断的关键简化（去掉一摞画蛇添足的补偿性动作）：
            # URI 粘贴 prompt 后焦点【本来就在中间编辑区 Claude 输入框】→「粘完 → 直接 bare Enter」即提交。
            # 旧逻辑的 claude-vscode.focus chord 会抓空侧栏 CC → Enter 落空 ABORT（主人目视：左侧栏高亮）。
            # 主人第二洞察：粘完 1~2s 就能 Enter，等太久(原 8s)反而给别窗口抢前台的机会 → settle 缩到 2s。
            # 真提交 ~1-2s 内 transcript 就增长；没增长(粘贴还没好/被抢前台)由 cold_submit_with_retry 的
            # 快速重试 + 「mismatch 则 AXRaise 抬回前台」兜底（AXRaise 不破坏输入框焦点，实测坐实）。
            if [ "$COLD_WINDOW" = "1" ]; then
                sleep "${HANDOFF_COLD_RENDER_SECS:-0.5}"   # 主人立法 2026-06-06: 粘完 0.5s 直接 Enter, 之间无任何搅焦动作
            else
                sleep 1.5
            fi
            _perf_mark "$TASK" "settle-sleep"
            # Three-state (lock-probe P0-1): only a CONFIRMED-unlocked screen (rc=1)
            # may receive the synthetic Enter. rc=0 (re-locked mid-window) OR rc=2
            # (UNKNOWN — Quartz probe timeout/error) ⇒ abort the submit; a keystroke
            # into a locked/indeterminate screen is forbidden + a silent no-op.
            # _perf_call times each preflight probe while preserving its exit code + short-circuit:
            # accessibility_trusted runs only when the screen is unlocked, is_frontmost_code only when
            # accessibility is trusted — exactly as the bare elif chain did (the 2026-06-07 +20s suspects).
            _perf_call "$TASK" "screen-is-locked" screen_is_locked; _SRC=$?
            if [ "$_SRC" != "1" ]; then
                # P1-6: screen re-locked (or lock state unconfirmable) during the
                # unlock→submit window. Abort the submit; the tab is open but
                # unsubmitted (visible park, owner finishes on unlock). caffeinate
                # should normally prevent a re-lock.
                log "ABORT-SUBMIT: screen not confirmed unlocked before Enter (rc=$_SRC) — 未按 (tab 已开). project=$PROJECT task=$TASK"
                write_ack "$PROJ_DIR" "$TASK" "failed" "screen not confirmed unlocked before submit Enter (rc=$_SRC)"
                # R2 P1: restore the .uri so a later (unlocked) tick can retry,
                # and mark deferred. The already-open tab stays for audit.
                mv "$LAUNCHED_FILE" "$URI_FILE" 2>/dev/null
                defer_uri "$PROJ_DIR" "$QUEUE" "$TASK" "re-locked-before-submit"
                # mp-locate-return P2-live-1: no _RETURN_DISPATCHED set here → the return jump is
                # suppressed by the positive gate below (nothing truly dispatched; .uri restored for retry).
            elif ! _perf_call "$TASK" "accessibility-trusted" accessibility_trusted; then
                # Skip the doomed keystroke entirely — it would just log a WARN
                # and leave the tab un-submitted. Surface it loudly instead.
                warn_accessibility_once
                log "ABORT-SUBMIT: Accessibility 权限缺失 — Enter 未按 (tab 已开, 需手动按一次). project=$PROJECT task=$TASK"
                write_ack "$PROJ_DIR" "$TASK" "failed" "accessibility-missing: 需手动按 Enter (System Settings → 辅助功能)"
            elif [ "$COLD_WINDOW" = "1" ] || [ "$SINGLEPANE_WINDOW" = "1" ] || _perf_call "$TASK" "is-frontmost-code" is_frontmost_code; then
                # SINGLEPANE short-circuit (sw-sp-enter-retry): mirror of the cold rationale
                # below — singlepane_submit_with_retry's per-press gates (title-nonce atomic
                # first press; title-nonce + focused-input-marker retries) are STRICTLY
                # STRONGER than the app-level is_frontmost_code, and a not-frontmost moment
                # here must flow into the machinery's front-mismatch → raise → retry path
                # (the one-shot "frontmost not Code → give up" abort is exactly the
                # no-second-chance bug being fixed).
                # +20s fix (2026-06-07 spawn-speedup): for the COLD path, SHORT-CIRCUIT past is_frontmost_code.
                # PERF instrumentation proved is_frontmost_code (app==Code only) BLOCKED ~10s under cold window
                # render — yet it is REDUNDANT for cold: cold_submit_with_retry's atomic poll is a STRICTLY
                # STRONGER gate (it asserts, in ONE osascript before the keystroke, that the front WINDOW title
                # AND the focused Claude input value both carry the task token — so a stray Enter still can never
                # land on a wrong window; the multi-window red line is held by the stronger check, not weakened).
                # The `[ "$COLD_WINDOW" = "1" ] ||` short-circuits so the osascript is NEVER run on the cold path;
                # WARM still evaluates is_frontmost_code unchanged (its `else` not-frontmost abort below applies to
                # warm only — a cold not-frontmost case is handled by cold_submit_with_retry's rc=5 withhold).
                # Window-guarded submit. token = task id (cold worktree, .code-workspace title) |
                # workspace name (warm, default title rootName). The window guard (one osascript asserts
                # app=Code AND front window title contains token, then keystroke in the SAME process)
                # closes the TOCTOU gap + never fires a stray Enter onto a wrong window. COLD uses a
                # bounded transcript-GATED retry (cold-start can swallow a single Enter / owner-reported
                # on stage1-10d); WARM submits once (window already rendered). Warm escape hatch
                # HANDOFF_WARM_WINDOW_GUARD=0 → app-level Enter for a custom window.title without rootName.
                _submit_token="$TASK"
                if [ "$SINGLEPANE_WINDOW" = "1" ]; then
                    # singlepane: gate on the unguessable spawn_nonce from the JSON sidecar (R2 M1 TOCTOU)
                    # — the generated workspace title binds project·task·role·nonce, so an exact nonce match
                    # ATOMICALLY proves THIS is the window we launched (a stale/sibling/guessed-task window
                    # carries the task but not the nonce). Fall back to the task token (which the title also
                    # carries) only when the sidecar had no nonce (legacy/parse-fail) — never worse than before.
                    _submit_token="${SINGLEPANE_NONCE:-$TASK}"
                elif [ "$COLD_WINDOW" != "1" ]; then
                    _submit_token=$(basename "$WORKSPACE")   # warm: window.title rootName = the folder basename
                fi
                if [ "$COLD_WINDOW" = "1" ]; then
                    cold_submit_with_retry "$_submit_token" "$WORKSPACE" "$_COLD_BASE"
                    case $? in
                        0)
                            log "AUTO-SUBMIT: Enter + worktree-transcript verified (cold window, auto) for project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "submitted" "Enter + worktree transcript growth verified (cold window, auto)"
                            _RETURN_DISPATCHED=1   # mp-locate-return P2-live-1: real submit → return jump armed
                            ;;
                        3)
                            # already-grew: an external/manual Enter started the session before ours. It IS running →
                            # mark submitted (do NOT re-trigger a duplicate window) but HONEST it was not our auto-Enter.
                            log "AUTO-SUBMIT: cold session already running via external/manual Enter (NOT script-verified) for project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "submitted" "external/manual Enter started the session before auto-submit — running, NOT script-verified (cold)"
                            _RETURN_DISPATCHED=1   # mp-locate-return P2-live-1: session running (external Enter) → return jump armed
                            ;;
                        2)
                            warn_accessibility_once
                            log "WARN: osascript keystroke failed despite accessibility preflight OK (transient / 权限 mid-run 撤销?) project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "failed" "osascript keystroke failed post-preflight"
                            ;;
                        5)
                            # readiness never arrived: focus never settled on the prompt-bearing input within the
                            # window → Enter WITHHELD (never a blind Enter onto the empty sidebar / a wrong window).
                            log "ABORT-SUBMIT: cold window — focus never settled on the prompt input (the center Claude tab never grabbed focus from the sidebar in time) — Enter WITHHELD, tab 已开, 主人手动按一次 Enter. project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "failed" "readiness never arrived — Enter withheld, manual needed (cold)"
                            ;;
                        *)
                            log "ABORT-SUBMIT: cold window — Enter sent on the VERIFIED prompt input but transcript did NOT grow (unexpected) — tab 已开, 主人手动按一次 Enter. project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "failed" "Enter on verified input but no transcript growth — manual Enter needed (cold)"
                            ;;
                    esac
                    # SINGLE-PANE (2026-06-06 v4 / owner chose codex's onStartupFinished after a dual-brain audit):
                    # the close is NO LONGER fired here. Doing it after the submit made the window stay 3-column
                    # for too long ("等那么久干嘛"). Instead the handoff-helper extension (v0.3.0) collapses the
                    # side bars on `onStartupFinished` — i.e. as soon as the worktree window LOADS, guarded to
                    # `.handoff.code-workspace`. The launcher's `close_sidebars_if_front_window_contains` (the
                    # /singlepane URI) is kept defined as a FALLBACK only — re-enable it here (right after the URI)
                    # if a real spawn is ever observed to re-open a side bar after startup (codex: "only if observed").
                elif [ "$SINGLEPANE_WINDOW" = "1" ]; then
                    # SINGLEPANE bounded submit (sw-sp-enter-retry): replaces the warm one-shot
                    # gate for this path only. NOTE: singlepane deliberately no longer falls
                    # through to the HANDOFF_WARM_WINDOW_GUARD=0 escape hatch below — an
                    # app-level ungated Enter would bypass the spawn_nonce red line.
                    singlepane_submit_with_retry "$_submit_token" "$TASK" "$WORKSPACE" "$_SP_BASE_JSONLS"
                    case $? in
                        0)
                            log "AUTO-SUBMIT: Enter + new 🆔-marked transcript jsonl verified (singlepane, auto) for project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "submitted" "Enter + new 🆔-marked transcript jsonl verified (singlepane, auto)"
                            _RETURN_DISPATCHED=1   # mp-locate-return P2-live-1: real submit → return jump armed
                            ;;
                        3)
                            # confirm arrived without our machinery pressing — running, but HONEST
                            log "AUTO-SUBMIT: singlepane session already running via external/manual Enter (NOT script-verified) for project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "submitted" "external/manual Enter started the session before auto-submit — running, NOT script-verified (singlepane)"
                            _RETURN_DISPATCHED=1   # mp-locate-return P2-live-1: session running (external Enter) → return jump armed
                            ;;
                        2)
                            warn_accessibility_once
                            log "WARN: osascript keystroke failed despite accessibility preflight OK (transient / 权限 mid-run 撤销?) project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "failed" "osascript keystroke failed post-preflight"
                            ;;
                        6)
                            log "ABORT-SUBMIT: singlepane ambiguous-after-first-enter — Enter 已按、输入框已空/无标记、轮询窗内无 🆔 新 jsonl — 绝不重按. 主人请核实会话是否已在跑(勿盲按). project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "failed" "ambiguous-after-first-enter: Enter pressed, input then empty/markerless, no new 🆔 jsonl in poll window — NOT re-pressed; 请核实窗口里会话是否已在跑, 没跑再手动按一次 Enter (singlepane)"
                            ;;
                        1)
                            log "ABORT-SUBMIT: singlepane Enter sent but NO new 🆔-marked jsonl within budget — tab 已开, 主人核实/手动按一次 Enter. project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "failed" "Enter sent but no new 🆔-marked jsonl within poll budget — manual check/Enter needed (singlepane)"
                            ;;
                        *)
                            log "ABORT-SUBMIT: singlepane submit exhausted without a safe press (last=${SP_LAST_OUTCOME:-none}) — Enter WITHHELD, tab 已开, 主人手动按一次 Enter. project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "failed" "submit withheld after bounded retries (last=${SP_LAST_OUTCOME:-none}): 手动按一次 Enter (singlepane)"
                            ;;
                    esac
                elif [ "${HANDOFF_WARM_WINDOW_GUARD:-1}" = "0" ]; then
                    # warm escape-hatch: legacy app-level Enter (custom window.title without folder name)
                    if "$HANDOFF_OSASCRIPT_CMD" -e 'tell application "System Events" to tell process "Code" to keystroke return' 2>>"$LOG"; then
                        log "AUTO-SUBMIT: pressed Enter (warm, app-level escape hatch) for project=$PROJECT task=$TASK"
                        write_ack "$PROJ_DIR" "$TASK" "submitted" "Enter sent (warm app-level / window guard off)"
                        _RETURN_DISPATCHED=1   # mp-locate-return P2-live-1: real submit → return jump armed
                    else
                        warn_accessibility_once
                        log "WARN: osascript keystroke failed despite accessibility preflight OK (transient / 权限 mid-run 撤销?) project=$PROJECT task=$TASK"
                        write_ack "$PROJ_DIR" "$TASK" "failed" "osascript keystroke failed post-preflight"
                    fi
                else
                    # warm: single atomic window-guarded submit (warm window is already rendered/ready)
                    submit_enter_if_front_window_contains "$_submit_token"
                    case $? in
                        0)
                            log "AUTO-SUBMIT: pressed Enter (warm, window-guarded '$_submit_token') for project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "submitted" "Enter sent to matched window ($_submit_token)"
                            _RETURN_DISPATCHED=1   # mp-locate-return P2-live-1: real submit → return jump armed
                            ;;
                        2)
                            warn_accessibility_once
                            log "WARN: osascript keystroke failed despite accessibility preflight OK (transient / 权限 mid-run 撤销?) project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "failed" "osascript keystroke failed post-preflight"
                            ;;
                        *)
                            log "ABORT-SUBMIT: front window not '$_submit_token' (focus drift) — tab 已开, 主人手动按一次 Enter. project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "failed" "submit withheld: front window not '$_submit_token' (focus drift)"
                            ;;
                    esac
                fi
            else
                front_app=$("$HANDOFF_OSASCRIPT_CMD" -e 'tell application "System Events" to name of first application process whose frontmost is true' 2>/dev/null)
                log "ABORT-SUBMIT: frontmost is '$front_app' (not Code) — Enter 未按, 主人需手动按一次 Enter"
                write_ack "$PROJ_DIR" "$TASK" "failed" "frontmost was '$front_app' not Code, abort osascript Enter"
            fi
            # djs-jump-return: the URI dispatched (window opened on A + prompt injected) and the whole
            # submit sequence is done — NOW snap the owner's view back to origin (B). mp-locate-return
            # P2-live-1: gated POSITIVELY on _RETURN_DISPATCHED (set ONLY where a submit actually
            # SUCCEEDED, ack `submitted`) — so EVERY un-confirmed-Enter path (screen re-lock, accessibility
            # missing, Enter withheld / no transcript growth, frontmost-not-Code) suppresses the return
            # and the owner is NEVER snapped back while the worker tab sits unsubmitted on A. Armed only
            # for a SPAWNER_FOCUS cold/singlepane spawn; a no-op otherwise. Synchronous + fail-open.
            [ "$_RETURN_DISPATCHED" = "1" ] && _return_jump_back "${_submit_token:-}"
            sleep 0.5  # 防同次 launchd run 内连续 spawn 让主人晕
        else
            log "FAIL: open URI failed for project=$PROJECT task=$TASK, restoring"
            write_ack "$PROJ_DIR" "$TASK" "failed" "open URI failed, restored to queue"
            mv "$LAUNCHED_FILE" "$URI_FILE"
        fi

        # post-iteration: re-lock if WE unlocked + stop caffeinate + release mutex.
        _post_iter_cleanup
        # R2 P1: if re-lock failed, halt further spawns — do not keep launching
        # sessions while the Mac is stuck unlocked.
        if [ "$RELOCK_FAILED" = "1" ]; then
            log "HALT: relock failed — stopping all further spawns this run"
            break 2
        fi
    done
done

if [ $SPAWNED -gt 0 ] || [ $DEFERRED -gt 0 ]; then
    log "DONE: spawned $SPAWNED deferred $DEFERRED task(s) this run (across all projects)"
fi


# ─── helpers for the follow-up overdue scanner ──────────────────────────────
# v5.4 Phase 4d D-4. Designed to be idempotent: missing inputs short-circuit
# instead of erroring out so a partially provisioned project never blocks the
# rest of the loop.

now_iso_utc() {
    # ISO-8601 to-the-second UTC — matches `datetime.now(UTC).isoformat(timespec="seconds")`
    /bin/date -u +"%Y-%m-%dT%H:%M:%S+00:00"
}

# Timezone-correct "is now (UTC) strictly past <deadline>?" — exit 0 = overdue.
# A lexical string compare on ISO-8601 mis-sorts mixed offsets (P0: a
# `+08:00` deadline compared against a `+00:00` 'now' string sorts wrong, and a
# bare-date or `Z`-suffixed deadline sorts wrong too), so delegate the parse to
# python3. Naive (tz-less) deadlines are assumed UTC. Any parse error exits
# non-zero so the caller treats it as "not overdue" (fail-safe: never fabricate
# an overdue gate the next dump would hard-fail on).
iso_now_past_deadline() {
    "$HANDOFF_PYTHON_CMD" - "$1" <<'PY' 2>/dev/null
import sys
from datetime import datetime, timezone
raw = sys.argv[1].strip().replace("Z", "+00:00")
try:
    dt = datetime.fromisoformat(raw)
except ValueError:
    sys.exit(2)
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
sys.exit(0 if datetime.now(timezone.utc) > dt else 1)
PY
}

# very small JSON value extractor: looks for "<key>"\s*:\s*"<value>" or numeric.
# Good enough for the flat one-level schemas we read (old_ready / override.json /
# queue/<task>.singlepane). CONTRACT: these are written COMPACT (no pretty-print) by
# the Python writers (dump.compute_*/maybe_write_singlepane_sidecar, handoff_precheck).
# This awk is line-oriented and stops at the first line carrying the key, so it relies on
# the value sitting on the same line as the key. The singlepane sidecar in particular is
# locked to single-line JSON on the writer side (see dump.maybe_write_singlepane_sidecar)
# — do not switch any of these writers to indent=/pretty without making this tolerant.
json_get() {
    local file="$1"; local key="$2"
    /usr/bin/awk -v key="\"$key\"" '
        BEGIN { found="" }
        {
            i = index($0, key)
            if (!i) next
            rest = substr($0, i + length(key))
            # strip whitespace and colon
            sub(/^[[:space:]]*:[[:space:]]*/, "", rest)
            # quoted value?
            if (substr(rest, 1, 1) == "\"") {
                rest = substr(rest, 2)
                end = index(rest, "\"")
                if (end > 0) {
                    print substr(rest, 1, end - 1)
                    found=1
                    exit
                }
            } else {
                # numeric / bool — strip trailing comma/brace/whitespace
                gsub(/[,}\r\n[:space:]].*/, "", rest)
                print rest
                found=1
                exit
            }
        }
    ' "$file" 2>/dev/null
}


# ─── v5.4 §7.9 — follow-up overdue scanner (runs every invocation) ──────────
# When a P0 task used HANDOFF_RETRO_BYPASS=1 to skip the retro gate, dump-handoff
# writes ack/<task>.retro.override.json carrying a follow_up_retro_task_id and
# follow_up_deadline. The promise: a later session will create the matching
# precheck/<follow_task>.retro.evidence.json before that deadline. If the
# deadline passes without the evidence appearing, this scanner stamps an
# overdue marker the next dump in the same project hard-fails on (exit 6).
#
# Phase C (codex audit gate, spec §6 / §4 module table): the SAME machinery is
# reused for the codex-audit bypass debt — ack/<task>.audit.override.json with
# a follow_up_audit_task_id. scan_overdue_kind is the parameterized core; the
# two kinds differ only in glob suffix / follow-key / marker names. NOTE: the
# codex-audit override *producer* (the bypass sidecar artifact carrying
# follow_up_audit_task_id + follow_up_deadline) is an owner-decision item
# deferred to before Phase D (spec §7.3); until it lands no *.audit.override.json
# files exist, so the codex kind is dormant-but-ready and a strict no-op.

# Is the follow-up debt actually satisfied by the follow-up evidence file?
#   $1 evidence file (already confirmed to exist)
#   $2 require_audit  — "1" = codex-audit kind (needs a real audit), else retro
# Retro debt clears on mere evidence existence. Codex-audit debt is stricter
# (R1 P1-2): the owed audit must have actually run, so the follow-up evidence
# must carry a top-level codex_audit block whose audit_mode is a real (non-
# bypass) mode. This is parsed STRUCTURALLY via python3 (R2 P1): a flat key
# scan (json_get) is spoofable by a stray "audit_mode" elsewhere in the JSON
# (e.g. an extra phase-status field), which would falsely discharge the debt.
# The non-bypass enum is mirrored from handoff_precheck.AUDIT_MODE_* — keep in
# sync. Any parse error / missing block ⇒ not satisfied (fail-safe: keep owing).
follow_up_satisfied() {
    local evid="$1" require_audit="$2"
    [ "$require_audit" = "1" ] || return 0
    "$HANDOFF_PYTHON_CMD" - "$evid" <<'PY' 2>/dev/null
import json
import sys

NON_BYPASS = {"full_codex_audit", "empty_diff_attestation", "docs_only_light_audit"}
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError):
    sys.exit(1)
block = data.get("codex_audit") if isinstance(data, dict) else None
mode = block.get("audit_mode") if isinstance(block, dict) else None
sys.exit(0 if mode in NON_BYPASS else 1)
PY
}

# Generic overdue scanner for one override "kind".
#   $2 glob_suffix   — file suffix after the task id (e.g. "retro.override.json")
#   $3 follow_key     — JSON key holding the follow-up task id
#   $4 marker_suffix  — overdue marker file suffix (e.g. "retro_overdue.txt")
#   $5 audit_suffix   — closing-audit jsonl suffix (e.g. "retro.retry_audit.jsonl")
#   $6 kind_label     — human label for the notification / log line
#   $7 require_audit  — "1" ⇒ follow-up must carry a real (non-bypass) codex audit
# The follow-up clears on precheck/<follow_task>.retro.evidence.json for both
# kinds; the codex kind additionally requires that evidence to prove the audit.
scan_overdue_kind() {
    local proj_dir="$1" glob_suffix="$2" follow_key="$3"
    local marker_suffix="$4" audit_suffix="$5" kind_label="$6" require_audit="${7:-0}"
    local project; project=$(basename "$proj_dir")
    local ack_dir="$proj_dir/ack"
    local precheck_dir="$proj_dir/precheck"
    [ -d "$ack_dir" ] || return 0
    local now_iso; now_iso=$(now_iso_utc)
    for ovr in "$ack_dir"/*."$glob_suffix"; do
        [ -f "$ovr" ] || continue
        local task; task=$(basename "$ovr" ".$glob_suffix")
        local deadline follow_task
        deadline=$(json_get "$ovr" "follow_up_deadline")
        follow_task=$(json_get "$ovr" "$follow_key")
        [ -z "$deadline" ] && continue
        [ -z "$follow_task" ] && continue
        # P0: the follow task id is interpolated into a precheck/<task> evidence
        # path below. Reject anything outside kebab-case so a crafted value
        # (e.g. "../foreign") can't resolve an out-of-tree file and falsely
        # clear the overdue gate.
        case "$follow_task" in
            *[!a-z0-9-]*)
                log "OVERDUE-SKIP: kind=$kind_label project=$project task=$task — unsafe follow_task '$follow_task'"
                continue ;;
        esac
        # P0: timezone-correct overdue check. A lexical compare mis-sorts mixed
        # offsets (a `+08:00` deadline vs the `+00:00` 'now', or a bare-date /
        # `Z`-suffixed deadline), silently disabling the gate. iso_now_past_deadline
        # exits 0=overdue / 1=not-yet / >=2=parse-or-python-failure.
        local odrc; iso_now_past_deadline "$deadline"; odrc=$?
        if [ "$odrc" -ne 0 ]; then
            # rc>=2 means we couldn't decide (bad deadline / python3 missing) —
            # fail safe (don't mark overdue) but log so the gate can't go dark silently.
            [ "$odrc" -ge 2 ] && log "OVERDUE-SCAN-WARN: kind=$kind_label project=$project task=$task — undecidable deadline (rc=$odrc) deadline=$deadline"
            continue
        fi
        local follow_evid="$precheck_dir/$follow_task.retro.evidence.json"
        local audit="$ack_dir/$task.$audit_suffix"
        local overdue_marker="$ack_dir/$task.$marker_suffix"
        if [ -f "$follow_evid" ] && follow_up_satisfied "$follow_evid" "$require_audit"; then
            # Follow-up arrived (and, for codex-audit, actually carries the owed
            # audit): unlink any prior overdue marker + the override (§7.9 解除
            # 条件), then append the closing audit line. An evidence file that
            # does NOT discharge the debt falls through to overdue marking below.
            if [ -f "$overdue_marker" ]; then
                rm -f "$overdue_marker"
                # R4 P2: keep the retro line byte-identical to its pre-Phase-C
                # shape (no `kind` field); only the new codex-audit kind tags it.
                if [ "$kind_label" = "retro" ]; then
                    printf '{"event":"follow-up-closed","follow_task":"%s","closed_at":"%s"}\n' \
                        "$follow_task" "$now_iso" >> "$audit"
                else
                    printf '{"event":"follow-up-closed","kind":"%s","follow_task":"%s","closed_at":"%s"}\n' \
                        "$kind_label" "$follow_task" "$now_iso" >> "$audit"
                fi
            fi
            rm -f "$ovr"
            continue
        fi
        if [ ! -f "$overdue_marker" ]; then
            # P2: atomic first-writer-wins. Two concurrent launchd runs can both
            # pass the -f test above; noclobber makes the redirect fail for all
            # but the first, so only one writer notifies. R4 P2: retro marker
            # bytes are preserved verbatim; only codex-audit adds the `kind` tag.
            if ( set -o noclobber
                 if [ "$kind_label" = "retro" ]; then
                     printf '{"event":"overdue","task":"%s","deadline":"%s","now":"%s"}\n' \
                        "$task" "$deadline" "$now_iso" > "$overdue_marker"
                 else
                     printf '{"event":"overdue","kind":"%s","task":"%s","deadline":"%s","now":"%s"}\n' \
                        "$kind_label" "$task" "$deadline" "$now_iso" > "$overdue_marker"
                 fi ) 2>/dev/null; then
                "$HANDOFF_OSASCRIPT_CMD" -e \
                    "display notification \"Follow-up $kind_label overdue: $task\" with title \"Handoff\"" \
                    2>>"$LOG" || true
                log "OVERDUE: kind=$kind_label project=$project task=$task deadline=$deadline"
                OVERDUE_MARKED=$((OVERDUE_MARKED + 1))
            fi
        fi
    done
}

# v5.4 retro mandate (HANDOFF_RETRO_BYPASS) + Phase C codex-audit bypass share
# the same overdue machinery, differing only by override kind.
scan_overdue_overrides() {
    local proj_dir="$1"
    scan_overdue_kind "$proj_dir" "retro.override.json" "follow_up_retro_task_id" \
        "retro_overdue.txt" "retro.retry_audit.jsonl" "retro" "0"
    scan_overdue_kind "$proj_dir" "audit.override.json" "follow_up_audit_task_id" \
        "audit_overdue.txt" "audit.retry_audit.jsonl" "codex-audit" "1"
}

for PROJ_DIR in "$HANDOFF_ROOT"/*/; do
    [ -d "$PROJ_DIR" ] || continue
    scan_overdue_overrides "$PROJ_DIR"
done


# ─── v4 path-D autoclose — role-gated supervisor-succession close ────────────
# (spawn-window-unify Task 4.1 — restored from 21dad1b, adapted to the Phase 4
# role-gated consumer + Phase 2 JSON sidecar + Phase 1 project spawn lock.)
#
# Opt-in (HANDOFF_AUTOCLOSE_ENABLED / autoclose.enabled sentinel). The watcher
# only fires when a fresh successor tab has been submitted (ack/<task>.submitted),
# its retro evidence is intact (ack/<task>.old_ready — owner's re-enablement gate:
# never close a window that didn't finish its retro), AND the Phase 2 singlepane
# sidecar declares this spawn a `supervisor_succession` carrying the predecessor's
# spawn_nonce. A `worker` (the common case) closes nothing — the parallel worker
# windows accumulate by design. The extension (handleAutoclose, c2ac814) is the
# precise self-targeting actor: only the window whose own title carries
# predecessor_nonce closes itself; everything else fail-closes. The watcher is
# the producer of `?role=supervisor_succession&predecessor_nonce=…` — it never
# decides WHICH window dies, only that a close is warranted.

# epoch mtime of a path. BSD (`stat -f %m`, macOS) first, GNU (`stat -c %Y`,
# Linux CI) fallback — the lock-staleness math must work on both.
mtime_sec() {
    stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null
}

# Break a lock dir older than ttl seconds (a crashed holder must not deadlock the
# project forever). Mirrors handoff_fanout.spawn_lock's TTL stale-break. Idempotent.
clean_stale_lock() {
    local lock="$1" ttl="$2"
    [ -d "$lock" ] || return 0
    local mt; mt=$(mtime_sec "$lock") || return 0
    [ -z "$mt" ] && return 0
    local now; now=$(/bin/date +%s)
    if [ "$((now - mt))" -gt "$ttl" ]; then
        rmdir "$lock" 2>/dev/null || true
    fi
}

# sha256 of a file via whichever helper the host provides (shasum on macOS,
# sha256sum on Linux) — must agree with dump.compute_retro_evidence_hash (plain
# file-bytes sha256) so a hash-tamper is caught.
sha256_file() {
    local f="$1"
    if [ -x "$HANDOFF_SHA256_CMD" ] && [ "$(basename "$HANDOFF_SHA256_CMD")" = "shasum" ]; then
        "$HANDOFF_SHA256_CMD" -a 256 "$f" 2>/dev/null | awk '{print $1}'
    elif command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$f" 2>/dev/null | awk '{print $1}'
    else
        /usr/bin/shasum -a 256 "$f" 2>/dev/null | awk '{print $1}'
    fi
}

# A spawn nonce is secrets.token_hex(8) → exactly 16 lowercase hex chars. Mirror
# the extension's NONCE_RE so we reject a malformed predecessor_nonce HERE (record
# a failure marker) instead of firing a URI the extension would only fail-close on.
is_hex16() {
    case "$1" in
        ""|*[!0-9a-f]*) return 1 ;;
    esac
    [ "${#1}" -eq 16 ]
}

autoclose_enabled_for_project() {
    local proj_dir="$1"
    [ "$HANDOFF_AUTOCLOSE_ENABLED" = "1" ] && return 0
    [ -f "$HANDOFF_ROOT/autoclose.enabled" ] && return 0
    [ -f "$proj_dir/autoclose.enabled" ] && return 0
    return 1
}

# old_ready.schema_version whitelist. Keep in sync with
# handoff_precheck.EVIDENCE_SCHEMA_VERSION (== dump.OLD_READY_SCHEMA_VERSION).
KNOWN_SCHEMA_VERSIONS="5.5.0"
ROLE_SUCCESSION="supervisor_succession"

# Validate the retro + role gates, then fire the helper URI under the project
# spawn lock. Every failure path leaves a `<task>.autoclose_failed.txt` so the
# watcher won't loop on the same task; non-fire SKIP paths (worker / no sidecar /
# BLOCKED) leave NO marker (they may legitimately become fire-able later, e.g. a
# sidecar that lands on the next dump).
try_autoclose() {
    local proj_dir="$1"; local task="$2"
    local project; project=$(basename "$proj_dir")
    local ack="$proj_dir/ack"
    local queue="$proj_dir/queue"
    local old_ready="$ack/$task.old_ready"
    local sidecar="$queue/$task.singlepane"
    local done_marker="$ack/$task.autoclose_done"
    local failed_marker="$ack/$task.autoclose_failed.txt"

    # ── Cheap pre-lock fast path — OPTIMIZATION ONLY. Every value that feeds the URI
    # is (re-)read INSIDE the lock below; nothing here reads a URI-feeding value. These
    # are monotonic / nothing-to-do bail-outs: the done/failed markers never un-set, and
    # a missing old_ready/sidecar simply means "not fire-able this tick" (a later tick
    # re-evaluates once one lands). They let the overwhelmingly common no-candidate tick
    # skip the PROJECT spawn lock (shared with spawn-intent) entirely, so we only contend
    # for the lock when a close is genuinely plausible.
    [ -f "$done_marker" ] && return 0
    [ -f "$failed_marker" ] && return 0
    [ -f "$old_ready" ] || return 0
    [ -f "$sidecar" ] || return 0

    # ── Single critical section under the PROJECT spawn lock (Phase 1 parity; design
    # §6 R2r2-R2). The lock is acquired BEFORE reading the sidecar / old_ready / evidence
    # so the role read, the retro-evidence gate, and the URI emit are ONE atomic critical
    # section. This closes the R2 lock-order TOCTOU: a concurrent spawn-intent (which holds
    # this same lock while it (re)writes $task.singlepane) cannot rewrite the sidecar
    # between our predecessor_nonce read and the URI we fire. The invariant: every value
    # that feeds the URI (predecessor_nonce, spawn_nonce, retro-evidence hash) is read
    # under the lock and cannot change before we emit. Same lock dir + TTL as
    # handoff_fanout.spawn_lock.project_spawn_lock so the close is also mutually exclusive
    # with a concurrent launchd autoclose tick. Autoclose is best-effort: on contention we
    # SKIP (retry next tick) rather than block like the Python CM.
    local lock="$proj_dir/.spawn.lock"
    clean_stale_lock "$lock" "$HANDOFF_SPAWN_LOCK_TTL"
    if ! mkdir "$lock" 2>/dev/null; then
        log "AUTOCLOSE-SKIP: project=$project task=$task — spawn lock held"
        return 0
    fi
    # Single release point: with functrace OFF (set -u only), this RETURN trap fires on
    # EVERY return below — and only when try_autoclose itself returns, never on the nested
    # json_get / sha256_file command substitutions — so each gate may plainly `return 0`
    # and the lock is freed exactly once. Do NOT add explicit rmdir / `trap - RETURN`.
    trap 'rmdir "$lock" 2>/dev/null || true' RETURN

    # ── Re-evaluate the full gate INSIDE the lock. The pre-lock checks above were only a
    # fast path; from here on every check/read is authoritative and lock-protected. Re-read
    # the idempotency sentinels first (another tick may have completed since the fast path),
    # then the manual-hold / terminal gates, then existence (files may have vanished).
    [ -f "$done_marker" ] && return 0
    [ -f "$failed_marker" ] && return 0
    [ -f "$queue/$task.BLOCKED.md" ] && {
        log "AUTOCLOSE-SKIP: project=$project task=$task — BLOCKED.md present"
        return 0
    }
    [ -f "$queue/$task.done" ] && return 0
    [ -f "$old_ready" ] || return 0
    [ -f "$sidecar" ] || return 0

    # ── Role gate (Phase 4 contract): only a supervisor_succession spawn closes a
    # predecessor. role + predecessor_nonce live in the Phase 2 JSON sidecar (read HERE,
    # under the lock — the R2 fix). No sidecar / role!=succession (e.g. role=worker) ⇒
    # silent SKIP, never a failure marker — a worker window legitimately keeps, and a
    # non-singlepane spawn has no sidecar at all. Mirrors the extension's worker-keep /
    # fail-closed semantics.
    local role; role=$(json_get "$sidecar" "role")
    [ "$role" = "$ROLE_SUCCESSION" ] || return 0
    local pred_nonce; pred_nonce=$(json_get "$sidecar" "predecessor_nonce")
    if ! is_hex16 "$pred_nonce"; then
        printf 'task_id: %s\nreason: predecessor_nonce_invalid\npredecessor_nonce: %s\ntime: %s\n' \
            "$task" "$pred_nonce" "$(now_iso_utc)" > "$failed_marker"
        log "AUTOCLOSE-FAIL: project=$project task=$task reason=predecessor_nonce_invalid"
        return 0
    fi
    # The successor's own spawn_nonce → the URI `nonce` (diagnostic; the extension
    # gates on predecessor_nonce, not this). Fall back to old_ready.nonce if the
    # sidecar omitted it (legacy), so the URI always carries something traceable.
    local new_nonce; new_nonce=$(json_get "$sidecar" "spawn_nonce")
    [ -z "$new_nonce" ] && new_nonce=$(json_get "$old_ready" "nonce")

    # ── Retro gate (unchanged from 21dad1b): schema whitelist + evidence integrity.
    local schema; schema=$(json_get "$old_ready" "schema_version")
    if ! printf '%s\n' "$KNOWN_SCHEMA_VERSIONS" | tr ' ' '\n' | grep -Fxq "$schema"; then
        printf 'task_id: %s\nreason: schema_version_unknown\nschema_version: %s\ntime: %s\n' \
            "$task" "$schema" "$(now_iso_utc)" > "$failed_marker"
        log "AUTOCLOSE-FAIL: project=$project task=$task reason=schema_version_unknown ($schema)"
        return 0
    fi

    # Resolve the evidence file: absolute path is the fast path; fall back to the
    # project-relative path (§7.6 portability) when the absolute path is gone.
    local rel_path abs_path declared_hash evidence_file
    declared_hash=$(json_get "$old_ready" "retro_evidence_hash")
    rel_path=$(json_get "$old_ready" "retro_evidence_path")
    abs_path=$(json_get "$old_ready" "retro_evidence_path_absolute")
    if [ -n "$abs_path" ] && [ -f "$abs_path" ]; then
        evidence_file="$abs_path"
    elif [ -n "$rel_path" ] && [ -f "$proj_dir/$rel_path" ]; then
        evidence_file="$proj_dir/$rel_path"
    else
        printf 'task_id: %s\nreason: missing_retro_evidence\nrel_path: %s\nabs_path: %s\ntime: %s\n' \
            "$task" "$rel_path" "$abs_path" "$(now_iso_utc)" > "$failed_marker"
        log "AUTOCLOSE-FAIL: project=$project task=$task reason=missing_retro_evidence"
        return 0
    fi
    local actual_hash; actual_hash=$(sha256_file "$evidence_file")
    if [ -z "$actual_hash" ] || [ "$actual_hash" != "$declared_hash" ]; then
        printf 'task_id: %s\nreason: retro_evidence_invalid\ndeclared: %s\nactual: %s\ntime: %s\n' \
            "$task" "$declared_hash" "$actual_hash" "$(now_iso_utc)" > "$failed_marker"
        log "AUTOCLOSE-FAIL: project=$project task=$task reason=retro_evidence_invalid"
        return 0
    fi

    # ── Pending-intent gate (design §6 临界区①, still under the lock). Atomicity is
    # exact for the lock-held .uri publishers — spawn worker singlepane, spawn worktree,
    # spawn succession singlepane (since t41b-fix1), and dump's singlepane-worker path
    # all publish while holding this same project .spawn.lock, so their publishes cannot
    # race this check. dump's active(non-singlepane)/batch/fan-in publishers do NOT hold
    # the lock yet (known residual; backlog: folded in when dump's publish paths move to
    # the shared module) — against those the gate is best-effort: a .uri landing between
    # this scan and the close decision can slip past one tick. An unconsumed
    # queue/<other>.uri is an in-flight spawn intent the watchdog has not yet mv'ed →
    # launched/ — typically a worker the OLD coordinator dispatched; closing the
    # predecessor now could orphan that dispatch (§6: 关窗不得吞掉在飞派发). SKIP this
    # tick (no marker — same semantics as the lock-contention skip): once the intent is
    # consumed, a later tick re-evaluates and fires. A STALE never-consumed .uri
    # therefore withholds autoclose indefinitely — surfacing/reclaiming that is the §6c
    # reclaim-report/patrol scope, deliberately not this gate's job. The succession's
    # OWN residual .uri is excluded — gating on it would deadlock the very close it
    # belongs to. nullglob (top of script) makes the loop a no-op when no .uri exists.
    # Ranked AFTER the retro gate: §6 makes the retro evidence gate the highest
    # precondition (排在竞态守门之前), so a terminal evidence failure still marks even
    # while an intent is in flight.
    local pending
    for pending in "$queue"/*.uri; do
        [ -f "$pending" ] || continue
        [ "$pending" = "$queue/$task.uri" ] && continue
        log "AUTOCLOSE-SKIP: project=$project task=$task — spawn intent in flight ($(basename "$pending"))"
        return 0
    done

    # ── Fire the helper URI (still under the lock). Injection-safe by construction: `task`
    # and `project` are already-validated slugs (handoff_fanout slug rules), `pred_nonce`
    # passed is_hex16 above, and `new_nonce` is a spawn_nonce (secrets.token_hex shape) or
    # the old_ready nonce — no shell/URL metacharacter can reach the query string, so no
    # extra percent-encoding is required.
    local uri="vscode://dharmaxis.handoff-helper/autoclose?task_id=${task}&nonce=${new_nonce}&project=${project}&role=${ROLE_SUCCESSION}&predecessor_nonce=${pred_nonce}"
    if "$HANDOFF_OPEN_CMD" "$uri" 2>>"$LOG"; then
        printf 'task_id: %s\nnonce: %s\npredecessor_nonce: %s\nrole: %s\nuri: %s\ntime: %s\n' \
            "$task" "$new_nonce" "$pred_nonce" "$ROLE_SUCCESSION" "$uri" "$(now_iso_utc)" > "$done_marker"
        log "AUTOCLOSE: project=$project task=$task uri=$uri"
        AUTOCLOSED=$((AUTOCLOSED + 1))
    else
        printf 'task_id: %s\nnonce: %s\nreason: open_uri_failed\ntime: %s\n' \
            "$task" "$new_nonce" "$(now_iso_utc)" > "$failed_marker"
        log "AUTOCLOSE-FAIL: project=$project task=$task reason=open_uri_failed"
    fi
    # The RETURN trap releases the lock as try_autoclose returns here.
}

for PROJ_DIR in "$HANDOFF_ROOT"/*/; do
    [ -d "$PROJ_DIR" ] || continue
    autoclose_enabled_for_project "$PROJ_DIR" || continue
    ACK_DIR="$PROJ_DIR/ack"
    [ -d "$ACK_DIR" ] || continue
    for SUBMITTED in "$ACK_DIR"/*.submitted; do
        [ -f "$SUBMITTED" ] || continue
        TASK=$(basename "$SUBMITTED" .submitted)
        try_autoclose "$PROJ_DIR" "$TASK"
    done
done

if [ $OVERDUE_MARKED -gt 0 ] || [ $AUTOCLOSED -gt 0 ]; then
    log "DONE: overdue_marked=$OVERDUE_MARKED autoclose=$AUTOCLOSED this run"
fi
