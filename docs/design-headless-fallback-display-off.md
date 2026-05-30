# Design Spec — Display-Off / Lock-Screen Resilient Auto-Continue (headless fallback)

> Status: **V6 DRAFT** (codex R1→V2→R2→V3→R3→V4→R4→V5→R5→V6; 5 codex rounds; direction confirmed "directionally sane"; R5 last 2 P0 spec-consistency items closed)
>
> **R5→V6 (final consistency pass):** purged stale `WatchPaths` text in §3.3/§3.5
> (trigger is `QueueDirectories` everywhere; the only WatchPaths left is the
> *existing GUI launcher*'s, correctly labelled); purged stale `gtimeout`/`rc=124`
> bash text in §3.4 + failure matrix (Python `communicate(timeout=)`+`killpg`);
> janitor PID-reuse check now uses **process start-time (`ps -o lstart`)** — an
> actually-queryable property — instead of an env/argv marker `claude -p` doesn't
> expose. Internally consistent; design-phase complete.
>
> **R4→V5 fixes (spec-consistency + runner guards):** trigger unified to
> `QueueDirectories` everywhere (removed the WatchPaths/QueueDirectories
> contradiction); `AbandonProcessGroup` wording corrected (runner+janitor own the
> new-session child, not launchd); janitor verifies leader pid + `HANDOFF_TASK`
> marker before `killpg` (PGID-reuse safe); timeout cleanup wraps `proc.wait` so
> SIGKILL/finalize always run; opt-in **re-checked at spawn time** (revocation
> honored); cap-busy backoff (no QueueDirectories hot-loop); `ExitTimeOut`≥60 for
> the SIGTERM grace; `killpg` daemonizing-escape caveat added to the §2.2 spike.
>
> **Residual open items are now (a) implementation of the V5 design + (b) the §2.2
> on-box empirical spikes** (QueueDirectories re-launch, killpg reap, orphan
> reconcile, lock-probe-under-launchd) — these gate ENABLING headless, and cannot
> be settled by more design prose. Codex agrees the approach is directionally
> sound; this is the design-phase exit.
>
> **R3→V4 structural pivot — bash runner → Python runner** (`handoff_fanout.headless_runner`).
> R3 proved the V3 bash design was macOS-broken: `setsid` binary absent on Darwin,
> and `setsid`-into-a-new-pgid contradicts `AbandonProcessGroup=false` (launchd
> only reaps same-pgid trees). V4 fixes: ① trigger = **`QueueDirectories`** (not
> lossy WatchPaths) so a mid-run req isn't lost; ② Python `start_new_session`
> (`os.setsid` syscall, no binary) + `os.killpg` whole-tree kill + the **runner**
> owns lifecycle (AbandonProcessGroup moot) + janitor sweep for the SIGKILL-orphan
> window; ③ timeout classified by Python `TimeoutExpired` (not rc-124 guess);
> ④ workspace parsed from `.req` + `cwd=workspace`; ⑤ argv **list** (no multi-word
> quoting bug); ⑥ finalize uses a **name** baseline (no mtime granularity race).
> R3's empirical questions (QueueDirectories re-launch, killpg reap, orphan
> reconcile) are now explicit **pre-enable spike gates** (§2.2), not prose claims.
> Author: Claude Code (集团中枢) · 2026-05-30
> Owner decision on record: **方案 2 — 显示器可关，改无头执行**
> Scope: `install/auto-continue.sh` + **new launchd-owned headless runner** + 1 `dump.py`
> atomicity fix + tests. No change to gate semantics.

---

## 0. TL;DR

Owner walks away → display sleeps (~10 min AC idle) → `screenLock=immediate` locks
instantly → the launcher's `osascript … keystroke return` (a **synthetic
keystroke**, forbidden against a locked session) silently fails → the spawned tab
never submits → relay **dead-stalls**. Separately, full idle system-sleep suspends
launchd and freezes running work.

