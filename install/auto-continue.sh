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
# tests set HANDOFF_SKIP_SPAWN=1 to exercise the new autoclose / overdue
# segments without depending on a live VS Code instance.
HANDOFF_SKIP_SPAWN="${HANDOFF_SKIP_SPAWN:-0}"
# tests set HANDOFF_VSCODE_CHECK=0 to skip the `pgrep "Visual Studio Code"`
# global guard (no-op in CI / headless contexts).
HANDOFF_VSCODE_CHECK="${HANDOFF_VSCODE_CHECK:-1}"
# autoclose feature gate: default OFF, opt-in via this env or via the
# autoclose.enabled sentinel files documented in v4 改进 #6.
HANDOFF_AUTOCLOSE_ENABLED="${HANDOFF_AUTOCLOSE_ENABLED:-0}"
# python3 is a hard dependency of this system (the dump/precheck CLIs are a
# Python package); the overdue scanner uses it for timezone-correct ISO-8601
# comparison. Overridable so tests can point at a specific interpreter.
HANDOFF_PYTHON_CMD="${HANDOFF_PYTHON_CMD:-python3}"

CODE_BIN="${HANDOFF_CODE_BIN:-/usr/local/bin/code}"
[ ! -x "$CODE_BIN" ] && CODE_BIN="/opt/homebrew/bin/code"
# fallback: which code
[ ! -x "$CODE_BIN" ] && CODE_BIN=$(command -v code 2>/dev/null)

log() {
    mkdir -p "$HANDOFF_ROOT" 2>/dev/null
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
}

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
# Skip the strict check when only running autoclose / overdue segments since
# those do not touch `code -r`.
if [ "$HANDOFF_SKIP_SPAWN" != "1" ]; then
    if [ -z "$CODE_BIN" ] || [ ! -x "$CODE_BIN" ]; then
        log "FATAL: code CLI not found (workspace routing unavailable)"
        exit 1
    fi
fi

SPAWNED=0
AUTOCLOSED=0
OVERDUE_MARKED=0
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

        # Atomic claim
        TS=$(date +%s%N)
        LAUNCHED_FILE="$LAUNCHED/$TASK-$TS.txt"
        if ! mv "$URI_FILE" "$LAUNCHED_FILE" 2>/dev/null; then
            log "SKIP: race lost for project=$PROJECT task=$TASK"
            continue
        fi

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
            if ! accessibility_trusted; then
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
    done
done

if [ $SPAWNED -gt 0 ]; then
    log "DONE: spawned $SPAWNED task(s) this run (across all projects)"
fi


# ─── helpers shared by autoclose + overdue scanner ──────────────────────────
# v5.4 Phase 4d D-4. Designed to be idempotent: missing inputs short-circuit
# instead of erroring out so a partially provisioned project never blocks the
# rest of the loop.

now_iso_utc() {
    # ISO-8601 to-the-second UTC — matches `datetime.now(UTC).isoformat(timespec="seconds")`
    /bin/date -u +"%Y-%m-%dT%H:%M:%S+00:00"
}

mtime_sec() {
    # Epoch mtime, portable across BSD/macOS (`stat -f %m`) and GNU/Linux
    # (`stat -c %Y`). Production autoclose only runs on macOS, but the test
    # suite exercises this on Linux CI — a BSD-only form there returns empty,
    # which made clean_stale_lock silently skip recycling (A08 red on ubuntu).
    case "$(uname)" in
        Darwin) /usr/bin/stat -f %m "$1" 2>/dev/null ;;
        *) stat -c %Y "$1" 2>/dev/null ;;
    esac
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

# Release a per-task lock dir created by mkdir + a `pid` ownership file.
release_lock() {
    rm -f "$1/pid" 2>/dev/null || true
    rmdir "$1" 2>/dev/null || true
}

# stale lock cleanup: recycle only when the recorded owner pid is gone AND the
# dir is older than ttl seconds. Checking pid liveness first (P1) stops a slow
# but still-running holder from having its lock stolen on the TTL alone.
clean_stale_lock() {
    local lock="$1"; local ttl="$2"
    [ -d "$lock" ] || return 0
    local pid=""
    [ -f "$lock/pid" ] && pid=$(cat "$lock/pid" 2>/dev/null)
    case "$pid" in ''|*[!0-9]*) pid="" ;; esac  # ignore empty/garbage pid file
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0  # owner alive — never recycle regardless of age
    fi
    local mt; mt=$(mtime_sec "$lock") || return 0
    [ -z "$mt" ] && return 0
    local now; now=$(/bin/date +%s)
    if [ "$((now - mt))" -gt "$ttl" ]; then
        release_lock "$lock"
    fi
}

