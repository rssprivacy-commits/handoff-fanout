#!/usr/bin/env bash
# handoff-fanout — idempotent installer.
#
# Drops the following in place (each step is no-op when already installed):
#   1. $HANDOFF_HOME directory tree
#   2. $HANDOFF_HOME/config.json (from install/examples/config.json, if missing)
#   3. git pre-commit hook in the current repo (if any), symlinked to install/git-hooks/pre-commit
#   4. macOS launchd agent for the watchdog (if --no-launchd not given and uname=Darwin)
#   5. installs/refreshes the handoff-helper VS Code extension from the current
#      vsix build (drives single-pane fold, §6c worktree reclaim, and succession
#      window-close — required for the spawn/coordinator UX). --no-extension
#      skips this step.
#
# Pre-req: `pip install handoff-fanout` (provides the `handoff` console script).
#
# Run modes:
#   - from a clone of the repo:      ./install/install.sh
#   - from a curl pipe:              curl -L .../install/install.sh | bash
#     (this script will auto-clone the repo to a temp dir to find the asset files)
#
# Options:
#   --home PATH       use PATH as HANDOFF_HOME (default: $HANDOFF_HOME or ~/.handoff)
#   --no-hooks        skip git pre-commit hook installation
#   --no-launchd      skip launchd plist installation (macOS only step regardless)
#   --no-config       don't write $HANDOFF_HOME/config.json
#   --no-extension    skip installing/refreshing the handoff-helper extension
#   --uninstall       reverse everything this script installs
#   --sync-launcher   push canonical auto-continue.sh → ~/.local/bin + record sha
#                     (keeps the com.dharmaxis.auto-continue runtime copy canonical)
#   --sync-dump       push canonical dump-handoff.py re-exec shim → ~/.local/bin
#                     + record sha (routes the global dump entry to the v5.4 engine)
#   -h | --help       show this message

set -euo pipefail

HANDOFF_HOME_DEFAULT="${HANDOFF_HOME:-$HOME/.handoff}"
HANDOFF_HOME="$HANDOFF_HOME_DEFAULT"
INSTALL_HOOKS=1
INSTALL_LAUNCHD=1
INSTALL_CONFIG=1
INSTALL_EXTENSION=1
UNINSTALL=0
DO_SYNC_LAUNCHER=0
DO_SYNC_DUMP=0
REPO_URL="https://github.com/rssprivacy-commits/handoff-fanout.git"

usage() {
    sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --home)        HANDOFF_HOME="$2"; shift 2 ;;
        --no-hooks)    INSTALL_HOOKS=0; shift ;;
        --no-launchd)  INSTALL_LAUNCHD=0; shift ;;
        --no-config)   INSTALL_CONFIG=0; shift ;;
        --no-extension) INSTALL_EXTENSION=0; shift ;;
        --uninstall)   UNINSTALL=1; shift ;;
        --sync-launcher) DO_SYNC_LAUNCHER=1; shift ;;
        --sync-dump)   DO_SYNC_DUMP=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        *)             echo "Unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# ─── locate install/ asset directory ────────────────────────────────────────
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
if [[ "$SCRIPT_PATH" == "bash" || "$SCRIPT_PATH" == "-" ]]; then
    # Being piped from curl — clone the repo to a temp dir.
    TMPCLONE="$(mktemp -d)"
    trap 'rm -rf "$TMPCLONE"' EXIT
    echo "→ cloning $REPO_URL into $TMPCLONE (curl-piped install mode)"
    git clone --depth 1 "$REPO_URL" "$TMPCLONE/repo" >/dev/null
    ASSET_DIR="$TMPCLONE/repo/install"
else
    ASSET_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
fi

if [[ ! -d "$ASSET_DIR/git-hooks" || ! -d "$ASSET_DIR/launchd" || ! -d "$ASSET_DIR/examples" ]]; then
    echo "❌ asset dir layout looks wrong: $ASSET_DIR" >&2
    echo "   expected git-hooks/, launchd/, examples/ siblings" >&2
    exit 1
fi