Fix: when locked, **do not** use the GUI tab + osascript path. Instead **hand the
next session to a dedicated, launchd-OWNED headless runner** (`claude -p` via
stdin, under `caffeinate -i`). launchd owns the process group → no orphan
SIGKILL; the runner waits for the child, records the real outcome, and parks the
chain on failure. Headless is **opt-in per project** (default OFF) because
unattended `bypassPermissions` is a real blast-radius surface. When unlocked, the
existing GUI path is unchanged.

---

## 1. Root cause (evidence-backed — Phase 0 audit)

| Layer | Mechanism | Verified evidence |
|---|---|---|
| **L1 — lock blocks osascript (primary)** | display sleep → **immediate lock** → frontmost=`loginwindow` → `is_frontmost_code` aborts → Enter never pressed → tab open but unsubmitted → chain dead-stalls | `sysadminctl -screenLock status` → **"delay is immediate"**. Code [`auto-continue.sh:238-253`]. One real abort in logs (`frontmost was 'WeChat'`, same code path). |
| **L2 — full sleep suspends launchd + freezes work** | no assertion → AC idle `displaysleep 10` then system idle-sleep → `launchd StartInterval(60)` suspended; new `.uri` not consumed until wake; in-flight session frozen | `pmset -g log` shows real **Hibernate** 05-24/05-25. `pmset -g` `sleep 0` **only** while "Claude, Codex" assertions held. |

**Why logs look ~100 % healthy (106/107 submitted, 0 lock failures):** every
historical spawn happened with the owner present / machine awake. The failure is
**unattended-only** and occurs *before* the logging code runs, so it was never
recorded as a failure — the silent gap 立法 #1 targets.

**Hard constraint:** `screenLock=immediate` ⟹ display-off ⟹ locked ⟹ synthetic
keystrokes forbidden. osascript cannot "punch through" the lock; the only
display-off-compatible path removes the keystroke dependency → headless.

---

## 2. codex R1 → V2 change map (audit trail)

| codex finding (sev) | V2 resolution | §  |
|---|---|---|
| P0 bare `&` child orphaned/SIGKILLed by launchd | **launchd-owned runner job** owns the process group; no backgrounding from the launcher | §3.2 |
| P0 `exec …` leaves no shell to record timeout/BLOCKED | runner **does not `exec`**; it runs child, `wait`s, captures `$?`, writes outcome | §3.2/§3.4 |
| P0 `/usr/bin/timeout` absent on macOS | Python runner uses `communicate(timeout=)` + `os.killpg` — no external timeout binary at all (V4 pivot) | §3.2 |
| P0 STOP kills only caffeinate, leaves claude | run child in its own **process group**; STOP = `kill -TERM -<pgid>` / `launchctl kill` the runner job | §3.4 |
| P0 GUI guards (`:88` VS Code, `:96` code CLI) block headless | guards made **conditional on GUI mode**; headless dispatch is independent | §3.1 |
| P0 mandate inheritance unproven + silent park | runner **completion-verifies**: nonzero exit / no next-`.uri` ⇒ write `BLOCKED.md` (visible park, never silent) + runtime proof gate before enable | §2.3/§3.4 |
| P1 lock probe fail-open recreates dead-stall | **fail-CLOSED**: unknown lock ⇒ do NOT claim (GUI) / headless only if opted-in; default defer + notify | §3.1 |
| P1 ioreg probe under-verified | **pre-enable runtime validation gate** (prove from launchd while truly locked / FUS / screensaver) | §2.2 |
| P1 caffeinate can't wake a sleeping Mac | acknowledged; L2-after-full-sleep is **out of scope** (resumes on wake) — no overclaim | §3.3 |
| P1 micro-gap "impossible to sleep" unproven | claim removed; documented as **bounded-degraded** (resumes on wake), not "impossible" | §3.3 |
| P1 single-task dump `.md`/`.uri` not atomic | switch single-task dump to `atomic.write_with_fsync` (match batch path) | §3.7 |
| P1 `-p "$(cat md)"` E2BIG | pass prompt via **stdin** (`claude -p < md` / `--input-format`), never argv | §3.2 |
| P1 bypassPermissions uncontained | per-project opt-in + clean-worktree/protected-branch precheck + max-concurrent + env hygiene + whole-tree kill + post-run git verify | §3.4 |
| P1 pidfile PID-reuse unsafe | pidfile carries pgid + `start_epoch` + `start_lstart`; liveness/janitor validate **process start-time** (R5) | §3.5 |
| P1 double-spawn global | global **max-concurrent-headless** cap + claim **one** `.uri` per locked tick | §3.5 |
| P2 autoclose stale GUI tab on GUI→headless | prior GUI task's own `old_ready`/`submitted` still autocloses it independently — documented | §3.6 |
| P2 lock→unlock human collision | visible **"headless active" marker** + verified STOP path | §3.6 |

