#!/usr/bin/env bash
# Regression test for the place-role-explicit-contract (sw-place-role-explicit-contract / 2026-06-29):
# the launcher's window-placement role is now taken from the engine-stamped ROLE= line in the .uri
# (passed to maybe_place_window as $6 = uri_role), NOT from UI appearance (no 🧭中枢 title / red
# titleBar sniffing). When ROLE= is absent (a legacy / in-flight .uri) the launcher falls back to the
# transitional per-project singlepane sidecar `role`, ultimately defaulting to worker.
#
# This drives the REAL maybe_place_window function extracted from install/auto-continue.sh (so a
# future drift is caught) with a fake HANDOFF_TIMEOUT_CMD recorder that captures the `--role <role>`
# the function forwards to coord-place-window.py. We assert the resolved role per case:
#   1. explicit ROLE=coord          -> --role coord     (worktree / singlepane / cold-start coordinator)
#   2. explicit ROLE=worker         -> --role worker
#   3. ROLE= absent + sidecar role=supervisor_succession -> --role coord  (transitional fallback)
#   4. ROLE= absent + sidecar role=solo                  -> --role worker (fallback, non-coord sidecar)
#   5. ROLE= absent + NO sidecar                         -> --role worker (safe default)
#   6. ROLE= absent + sidecar role=worker                -> --role worker
#   7. explicit PRESENT-BUT-UNRECOGNIZED ROLE= (+ coord sidecar) -> --role worker (sidecar IGNORED)
# Plus a structural guard: the launcher must NOT sniff UI appearance for the placement role.
#
# Exit 0 on full pass, non-zero on any failure. Run: bash tests/test_place_role_contract.sh

set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$SELF_DIR/../install/auto-continue.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail=0
pass() { printf 'PASS: %s\n' "$1"; }
fyi()  { printf 'FAIL: %s\n' "$1"; fail=1; }

# --- extract the REAL maybe_place_window from the launcher ----------------------
# awk: print from the line beginning `maybe_place_window() {` up to and including the
# first line that is exactly `}` at column 0.
extract_fn() {
    awk -v fn="$1" '
        $0 ~ "^" fn "\\(\\) \\{" { inblk=1 }
        inblk { print }
        inblk && /^\}$/ { exit }
    ' "$LAUNCHER"
}

FNS="$WORK/funcs.sh"
extract_fn maybe_place_window > "$FNS"
grep -q '^maybe_place_window() {' "$FNS" || { echo "ERROR: could not extract maybe_place_window"; exit 2; }

# the launcher's python the sidecar-read heredoc runs under (real python3, never bare none)
if [ -x /usr/bin/python3 ]; then HANDOFF_PYTHON_CMD=/usr/bin/python3; else HANDOFF_PYTHON_CMD=python3; fi
export HANDOFF_PYTHON_CMD

# --- env scaffolding maybe_place_window references ------------------------------
LOG="$WORK/launcher.log"; export LOG
log() { printf '%s\n' "$*" >> "$LOG"; }                       # minimal launcher log()
HANDOFF_ROOT="$WORK/root"; mkdir -p "$HANDOFF_ROOT"; export HANDOFF_ROOT
HANDOFF_PLACE_WAIT=0; export HANDOFF_PLACE_WAIT
HANDOFF_PLACE_TIMEOUT=5; export HANDOFF_PLACE_TIMEOUT
HANDOFF_PLACE_PYTHON=/usr/bin/true; export HANDOFF_PLACE_PYTHON   # never actually run (recorder intercepts)

# HANDOFF_PLACE_TOOL must EXIST as a file (maybe_place_window early-returns 0 if missing). Content
# never executes: the fake HANDOFF_TIMEOUT_CMD recorder below captures the args without exec'ing it.
HANDOFF_PLACE_TOOL="$WORK/coord-place-window.py"; : > "$HANDOFF_PLACE_TOOL"; export HANDOFF_PLACE_TOOL

# Fake timeout recorder: maybe_place_window's HANDOFF_TIMEOUT_CMD path runs
#   "$HANDOFF_TIMEOUT_CMD" <timeout> <place_python> <place_tool> --project P <sel> V --role ROLE ...
# The recorder writes ALL its args (one per line) to $REC and exits 0 (so the `|| true` is moot and
# nothing real runs). We then grep the recorded args for the resolved `--role <value>` pair.
REC="$WORK/recorded_args"
RECORDER="$WORK/fake_timeout"
{
    printf '#!/usr/bin/env bash\n'
    printf 'printf "%%s\\n" "$@" > "%s"\n' "$REC"
    printf 'exit 0\n'
} > "$RECORDER"
chmod +x "$RECORDER"
HANDOFF_TIMEOUT_CMD="$RECORDER"; export HANDOFF_TIMEOUT_CMD

# shellcheck disable=SC1090
. "$FNS"

# --- helpers --------------------------------------------------------------------
# Returns the `--role` value the function forwarded (reads the recorder output: the token right
# after the literal "--role"). Empty if --role was never passed (placement skipped).
recorded_role() {
    [ -f "$REC" ] || { printf ''; return; }
    awk '/^--role$/{getline; print; exit}' "$REC"
}

