# 派窗机制架构图 — SPAWN + WORKTREE + FOCUS-JUMP + LOCK + CONFIG + CLI

Shard: 派窗 mechanics. Repo `/Users/chenmingzhong/Projects/handoff-fanout`, git HEAD `5e8d7b2`. Read-only analysis.

Scope files: `spawn.py`, `worktree.py`, `spawner_focus.py`, `spawn_lock.py`, `config.py`, `cli.py` (+ cross-reference `dump.py` `_spawner_focus_line` and `install/auto-continue.sh` for the consumer wiring).

---

## 1. 运行时角色 — 派窗 how it works

派窗 produces a *spawn intent* (a set of sidecar files under `$HANDOFF_HOME/<project>/`); a launchd-driven watchdog (`install/auto-continue.sh`) consumes the intent and opens the VS Code window. The producer never opens a window itself.

End-to-end role chain:

1. **`dx-spawn-session.sh --project <fullpath> --brief <f> --task-id <id>`** (cross-project spawn entry, owner / coordinator invokes). The shared spawn engine consults the **registry `worker_isolation`** map to pick a route.
2. **registry `worker_isolation` routing** → `config.worker_isolation_for(project)` returns `"worktree" | "singlepane" | None` (`config.py:234`). `None` ⇒ caller MUST fail closed — the engine never guesses an isolation mode (design §2.2 no-guess, `config.py:139-147`).
3. **`handoff spawn`** (`spawn.run_spawn`, `spawn.py:495`) is the fresh-spawn intent producer. NO retro-mandate gate (never exits 4 — `spawn.py:74`), NO roadmap injection. It validates identity (kebab slug ≤60), enforces the unified-spawn switch, and branches on `--isolation`.
   * `--isolation worktree` → `_spawn_worktree` → `worktree.create_worktree` (per-session git worktree).
   * `--isolation singlepane` → `_spawn_singlepane` → out-of-tree `.handoff.code-workspace` over the real repo (no tree dirtying).
4. **去程 focus-jump (SPAWNER_FOCUS)**: the spawning coordinator's own `.handoff.code-workspace` path is resolved and written as a `SPAWNER_FOCUS=<path>` line into `queue/<task>.uri`. The watchdog exports it as `$HANDOFF_SPAWNER_FOCUS`; `code-router.sh` runs `code <SPAWNER_FOCUS>` to slide macOS to the spawner's Space in ONE native step *before* opening the worker → the worker is born on the coordinator's desktop. Resolution has 3 tiers (see §3).
5. **unpushed-HEAD gate**: for worktree mode, `create_worktree` BLOCKS (does not degrade) if source HEAD is not published to `origin/<integration-branch>` (`worktree.py:922-929`) or local `<int>` is ahead of origin (`worktree.py:933-941`) — the successor would otherwise branch from stale/lost code.

---

## 2. 核心模块

| file | 责任 | LOC |
|---|---|---|
| `spawn.py` | `handoff spawn` fresh-spawn intent producer: identity/arg validation, succession-token (G4 收口), worktree-vs-singlepane orchestration, sidecar+uri+workspace JSON writers, rollback, singlepane §5.4 one-worker REJECT, focus-jump resolution glue. | 761 |
| `worktree.py` | per-session git worktree lifecycle: mode resolution, integration-branch resolution, dirty/published classification, `create_worktree` (degrade vs BLOCK), `.handoff.code-workspace` injection incl. red-top coordinator marker + session-identity env, orphan reclaim state machine, GC. | 1491 |
| `spawner_focus.py` | THE single security gate for the去程 focus path: `validate_spawner_focus` (realpath/suffix/allowed-root), `derive_singlepane_focus` (self-report from task), `resolve_spawner_focus_path` (Tier-1 cwd / Tier-2 singlepane). | 135 |
| `spawn_lock.py` | `project_spawn_lock` atomic-mkdir project mutex: bounded retry loop, TTL stale-break, `wait` param for parallel worktree workers, always-release finally. | 85 |
| `config.py` | `$HANDOFF_HOME/config.json` loader: paths, `home_dir`, `worker_isolation` routing, `singlepane_projects`, `unified_spawn_enabled` fail-closed parse, worktree/reclaim blocks. | 571 |
| `cli.py` | `handoff <subcommand>` dispatcher; lazily imports each module's `main()`. `gc-singlepane` newly added. | 177 |