---

## 3. Verified technical preconditions (no assumptions)

1. **Lock probe** — `ioreg -n Root -d1` shows `"CGSSessionScreenIsLocked" = Yes`
   when locked; key **absent** when unlocked (verified from Terminal now).
   **⚠ MUST additionally verify from the launchd context while truly locked
   (incl. FUS / screensaver) — §2.2 pre-enable gate.** Env-overridable
   (`HANDOFF_LOCK_CHECK_CMD`) for tests.
2. **`claude` is a zsh function** wrapping
   `python3 ~/.openclaw/scripts/claude-rc.py --dangerously-skip-permissions` —
   **not on PATH for launchd bash.** Runner uses a configurable
   `HANDOFF_CLAUDE_HEADLESS_CMD` (default the python entrypoint), never bare `claude`.
3. **Headless flags** (v2.1.156): `-p/--print`, `--permission-mode`, `--model`,
   `--output-format`, `--input-format` (stdin), `--add-dir`. **Prompt via stdin.**
4. **`caffeinate`** — `-i` prevents idle *system* sleep, *allows* display sleep
   (what 方案 2 wants). It does **not wake** a sleeping Mac (§3.3).
5. **Prompt source** — `dump` writes `queue/<task>.md` (= `build_handoff_md`); the
   runner reads it. The `.uri` only holds `WORKSPACE=`/`URI=`.
6. **Mandate envs** — `com.dharmaxis.auto-continue.plist` sets
   `HANDOFF_AUDIT_MANDATE=1` + `HANDOFF_RETRO_MANDATE=1`. The new headless job's
   plist **MUST set the same** (do NOT rely on cross-job inheritance —
   each launchd job has its own env). **Verify the gate fires inside `claude -p`
   with no TTY** before enabling (§2.3).

### 2.2 Pre-enable runtime validation gate (blocking — no enable without it)

Before headless is allowed ON for any project, capture from the **launchd**
context (not Terminal), with the screen ACTUALLY locked, for each of: normal
lock, fast-user-switching, screensaver-lock:
- the `ioreg` probe returns `= Yes`,
- a stub runner spawned by the headless job actually starts + writes its ack,
- `caffeinate -i` is visible in `pmset -g assertions` for the run's lifetime.

**Process-mechanics spikes (R3 — empirical, cannot be settled by docs alone):**
- **QueueDirectories re-launch**: drop `.req` B while the runner is mid-`.req` A;
  confirm launchd re-launches the runner after A so B is drained (no stuck req).
- **killpg tree reap**: a headless child that spawns a bash grandchild → on STOP
  and on timeout, confirm `os.killpg` leaves **zero** survivors (`pgrep` the tree).
  **Caveat (R4 P1):** `killpg` only reaches descendants still in the group; a child
  that itself calls `setsid`/daemonizes escapes. claude + its bash tool-calls stay
  in-group in practice, but the spike must explicitly probe a `setsid`-ing
  grandchild and, if any class escapes, add a pid-tree walk (`pgrep -P` recursion)
  fallback before enable.
- **runner-SIGKILL orphan reconcile**: `kill -9` the runner mid-run → confirm the
  janitor sweep on next launch kills the orphaned child pgid via the pidfile.
- **start_new_session vs launchd**: confirm the child is NOT prematurely reaped by
  launchd job exit and IS reaped by the runner/janitor.

Record all evidence in `docs/headless-validation-evidence.md`. Any item failing
⇒ headless stays OFF (the GUI path + fail-closed defer remain the safe default).

### 2.3 Mandate-inside-headless proof (blocking)