# ─── --sync-launcher : push canonical auto-continue.sh to the runtime ────────
# The dharmaxis auto-continue launcher (~/.local/bin/auto-continue.sh, run by
# com.dharmaxis.auto-continue) is a deployed COPY of this repo's canonical
# install/auto-continue.sh. There is no other sync path, so it drifts. This step
# pushes the canonical copy to the runtime location, records its sha as the
# canonical sha (read by the launcher's startup drift guard), and verifies the
# byte-for-byte copy. Standalone (does not run during a normal install).
if [[ $DO_SYNC_LAUNCHER -eq 1 ]]; then
    SRC="$ASSET_DIR/auto-continue.sh"
    DEST="$HOME/.local/bin/auto-continue.sh"
    SHA_FILE="$HOME/.claude-handoff/.auto-continue.canonical.sha"
    if [[ ! -f "$SRC" ]]; then
        echo "❌ canonical launcher missing: $SRC" >&2; exit 1
    fi
    mkdir -p "$HOME/.local/bin" "$HOME/.claude-handoff"
    cp "$SRC" "$DEST" && chmod +x "$DEST"
    shasum "$SRC" | awk '{print $1}' > "$SHA_FILE"
    if [[ "$(shasum "$DEST" | awk '{print $1}')" == "$(cat "$SHA_FILE")" ]]; then
        echo "✓ launcher synced: $DEST == canonical ($(cat "$SHA_FILE"))"
    else
        echo "✗ launcher sync verify FAILED" >&2; exit 1
    fi
    exit 0
fi

# ─── --sync-dump : push canonical dump-handoff.py re-exec shim to the runtime ─
# The global dump entry (~/.local/bin/dump-handoff.py) is a deployed COPY of this
# repo's canonical install/dump-handoff.py. The pre-A4 deployer (ERP
# install-handoff.sh) only copied it when MISSING, so it drifted to a stale
# standalone that never routed to the engine (v5.4 mandate gate silently dead).
# This step force-refreshes the canonical re-exec shim to the runtime location,
# records its sha, and verifies the byte-for-byte copy. Idempotent: re-running
# converges to the canonical content (no skip-if-exists drift). Standalone.
if [[ $DO_SYNC_DUMP -eq 1 ]]; then
    SRC="$ASSET_DIR/dump-handoff.py"
    DEST="$HOME/.local/bin/dump-handoff.py"
    SHA_FILE="$HOME/.claude-handoff/.dump-handoff.canonical.sha"
    if [[ ! -f "$SRC" ]]; then
        echo "❌ canonical dump shim missing: $SRC" >&2; exit 1
    fi
    mkdir -p "$HOME/.local/bin" "$HOME/.claude-handoff"
    cp "$SRC" "$DEST" && chmod +x "$DEST"
    shasum "$SRC" | awk '{print $1}' > "$SHA_FILE"
    if [[ "$(shasum "$DEST" | awk '{print $1}')" == "$(cat "$SHA_FILE")" ]]; then
        echo "✓ dump shim synced: $DEST == canonical ($(cat "$SHA_FILE"))"
    else
        echo "✗ dump shim sync verify FAILED" >&2; exit 1
    fi
    exit 0
fi

# ─── uninstall path ─────────────────────────────────────────────────────────
if [[ $UNINSTALL -eq 1 ]]; then
    echo "→ uninstalling handoff-fanout integrations"
    if [[ "$(uname)" == "Darwin" ]]; then
        PLIST="$HOME/Library/LaunchAgents/com.handoff-fanout.watchdog.plist"
        if [[ -f "$PLIST" ]]; then
            launchctl unload "$PLIST" 2>/dev/null || true
            rm -f "$PLIST"
            echo "  ✓ removed launchd agent"
        fi
    fi
    if git rev-parse --git-dir >/dev/null 2>&1; then
        GIT_DIR="$(git rev-parse --git-dir)"
        HOOK="$GIT_DIR/hooks/pre-commit"
        if [[ -L "$HOOK" ]] && readlink "$HOOK" | grep -q "handoff-fanout"; then
            rm -f "$HOOK"
            echo "  ✓ removed git pre-commit hook in $(pwd)"
        fi
    fi
    CODE_BIN="$(command -v code || true)"
    if [[ -n "$CODE_BIN" ]] && "$CODE_BIN" --list-extensions 2>/dev/null | grep -qx "dharmaxis.handoff-helper"; then
        if "$CODE_BIN" --uninstall-extension dharmaxis.handoff-helper >/dev/null 2>&1; then
            echo "  ✓ uninstalled handoff-helper VS Code extension"
        else
            echo "  ⚠ failed to uninstall handoff-helper extension — remove manually via 'code --uninstall-extension dharmaxis.handoff-helper'"
        fi
    fi
    echo "  (keeping $HANDOFF_HOME and config.json — remove manually if desired)"
    echo "✅ uninstall complete"
    exit 0