---

## 3. 工作机制 (with file:line)

### Spawn flow end-to-end
- Entry `spawn.run_spawn` (`spawn.py:495`). Validates project/task slug (`spawn.py:520-525`), exactly-one of `--brief`/`--prompt` (`spawn.py:526`), close_policy enum (`spawn.py:543`), succession-token G4 收口 (`spawn.py:548-565`: `supervisor_succession` is NOT a manual path — needs the one-time token issued by retro-gated `audit-close --coordinator --status active`).
- `config.load()` (`spawn.py:567`); refuse if `unified_spawn_enabled` False (untrusted/disabled config, `spawn.py:571`).
- source dir resolution + close_policy default (`spawn.py:575-594`); a succession token is consumed LAST just before produce (`spawn.py:600-615`), bound to THIS task.
- `nonce = _spawn_nonce.new_nonce()` (`spawn.py:617`); prompt built with `🆔<task>` prefix (`spawn.py:95-103`).
- focus path resolution (`spawn.py:621-648`): CLI `--spawner-focus-path` → `validate_spawner_focus`; if None, env-independent `resolve_spawner_focus_path(cwd, …, self_task)`.
- dispatch on isolation (`spawn.py:664-667`): `_spawn_worktree` or `_spawn_singlepane`.
- succession spawn writes a memory baseline AFTER publish (`spawn.py:672-675`).

### Worktree创建 + workspace JSON injection (red-top)
- `_spawn_worktree` (`spawn.py:381`) wraps `_produce_worktree` (`spawn.py:417`) under `project_spawn_lock(..., wait=120.0)` (`spawn.py:393`, `_WORKTREE_LOCK_WAIT` `spawn.py:378`).
- `create_worktree` (`worktree.py:821`): mode resolution → fetch tracking ref → unpushed-HEAD BLOCK gates (`worktree.py:922-941`) → collision classify (`worktree.py:949-977`) → REUSE branch for clean+published same-base worktree (`worktree.py:984-1015`, sets `reused=True`) → else `git worktree add -b` (`worktree.py:1033`).
- `inject_vscode_workspace` (`worktree.py:658`): writes `.handoff.code-workspace` with nonce-bound `window.title` (`title_for`), single-pane UX keys, and — when `is_coordinator` — the `🧭中枢·` title prefix + red titleBar `colorCustomizations` (`worktree.py:775-789`). `is_coordinator` is derived from `role == ROLE_SUCCESSION` (`spawn.py:431`) — single source of truth.
- Red-top is also idempotently patched onto a REUSED workspace file via `_ensure_coordinator_redtop` (`worktree.py:607`), with a NON-silent WARN if un-patchable (`worktree.py:597`, `_warn_coordinator_unredtopped`).
- MUST 1 fail-closed: if the workspace title cannot carry this spawn's nonce (user-tracked file / write failure → `inject_vscode_workspace` returns None), the spawn refuses (`spawn.py:449-462`).

### spawner_focus 3 tiers
- **Tier-1 WORKTREE** (`spawner_focus.py:124-128`): `<cwd>/.handoff.code-workspace` — a worktree coordinator's cwd IS its worktree, so it self-identifies with no flag.
- **Tier-2 SINGLEPANE** (`spawner_focus.py:129-134` → `derive_singlepane_focus`, `spawner_focus.py:63`): a singlepane coordinator's cwd is the shared repo root, so it can't self-identify from cwd. It self-reports its own task via `--self-task`; the engine reconstructs `<home>/<project>/singlepane/<self_task>.handoff.code-workspace` (the path the engine wrote at coordinator spawn). Returns the path only if it exists as a regular file.
- **env path (legacy / Tier-0)**: `$HANDOFF_WINDOW_FOCUS_PATH` read first in `dump._spawner_focus_line` (`dump.py:797`) and as `--spawner-focus-path` on spawn (`spawn.py:627`). Empty in extension-auto-spawned agent shells (see §5/§6).
- Every candidate is re-validated by `validate_spawner_focus` (`spawner_focus.py:28`): realpath+`~`-expand, absolute, ends `.handoff.code-workspace`, isfile, lives under an allowed root (handoff home / `~/.claude-handoff` / tempdir / `$TMPDIR` / `/tmp` / `/private/tmp`). One security boundary, shared by spawn and dump.

