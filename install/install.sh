#!/usr/bin/env bash
# handoff-fanout — idempotent installer.
#
# Drops the following in place (each step is no-op when already installed):
#   1. $HANDOFF_HOME directory tree
#   2. $HANDOFF_HOME/config.json (from install/examples/config.json, if missing)
#   3. git pre-commit hook in the current repo (if any), symlinked to install/git-hooks/pre-commit
#   4. macOS launchd agent for the watchdog (if --no-launchd not given and uname=Darwin)
#   5. handoff-helper VS Code extension for tab autoclose (if --no-extension not
#      given and both `code` and `npm` are on PATH); builds the .vsix from source
#      and `code --install-extension`s it. Idempotent — skips if the same version
#      is already installed.
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
#   --no-extension    skip building/installing the handoff-helper VS Code extension
#   --uninstall       reverse everything this script installs
#   -h | --help       show this message

set -euo pipefail

HANDOFF_HOME_DEFAULT="${HANDOFF_HOME:-$HOME/.handoff}"
HANDOFF_HOME="$HANDOFF_HOME_DEFAULT"
INSTALL_HOOKS=1
INSTALL_LAUNCHD=1
INSTALL_CONFIG=1
INSTALL_EXTENSION=1
UNINSTALL=0
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

# ─── 3. git pre-commit hook (per-repo) ──────────────────────────────────────
if [[ $INSTALL_HOOKS -eq 1 ]]; then
    if git rev-parse --git-dir >/dev/null 2>&1; then
        GIT_DIR="$(git rev-parse --git-dir)"
        # core.hooksPath respect, if set
        CONFIGURED_HOOKS_DIR="$(git config --get core.hooksPath || true)"
        HOOK_BASE="${CONFIGURED_HOOKS_DIR:-$GIT_DIR/hooks}"
        mkdir -p "$HOOK_BASE"
        HOOK_PATH="$HOOK_BASE/pre-commit"
        SRC="$ASSET_DIR/git-hooks/pre-commit"
        chmod +x "$SRC"
        if [[ -L "$HOOK_PATH" ]] && readlink "$HOOK_PATH" | grep -q "handoff-fanout"; then
            echo "✓ git pre-commit already symlinked to handoff-fanout"
        else
            if [[ -e "$HOOK_PATH" ]]; then
                BAK="$HOOK_PATH.bak-$(date +%s)"
                mv "$HOOK_PATH" "$BAK"
                echo "  (backed up existing hook → $BAK)"
            fi
            ln -sfn "$SRC" "$HOOK_PATH"
            echo "✓ symlinked git pre-commit → $HOOK_PATH"
        fi
    else
        echo "⊘ git hook skipped (not in a git repo)"
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

# ─── 5. handoff-helper VS Code extension (tab autoclose) ────────────────────
if [[ $INSTALL_EXTENSION -eq 1 ]]; then
    EXT_DIR="$(cd "$ASSET_DIR/.." && pwd)/extension"
    CODE_BIN="$(command -v code || true)"
    if [[ -z "$CODE_BIN" ]]; then
        echo "⊘ extension skipped (no \`code\` CLI on PATH — open VS Code → Cmd+Shift+P → 'Shell Command: Install code command')"
    elif [[ ! -d "$EXT_DIR" ]]; then
        echo "⊘ extension skipped (source dir not found: $EXT_DIR)"
    else
        # Read the target version from the extension manifest so the idempotency
        # check compares against what we're about to build.
        EXT_VERSION="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$EXT_DIR/package.json" | head -1)"
        if "$CODE_BIN" --list-extensions --show-versions 2>/dev/null | grep -qx "dharmaxis.handoff-helper@$EXT_VERSION"; then
            echo "✓ handoff-helper extension already installed (v$EXT_VERSION)"
        elif ! command -v npm >/dev/null 2>&1; then
            echo "⊘ extension skipped (npm not on PATH — needed to build the .vsix)"
        else
            echo "→ building handoff-helper.vsix (v$EXT_VERSION)"
            VSIX="$EXT_DIR/handoff-helper.vsix"
            # Guard the build in an `if` so a failure under `set -e` falls through
            # to the warning instead of aborting the whole installer.
            if ( cd "$EXT_DIR" && { [[ -d node_modules ]] || npm install --silent; } && npm run package --silent ) \
                && [[ -f "$VSIX" ]] \
                && "$CODE_BIN" --install-extension "$VSIX" --force >/dev/null 2>&1; then
                echo "✓ installed handoff-helper extension (v$EXT_VERSION)"
                echo "  (autoclose stays OFF until you opt in — see config.json autoclose note)"
            else
                echo "⚠ extension build/install failed — run \`cd $EXT_DIR && npm install && npm run package\` manually"
            fi
        fi
    fi
fi

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