Run one real headless task whose end calls `handoff dump` and confirm: with
mandate ON + no `--retro-evidence`, the dump exits nonzero AND the runner turns
that into a visible `BLOCKED.md` (not a silent death). Record evidence.

---

## 3. Design

### 3.1 Lock-aware routing + guard conditionalization (`auto-continue.sh`)

```bash
HANDOFF_LOCK_CHECK_CMD="${HANDOFF_LOCK_CHECK_CMD:-}"
screen_is_locked() {                       # exit 0 = locked, 1 = unlocked, 2 = unknown
    if [ -n "$HANDOFF_LOCK_CHECK_CMD" ]; then
        case "$("$HANDOFF_LOCK_CHECK_CMD")" in locked) return 0;; unlocked) return 1;; *) return 2;; esac
    fi
    local out; out=$(/usr/sbin/ioreg -n Root -d1 2>/dev/null) || return 2
    printf '%s' "$out" | /usr/bin/grep -q '"CGSSessionScreenIsLocked" = Yes' && return 0
    printf '%s' "$out" | /usr/bin/grep -q '"CGSSessionScreenIsLocked" = No'  && return 1
    return 2                                # key absent / ioreg garbage = UNKNOWN
}
```

- **Guard conditionalization (P0 #5):** the `pgrep "Visual Studio Code"` (`:88`)
  and `code` CLI (`:96`) exits are required **only for the GUI path**. Move them
  out of the global preamble: compute lock state first; if locked + headless
  opted-in, skip the GUI guards entirely.
- **Per-`.uri` routing (fail-closed, P1):**

| lock state | headless opted-in for project? | action |
|---|---|---|
| unlocked | — | GUI path (unchanged): `code -r` + `open URI` + osascript Enter |
| locked | yes | claim **one** `.uri`, dispatch headless (§3.2) |
| locked | no | **defer**: leave `.uri` in queue, `display notification "锁屏待接续 — 解锁或开启 headless"` (once / 6h), do NOT dead-spawn |
| unknown | — | **fail-closed**: same as "locked + not opted-in" → defer + notify; never GUI-submit into an unknown state |

Headless opt-in (mirrors autoclose): `HANDOFF_HEADLESS_ENABLED=1` env, or
`~/.claude-handoff/headless.enabled` (global) / `<project>/headless.enabled`.

**Defer visibility (R2 P1).** A `defer` writes a **durable** marker
`queue/<task>.deferred` (KV: reason=`locked-not-opted-in`|`lock-unknown`, first
+ last epoch, tick count) — not just an ephemeral notification. The `状态`/`status`
shortcut and the handoff watchdog enumerate `*.deferred` so "N tasks paused
waiting for unlock" is surfaced, and the watchdog can escalate (notification /
optional opt-in headless) after K ticks. The marker is removed when the `.uri` is
finally consumed (unlock → GUI, or owner enables headless).

**Explicit product behavior (R2 P1 — stated, not a bug):** with headless **OFF**
(the default), a locked machine **PAUSES the relay until unlock**. This is the
chosen safe default — no unattended `bypassPermissions` agent runs without an
explicit per-project opt-in. Owners who want overnight progress set the opt-in
sentinel; owners who don't accept the pause and see the `.deferred` count on
return.

### 3.2 Headless runner — launchd-OWNED (P0 #1/#2/#3)

**No backgrounding from the launcher.** A dedicated launchd agent owns lifecycle:

- New plist `com.dharmaxis.handoff-headless.plist`: `ProcessType=Background`,
  `RunAtLoad=false`, `KeepAlive=false`, **`QueueDirectories`** = each project's
  `headless-req/` dir (R4 P0: ONE trigger key, QueueDirectories everywhere — no
  `WatchPaths`), **`ExitTimeOut` ≥ 60** (R4 P1: launchd's graceful-stop window
  must exceed the runner's 30 s TERM→KILL plan so the SIGTERM handler can
  `killpg` the child before launchd SIGKILLs the runner).
  **`AbandonProcessGroup` correctness (R4 P0):** the headless child runs in its
  **own session** (`start_new_session`), so launchd does NOT reap it via job
  pgid — the **runner + janitor own the child's lifecycle** (the V4 pivot). The
  `AbandonProcessGroup=false` default only cleans up any *same-group* stragglers
  of the runner itself; it is explicitly **NOT** relied on for the new-session
  child. (Earlier drafts wrongly said launchd "reaps the tree" — corrected.)
  **Env drift defence (R2 P1):** `install/install.sh` generates BOTH this plist
  and `com.dharmaxis.auto-continue.plist` from **one** shared env block, with an
  install-time test asserting both carry `HANDOFF_AUDIT_MANDATE=1` +
  `HANDOFF_RETRO_MANDATE=1` (each launchd job has its own env; no cross-job
  inheritance assumed).