### spawn_lock concurrency contract
- `project_spawn_lock` (`spawn_lock.py:24`): atomic `mkdir` acquire (`spawn_lock.py:47`) — macOS has no portable flock. TTL default 120s (`spawn_lock.py:30`), `max_stale_breaks=5` (`spawn_lock.py:31`), `wait=0.0` default non-blocking (`spawn_lock.py:32`).
- **singlepane** uses default `wait=0.0` → immediate `LockHeld` raise on contention (`spawn.py:299`), so the §5.4 hard-REJECT and the watchdog skip-on-contention fire instantly.
- **parallel worktree workers** pass `wait=120.0` (`spawn.py:393`): legitimate concurrency (design §2.2) — they mutate the SAME source repo's `.git/config`/tracking refs, so they QUEUE rather than reject. The wait is bounded and polls (`spawn_lock.py:60-61`); a stale lock is broken immediately regardless of wait (`spawn_lock.py:67-79`).
- Crash-free under its own race: when two workers race to break the same stale lock, the loser's re-`mkdir` raises `FileExistsError` → re-inspect → rival's FRESH lock yields clean `LockHeld` (`spawn_lock.py:59-66`); churn is capped by `max_stale_breaks` (`spawn_lock.py:71-77`). Always-release in `finally` (`spawn_lock.py:83-85`).

---

## 4. 数据流/状态流 — files / sidecars / locks (all under `$HANDOFF_HOME = ~/.claude-handoff` in live config)

| artifact | path | producer | consumer | role |
|---|---|---|---|---|
| `.uri` (launchd trigger, written LAST) | `<home>/<project>/queue/<task>.uri` | `_write_uri` (`spawn.py:111`) / `dump` | launchd `WatchPaths` → `auto-continue.sh` | `WORKSPACE=`+`URI=`(+optional `SPAWNER_FOCUS=`). `WORKSPACE` under `*/worktrees/*` ⇒ COLD path; real repo ⇒ SINGLEPANE path. |
| singlepane sidecar | `<home>/<project>/queue/<task>.singlepane` | `_write_sidecar` (`spawn.py:133`) | watchdog `json_get` + `try_autoclose` | compact single-line JSON: `workspace/role/close_policy/spawn_nonce/isolation/predecessor_nonce` (+optional `wave_id`). |
| singlepane workspace file | `<home>/<project>/singlepane/<task>.handoff.code-workspace` | `_produce_singlepane` (`spawn.py:333`) / `dump.maybe_write_singlepane_sidecar` | VS Code (opened by watchdog) | out-of-tree, `folders`→real repo, nonce title, session-identity env. THE Tier-2 focus target reconstructed by `derive_singlepane_focus`. |
| worktree workspace file | `<worktree>/.handoff.code-workspace` (`WORKTREE_VSCODE_FILE`, `worktree.py:51`) | `inject_vscode_workspace` (`worktree.py:658`) | watchdog `find` (COLD), VS Code | nonce title + `.vscode` symlink + red-top if coordinator. |
| worktree dir + branch | `<home>/<project>/worktrees/<task>/` (`worktree_path`, `worktree.py:357`), branch `handoff/<task>` (`branch_name`, `worktree.py:361`) | `create_worktree` | session, GC | isolated git tree/index/HEAD over shared object store. |
| project spawn lock | `<home>/<project>/.spawn.lock` (`spawn_lock.py:41`) | `project_spawn_lock` | spawn / dump / autoclose | atomic-mkdir mutex; mtime at acquire. |
| worktree GC sidecar | `<home>/<project>/ack/<task>.worktree` | dump | `find_reclaimable` (`worktree.py:1277`), `gc` (`worktree.py:1350`) | records path/branch/source for reclaim. |
| heartbeat | `<home>/<project>/queue/<task>.heartbeat` | session | `_heartbeat_fresh` (`worktree.py:1263`) | a fresh touch (<600s) marks a LIVE worktree → never GC'd. |
| succession token | issued by `audit-close --coordinator --status active`, consumed `spawn.py:603` | retro-gated audit-close | `spawn.run_spawn` | one-time authority for `supervisor_succession` (G4 收口). |