# Run maybe_place_window for one case. Args: <queue_dir> <uri_role>
# Uses a fresh QUEUE so a per-case singlepane sidecar is isolated. wid="" forces the title path
# (the role resolution under test is independent of the WID selector).
run_case() {  # <queue_dir> <uri_role>
    rm -f "$REC"
    maybe_place_window "demo-project" "demo-task" "$WORK/projdir" "$1" "" "$2"
}

QBASE="$WORK/queues"; mkdir -p "$QBASE"
new_queue() { local q="$QBASE/q$RANDOM$RANDOM"; mkdir -p "$q"; printf '%s' "$q"; }
write_sidecar() {  # <queue_dir> <role>
    printf '{"workspace":"/x","role":"%s","close_policy":"keep","spawn_nonce":"deadbeef","isolation":"singlepane","predecessor_nonce":null}' \
        "$2" > "$1/demo-task.singlepane"
}

assert_role() {  # <expected> <label>
    local got; got="$(recorded_role)"
    if [ "$got" = "$1" ]; then
        pass "$2 -> --role $1"
    else
        fyi  "$2: expected --role $1, got --role [$got]"
    fi
}

# ================================================================================
# CASE 1: explicit ROLE=coord (the authoritative contract — every coordinator path:
#         worktree coord, singlepane succession coord, singlepane COLD-START coord).
#         No sidecar present, proving ROLE= alone suffices (the cold-start fix:
#         its singlepane sidecar records role="worker", so ROLE= is the ONLY signal).
# ================================================================================
Q="$(new_queue)"   # deliberately NO sidecar
run_case "$Q" "coord"
assert_role "coord" "CASE1: explicit ROLE=coord, no sidecar (cold-start coordinator)"

# ================================================================================
# CASE 2: explicit ROLE=worker -> worker (byte-behavior of a worker spawn).
# ================================================================================
Q="$(new_queue)"
run_case "$Q" "worker"
assert_role "worker" "CASE2: explicit ROLE=worker"

# ================================================================================
# CASE 3: ROLE= absent + transitional sidecar role=supervisor_succession -> coord.
#         (back-compat for a legacy / in-flight .uri written before this contract.)
# ================================================================================
Q="$(new_queue)"; write_sidecar "$Q" "supervisor_succession"
run_case "$Q" ""
assert_role "coord" "CASE3: ROLE= absent + sidecar supervisor_succession (fallback)"

# ================================================================================
# CASE 4: ROLE= absent + sidecar role=solo -> worker (a non-coord sidecar role).
# ================================================================================
Q="$(new_queue)"; write_sidecar "$Q" "solo"
run_case "$Q" ""
assert_role "worker" "CASE4: ROLE= absent + sidecar solo (fallback -> worker)"

# ================================================================================
# CASE 5: ROLE= absent + NO sidecar -> worker (the safe default).
# ================================================================================
Q="$(new_queue)"
run_case "$Q" ""
assert_role "worker" "CASE5: ROLE= absent + no sidecar (safe default)"

# ================================================================================
# CASE 6: ROLE= absent + sidecar role=worker -> worker.
# ================================================================================
Q="$(new_queue)"; write_sidecar "$Q" "worker"
run_case "$Q" ""
assert_role "worker" "CASE6: ROLE= absent + sidecar worker (fallback)"

# ================================================================================
# CASE 7: explicit PRESENT-BUT-UNRECOGNIZED ROLE= (nonempty, neither coord nor
#         worker) -> worker, IGNORING a coord-driving sidecar. Once the ROLE=
#         contract is present it is authoritative: an unknown value must fail-safe
#         to worker directly, NOT revert to the legacy sidecar fallback (which
#         would mis-resolve to coord here). We deliberately plant a coord sidecar
#         (role=supervisor_succession) in the SAME queue to prove the function does
#         NOT consult it for a nonempty-unknown ROLE=.
# ================================================================================
Q="$(new_queue)"; write_sidecar "$Q" "supervisor_succession"
run_case "$Q" "supervisor_succession"
assert_role "worker" "CASE7: nonempty-unknown ROLE=supervisor_succession + coord sidecar -> --role worker (sidecar ignored)"

# ================================================================================
# STRUCTURAL GUARD: the launcher's placement role must NOT be derived from UI
# appearance. The superseded sw-place-coord-role-fix sniffed the workspace
# .handoff.code-workspace for the 🧭中枢 title / titleBar.activeBackground #8B0000.
# The explicit-contract end state removes ALL such sniffing — there must be NO
# _place_role_for helper and NO titleBar/#8B0000 inspection in the launcher.
# ================================================================================
if grep -q '_place_role_for' "$LAUNCHER"; then
    fyi  "GUARD: launcher still defines _place_role_for (UI-sniffing helper must be gone)"
else
    pass "GUARD: no _place_role_for UI-sniffing helper in launcher"
fi
if grep -Eq 'titleBar\.activeBackground|#8[Bb]0000|colorCustomizations' "$LAUNCHER"; then
    fyi  "GUARD: launcher still inspects titleBar/#8B0000/colorCustomizations for the role"
else
    pass "GUARD: no titleBar/#8B0000/colorCustomizations role-sniffing in launcher"
fi

# --------------------------------------------------------------------------------
if [ "$fail" -eq 0 ]; then
    echo "ALL PASS"
    exit 0
else
    echo "SOME FAILED"
    exit 1
fi