fi

# ─── 1. HANDOFF_HOME tree ───────────────────────────────────────────────────
mkdir -p "$HANDOFF_HOME"
echo "✓ HANDOFF_HOME ready: $HANDOFF_HOME"

# ─── 2. config.json ─────────────────────────────────────────────────────────
if [[ $INSTALL_CONFIG -eq 1 ]]; then
    CFG="$HANDOFF_HOME/config.json"
    if [[ -f "$CFG" ]]; then
        echo "✓ $CFG already exists (not overwriting)"
    else
        cp "$ASSET_DIR/examples/config.json" "$CFG"
        echo "✓ wrote $CFG (from template — edit to taste)"
    fi
fi

# ─── 3. git hooks (per-repo): pre-commit (layer-2 guard) + post-commit (runtime auto-sync) ──
if [[ $INSTALL_HOOKS -eq 1 ]]; then
    if git rev-parse --git-dir >/dev/null 2>&1; then
        GIT_DIR="$(git rev-parse --git-dir)"
        # core.hooksPath respect, if set
        CONFIGURED_HOOKS_DIR="$(git config --get core.hooksPath || true)"
        HOOK_BASE="${CONFIGURED_HOOKS_DIR:-$GIT_DIR/hooks}"
        mkdir -p "$HOOK_BASE"
        # _link_hook NAME — symlink $ASSET_DIR/git-hooks/NAME into the repo's hook dir, backing up any
        # pre-existing non-handoff hook. post-commit auto-fires --sync-launcher / --sync-dump when a
        # commit touches the canonical asset, so the deployed runtime copy never drifts behind a fix.
        _link_hook() {
            local name="$1" hp="$HOOK_BASE/$1" src="$ASSET_DIR/git-hooks/$1"
            if [[ ! -f "$src" ]]; then
                echo "⊘ git $name skipped (asset missing: $src)"
                return 0
            fi
            chmod +x "$src"
            if [[ -L "$hp" ]] && readlink "$hp" | grep -q "handoff-fanout"; then
                echo "✓ git $name already symlinked to handoff-fanout"
            else
                if [[ -e "$hp" ]]; then
                    local bak="$hp.bak-$(date +%s)"
                    mv "$hp" "$bak"
                    echo "  (backed up existing $name → $bak)"
                fi
                ln -sfn "$src" "$hp"
                echo "✓ symlinked git $name → $hp"
            fi
        }
        _link_hook pre-commit
        _link_hook post-commit
        # delivery-audit machine gate (2026-06-12, hf pilot): block un-audited pushes to
        # main (pre-push) + warn-only audit_pending marker on un-audited main merges
        # (post-merge). Policy lives in `handoff audit-check`.
        _link_hook pre-push
        _link_hook post-merge
    else
        echo "⊘ git hooks skipped (not in a git repo)"
    fi
fi