# sha256 of a file using whichever helper the host provides.
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

scan_overdue_overrides() {
    local proj_dir="$1"
    local project; project=$(basename "$proj_dir")
    local ack_dir="$proj_dir/ack"
    local precheck_dir="$proj_dir/precheck"
    [ -d "$ack_dir" ] || return 0
    local now_iso; now_iso=$(now_iso_utc)
    for ovr in "$ack_dir"/*.retro.override.json; do
        [ -f "$ovr" ] || continue
        local task; task=$(basename "$ovr" .retro.override.json)
        local deadline follow_task
        deadline=$(json_get "$ovr" "follow_up_deadline")
        follow_task=$(json_get "$ovr" "follow_up_retro_task_id")
        [ -z "$deadline" ] && continue
        [ -z "$follow_task" ] && continue
        # P0: follow_up_retro_task_id is interpolated into a precheck/<task>
        # evidence path below. Reject anything outside kebab-case so a crafted
        # value (e.g. "../foreign") can't resolve an out-of-tree file and
        # falsely clear the overdue gate.
        case "$follow_task" in
            *[!a-z0-9-]*)
                log "OVERDUE-SKIP: project=$project task=$task — unsafe follow_task '$follow_task'"
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
            [ "$odrc" -ge 2 ] && log "OVERDUE-SCAN-WARN: project=$project task=$task — undecidable deadline (rc=$odrc) deadline=$deadline"
            continue
        fi
        local follow_evid="$precheck_dir/$follow_task.retro.evidence.json"
        local audit="$ack_dir/$task.retro.retry_audit.jsonl"
        local overdue_marker="$ack_dir/$task.retro_overdue.txt"
        if [ -f "$follow_evid" ]; then
            # Follow-up retro arrived: unlink any prior overdue marker + the
            # override (§7.9 解除条件), then append the closing audit line.
            if [ -f "$overdue_marker" ]; then
                rm -f "$overdue_marker"
                printf '{"event":"follow-up-closed","follow_task":"%s","closed_at":"%s"}\n' \
                    "$follow_task" "$now_iso" >> "$audit"
            fi
            rm -f "$ovr"
            continue
        fi
        if [ ! -f "$overdue_marker" ]; then
            # P2: atomic first-writer-wins. Two concurrent launchd runs can both
            # pass the -f test above; noclobber makes the redirect fail for all
            # but the first, so only one writer notifies.
            if ( set -o noclobber
                 printf '{"event":"overdue","task":"%s","deadline":"%s","now":"%s"}\n' \
                    "$task" "$deadline" "$now_iso" > "$overdue_marker" ) 2>/dev/null; then
                "$HANDOFF_OSASCRIPT_CMD" -e \
                    "display notification \"Follow-up retro overdue: $task\" with title \"Handoff\"" \
                    2>>"$LOG" || true
                log "OVERDUE: project=$project task=$task deadline=$deadline"
                OVERDUE_MARKED=$((OVERDUE_MARKED + 1))
            fi
        fi
    done
}

for PROJ_DIR in "$HANDOFF_ROOT"/*/; do
    [ -d "$PROJ_DIR" ] || continue
    scan_overdue_overrides "$PROJ_DIR"
done


# ─── v5.4 Phase 4d D-4 — autoclose old tab via helper extension URI ─────────
# Opt-in: a session-wide env var, or a sentinel file at the global or
# project level. The watcher only triggers when a fresh new tab has been
# submitted AND the matching ack/<task>.old_ready evidence is present.

autoclose_enabled_for_project() {
    local proj_dir="$1"
    [ "$HANDOFF_AUTOCLOSE_ENABLED" = "1" ] && return 0
    [ -f "$HANDOFF_ROOT/autoclose.enabled" ] && return 0
    [ -f "$proj_dir/autoclose.enabled" ] && return 0
    return 1
}

# Watcher-readable allow-list. MUST track dump.py OLD_READY_SCHEMA_VERSION
# (= handoff_precheck.EVIDENCE_SCHEMA_VERSION). Keep prior versions here so an
# old_ready written by an earlier build still autocloses (P1: backward compat).
# An unknown version fails closed (writes autoclose_failed.txt, logged — never
# closes a tab on a schema it can't verify).
KNOWN_SCHEMA_VERSIONS="5.5.0 v5.4.1 v5.4.0"

# Validate old_ready then trigger the helper URI. All failure paths leave a
# `<task>.autoclose_failed.txt` next to the ack files so the watcher won't
# loop on the same task forever.
try_autoclose() {
    local proj_dir="$1"; local task="$2"
    local project; project=$(basename "$proj_dir")
    local ack="$proj_dir/ack"
    local queue="$proj_dir/queue"
    local locks="$proj_dir/locks"
    local old_ready="$ack/$task.old_ready"
    local done_marker="$ack/$task.autoclose_done"
    local failed_marker="$ack/$task.autoclose_failed.txt"

    [ -f "$done_marker" ] && return 0
    [ -f "$failed_marker" ] && return 0
    [ -f "$queue/$task.BLOCKED.md" ] && {
        log "AUTOCLOSE-SKIP: project=$project task=$task — BLOCKED.md present"
        return 0
    }
    [ -f "$queue/$task.done" ] && return 0
    [ -f "$old_ready" ] || return 0

    # Cheap schema_version whitelist (§7.6 R2 T-B.1).
    local schema; schema=$(json_get "$old_ready" "schema_version")
    if ! printf '%s\n' "$KNOWN_SCHEMA_VERSIONS" | tr ' ' '\n' | grep -Fxq "$schema"; then
        printf 'task_id: %s\nreason: schema_version_unknown\nschema_version: %s\ntime: %s\n' \
            "$task" "$schema" "$(now_iso_utc)" > "$failed_marker"
        log "AUTOCLOSE-FAIL: project=$project task=$task reason=schema_version_unknown ($schema)"
        return 0
    fi

    # Resolve evidence file: absolute path is the fast path; fall back to the
    # relative path rooted at $proj_dir (§7.6 移植性) when the absolute path
    # is gone (different machine / container).
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

    local nonce; nonce=$(json_get "$old_ready" "nonce")

    # P0: task / project / nonce are interpolated unescaped into the helper URI
    # query below. task & project are kebab-constrained upstream (dump-time
    # validate_task_id / validate_project_slug + the .submitted filename), but
    # nonce is operator-supplied — reject any value that could inject extra
    # query params (& # = / space …) so the helper can never be steered onto the
    # wrong tab and close an unrelated session.
    case "$task$project" in
        *[!a-z0-9-]*)
            printf 'task_id: %s\nreason: unsafe_uri_param\nfield: task_or_project\ntime: %s\n' \
                "$task" "$(now_iso_utc)" > "$failed_marker"
            log "AUTOCLOSE-FAIL: project=$project task=$task reason=unsafe_uri_param(task/project)"
            return 0 ;;
    esac
    case "$nonce" in
        *[!A-Za-z0-9._-]*)
            printf 'task_id: %s\nreason: unsafe_uri_param\nfield: nonce\nnonce: %s\ntime: %s\n' \
                "$task" "$nonce" "$(now_iso_utc)" > "$failed_marker"
            log "AUTOCLOSE-FAIL: project=$project task=$task reason=unsafe_uri_param(nonce)"
            return 0 ;;
    esac

    # Per-task lock (§7.3 — locks/<task>.autoclose.lock, 5min stale TTL).
    mkdir -p "$locks"
    local lock="$locks/$task.autoclose.lock"
    clean_stale_lock "$lock" 300
    if ! mkdir "$lock" 2>/dev/null; then
        log "AUTOCLOSE-SKIP: project=$project task=$task — lock held"
        return 0
    fi
    echo "$$" > "$lock/pid" 2>/dev/null \
        || log "AUTOCLOSE-WARN: project=$project task=$task — pid file unwritable (stale-lock detection degraded to TTL)"
    trap 'release_lock "$lock"' RETURN
    # Re-check sentinels after acquiring the lock (TOCTOU defence per v4 #4).
    if [ -f "$done_marker" ] || [ -f "$failed_marker" ]; then
        release_lock "$lock"
        trap - RETURN
        return 0
    fi

    local uri="vscode://dharmaxis.handoff-helper/autoclose?task_id=${task}&nonce=${nonce}&project=${project}"
    if "$HANDOFF_OPEN_CMD" "$uri" 2>>"$LOG"; then
        printf 'task_id: %s\nnonce: %s\nuri: %s\ntime: %s\n' \
            "$task" "$nonce" "$uri" "$(now_iso_utc)" > "$done_marker"
        log "AUTOCLOSE: project=$project task=$task uri=$uri"
        AUTOCLOSED=$((AUTOCLOSED + 1))
    else
        printf 'task_id: %s\nnonce: %s\nreason: open_uri_failed\ntime: %s\n' \
            "$task" "$nonce" "$(now_iso_utc)" > "$failed_marker"
        log "AUTOCLOSE-FAIL: project=$project task=$task reason=open_uri_failed"
    fi
    release_lock "$lock"
    trap - RETURN
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

if [ $AUTOCLOSED -gt 0 ] || [ $OVERDUE_MARKED -gt 0 ]; then
    log "DONE: autoclose=$AUTOCLOSED overdue_marked=$OVERDUE_MARKED this run"
fi