- auto-continue.sh, on a locked+opted-in `.uri`, writes a request file
  `<project>/headless-req/<task>.req` (atomic temp+rename) carrying
  `WORKSPACE=`/`task=` (R3 P0: the runner needs the workspace; `.uri` has it,
  `.req` mirrors it). The headless plist uses **`QueueDirectories`** on the
  `headless-req/` dirs — launchd's documented non-empty-directory mechanism that
  **keeps re-launching the job while any `.req` remains** (R3 P0: `WatchPaths` is
  lossy and does not re-fire reliably for a request that arrives mid-run).
  **Never `launchctl kickstart -k`** (R2 P0). auto-continue.sh does not run claude.
- `install/headless-runner.sh` (run by that job, foreground, no `&`):

**The runner is a Python module** (`handoff_fanout.headless_runner`), run by the
launchd job. Python dissolves the R3 bash-on-macOS problems: `os.setsid` is a
**syscall** (no missing `setsid` binary), `subprocess` takes an **argv list**
(no multi-word-command quoting bug), `communicate(timeout=)` gives wall-clock,
and `os.killpg` does whole-tree kill.

```python
# headless_runner.py — invoked by launchd (QueueDirectories on headless-req/).
# Drains EVERY pending .req this invocation; launchd re-launches while any remain.
for req in sorted(req_dir.glob("*.req")):
    task, workspace = parse_req(req)                  # R3 P0: workspace from .req
    prompt_md = queue / f"{task}.md"
    if is_halted(proj): break                         # STOP/done: stop taking new work
    if not headless_enabled(proj): defer(task, "opt-in revoked"); req.unlink(); continue  # R4 P0: re-check opt-in at spawn time
    if not acquire_slot(task):                         # cap + per-task lock (§3.5)
        # R4 P1 busy-loop defence: over-cap leaves .req, but DON'T let
        # QueueDirectories respin instantly — the runner sleeps a bounded backoff
        # (or stays resident waiting on a slot) so launchd isn't hot-looping.
        backoff(); continue
    if not safety_precheck(workspace, task): block(task, "unsafe worktree"); req.unlink(); continue

    base_uris = {p.name for p in queue.glob("*.uri")} # R3 P1: NAME baseline (no mtime granularity race)
    argv = [*HEADLESS_CLAUDE_ARGV, "--permission-mode", "bypassPermissions",
            "--model", HEADLESS_MODEL, "-p"]          # argv list ⇒ no quoting bug (R3 P1)
    # caffeinate -i wraps the tree: keeps SYSTEM awake, lets display sleep.
    proc = subprocess.Popen(["caffeinate", "-i", *argv],
        cwd=workspace,                                # R3 P0: cd into workspace
        stdin=open(prompt_md), stdout=log, stderr=log,
        env={**os.environ, "HANDOFF_TASK": task},     # log/debug only (NOT the reuse check — §3.5)
        start_new_session=True)                       # os.setsid in child ⇒ pgid==proc.pid
    pgid = os.getpgid(proc.pid)                        # syscall, not racy `ps`
    start_lstart = ps_lstart(proc.pid)                 # R5: start-time = PID-reuse defence
    write_pidfile(task, proc.pid, pgid, start_epoch, start_lstart)
    halt_watcher = spawn_thread(poll_halt, on_halt=lambda: _killpg(pgid))  # STOP mid-run
    try:
        proc.communicate(timeout=HEADLESS_TIMEOUT)    # wall-clock, no gtimeout dependency
        rc, reason = proc.returncode, None
    except subprocess.TimeoutExpired:
        _killpg(pgid)                                  # SIGTERM the whole tree
        try: proc.wait(30)                             # R4 P0: wait can itself time out…
        except subprocess.TimeoutExpired: pass         # …so never let it skip the SIGKILL
        _killpg(pgid, signal.SIGKILL)
        try: proc.wait(10)
        except subprocess.TimeoutExpired: pass
        rc, reason = proc.returncode, "timeout"
    finally:
        halt_watcher.stop()
    finalize(task, rc, reason, base_uris)
    req.unlink(); release_slot(task)

def _killpg(pgid, sig=signal.SIGTERM):
    try: os.killpg(pgid, sig)                          # whole tree: caffeinate+claude+grandchildren
    except ProcessLookupError: pass
```

