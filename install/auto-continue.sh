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
HANDOFF_PYTHON_CMD="${HANDOFF_PYTHON_CMD:-python3}"

# ── unlock-pivot (lock-screen → auto-unlock → visible GUI; design §4 / codex R1) ──
# The GUI submit (code -r / open / osascript Enter) needs an UNLOCKED screen —
# synthetic keystrokes are forbidden against the macOS lock screen. When locked +
# the project opted in, auto-unlock first (MindPersist's CGEvent password
# injection CLI), then run the visible GUI path so the owner can still audit the
# tab. Locked + not-opted-in / unlock-failed / unknown ⇒ defer (keep .uri, notify,
# resume on unlock) — never a silent dead-stall, never a blind-box. Default OFF.
HANDOFF_UNLOCK_ENABLED="${HANDOFF_UNLOCK_ENABLED:-0}"   # per-project opt-in (P0-1)
HANDOFF_LOCK_CHECK_CMD="${HANDOFF_LOCK_CHECK_CMD:-}"    # tests stub: prints locked|unlocked|*
HANDOFF_IOREG_CMD="${HANDOFF_IOREG_CMD:-/usr/sbin/ioreg}"
HANDOFF_UNLOCK_CMD="${HANDOFF_UNLOCK_CMD:-}"            # e.g. "<mp>/.venv/bin/python -m src.agent.unlock_cli --unlock"
HANDOFF_RELOCK_CMD="${HANDOFF_RELOCK_CMD:-}"            # e.g. "<mp>/.venv/bin/python -m src.agent.unlock_cli --lock"
HANDOFF_UNLOCK_TIMEOUT="${HANDOFF_UNLOCK_TIMEOUT:-90}"  # wall-clock cap for the unlock CLI (P1-5)
HANDOFF_RELOCK_TIMEOUT="${HANDOFF_RELOCK_TIMEOUT:-20}"
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

# 全局 Guard 5: code CLI 必须可用 (workspace routing 核心)
# Skip the strict check when only running the overdue segment since it does
# not touch `code -r`.
if [ "$HANDOFF_SKIP_SPAWN" != "1" ]; then
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

# ─── unlock-pivot helpers (lock-aware GUI gating; defined before the loop) ───

# Lock probe. exit 0=locked / 1=unlocked / 2=UNKNOWN (ioreg failed/empty).
# EMPIRICAL: unlocked macs have CGSSessionScreenIsLocked ABSENT (no `= No`); only
# `= Yes` = locked. key-absent ⇒ UNLOCKED — mapping it to UNKNOWN would defer
# every unlocked machine (100% stall). Only a genuine ioreg failure is UNKNOWN.
screen_is_locked() {
    if [ -n "$HANDOFF_LOCK_CHECK_CMD" ]; then
        case "$("$HANDOFF_LOCK_CHECK_CMD" 2>/dev/null)" in
            locked) return 0 ;; unlocked) return 1 ;; *) return 2 ;;
        esac
    fi
    local out
    out=$("$HANDOFF_IOREG_CMD" -n Root -d1 2>/dev/null) || return 2
    [ -z "$out" ] && return 2
    printf '%s' "$out" | /usr/bin/grep -q '"CGSSessionScreenIsLocked" = Yes' && return 0
    return 1
}

# Unlock opt-in (R2 P0-1: per-project ONLY — NO global sentinel). Auto-unlock
# injects the login password, so every project must be enabled deliberately via
# its own sentinel; there is intentionally no `$HANDOFF_ROOT/unlock.enabled`
# all-projects switch. HANDOFF_UNLOCK_ENABLED (default OFF) is an explicit
# operator/test override only.
unlock_enabled_for_project() {
    local proj_dir="$1"
    [ "$HANDOFF_UNLOCK_ENABLED" = "1" ] && return 0
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
    case "$nr" in ''|*[!0-9]*) return 1 ;; esac
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
_post_iter_cleanup() {
    # Re-lock (while still holding the mutex + caffeinate) if WE unlocked, then
    # drop caffeinate, then release the global unlock mutex last (P0-2: the mutex
    # spans the whole unlock→submit→relock critical section).
    [ "$UNLOCKED_BY_US" = "1" ] && do_relock
    UNLOCKED_BY_US=0
    [ -n "$CAFF_PID" ] && kill "$CAFF_PID" 2>/dev/null
    CAFF_PID=""
    [ "$UNLOCK_LOCK_HELD" = "1" ] && { release_unlock_lock; UNLOCK_LOCK_HELD=0; }
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

    # Per-project Guard: 项目级 STOP_AUTO / done
    if [ -f "$PROJ_DIR/STOP_AUTO" ]; then
        continue
    fi
    if [ -f "$PROJ_DIR/done" ]; then
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
            # Re-probe under the mutex (P0-2): another tick may have unlocked already.
            if screen_is_locked; then
                run_with_timeout "$HANDOFF_UNLOCK_TIMEOUT" $HANDOFF_UNLOCK_CMD >>"$LOG" 2>&1; _URC=$?
                if screen_is_locked; then
                    unlock_fail_bump "$PROJ_DIR" "$_URC"
                    defer_uri "$PROJ_DIR" "$QUEUE" "$TASK" "unlock-failed-rc$_URC"
                    _post_iter_cleanup   # kills caffeinate + releases the mutex (no relock — we never unlocked)
                    continue
                fi
                unlock_fail_reset "$PROJ_DIR"
                UNLOCKED_BY_US=1
                log "UNLOCK-OK: project=$PROJECT task=$TASK (rc=$_URC)"
            fi
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
        if [ -n "$WORKSPACE" ] && [ -d "$WORKSPACE" ]; then
            "$CODE_BIN" -r "$WORKSPACE" 2>>"$LOG" || log "WARN: code -r $WORKSPACE failed (continue with open)"
            sleep 0.4  # 等 VS Code 窗口 frontmost
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
            sleep 1.5  # 等 Claude Code 渲染输入栏 + prompt 粘贴完成
            if screen_is_locked; then
                # P1-6: screen re-locked during the unlock→submit window — a synthetic
                # Enter against the lock screen is forbidden + dangerous. Abort the
                # submit; the tab is open but unsubmitted (visible park, owner finishes
                # on unlock). caffeinate should normally prevent this.
                log "ABORT-SUBMIT: screen re-locked before Enter — 未按 (tab 已开). project=$PROJECT task=$TASK"
                write_ack "$PROJ_DIR" "$TASK" "failed" "screen re-locked before submit Enter"
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
                if "$HANDOFF_OSASCRIPT_CMD" -e 'tell application "System Events" to tell process "Code" to keystroke return' 2>>"$LOG"; then
                    log "AUTO-SUBMIT: pressed Enter for project=$PROJECT task=$TASK"
                    write_ack "$PROJ_DIR" "$TASK" "submitted" "osascript Enter success"
                else
                    # Preflight said trusted but keystroke still failed — transient,
                    # or permission revoked mid-run. Treat as accessibility-class.
                    warn_accessibility_once
                    log "WARN: osascript keystroke failed despite accessibility preflight OK (transient / 权限 mid-run 撤销?) project=$PROJECT task=$TASK"
                    write_ack "$PROJ_DIR" "$TASK" "failed" "osascript keystroke failed post-preflight"
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
