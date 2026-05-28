# Architecture — the 5-layer defense

`handoff-fanout` solves four orthogonal failure modes (orphan tabs, git index hijack, stale baselines, missing fan-out/fan-in primitives) using five independent layers. Each layer makes a narrow guarantee. The combined effect is that **for a real hijack or orphan to land, multiple layers must fail simultaneously**, and the failures must be of different kinds (process scheduling, kernel file ops, shell `PATH`, and git plumbing).

This document walks through every layer with its concrete code, the failure it prevents, and how it composes with the others.

---

## Layer 1 — `git-guard`: PATH-injected `git` wrapper

**Code:** `src/handoff_fanout/git_guard/git`
**Tests:** `tests/test_git_guard.py` (15 cases)
**Source:** shipped in the wheel via `[tool.hatch.build.targets.wheel.shared-data]`; resolved at runtime by `handoff_fanout.git_guard.git_guard_dir()`.

### What it prevents

A sub-task tab calling `git commit`, `git push`, `git rebase`, `git cherry-pick`, `git reset`, `git revert`, `git tag`, `git am`, `git format-patch`, or `git merge`. Only the fan-in tab is allowed to commit; sub-tasks must not even *attempt* it.

### How

The wrapper is a plain shell script named `git`. The role environment writes it to the front of `PATH`:

```bash
export PATH="<git-guard-dir>:$PATH"
```

When the sub-task tab spawns a shell and the user (or the agent) types `git commit -m ...`, the shell resolves `git` against `PATH` left-to-right and lands on the wrapper. The wrapper inspects `$1`:

```
if HANDOFF_ROLE = "sub-task"
and $1 ∈ {commit, push, rebase, cherry-pick, reset, revert, tag, am, format-patch, merge}
    → exit 1 with a clear error mentioning the role and what to do instead
else
    → exec the real git via `/usr/bin/git` or `/opt/homebrew/bin/git` (resolved at install time)
```

### Why this is necessary (it sounds belt-and-braces)

In a multi-tab AI workflow, the agent inside a sub-task tab will absolutely try `git commit -m "..."` if the user nudges it. The agent has no awareness that a peer tab is mid-edit. Without Layer 1, the only thing stopping it is etiquette.

### Failure mode

If the agent invokes `git` via an absolute path (`/usr/bin/git commit ...`), Layer 1 is bypassed. That is intentional — the wrapper is **role-based discouragement**, not a security boundary. The next layer catches absolute invocations.

---

## Layer 2 — pre-commit hook: `HANDOFF_EXPECTED_FILES` invariant

**Installed by:** `install/install.sh`
**Hook source:** `install/git-hooks/pre-commit`
**Triggered by:** any real `git commit` (regardless of `PATH`).

### What it prevents

A `git commit` that picks up files outside of an explicit expected set. The classic hijack scenario: Tab A runs `git add tab-a.py` and is about to `commit`; Tab B (unaware) runs `git add tab-b.py && git commit -m "B"`. With no protection, Tab B's commit gets `{tab-a.py, tab-b.py}` because `.git/index` is shared.

### How

The hook reads `HANDOFF_EXPECTED_FILES` from the environment (a `:`-separated list of workspace-relative paths). It computes:

```
staged   := git diff --cached --name-only
expected := split HANDOFF_EXPECTED_FILES on ":"
extra    := staged − expected
```

If `extra` is non-empty, the hook prints a diagnostic and exits non-zero, blocking the commit. The `handoff safe-commit` wrapper (Layer 3) is the canonical setter of `HANDOFF_EXPECTED_FILES`; manual `git commit` invocations from a sane shell will simply pass through (no env var = no check).

### Why this catches absolute-path bypass

Even if the agent calls `/usr/bin/git commit ...` and skips Layer 1, the **server-side** of the commit (the hook) is still invoked by git itself. The hook lives in `.git/hooks/pre-commit` of the target repo, so it cannot be bypassed without `--no-verify`.

### Failure mode

The agent passes `--no-verify`. Layer 3 catches that.

---

## Layer 3 — `safe-commit`: `flock` + invariant + hook-aware wrapper

**Code:** `src/handoff_fanout/safe_commit.py`
**Tests:** `tests/test_safe_commit.py` (9 cases) + `tests/test_handoff_hijack.py` (8 cases)

### What it prevents

The race between `git add` and `git commit` that allows Tab B to sweep in Tab A's `add`. Combined with Layer 2, also prevents `--no-verify` escapes.

### How

`safe-commit FILES... -- -m "msg"` does, in order:

1. **Acquire a flock** on `$HANDOFF_HOME/git-commit.lock`. Cross-process exclusive lock. If another process holds it, retry up to 5 times with stale-lock recovery (5 min ttl).
2. **Set `HANDOFF_EXPECTED_FILES=FILES`** in the child env. This is the contract Layer 2 verifies.
3. **`git add -- FILES`** — only the explicitly named paths, never `-A`.
4. **`git commit --only FILES -m "msg"`** — `--only` is git's belt to Layer 2's braces. Even without the hook, only the named files are committed.
5. **Verify `git diff --cached --name-only` ⊆ FILES** after the commit (post-condition). If anything else is in the new commit, abort and write a forensic file under `$HANDOFF_HOME/<project>/incidents/`.
6. **Release the flock** in a `finally`.

If `HANDOFF_SAFE_COMMIT_BYPASS=1` is set, the wrapper still acquires the lock (so peers wait) but skips the invariant check. This is the explicit escape hatch — use it consciously, and the reason should appear in the commit message.

### Why all four together

Layer 1 stops the *intent* (don't type commit). Layer 2 stops the *server* (hook rejects). Layer 3 stops the *race* (lock serializes) and adds a *post-condition* (verify what actually landed). The hooks alone don't protect against `--no-verify`; the lock alone doesn't protect against forgotten `git add`; only the combination is hijack-proof.

### Failure mode

`HANDOFF_SAFE_COMMIT_BYPASS=1` + ignoring the post-incident file. That is now a human policy problem, not a tool problem.

---

## Layer 4 — atomic file primitives

**Code:** `src/handoff_fanout/atomic.py`
**Tests:** `tests/test_atomic.py` (10 cases)

### What it prevents

A consumer reading a half-written queue file or a torn batch manifest, then making a wrong decision (e.g. spawning a sub-task tab into an env-less husk).

### How

Three primitives:

```python
atomic_create(path)
    # open(O_CREAT | O_EXCL | O_WRONLY) → fsync(fd) → fsync(dir)
    # Two concurrent callers: one gets None, the other raises FileExistsError.

write_with_fsync(path, content)
    # tempfile in sibling dir → write → fsync(temp) → os.replace(temp, path) → fsync(dir)
    # Readers always see old or new content, never a partial write.

with acquire_dir_lock(dir):
    # flock(dir_fd, LOCK_EX) → yield → flock(dir_fd, LOCK_UN)
    # Used by safe-commit and by dump's batch-init sequence.
```

`os.replace` is POSIX-atomic on the same filesystem. `fsync(dir)` ensures the rename is durable across power-loss. Both `dump` and `watchdog` use these for every queue/batch write.

### Why this matters

A sub-task tab whose `.env` file is half-written will source garbage and run with the wrong `PATH`, defeating Layer 1. Atomic file primitives are how all the upper layers stay honest.

### Failure mode

The underlying filesystem doesn't honor `fsync` (e.g. some virtualized environments). The tool can't fix that; it documents the requirement and CI runs on real ext4 / APFS.

---

## Layer 5 — watchdog: orphan, stale, heartbeat scan

**Code:** `src/handoff_fanout/watchdog.py`
**Tests:** `tests/test_handoff_orphan.py` (15 cases)
**Trigger:** `launchd` `WatchPaths` (macOS) or `cron`/`systemd-timer` (Linux), every 60s.

### What it prevents

A batch where the last sub-task crashes silently. Without Layer 5, the fan-in handoff is never written and the batch hangs forever.

### How

For every batch under every project, the scanner inspects:

| Condition | Mode | Action |
|---|---|---|
| All sub-tasks have `.done` or `.blocked`, but no `_fanin_triggered` | `last-one-out` | Write `_fanin_triggered` + dump fan-in. |
| Sub-task `.heartbeat` mtime > 3 min | `stale-heartbeat` | Write `<id>.blocked` with reason; re-run last-one-out check. |
| `_fan_in_heartbeat` mtime > 3 min | `fan-in-stalled` | Re-dump fan-in handoff (idempotent). |
| `ack/<task>.spawned` exists, no `queue/<task>.md` / `.done` / `.BLOCKED.md` | `orphan-sub-task` | Write `queue/<task>.BLOCKED.md` (orphan template). |

The scanner is fully idempotent: running it twice in the same second produces the same end state. It holds no locks; it relies on Layer 4 atomicity for all writes.

### Why a separate process

If the fan-in detection logic lived inside the last sub-task tab, a `kill -9` of that tab would deadlock the batch forever. The watchdog is a **separate scheduler** that doesn't share fate with any participant.

### Failure mode

`launchd` itself is stopped. The user has bigger problems. The watchdog is the recovery net; if it's down, the user will manually rerun it (`handoff watchdog`) or restart the LaunchAgent.

---

## How the layers compose: the hijack scenario

Suppose Tab A and Tab B are racing. Tab A staged `src/a.py`, then context-switched. Tab B is now about to commit `src/b.py`.

```
Tab A                                Tab B
─────                                ─────
git add src/a.py                     (sleeping)
(context switch)
                                     git add src/b.py
                                     git commit -m "B's work"

Without handoff-fanout:
                                     ┌──────────────────────┐
                                     │ HEAD commit contains │
                                     │ {src/a.py, src/b.py} │
                                     │ — TAB A's WORK GONE  │
                                     └──────────────────────┘
```

With `handoff-fanout`:

```
Tab A                                Tab B
─────                                ─────
handoff safe-commit src/a.py -m "A"  handoff safe-commit src/b.py -m "B"
  └─ Layer 3: flock acquire OK         └─ Layer 3: flock acquire WAITS
  └─ Layer 3: HANDOFF_EXPECTED=a.py
  └─ Layer 3: git add -- a.py
  └─ Layer 3: git commit --only a.py
       └─ Layer 2: pre-commit hook
          checks staged ⊆ {a.py} ✓
  └─ Layer 3: post-cond: HEAD diff ⊆ {a.py} ✓
  └─ Layer 3: flock release
                                       └─ Layer 3: flock acquire OK (now)
                                       └─ Layer 3: HANDOFF_EXPECTED=b.py
                                       └─ Layer 3: git add -- b.py
                                       └─ Layer 3: git commit --only b.py
                                            └─ Layer 2: hook checks ⊆ {b.py} ✓
                                       └─ Layer 3: post-cond OK
                                       └─ Layer 3: flock release
```

The flock serializes them; the invariant checks the staged set; the hook is the server-side guard. **Three independent mechanisms must all fail for a hijack to land.**

## How the layers compose: the orphan scenario

```
Time          Event                                Layer responsible
────          ─────                                ─────────────────
00:00         dump --open-batch (3 sub-tasks)      Layer 4 (atomic writes)
00:00         launchd opens 3 IDE tabs             —
00:01         sub-task A finishes → .done          —
01:00         sub-task B finishes → .done          —
01:30         sub-task C crashes (no .done)        —
02:00         (nothing happens; batch is stuck)
04:30         watchdog tick                        Layer 5
              C.heartbeat mtime = 04:30 - 3min ago
              → stale-heartbeat → C.blocked written
              → last-one-out check passes
              → _fanin_triggered + fan-in dumped
04:31         fan-in tab spawns, handles degraded mode
```

Without Layer 5, the batch is stuck. With it, mean time-to-recovery is bounded by the watchdog tick interval (default 60s).

---

## Comparison to alternative architectures

### "Why not a database?"

A queue file on disk gives you atomic `os.replace`, plain-text inspectability, easy backup, no daemon to keep running, and zero dependencies. A SQLite or Postgres queue would add a runtime dependency and a daemon, and the user would still need a watchdog process anyway.

### "Why not Celery?"

Celery is built around **distributed worker pools** consuming **immutable tasks** from a **broker**. Our workers (AI coding tabs) are not interchangeable: each has IDE state, agent context, and tab identity. Submitting a task to "any worker" is wrong here. Celery's strengths (durability, retries, result backends) don't map; its costs (broker, monitoring) are high.

### "Why not Argo Workflows?"

Argo runs DAGs on Kubernetes pods. Our DAG node is "open a new IDE tab on this laptop and let a human watch the agent." Not a Kubernetes problem.

### "Why not Temporal?"

Temporal is the gold standard for durable workflow execution across distributed services. The cost is a Temporal server, a database, and SDK boilerplate. For one workstation orchestrating IDE tabs, file-based queues plus a watchdog are 1% of the operational cost.

---

## Source-of-truth pointers

| Concern | File |
|---|---|
| Schema constants | `src/handoff_fanout/dump.py` (top of file) |
| Config dataclass | `src/handoff_fanout/config.py` |
| Markdown templates | `src/handoff_fanout/templates.py` |
| Single-task dump | `src/handoff_fanout/dump.py` (`write_active_dump`) |
| Fan-out dump | `src/handoff_fanout/dump.py` (`handle_open_batch`) |
| Atomic primitives | `src/handoff_fanout/atomic.py` |
| Safe-commit | `src/handoff_fanout/safe_commit.py` |
| Watchdog | `src/handoff_fanout/watchdog.py` |
| Heartbeat & metrics | `src/handoff_fanout/heartbeat.py` |
| PATH-injected git | `src/handoff_fanout/git_guard/git` |
| Wire format | [`docs/PROTOCOL.md`](PROTOCOL.md) |