- **Process ownership (resolves R3's setsid/AbandonProcessGroup contradiction):**
  the child runs in its **own session** (`start_new_session`), and the **Python
  runner owns its lifecycle** — it always `communicate`s/kills/reaps it.
  `AbandonProcessGroup` is therefore **moot** for the child (it's not relying on
  launchd to reap a same-pgid tree). The only orphan window is launchd SIGKILLing
  the *runner* itself before cleanup; covered by (a) a runner SIGTERM handler that
  `_killpg`s the child during launchd's graceful-stop grace period (`ExitTimeOut`
  ≥ 60), and (b) a **janitor sweep** at every runner/launcher start.
  **Janitor PGID-reuse safety (R4/R5 P0):** before `killpg(stored_pgid)` the
  janitor MUST prove the group leader is still *our* child via **process
  start-time** (the canonical PID-reuse defence — a recycled PID has a different
  start time, and start-time IS reliably queryable unlike env vars):
  `kill -0 stored_pid`, then `ps -o lstart= -p stored_pid` matches the pidfile's
  recorded `start_lstart` (captured at spawn) AND `ps -o pgid= -p stored_pid` ==
  `stored_pgid`. Mismatch ⇒ PID recycled ⇒ do NOT kill, just clear the stale
  pidfile. Never `killpg` on liveness alone. (`HANDOFF_TASK` env at spawn is for
  log/debug only — env is NOT visible to `ps -o command`, so it is **not** the
  reuse check; start-time is.)
- **Whole-tree kill (R3 ②):** `os.killpg(pgid, …)` hits caffeinate + claude +
  any bash grandchildren from tool calls. Grandchild-survives test mandatory (§5).
- **Timeout class (R3 ④):** classified by the `TimeoutExpired` branch, **not** by
  guessing rc 124 (a signal-killed child returns negative/128+sig — Python's
  `TimeoutExpired` is the authoritative signal).
- **`finalize` (R3 ④, name-baselined):**
  - timeout branch → `<task>.BLOCKED.md` "timeout".
  - `rc != 0` → `<task>.BLOCKED.md` "headless exit rc" (a retro/audit mandate gate
    hard-fail lands here → relay **parks visibly, never silent**).
  - `rc == 0` AND a `queue/<name>.uri` exists whose **name ∉ base_uris** → next
    leg dispatched, confirm `submitted-headless` (name-based ⇒ no clock-granularity
    false accept/reject).
  - `rc == 0` AND `queue/<task>.done` exists (dump status=done writes `.done`,
    unlinks `.uri` — [dump.py:397-405]) → **terminal success**, not blocked.
  - else → `<task>.BLOCKED.md` "completed without handoff artifact".

### 3.3 Power-assertion honesty (P1)

- `caffeinate -i` keeps the **system** awake for the *lifetime of a running
  headless child*, letting the display sleep — so launchd keeps firing and the
  child keeps running while a session is active. ✓
