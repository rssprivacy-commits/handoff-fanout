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

# Drift guard (Phase D / Task 2): warn — never abort — when this DEPLOYED copy
# has diverged from the canonical install/auto-continue.sh that `install.sh
# --sync-launcher` last pushed. The mandate-on overdue-debt scanner lives only
# in the canonical copy, so a stale runtime launcher silently never enforces it.
# Non-fatal so a missing/older sha file can never break the接续 loop.
_CANON_SHA_FILE="$HANDOFF_ROOT/.auto-continue.canonical.sha"
if [ -f "$_CANON_SHA_FILE" ]; then
    _self_sha="$("$HANDOFF_SHA256_CMD" "$0" 2>/dev/null | awk '{print $1}')"
    _canon_sha="$(cat "$_CANON_SHA_FILE" 2>/dev/null)"
    if [ -n "$_self_sha" ] && [ -n "$_canon_sha" ] && [ "$_self_sha" != "$_canon_sha" ]; then
        log "⚠ auto-continue.sh drift: running $_self_sha != canonical $_canon_sha — run install.sh --sync-launcher"
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
raise_task_window() {
    local task="$1"
    "$HANDOFF_OSASCRIPT_CMD" -e "tell application \"Visual Studio Code\" to activate" \
        -e "delay 0.3" \
        -e "tell application \"System Events\" to tell process \"Code\"
            repeat with w in windows
                if name of w contains \"${task}\" then
                    perform action \"AXRaise\" of w
                    exit repeat
                end if
            end repeat
        end tell" 2>>"$LOG"
    return 0
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
# 0.2s step (focus/render usually settles in 10s–100s of ms; R2: a 1s step wastes up to ~0.9s/iter).
wait_target_window_frontmost() {
    local task="$1" i=0 attempts=$(( ${2:-8} * 5 ))
    while [ "$i" -lt "$attempts" ]; do
        target_window_frontmost "$task" && return 0
        sleep 0.2
        i=$((i + 1))
    done
    return 1
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

# Cold worktree submit with bounded, transcript-GATED retry (2026-06-03 owner-approved hardening).
# The Claude extension cold-starts slower than the render wait, so a single Enter can be SWALLOWED
# (input box not yet focused) → owner had to press Enter manually (observed: stage1-10d). Retry the
# Enter — but ONLY while the worktree transcript has NOT grown (= prompt not yet submitted / session
# not started). So a retry is the first REAL submit, NEVER a double-submit into an already-started-but-
# slow session (closes Gemini R2 P0-1: the heartbeat was too late to gate this; transcript growth is the
# fast, reliable signal). Each Enter is window-guarded (submit_enter_if_front_window_contains re-asserts
# the task window is frontmost) → never fires onto a wrong window even on retry. rc: 0 = transcript grew
# (submitted) / 2 = osascript keystroke genuinely errored (accessibility) / 1 = attempts exhausted.
cold_submit_with_retry() {
    local token="$1" ws="$2"
    local attempts="${HANDOFF_COLD_SUBMIT_ATTEMPTS:-3}"
    local per="${HANDOFF_COLD_SUBMIT_WAIT_SECS:-8}"   # 3×8s + 6s render ≪ the 120s timeout wrapper (R2)
    local base i=1 w rc cur fw
    base=$(worktree_transcript_lines "$ws")
    # Per-attempt diagnostics (2026-06-05 / dual-brain codex+Gemini Day-1 ask): the old loop logged
    # NOTHING per attempt, so an ABORT couldn't be told apart — Enter SENT but transcript never grew
    # (focus landed on an EMPTY sidebar Claude: claude-vscode.focus skips editor.openLast while a sidebar
    # Claude webview is visible) vs window MISMATCH (focus drifted to a wrong window, no Enter sent). Log
    # rc + the frontmost window name + base→current transcript lines so the next live spawn pins it down.
    log "COLD-SUBMIT-START: token=$token base_lines=$base attempts=$attempts per=${per}s ws=$ws"
    while [ "$i" -le "$attempts" ]; do
        # already submitted (a prior Enter took / transcript grew) → STOP, never double-submit
        cur=$(worktree_transcript_lines "$ws")
        if [ "$cur" -gt "$base" ]; then
            log "COLD-SUBMIT: transcript grew ${base}→${cur} before attempt $i — already submitted, stop (no double-submit)"
            return 0
        fi
        raise_task_window "$token"   # re-focus the task window (drift between attempts) — best-effort
        fw=$(frontmost_code_window_name)   # diagnostic: which Code window is actually frontmost now
        submit_enter_if_front_window_contains "$token" 1; rc=$?   # do_focus=1: focus Claude input first
        log "COLD-SUBMIT-ATTEMPT $i/$attempts: rc=$rc (0=sent/1=mismatch/2=osa-err) front_window='$fw' lines=$base"
        if [ "$rc" = "2" ]; then return 2; fi   # accessibility/keystroke error — escalate, retry won't help
        if [ "$rc" != "0" ]; then
            # mismatch (window not frontmost / focus drift) → NO Enter was sent, so polling the transcript
            # for growth is futile; re-raise next iteration instead of burning `per` secs (R2 P2).
            sleep 2; i=$((i + 1)); continue
        fi
        w=0
        while [ "$w" -lt "$per" ]; do
            cur=$(worktree_transcript_lines "$ws")
            if [ "$cur" -gt "$base" ]; then
                log "COLD-SUBMIT: Enter landed on attempt $i — transcript grew ${base}→${cur} (submitted)"
                return 0
            fi
            sleep 1; w=$((w + 1))
        done
        log "COLD-SUBMIT-ATTEMPT $i: Enter sent (rc=0) but transcript still $base after ${per}s — swallowed or focus on EMPTY sidebar Claude"
        i=$((i + 1))
    done
    return 1
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

    # 遍历项目 queue 内 .uri 文件
    for URI_FILE in "$QUEUE"/*.uri; do
        [ ! -f "$URI_FILE" ] && continue

        TASK=$(basename "$URI_FILE" .uri)

        # Per-task Guards
        [ -f "$QUEUE/$TASK.done" ] && continue
        [ -f "$QUEUE/$TASK.BLOCKED.md" ] && continue

        # Parse URI file: 第一行 WORKSPACE= / 第二行 URI=
        WORKSPACE=$(grep -m1 '^WORKSPACE=' "$URI_FILE" 2>/dev/null | cut -d= -f2-)
        URI=$(grep -m1 '^URI=' "$URI_FILE" 2>/dev/null | cut -d= -f2-)

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
            if [ "$COLD_WINDOW" = "1" ]; then
                "$CODE_BIN" -n "$OPEN_TARGET" 2>>"$LOG" || log "WARN: code -n $OPEN_TARGET failed (continue with open)"
                # Wait for the fresh window to render + take focus (title carries the task id) BEFORE
                # `open URI`, so the Claude tab lands in THIS window — not a stale/other Code window.
                if ! wait_target_window_frontmost "$TASK" "${HANDOFF_WIN_FRONT_SECS:-8}"; then
                    # fallback: AXRaise THE task window, then re-wait for IT (not merely the Code app —
                    # else the URI/Enter could still target a wrong window; R2 codex). Best-effort.
                    raise_task_window "$TASK"; wait_target_window_frontmost "$TASK" 3
                fi
            else
                "$CODE_BIN" "$OPEN_TARGET" 2>>"$LOG" || log "WARN: code $OPEN_TARGET failed (continue with open)"
                wait_code_frontmost "${HANDOFF_WIN_FRONT_SECS_WARM:-3}" || sleep 0.4  # frontmost or floor
            fi
        else
            log "WARN: WORKSPACE empty/invalid ($WORKSPACE), falling back to frontmost"
        fi

        # Step 2: open URI in the activated workspace
        if "$HANDOFF_OPEN_CMD" "$URI"; then
            log "SUCCESS: spawned Claude tab in project=$PROJECT task=$TASK (archived: $TASK-$TS.txt)"
            write_ack "$PROJ_DIR" "$TASK" "spawned" "open URI success @ $TS"
            SPAWNED=$((SPAWNED + 1))
            # Step 3: auto-submit (Claude Code URI handler 仅粘贴 prompt 不自动发送 / Anthropic 安全设计)
            # 2026-05-28 codex audit blind-spot #4 修复:
            # 等 sleep 1.5 后必须验证 frontmost app 是 Code 才按 Enter
            # 否则可能按到 finder / 别 app, 触发不可预期行为 (写入文件名 / 触发快捷键等)
            # 等 Claude Code 渲染输入栏 + prompt 粘贴完成。cold worktree/.code-workspace 新窗口
            # 冷启动 Claude 扩展远超历史 1.5s（吞掉 Enter 的根因）→ cold window 等更久 + 显式把
            # 本 task 的窗口 AXRaise 到前台（多窗口下 is_frontmost_code 只验 app 不验窗口 → Enter
            # 可能落到别项目窗口；实测诊断 Enter 落到了 family-business 窗口）。
            if [ "$COLD_WINDOW" = "1" ]; then
                sleep "${HANDOFF_COLD_RENDER_SECS:-6}"
                raise_task_window "$TASK"
                sleep 1.0  # 让 AXRaise 生效 + Claude tab 输入框聚焦
            else
                sleep 1.5
            fi
            # Three-state (lock-probe P0-1): only a CONFIRMED-unlocked screen (rc=1)
            # may receive the synthetic Enter. rc=0 (re-locked mid-window) OR rc=2
            # (UNKNOWN — Quartz probe timeout/error) ⇒ abort the submit; a keystroke
            # into a locked/indeterminate screen is forbidden + a silent no-op.
            screen_is_locked; _SRC=$?
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
            elif ! accessibility_trusted; then
                # Skip the doomed keystroke entirely — it would just log a WARN
                # and leave the tab un-submitted. Surface it loudly instead.
                warn_accessibility_once
                log "ABORT-SUBMIT: Accessibility 权限缺失 — Enter 未按 (tab 已开, 需手动按一次). project=$PROJECT task=$TASK"
                write_ack "$PROJ_DIR" "$TASK" "failed" "accessibility-missing: 需手动按 Enter (System Settings → 辅助功能)"
            elif is_frontmost_code; then
                # Window-guarded submit. token = task id (cold worktree, .code-workspace title) |
                # workspace name (warm, default title rootName). The window guard (one osascript asserts
                # app=Code AND front window title contains token, then keystroke in the SAME process)
                # closes the TOCTOU gap + never fires a stray Enter onto a wrong window. COLD uses a
                # bounded transcript-GATED retry (cold-start can swallow a single Enter / owner-reported
                # on stage1-10d); WARM submits once (window already rendered). Warm escape hatch
                # HANDOFF_WARM_WINDOW_GUARD=0 → app-level Enter for a custom window.title without rootName.
                _submit_token="$TASK"
                [ "$COLD_WINDOW" != "1" ] && _submit_token=$(basename "$WORKSPACE")
                if [ "$COLD_WINDOW" = "1" ]; then
                    cold_submit_with_retry "$_submit_token" "$WORKSPACE"
                    case $? in
                        0)
                            log "AUTO-SUBMIT: Enter + worktree-transcript verified (cold window) for project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "submitted" "Enter + worktree transcript growth verified (cold window)"
                            ;;
                        2)
                            warn_accessibility_once
                            log "WARN: osascript keystroke failed despite accessibility preflight OK (transient / 权限 mid-run 撤销?) project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "failed" "osascript keystroke failed post-preflight"
                            ;;
                        *)
                            log "ABORT-SUBMIT: cold window — ${HANDOFF_COLD_SUBMIT_ATTEMPTS:-3} Enter attempts, no transcript growth — tab 已开, 主人手动按一次 Enter. project=$PROJECT task=$TASK"
                            write_ack "$PROJ_DIR" "$TASK" "failed" "no transcript growth after retries — manual Enter needed (cold worktree window?)"
                            ;;
                    esac
                elif [ "${HANDOFF_WARM_WINDOW_GUARD:-1}" = "0" ]; then
                    # warm escape-hatch: legacy app-level Enter (custom window.title without folder name)
                    if "$HANDOFF_OSASCRIPT_CMD" -e 'tell application "System Events" to tell process "Code" to keystroke return' 2>>"$LOG"; then
                        log "AUTO-SUBMIT: pressed Enter (warm, app-level escape hatch) for project=$PROJECT task=$TASK"
                        write_ack "$PROJ_DIR" "$TASK" "submitted" "Enter sent (warm app-level / window guard off)"
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
# Good enough for the flat one-level schemas we read (old_ready / override.json).
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

if [ $OVERDUE_MARKED -gt 0 ]; then
    log "DONE: overdue_marked=$OVERDUE_MARKED this run"
fi