---

## 5. 现状三态

- ✅ **`handoff spawn` worktree + singlepane producers** — full, fail-closed, lock-serialized, with rollback (`spawn.py:363-366`, `478-487`).
- ✅ **Red-top coordinator marker** — fresh-create + reuse paths both apply it, non-silent WARN on failure (`worktree.py:775-789`, `607-655`).
- ✅ **spawn_lock atomic mutex** — crash-free race handling, bounded stale-break, always-release.
- ✅ **`unified_spawn_enabled` / `worker_isolation` fail-closed config** (`config.py:416-476`).
- ✅ **SPAWNER_FOCUS去程 wired end-to-end on BOTH dump and spawn** — see below.
- ✅ **Tier-1 (worktree cwd) + Tier-2 (singlepane `--self-task`) self-identification** — env-independent.
- 🟡 **env-path channel `$HANDOFF_WINDOW_FOCUS_PATH`** — present in every engine-produced workspace file (`session_env_osx`, `worktree.py:538`), but DORMANT for extension-auto-spawned singlepane coordinators (does not reach their agent shell). Tiers 1/2 are the live path; this is a degraded-but-tolerated fallback.
- 🟡 **`add_worktree_or_reclaim_orphan`** — a PARALLEL primitive NOT yet wired into `create_worktree` (`worktree.py:1153-1159`); the live add path is `create_worktree`'s inline `git worktree add` (`worktree.py:1033`).
- ❌ **lock heartbeat / mtime refresh** — none; the holder's lock mtime is frozen at acquire (the §7 P1).

### Is SPAWNER_FOCUS去程 wired end-to-end on BOTH paths? — YES.
- **spawn path**: resolved in `run_spawn` (`spawn.py:621-648`), passed to both `_produce_singlepane` (`spawn.py:362`) and `_produce_worktree` (`spawn.py:477`), written into `.uri` by `_write_uri` (`spawn.py:124-127`).
- **dump path**: `dump._spawner_focus_line` (`dump.py:781`) appended to the `.uri` body at every dump write site (`dump.py:1038`, `1317`, `1407`).
- **consumer**: `auto-continue.sh` parses the `.uri` and `export HANDOFF_SPAWNER_FOCUS` regardless of producer (`auto-continue.sh:1422-1423`); `code-router.sh` runs the one-step focus-jump when set, else falls back to the per-project goto (`auto-continue.sh:1419-1421`). Symmetric — same consumer for both producers. Also drives the **回程** re-activation anchor armed ONLY for a SPAWNER_FOCUS spawn (`auto-continue.sh:1316-1322`).

### Is the env-path channel reaching the agent shell or dormant? — DORMANT for extension-auto-spawned singlepane coordinators.
`spawner_focus.py:63-79` and `dump.py:788` document the p19/p21 finding: `$HANDOFF_WINDOW_FOCUS_PATH`, injected via VS Code workspace `terminal.integrated.env.osx`, does NOT reach the agent shell of an extension-panel auto-spawned singlepane coordinator → the env-based path yields `""`. The Tier-1/Tier-2 self-identification (`resolve_spawner_focus_path`) was added precisely to route AROUND this dead channel. The env injection is still written (harmless, additive) and DOES reach worktree coordinator terminals, which also have the Tier-1 cwd signal as primary.

---

## 6. 🔴 半实现陷阱

1. **env-path focus channel that doesn't reach the agent shell (现象)**: `$HANDOFF_WINDOW_FOCUS_PATH` is injected into every engine workspace's `terminal.integrated.env.osx` (`worktree.py:534-557`, `spawn.py:202-204`) as if it were the focus source, but for extension-auto-spawned singlepane coordinators it is empty in the agent shell (`dump.py:788`, `spawner_focus.py:66-71`). **后果**: if Tier-1/Tier-2 had not been added, a singlepane coordinator's worker would silently NOT jump to its desktop (fail-open swallows it — no error). **正解 (already applied)**: env path is now best-effort Tier-0; the live mechanism is `resolve_spawner_focus_path` Tier-1 (cwd worktree) + Tier-2 (`--self-task` singlepane sidecar). Residual trap: a singlepane coordinator that omits `--self-task` falls back to fail-open (no jump) silently — the worker still spawns. This is by-design fail-open (a UX hint must never block a spawn), but it IS a silent UX degrade if `--self-task` is forgotten (MEMORY notes the「singlepane 中枢必传 `--self-task`」requirement).

