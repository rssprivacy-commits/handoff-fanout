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
Type "export HOOK=$(pwd)/install/git-hooks/pre-commit" Enter
Type "mkdir -p $HANDOFF_HOME && cd /tmp && rm -rf demo-repo && git init -q demo-repo && cd demo-repo" Enter
Type "git config user.email demo@handoff.test && git config user.name demo" Enter
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
Type "echo pricing.py > /tmp/expected" Enter
Type "HANDOFF_EXPECTED_FILES=/tmp/expected bash $HOOK; echo exit=$?" Enter
Sleep 4s

# Wrap
Type "echo '✅ done'" Enter
Sleep 2s
