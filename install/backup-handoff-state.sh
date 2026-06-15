#!/usr/bin/env bash
# backup-handoff-state.sh — lightweight MANUAL export of the irreplaceable runtime
# state under ~/.claude-handoff, EXCLUDING the reconstructible bulk.
#
# ─── Why this exists (architecture gap GAP-ANALYSIS §F#2 / B1) ───────────────
# ~/.claude-handoff (~26GB) has ZERO application-level backup: no .git, no export,
# only Time-Machine-*Included* (which masks the fact that there is no recovery
# story). But the 26GB is ~99% reconstructible:
#   • */worktrees   — git worktrees, rebuildable from each repo (`git worktree add`)
#   • *_venv        — Python venvs, reinstallable (`pip install -e`)
#   • *.log         — append-only logs, not state
# The IRREPLACEABLE state is small (~50MB total across all links): in-flight
# queue/.uri, audit evidence, succession sidecars, ack sentinels, locks, config.
# Lose that (with no fresh TM snapshot) → in-flight handoffs, the audit trail, and
# one-shot succession tokens are gone for good.
#
# This script tars JUST that small state so it is cheap to copy off-box / drop into
# a backed-up location. It is deliberately:
#   • MANUAL (no cron / launchd) — run it before risky ops or on a cadence you choose
#   • PARTIAL (excludes the bulk) — NOT a 24GB full mirror (anti-over-engineering)
#   • READ-ONLY on ~/.claude-handoff — never mutates live state (safe for all links)
# Recovery story + cadence guidance: docs/runbook-backup-and-recovery.md
#
# Usage:
#   install/backup-handoff-state.sh [DEST_DIR]      # default DEST = ~/.claude-handoff-backups
#   DEST_DIR defaults outside ~/.claude-handoff on purpose (no self-recursion / bloat).
#
# Env knobs (override defaults; all optional):
#   HANDOFF_HOME            source dir            (default ~/.claude-handoff)
#   HANDOFF_BACKUP_KEEP     retention count       (default 12; older pruned)
#   HANDOFF_BACKUP_MAXMB    leak-guard threshold  (default 500 MB; warn if archive bigger)
set -euo pipefail

HH="${HANDOFF_HOME:-$HOME/.claude-handoff}"
DEST="${1:-$HOME/.claude-handoff-backups}"
KEEP="${HANDOFF_BACKUP_KEEP:-12}"
MAXMB="${HANDOFF_BACKUP_MAXMB:-500}"

if [ ! -d "$HH" ]; then
    echo "❌ backup-handoff-state: 源目录不存在: $HH" >&2
    exit 1
fi
# Refuse to write backups inside the source — that would recursively swallow prior
# archives and defeat the "small" invariant. This guard runs BEFORE any mkdir/chmod so a
# symlinked DEST can never create/touch anything inside the source first (keep the
# "read-only on the source" invariant true).
#
# Two cases, both FAIL-CLOSED (never fall back to an unresolved literal path — that is what
# lets a symlinked DEST/ancestor slip the guard and have mkdir -p write into the source):
#   • DEST already exists (maybe a symlink) → canonicalize DEST itself, so a symlink in the
#     basename is resolved too (e.g. DEST is a symlink pointing into $HH).
#   • DEST does not exist yet → canonicalize its PARENT (which must exist) + basename; if the
#     parent can't be canonicalized we REFUSE (a symlinked ancestor + missing intermediate
#     would otherwise leave a literal path that mkdir -p follows into the source).
_real_hh="$(cd "$HH" && pwd -P)"
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
    if ! _real_dest="$(cd "$DEST" 2>/dev/null && pwd -P)"; then
        echo "❌ backup-handoff-state: DEST 存在但不可进入（dangling symlink / 非目录）: $DEST" >&2; exit 1
    fi
else
    if ! _dest_parent="$(cd "$(dirname "$DEST")" 2>/dev/null && pwd -P)"; then
        echo "❌ backup-handoff-state: DEST 的父目录不存在/不可进入，拒绝（不 fail-open，以防 symlink 祖先绕过自噬守卫）: $(dirname "$DEST")" >&2
        echo "   先创建目标父目录，或指定父目录已存在的 DEST。" >&2
        exit 1
    fi
    _real_dest="$_dest_parent/$(basename "$DEST")"
