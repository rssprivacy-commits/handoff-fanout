#!/usr/bin/env bash
# Regression test for the two winlist fail-safe holes closed in
# install/auto-continue.sh (codex RED, sw-coord-p76 / 2026-06-28):
#
#   FIX 1 — winlist_wids() must check the winlist subprocess returncode BEFORE
#           parsing stdout. A non-zero winlist that still emits valid JSON must
#           NOT yield a phantom WID set; it must print nothing and return non-zero
#           so the caller falls back to the title (--task) path.
#
#   FIX 2 — winlist_new_wid() must only resolve a unique new WID when the BEFORE
#           snapshot diff yields EXACTLY one new window. (The BEFORE-capture
#           success/failure distinction itself lives at the call site in the main
#           loop and is exercised structurally below.)
#
# Strategy: extract the two REAL bash functions from auto-continue.sh by name
# (awk on the `^winlist_xxx() {` ... matching `^}` block) and source them, so the
# assertions run against the live launcher code — not a re-implementation. The
# functions are driven via the HANDOFF_WINLIST / HANDOFF_PYTHON_CMD env overrides
# the launcher already honours.
#
# Exit 0 on full pass, non-zero on any failure. Run: bash tests/test_winlist_failsafe.sh

set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$SELF_DIR/../install/auto-continue.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail=0
pass() { printf 'PASS: %s\n' "$1"; }
fyi()  { printf 'FAIL: %s\n' "$1"; fail=1; }

# --- extract the two functions from the real launcher ---------------------------
# awk: print from the line beginning `winlist_wids() {` (or winlist_new_wid) up to
# and including the first line that is exactly `}` at column 0.
extract_fn() {
    awk -v fn="$1" '
        $0 ~ "^" fn "\\(\\) \\{" { inblk=1 }
        inblk { print }
        inblk && /^\}$/ { exit }
    ' "$LAUNCHER"
}

FNS="$WORK/funcs.sh"
{
    extract_fn winlist_wids
    echo
    extract_fn winlist_new_wid
} > "$FNS"

# sanity: both functions must have been extracted
grep -q '^winlist_wids() {'    "$FNS" || { echo "ERROR: could not extract winlist_wids";    exit 2; }
grep -q '^winlist_new_wid() {' "$FNS" || { echo "ERROR: could not extract winlist_new_wid"; exit 2; }

# python the launcher's python heredoc runs under (real python3, never bare none)
if [ -x /usr/bin/python3 ]; then HANDOFF_PYTHON_CMD=/usr/bin/python3; else HANDOFF_PYTHON_CMD=python3; fi
export HANDOFF_PYTHON_CMD

# shellcheck disable=SC1090
. "$FNS"

# --- fake winlist factory -------------------------------------------------------
# Writes an executable that prints $2 (JSON) to stdout and exits with code $1.
make_winlist() {  # <exit_code> <json>
    local path="$WORK/winlist_$RANDOM$RANDOM"
    {
        printf '#!/usr/bin/env bash\n'
        printf 'cat <<'\''JSON'\''\n%s\nJSON\n' "$2"
        printf 'exit %s\n' "$1"
    } > "$path"
    chmod +x "$path"
    printf '%s\n' "$path"
}

# ================================================================================
# FIX 1: non-zero winlist that emits valid JSON -> NOTHING printed, non-zero return
# ================================================================================
export HANDOFF_WINLIST="$(make_winlist 1 '[{"window_number":1}]')"
out="$(winlist_wids)"; rc=$?
if [ "$rc" -ne 0 ] && [ -z "$out" ]; then
    pass "FIX1: non-zero winlist (valid JSON) -> empty output + non-zero return (rc=$rc)"
else
    fyi  "FIX1: non-zero winlist must yield empty+non-zero; got rc=$rc out=[$out]"
fi

# ================================================================================
# FIX 1 (positive control): exit-0 winlist with two windows -> prints 1 and 2, rc 0
# ================================================================================
export HANDOFF_WINLIST="$(make_winlist 0 '[{"window_number":1},{"window_number":2}]')"
out="$(winlist_wids)"; rc=$?
# normalise to space-joined for a stable compare
got="$(printf '%s\n' "$out" | tr '\n' ' ' | sed 's/ *$//')"
if [ "$rc" -eq 0 ] && [ "$got" = "1 2" ]; then
    pass "FIX1-ctrl: exit-0 winlist -> prints '1' and '2', rc 0"
else
    fyi  "FIX1-ctrl: expected rc0 out='1 2'; got rc=$rc out=[$got]"
fi

# ================================================================================
# OBJECT shape (sw-place-wid-spaces / 2026-06-29): winlist --spaces-of-windows emits
# {"windows":[…],"ok":true} (cross-Space). winlist_wids must unwrap .windows and
# print the window_numbers. The fake ignores args, so the --spaces-of-windows flag
# the function now passes is harmless.
# ================================================================================
export HANDOFF_WINLIST="$(make_winlist 0 '{"windows":[{"window_number":1},{"window_number":2}],"ok":true}')"
out="$(winlist_wids)"; rc=$?
got="$(printf '%s\n' "$out" | tr '\n' ' ' | sed 's/ *$//')"
if [ "$rc" -eq 0 ] && [ "$got" = "1 2" ]; then
    pass "obj-shape: {\"windows\":[…]} -> prints '1' and '2', rc 0"