- It does **NOT** wake an already-sleeping Mac. If the Mac fully idle-sleeps in
  the gap between one child exiting (assertion released) and the next being
  claimed, the relay **resumes on next wake** (on wake launchd re-scans the GUI
  launcher's WatchPaths AND the headless `QueueDirectories`). This is
  **bounded-degraded, not broken** — the "~1 s impossible to sleep" claim from V1
  is **withdrawn**. (Optional future: a brief standing `caffeinate -i -t 180`
  held by the launcher across a claim, or `pmset` scheduled wake — out of scope.)

### 3.4 Lifecycle & safety — unattended `bypassPermissions` containment (P0 #4/#6, P1)

Minimum controls (all mandatory before enable):
1. **Per-project opt-in, default OFF** (§3.1). No project gets unattended headless
   without an explicit sentinel.
2. **STOP/done halts running work (P0 #4):** supervisor sweep at runner + launcher
   start: any live headless whose global/project `STOP_AUTO`/`done` now exists →
   `kill -TERM -<pgid>` (whole group), then `-KILL` after grace; clear pidfile.
   `暂停`/`永久停` must actually stop overnight agents.
3. **Hard timeout:** the Python runner's `communicate(timeout=HEADLESS_TIMEOUT)`
   (default 2700s) → on `TimeoutExpired`, `os.killpg` SIGTERM then SIGKILL the
   whole group (§3.2) ⇒ `BLOCKED.md` "timeout". No `gtimeout`/`/usr/bin/timeout`
   binary dependency.
4. **Worktree safety precheck:** refuse if working tree dirty in a way that risks
   loss, or branch is protected (e.g. would push to `main`), or `>N` concurrent
   headless already running. Write `BLOCKED.md` with reason.
5. **Env hygiene:** strip/῾scope secrets not needed by the task from the child env.
6. **Logging:** full stdout/stderr per task (replaces the lost "owner watched the
   tab" evidence).
7. **Post-run git verify:** after the child, record `git status`/HEAD delta to the
   log so a destructive run is auditable.
8. **Completion verify (P0 #6):** §3.2 `finalize_headless`.

### 3.5 Idempotency + concurrency (P1)

- Pidfile content (newline KV): `pid`, `pgid` (== pid via `start_new_session`),
  `start_epoch`, `start_lstart` (raw `ps -o lstart=` string captured at spawn),
  `task`. **PID-reuse defence = process start-time** (R5): liveness/janitor =
  `kill -0 pid` AND `ps -o lstart= -p pid` == recorded `start_lstart` AND
  `ps -o pgid= -p pid` == recorded `pgid` AND `now - start_epoch <
  HEADLESS_TIMEOUT + 60` — not bare `kill -0`. (No reliance on a `ps`-visible
  command/env marker, which `claude -p` does not provide.)
- **pgid is never racy**: `os.getpgid(proc.pid)` is a syscall on a child the
  runner just `Popen`ed with `start_new_session=True` (child is its own
  session/group leader ⇒ pgid==pid). A `Popen` that raises (ENOENT etc.) is the
  failed-start path → `failed` ack, no pidfile.
- Global cap `HANDOFF_MAX_HEADLESS` (default 1) across all projects, enforced by
  the **runner before spawn** (over cap ⇒ leave `.req`, re-tried next invocation);
  the cap can no longer be bypassed by a trigger because the trigger is
  non-killing `QueueDirectories` (§3.2), not `kickstart -k`. No fan-out of multiple bypass
  agents in one workspace.

### 3.6 autoclose + lock→unlock (P2)

- **GUI→headless tab (P2):** the *previous* GUI task already has its own
  `old_ready`/`submitted` → existing autoclose loop closes that tab independently;
  the new headless task simply has no tab. No new code; documented so it isn't
  mistaken for a leak.
- **lock→unlock collision (P2):** while any headless child is live, write a
  visible marker (`<project>/headless-active` + a `display notification` on
  start/finish) so a returning owner sees an invisible agent is editing the repo;
  STOP path (§3.4-2) verified to terminate it.

### 3.7 dump.py single-task atomicity (P1)

Single-task `dump` writes `<task>.md` ([dump.py:394]) and `<task>.uri`
([dump.py:428]) via `write_text` — not atomic. A supervisor kill mid-dump can
leave a partial file the launcher then misreads. Switch both to
**`atomic.atomic_replace`** (temp+`os.rename`, [atomic.py:63-94]) — **NOT
`write_with_fsync`** (that is durable in-place via `O_TRUNC`, [atomic.py:47-60],
which does not give crash-atomic replacement; R2 caught V2 naming the wrong
primitive). WatchPaths note: `atomic_replace`'s temp file MUST be named so it
never matches the launcher's `*.uri`/`*.md` globs (e.g. `.<task>.uri.tmpXXXX`);
the launcher only ever acts on the final renamed name, and an early WatchPaths
wakeup on the temp create is a harmless no-op (re-scan finds nothing actionable).
The batch path uses `write_with_fsync` today ([dump.py:669/680]); whether it
also needs `atomic_replace` is tracked separately (not this change's scope).

---

## 4. Failure-mode matrix (V2)

| Scenario | New behavior |
|---|---|
| locked + opted-in, `.uri` queued | launchd-owned runner spawns headless under `caffeinate -i`; chain continues |
| locked + NOT opted-in | defer (`.uri` kept) + notify; no dead tab, no risky agent |
| lock state UNKNOWN | fail-closed → defer + notify (never GUI-submit blind) |
| STOP_AUTO during headless run | supervisor sweep `kill -TERM -<pgid>` whole group; pidfile cleared |
| headless exceeds timeout | runner `communicate(timeout=)` → `os.killpg` group; `BLOCKED.md`; chain parks |
| headless `claude` exits nonzero / gate BLOCKED | `finalize_headless` writes `BLOCKED.md` + `failed` ack — **visible park, never silent** |
| `claude-rc.py` missing / E2BIG | stdin prompt avoids E2BIG; missing interp → nonzero → `BLOCKED.md` |
| Mac fully asleep before claim | resumes on wake (bounded-degraded; documented, not "fixed") |
| owner unlocks mid-run | run finishes; `headless-active` marker + notification visible; next task GUI |
| supervisor kill mid-dump | atomic dump (§3.7) → no partial `.md`/`.uri` |

## 5. Test plan

- `HANDOFF_LOCK_CHECK_CMD` stub → unlocked⇒GUI, locked+opt-in⇒headless-req written,
  locked+opt-out⇒defer, unknown⇒defer (fail-closed).
- runner with `HANDOFF_CLAUDE_HEADLESS_CMD=/bin/cat` stub → asserts: process-group
  spawn, **stdin** prompt = `queue/<task>.md`, `submitted-headless` ack, pidfile
  has pgid + `start_lstart`, log captured.
- timeout stub (sleeper > timeout) → `BLOCKED.md` + `failed`.
- nonzero-exit stub → `finalize` writes `BLOCKED.md` (not submitted).
- STOP mid-run → whole process group SIGTERMed; pidfile cleared.
- concurrency cap → 2nd `.req` over cap does not spawn; no QueueDirectories hot-loop.
- janitor start-time → a recycled PID (different `lstart`) is NOT killed, pidfile cleared.
- opt-in revoked between `.req` write and spawn → runner defers, does not spawn.
- dump atomicity → kill mid-write leaves no partial `.md`/`.uri` (temp+rename).
- guard conditionalization → headless dispatch works with VS Code NOT running.
- autoclose → `submitted-headless` not matched by `*.submitted` loop; prior GUI
  task still autocloses.
- regression → all 410 existing tests green; GUI path byte-identical when unlocked.

## 6. Open owner decisions (defaults; codex R2 to pressure-test)

- **(a)** headless opt-in default **OFF** (recommended) — explicit per-project enable.
- **(b)** permission posture: `bypassPermissions` + worktree/branch precheck
  (default) vs a stricter `acceptEdits` + allowlist.
- **(c)** model `opus` (default) vs `sonnet` (cheaper overnight).
- **(d)** timeout 45 min/run default.
- **(e)** `HANDOFF_MAX_HEADLESS=1` default.

## 7. Out of scope

- Removing the GUI path (kept for owner-present).
- Waking a fully-asleep Mac (`pmset` scheduled wake / WoL) — separate effort.
- Standing always-on power-assertion daemon.
- Engine gate semantics (dump/precheck/retro/audit) — only the single-task
  atomicity fix (§3.7) touches `dump.py`.