2. **`add_worktree_or_reclaim_orphan` is a parallel-but-unwired primitive (现象)**: it exists with full orphan-reclaim logic and an explicit RED-TOP INVARIANT warning (`worktree.py:1118-1196`) but is NOT called by `create_worktree` (`worktree.py:1153`). **后果**: a future refactor that routes the add through it WITHOUT re-calling `inject_vscode_workspace` would open a reclaimed coordinator worktree with NO red title bar — silently breaking「只要是中枢窗口就必须红顶」. Today: no bug (it's unwired), but it is a latent footgun documented inline. **正解**: the docstring already mandates keeping `inject` at the caller layer; no live defect.

3. **`fetch` failure on the unpushed-HEAD gate fails OPEN with a warning, not a block (现象)**: `create_worktree` warns + proceeds on a fetch failure (`worktree.py:914-915`) relying on the tracking ref staying authoritative. **后果**: a stale tracking ref only risks a SAFE false-BLOCK (the session re-dumps), never a wrong-base spawn (documented `worktree.py:905-907`). Acceptable; not a true trap.

No other silently-firing/never-firing path found in this shard. The spawn_lock TTL stale-break (§7) is the one genuine concurrency hazard.

---

## 7. 🔴 KNOWN P1 — spawn_lock TTL stale-break vs a long critical section — CONFIRMED REAL

**Claim**: the lock-dir mtime is set at acquisition and never refreshed (no heartbeat); `ttl=120s`. If a holder's critical section (`create_worktree`) runs >120s, a waiter computes `age ≥ ttl` and stale-breaks the lock → two concurrent critical sections.

**Verdict: CONFIRMED (real).**

Evidence:
- mtime is established only at acquire: `lockdir.mkdir()` (`spawn_lock.py:47`). There is no `os.utime`/touch anywhere in the held region — `yield` runs the caller's critical section (`spawn_lock.py:82`) with the mtime frozen, and the only other filesystem op is `rmdir` on release (`spawn_lock.py:85`). No heartbeat exists.
- A waiter computes age from that frozen mtime: `age = time.time() - lockdir.stat().st_mtime` (`spawn_lock.py:53`).
- The staleness test is purely `age` vs `ttl`, with NO liveness check (no PID, no process probe): `if age < ttl:` (`spawn_lock.py:59`). When `age ≥ ttl` the code falls through to the stale-break branch (`spawn_lock.py:67-79`): `stale_breaks += 1` then `lockdir.rmdir()` (`spawn_lock.py:79`) and re-loops to re-acquire.
- `ttl` default is `120.0` (`spawn_lock.py:30`).
- The worktree caller deliberately runs with `wait=120.0` (`spawn.py:393`, `_WORKTREE_LOCK_WAIT` `spawn.py:378`) so a queued worker polls for up to 120s — meaning a waiter is actively present right around the TTL boundary.

Mechanism: `create_worktree` does network `git fetch` (30s timeout, `worktree.py:909-913`) and `git worktree add` (60s timeout, `worktree.py:1034`) plus several `git` calls. Under a slow remote / large repo, the held critical section CAN exceed 120s. A second worktree worker that has been polling sees `age ≥ 120` and `rmdir`s the still-held lock (`spawn_lock.py:79`), then `mkdir`s its own — so the original holder is STILL inside `create_worktree` while worker #2 enters its own critical section. Both then mutate the same source repo's `.git/config` / tracking refs concurrently — the exact "could not lock config file" / index-clash class the lock exists to prevent.

Aggravating detail: on release the original holder does `lockdir.rmdir()` in `finally` (`spawn_lock.py:84-85`) suppressing OSError — but by then worker #2 owns a freshly-mkdir'd lockdir, so the holder's `rmdir` may delete worker #2's lock (different inode, same path) → a THIRD waiter could then acquire. The suppression hides the cross-delete.

This is a real correctness hazard, NOT theoretical: the design comment at `spawn.py:376-377` itself acknowledges "aligned with the wait's order of magnitude with the lock TTL (a crashed holder is stale-broken anyway)" — i.e. wait and TTL are BOTH 120s, so a legitimately-slow (not crashed) holder and a patient waiter collide exactly at the boundary. Mitigations to consider (not in scope to implement): a held-lock heartbeat (`os.utime` on a timer), a PID liveness check before stale-break, or `ttl` > worst-case `create_worktree` wall time (fetch 30s + add 60s + overhead ⇒ raise to e.g. 300s).

---

## 8. 承重事实 file:line 清单

1. `handoff spawn` never exits 4 (no retro gate) — `spawn.py:74` (`EXIT_FAIL_CLOSED = 2`).
2. Isolation dispatch worktree-vs-singlepane — `spawn.py:664-667`.
3. `supervisor_succession` requires a one-time token (G4 收口), not a manual path — `spawn.py:558-565`; consumed bound-to-task at `spawn.py:600-615`.
4. `is_coordinator` derived from `role == ROLE_SUCCESSION` (single source of truth) — `spawn.py:431`, `spawn.py:195-197`.
5. Singlepane §5.4 one-active-worker hard REJECT under the lock — `spawn.py:300-310`.
6. Worktree parallel workers QUEUE on `wait=120.0` — `spawn.py:378`, `spawn.py:393`.
7. SPAWNER_FOCUS written into `.uri` on both produce paths — `spawn.py:362` (singlepane), `spawn.py:477` (worktree), via `_write_uri` `spawn.py:124-127`.
8. spawner_focus 3-tier resolution + same security gate — `spawner_focus.py:102-135` (resolve), `spawner_focus.py:28-60` (validate), `spawner_focus.py:63-84` (derive singlepane).
9. env channel `$HANDOFF_WINDOW_FOCUS_PATH` documented dormant for extension-auto-spawned singlepane — `spawner_focus.py:66-71`, `dump.py:788`.
10. dump-side parity: `_spawner_focus_line` reads env first then Tier-1/2 — `dump.py:797-806`; appended at `dump.py:1038`/`1317`/`1407`.
11. Watchdog consumes SPAWNER_FOCUS regardless of producer — `install/auto-continue.sh:1422-1423`; arms the回程 anchor only for a SPAWNER_FOCUS spawn — `auto-continue.sh:1316-1322`.
12. spawn_lock atomic mkdir + TTL 120s + no heartbeat — `spawn_lock.py:47`, `spawn_lock.py:30`.
13. spawn_lock stale-break has NO liveness check (age-only) — `spawn_lock.py:59` + `spawn_lock.py:67-79`.
14. Unpushed-HEAD BLOCK (not degrade) — `worktree.py:922-929` (source HEAD), `worktree.py:933-941` (local ahead of origin).
15. Worktree REUSE adoption path sets `reused=True` to avoid rollback-removal of another session's worktree — `worktree.py:984-1015`; rollback respects it — `spawn.py:460-461`, `spawn.py:485-486`.
16. Red-top coordinator title prefix + red titleBar — `worktree.py:509-515`, applied `worktree.py:788-789`; reuse-path patch `worktree.py:607-655`; non-silent WARN `worktree.py:597-604`.
17. `worker_isolation_for` returns `None` ⇒ caller fail-closed (no guess) — `config.py:234-240`, `config.py:457-476`.
18. `unified_spawn_enabled` fail-closed on untrusted config (no `bool("false")` footgun) — `config.py:416-451`, `config.py:295-309`; refused at `spawn.py:571`.
19. `WORKTREE_VSCODE_FILE = ".handoff.code-workspace"` fixed engine name (exact-match in `is_dirty`) — `worktree.py:51`, `worktree.py:214`.
20. `add_worktree_or_reclaim_orphan` is an UNWIRED parallel primitive with a red-top latent-footgun warning — `worktree.py:1118`, `worktree.py:1153-1159`.
21. cli `gc-singlepane` subcommand newly wired — `cli.py:55-59`, `cli.py:148-151`.