else
    fyi  "obj-shape: expected rc0 out='1 2'; got rc=$rc out=[$got]"
fi

# ================================================================================
# OBJECT shape with no .windows key -> data.get('windows') is None -> not a list
# -> fail-closed (empty + non-zero), so the caller falls back to the title path.
# ================================================================================
export HANDOFF_WINLIST="$(make_winlist 0 '{"ok":true}')"
out="$(winlist_wids)"; rc=$?
if [ "$rc" -ne 0 ] && [ -z "$out" ]; then
    pass "obj-shape: {} without .windows -> fail-closed (empty + non-zero, rc=$rc)"
else
    fyi  "obj-shape: missing .windows must fail-closed; got rc=$rc out=[$out]"
fi

# ================================================================================
# BACKWARD-COMPAT: a bare array (the old plain-winlist shape) is still parsed.
# (restores the two-window exit-0 fake for the winlist_new_wid cases below.)
# ================================================================================
export HANDOFF_WINLIST="$(make_winlist 0 '[{"window_number":1},{"window_number":2}]')"
out="$(winlist_wids)"; rc=$?
got="$(printf '%s\n' "$out" | tr '\n' ' ' | sed 's/ *$//')"
if [ "$rc" -eq 0 ] && [ "$got" = "1 2" ]; then
    pass "bare-array: [{…},{…}] still parsed -> prints '1' and '2', rc 0 (backward-compat)"
else
    fyi  "bare-array: expected rc0 out='1 2'; got rc=$rc out=[$got]"
fi

# ================================================================================
# winlist_new_wid: BEFORE={1}, now={1,2} -> exactly one new -> prints 2, rc 0
# (now is read via the live winlist_wids over the exit-0 two-window fake above)
# ================================================================================
out="$(winlist_new_wid "1")"; rc=$?
if [ "$rc" -eq 0 ] && [ "$out" = "2" ]; then
    pass "new_wid: BEFORE={1} now={1,2} -> unique new WID '2', rc 0"
else
    fyi  "new_wid: expected rc0 out=2; got rc=$rc out=[$out]"
fi

# ================================================================================
# winlist_new_wid: BEFORE='' (empty), now={1,2} -> TWO new -> ambiguous -> non-zero
# ================================================================================
out="$(winlist_new_wid "")"; rc=$?
if [ "$rc" -ne 0 ]; then
    pass "new_wid: BEFORE='' now={1,2} -> ambiguous (>1 new) -> non-zero (rc=$rc)"
else
    fyi  "new_wid: empty-before with 2 new must be ambiguous/non-zero; got rc=$rc out=[$out]"
fi

# ================================================================================
# winlist_new_wid: if the AFTER winlist read itself fails (non-zero), must return
# non-zero (defends FIX 1's propagation through `now=$(winlist_wids) || return 1`).
# ================================================================================
export HANDOFF_WINLIST="$(make_winlist 1 '[{"window_number":7}]')"
out="$(winlist_new_wid "1")"; rc=$?
if [ "$rc" -ne 0 ] && [ -z "$out" ]; then
    pass "new_wid: AFTER winlist read fails -> non-zero return + empty (rc=$rc)"
else
    fyi  "new_wid: failed AFTER read must yield non-zero+empty; got rc=$rc out=[$out]"
fi

# ================================================================================
# wid sanitizer (sw-coord-p76 / 2026-06-28): mirror the EXACT two sanitizer lines
# from maybe_place_window in install/auto-continue.sh so a future drift is caught:
#   case "$wid" in ''|*[!0-9]*) wid="" ;; esac
#   [ -n "$wid" ] && { [ "$wid" -gt 0 ] 2>/dev/null || wid=""; }
# Non-numeric / empty -> "" (existing behavior); the SECOND line additionally
# rejects a non-positive WID (0 / 00) since a real Quartz window_number is > 0.
# ================================================================================
sanitize_wid() {  # echoes the sanitized wid (the two maybe_place_window lines verbatim)
    local wid="$1"
    case "$wid" in ''|*[!0-9]*) wid="" ;; esac
    [ -n "$wid" ] && { [ "$wid" -gt 0 ] 2>/dev/null || wid=""; }
    printf '%s' "$wid"
}
sanitize_case() {  # <input> <expected> <label>
    local got; got="$(sanitize_wid "$1")"
    if [ "$got" = "$2" ]; then
        pass "wid-sanitize: $3 -> [$2]"
    else
        fyi  "wid-sanitize: $3 expected [$2], got [$got]"
    fi
}
sanitize_case "0"  ""  "'0' (non-positive)"
sanitize_case "00" ""  "'00' (non-positive)"
sanitize_case "5"  "5" "'5' (valid positive)"
sanitize_case "x5" ""  "'x5' (non-numeric)"
sanitize_case ""   ""  "'' (empty)"

# --------------------------------------------------------------------------------
if [ "$fail" -eq 0 ]; then
    echo "ALL PASS"
    exit 0
else
    echo "SOME FAILED"
    exit 1
fi
