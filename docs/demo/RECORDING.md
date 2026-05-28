# 30-second demo recording script

The README.md links to `docs/demo/handoff-fanout-demo.gif`. This file documents the exact steps to (re-)record that gif so the asset stays reproducible across releases.

> Status: **script is final, gif capture pending v1.0.0 release.** Until the asset is committed, the README link 404s gracefully — that's fine for the v0.1.0 → v1.0.0 release branch.

## Tooling

Pick one:

- **[asciinema](https://asciinema.org/) + [agg](https://github.com/asciinema/agg)** (preferred, plain-text input, animated GIF output)
- **[VHS](https://github.com/charmbracelet/vhs)** (declarative `.tape` script → GIF)
- **[Terminalizer](https://github.com/faressoft/terminalizer)** (cross-platform, heavier)

VHS produces the smallest, sharpest GIFs. The tape script below is for VHS.

## VHS tape (`docs/demo/demo.tape`)

```tape
Output docs/demo/handoff-fanout-demo.gif

Set FontSize 18
Set Width 1200
Set Height 700
Set Theme "Dracula"
Set TypingSpeed 50ms
Set PlaybackSpeed 1.0

# Setup
Hide
Type "export HANDOFF_HOME=$(mktemp -d)/handoff" Enter
Type "mkdir -p $HANDOFF_HOME && cd /tmp && rm -rf demo-repo && git init -q demo-repo && cd demo-repo" Enter
Type "clear" Enter
Show

# Title card
Type "# handoff-fanout: 30-second demo" Sleep 1s Enter
Sleep 500ms

# Step 1: dump
Type "handoff dump \" Enter
Type "  --task fix-discount-bug \" Enter
Type "  --next 'Fix the off-by-one in pricing.py' \" Enter
Type "  --status active" Enter
Sleep 2s

# Step 2: inspect what was written
Type "ls $HANDOFF_HOME/*/queue/" Enter
Sleep 2s

# Step 3: show the URI sidecar (what the IDE launcher reads)
Type "cat $HANDOFF_HOME/*/queue/fix-discount-bug.uri" Enter
Sleep 3s

# Step 4: show the beginning of the handoff markdown
Type "head -25 $HANDOFF_HOME/*/queue/fix-discount-bug.md" Enter
Sleep 4s

# Step 5: simulate hijack defense
Type "echo 'import sys' > pricing.py && echo 'rogue = True' > rogue.py" Enter
Type "git add pricing.py rogue.py" Enter
Type "HANDOFF_EXPECTED_FILES=pricing.py bash install/git-hooks/pre-commit" Enter
Sleep 4s

# Wrap
Type "echo '✅ done'" Enter
Sleep 2s
```

## Capture command

```bash
# install VHS (one-time)
brew install vhs

# capture (writes docs/demo/handoff-fanout-demo.gif)
cd /path/to/handoff-fanout
vhs docs/demo/demo.tape
```

## Sanity check

The captured GIF should be:

- **< 30 seconds**
- **< 2 MB** (committable; if larger, lower `Set FontSize` or `Set Width`)
- **legible at 100% zoom** on GitHub
- show the four moments: `dump`, the `.uri` sidecar, the handoff markdown, and Layer 2 rejecting a rogue staged file

## When to re-record

- Major CLI flag changes
- Output format changes (e.g. new section in the handoff markdown)
- Theme refresh

Otherwise leave it.