fi
case "$_real_dest/" in
    "$_real_hh"/*) echo "❌ backup-handoff-state: DEST 落在 $HH 内部（会自吞历史归档 / symlink 绕过）: $_real_dest" >&2; exit 1 ;;
esac
case "$DEST/" in   # literal fast-check too (belt-and-suspenders for the direct case)
    "$HH"/*) echo "❌ backup-handoff-state: DEST 不能在 $HH 内部（会自吞历史归档）: $DEST" >&2; exit 1 ;;
esac

mkdir -p "$DEST"
chmod 700 "$DEST" 2>/dev/null || true   # operational state — keep it owner-only

ts="$(date +%Y%m%dT%H%M%S)"
host="$(hostname -s 2>/dev/null || echo host)"
out="$DEST/claude-handoff-state-$host-$ts.tar.gz"

# Exclude the reconstructible bulk. Both the dir entry and its contents are listed so
# the prune is reliable across bsdtar (macOS) and GNU tar. Patterns match the in-archive
# path (e.g. ".claude-handoff/erp-system/worktrees").
echo "📦 backup-handoff-state: 归档 $HH 的关键小 state（排除 worktrees/venv/log bulk）…" >&2
tar \
    --exclude='*/worktrees'   --exclude='*/worktrees/*' \
    --exclude='*_venv'        --exclude='*_venv/*' \
    --exclude='*.log'         --exclude='tmp.*' \
    --exclude='*.sock' \
    -czf "$out" -C "$(dirname "$HH")" "$(basename "$HH")"
chmod 600 "$out" 2>/dev/null || true

# Leak guard: if the bulk somehow slipped in (an exclude pattern mismatch on a future
# tar), the archive balloons past MAXMB — surface it loudly rather than silently shipping 26GB.
bytes="$(wc -c < "$out" | tr -d ' ')"
mb=$(( bytes / 1024 / 1024 ))
entries="$(tar -tzf "$out" 2>/dev/null | wc -l | tr -d ' ')"
echo "✅ 归档完成: $out" >&2
echo "   大小: ${mb} MB · 条目: ${entries}" >&2
if [ "$mb" -gt "$MAXMB" ]; then
    echo "⚠️  归档 ${mb}MB > 阈值 ${MAXMB}MB — 可能有 bulk 漏进来（检查 exclude 是否对当前 tar 生效）。" >&2
fi
# Self-check: the irreplaceable dirs must actually be present in the archive.
# NB: grep WITHOUT -q (and discard via >/dev/null) so grep consumes the whole stream —
# `tar | grep -q` can leave tar killed by SIGPIPE, which under `pipefail` would inherit a
# non-zero status and fire a spurious "missing" warning on some tar/platform combos.
missing=""
for want in queue ack audits config.json; do
    tar -tzf "$out" 2>/dev/null | grep -E "/$want(/|\$)" >/dev/null || missing="$missing $want"
done
[ -n "$missing" ] && echo "⚠️  归档中未见预期关键项:$missing（源是否真有它们？）" >&2

# Retention: keep the newest $KEEP, prune the rest (lexical sort == chronological, ts is sortable).
# NB: populate via while-read, NOT `mapfile` — macOS ships bash 3.2 which has no mapfile.
archives=()
while IFS= read -r _a; do [ -n "$_a" ] && archives+=("$_a"); done \
    < <(ls -1 "$DEST"/claude-handoff-state-*.tar.gz 2>/dev/null | sort)
n="${#archives[@]}"
if [ "$n" -gt "$KEEP" ]; then
    prune=$(( n - KEEP ))
    for old in "${archives[@]:0:$prune}"; do
        rm -f "$old" && echo "🗑️  prune 旧归档: $(basename "$old")" >&2
    done
fi

echo "$out"   # stdout = the archive path (scriptable)