# ─── 4. launchd agent (macOS only) ──────────────────────────────────────────
if [[ $INSTALL_LAUNCHD -eq 1 && "$(uname)" == "Darwin" ]]; then
    HANDOFF_BIN="$(command -v handoff || true)"
    if [[ -z "$HANDOFF_BIN" ]]; then
        echo "⚠ no \`handoff\` on PATH — install with \`pip install handoff-fanout\` first"
        echo "  skipping launchd; rerun this script after pip install"
    else
        AGENT_DIR="$HOME/Library/LaunchAgents"
        mkdir -p "$AGENT_DIR"
        PLIST="$AGENT_DIR/com.handoff-fanout.watchdog.plist"
        sed \
            -e "s|@@HANDOFF_BIN@@|$HANDOFF_BIN|g" \
            -e "s|@@HANDOFF_HOME@@|$HANDOFF_HOME|g" \
            "$ASSET_DIR/launchd/com.handoff-fanout.watchdog.plist" > "$PLIST.tmp"
        if [[ -f "$PLIST" ]] && cmp -s "$PLIST.tmp" "$PLIST"; then
            rm -f "$PLIST.tmp"
            echo "✓ launchd plist unchanged"
        else
            mv "$PLIST.tmp" "$PLIST"
            launchctl unload "$PLIST" 2>/dev/null || true
            launchctl load -w "$PLIST"
            echo "✓ loaded launchd agent → $PLIST"
        fi
    fi
elif [[ $INSTALL_LAUNCHD -eq 1 ]]; then
    echo "⊘ launchd skipped (not macOS)"
fi

# ─── 5. handoff-helper VS Code extension — install/refresh the current build ──
# The handoff-helper extension is REQUIRED, not obsolete: the live 0.6.0 build
# drives single-pane fold + §6c worktree reclaim + succession window-close (the
# old "autoclose-only" worldview that motivated removing it is long gone). Install
# the current vsix build if present; NEVER uninstall here (that would strip a live
# extension from anyone running the installer). The vsix is a build artifact
# (gitignored) produced by `npm run package` in extension/, so a fresh curl-piped
# clone won't have it — in that case we warn with the build command and no-op
# rather than fail. (--no-extension skips this step; --uninstall removes it.)
if [[ $INSTALL_EXTENSION -eq 1 ]]; then
    CODE_BIN="$(command -v code || true)"
    EXT_VSIX="$(cd "$ASSET_DIR/.." && pwd)/extension/handoff-helper.vsix"
    if [[ -z "$CODE_BIN" ]]; then
        echo "⊘ extension skipped — 'code' CLI not found (enable VS Code's 'code' command in PATH)"
    elif [[ -f "$EXT_VSIX" ]]; then
        if "$CODE_BIN" --install-extension "$EXT_VSIX" --force >/dev/null 2>&1; then
            echo "✓ installed/refreshed handoff-helper extension from $EXT_VSIX"
        else
            echo "  ⚠ failed to install handoff-helper extension — install manually via 'code --install-extension \"$EXT_VSIX\"'"
        fi
    else
        echo "⊘ extension vsix not found at $EXT_VSIX"
        echo "  build it: (cd \"$(dirname "$EXT_VSIX")\" && npm install && npm run package), then re-run the installer"
    fi
fi

# NOTE (2026-06-07): the old "step 6 — VS Code single-pane keybinding" (cmd+ctrl+alt+9 →
# closeSidebar/closeAuxiliaryBar via install_keybinding.py) was REMOVED. Single-pane is now done
# natively by the dharmaxis.handoff-helper extension on `onStartupFinished`, guarded to
# `.handoff.code-workspace` windows — no keybinding / osascript chord needed. See the extension
# (extension/) and lesson-singlepane-spawn-saga-2026-06-07.

# ─── final summary ──────────────────────────────────────────────────────────
cat <<DONE

✅ handoff-fanout installed.

HANDOFF_HOME: $HANDOFF_HOME
Config:       $HANDOFF_HOME/config.json
Watchdog log: $HANDOFF_HOME/watchdog.log (when running on macOS)

Smoke test:
    handoff --version
    handoff dump --task hello-world --next "demo task" --status active
    ls "$HANDOFF_HOME"/*/queue/ 2>/dev/null || true

Next reads:
    docs/PROTOCOL.md      — wire format spec
    docs/ARCHITECTURE.md  — 5-layer defense walk-through
DONE
