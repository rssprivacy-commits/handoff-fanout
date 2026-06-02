# Per-Session Git Worktree Isolation — Implementation Design (handoff-fanout)

> Status: **implementation design (for R1 dual-brain audit)**. Refines the ERP-side
> spec `project-files/handoff/per-session-worktree-isolation-design.md` §2.2 open
> points into concrete engine decisions. Default **OFF / opt-in / report-only first**
> (same rollout philosophy as v4 autoclose). 2026-06-03.

## 0. Problem recap (1 paragraph)

handoff-fanout auto-continue spawns each next Claude session into the **same repo's
same working tree + same `.git/index`**. Git's working tree + index are single-writer
state; N concurrent sessions = data race. The 2026-06-03 incident: a multi-tab window
accumulated 4 stranded `git stash` entries + `git reset --hard` destroyed 2 sessions'
uncommitted edits. Point defenses (safe-commit flock, no-bare-stash redline) don't
resolve the shared-tree root contradiction. Per-session `git worktree` gives each
session an independent working tree + index + HEAD sharing one object store.

## 1. Scope of THIS bar (deliberately narrow)

**IN:**
- Single-task `active` spawn path (`write_active_dump`) creates a worktree and points
  the new session's `WORKSPACE` at it.
- `worktree.py` module: create / remove / prune / list, with validation + graceful
  degrade.
- Config opt-in (env + sentinel + per-project list), default OFF.
- Cleanup: terminal-task worktree removal (prune) + orphan worktree GC + `git worktree
  prune`.
- venv convenience (shared-venv symlink) + honest caveat.
- Merge-back protocol documented in the worktree handoff `.md`.

**OUT (explicit honest boundaries — documented, not silently skipped):**
- **Batch fan-out** (`--open-batch`) sub-task worktrees → deferred to a follow-up bar.
  Batch already has `file_ownership` non-overlap (Gate A) as partial mitigation; the
  fan-in N-branch merge story is a separate design. v1 keeps batch on the shared tree.
- **Docker DB cross-talk** — all worktrees still connect to the same Postgres. Schema
  migrations + test data still need the chokepoint + RLS protocols. Worktree solves the
  **git tree** layer, NOT the **DB** layer.
- **alembic migration-chain fork** — each worktree has its own `alembic/versions/` work
  copy → still 2-head-fork-able (the chain is repo-level). Chokepoint protocol still
  required.

## 2. Open questions → recommended resolutions (R1 audits THESE)

### Q-A. Worktree root location
- Options: (1) `<repo>/.worktrees/<task>` same-disk fast, but inside the repo →
  pollutes scanners (ERP mutation-registry / deid / pytest collection) + needs
  `.gitignore`; (2) `$HANDOFF_HOME/<project>/worktrees/<task>` fully outside the repo;
  (3) `<repo>-worktrees/<task>` sibling.
- **Recommend (2)**: `$HANDOFF_HOME/<project>/worktrees/<task_id>`. The engine already
  owns that tree; fully outside the repo (no scanner pollution, no `.gitignore` churn);
  object store still shared (git's worktree gitdir link is an absolute path, no same-disk
  requirement). Configurable via `worktrees_root` (default
  `$HANDOFF_HOME/<project>/worktrees`).

### Q-B. Branch strategy
- `--detach` (detached HEAD) vs `-b <prefix>/<task>` (named branch).
- **Recommend named branch** `handoff/<task_id>` (configurable `worktree_branch_prefix`,
  default `handoff/`). Named branch → clean `git log` / push / merge-back / auditability.
  Detached HEAD forces cherry-pick-by-SHA on fan-in.
- **Collision handling**: if branch or worktree dir already exists for `<task_id>`
  (retry / re-dump): if a registered worktree exists and is clean at the expected base,
  REUSE it; else if it's a stale leftover (no live session), `worktree remove --force` +
  recreate; if it has uncommitted work we cannot attribute, **degrade to shared tree +
  loud warn** (never silently destroy unknown WIP — the redline this whole feature
  defends).

### Q-C. Base commit
- **Recommend**: branch from the **latest integration ref**, resolved as: `git fetch
  origin <default_branch>` (best-effort, short timeout) then base = `origin/<default_branch>`
  if it resolves, else local `HEAD`. This makes each new worktree build on the most recent
  pushed work (the serial relay's "each builds on previous" — see Q-E). No-remote repos
  fall back to local `HEAD` cleanly.
- `<default_branch>` resolved via `git symbolic-ref refs/remotes/origin/HEAD` →
  fallback `git rev-parse --abbrev-ref HEAD` → fallback `main`.

### Q-D. venv / uv handling (CRITICAL for target projects like ERP)
- A fresh `git worktree` checkout contains only tracked files; `.venv` (gitignored)
  does NOT exist in it. A worktree session running `uv run` / `.venv/bin/python` would
  rebuild a fresh venv (slow / heavy for ERP) or fail.
- Options: (1) symlink `<worktree>/.venv -> <main_repo>/.venv` (cheap, shared);
  (2) inject `UV_PROJECT_ENVIRONMENT=<main>/.venv` env; (3) per-worktree fresh venv
  (expensive); (4) document only.
- **Recommend (1) symlink, best-effort + configurable**: during worktree setup, if
  `<main_repo>/.venv` exists, create `<worktree>/.venv` symlink to it. The session's
  shebangs + `uv run` resolve to the main venv (real path), no rebuild.
  **Honest caveat (documented, same class as the Docker-DB boundary)**: the venv is
  SHARED across worktrees → worktree isolates the git tree, NOT the venv. A concurrent
  `uv sync` / editable rebuild in one session mutates the shared venv. Acceptable: read
  usage (run python/pytest) dominates; rebuilds are rare and project-controlled.
  Skippable via `worktree_link_venv: false`. Generic: only `.venv` at repo root (the
  conventional name) is linked; configurable `worktree_venv_names`.

### Q-E. Merge-back protocol (the hard one)
- Tension: today every session commits to `main` in the shared tree + pushes. With
  worktrees, `main` can be checked out only ONCE, so worktree sessions commit to
  `handoff/<task>`, and `main` must be advanced by SOMETHING.
- **Recommend for v1 (single-task serial relay)**: the worktree session, at closure,
  (a) commits to its branch `handoff/<task>`, (b) `git push origin handoff/<task>`
  (preserve the branch), (c) **fast-forward-publishes to the integration branch**:
  `git push origin HEAD:<default_branch>` (ff-only; on non-ff → `git fetch` + rebase +
  retry, the normal push-serialization the spec already accepts). The **main worktree is
  a passive ref holder** — sessions never edit its files; only `git fetch` (refs, not
  tree) touches it. "main advances" becomes "origin/<default_branch> advances", and the
  NEXT dump (Q-C) branches from the freshly-fetched integration ref → builds on prior
  work. Clean serial relay + fully isolated trees.
- This is a **closure-protocol change** surfaced in the worktree handoff `.md` (the
  worktree session is told: commit to your branch, push branch, ff-publish to
  `<default_branch>`). Sessions NOT in a worktree keep the current "commit to main +
  push" flow unchanged (feature is opt-in).
- **Alternatives considered**: (i) main worktree merges each completed branch — reintroduces
  contention on the main tree (owner's manual tab); (ii) stacked branches (B from
  handoff/A) — accumulates branch lineage, a bad middle task poisons the stack;
  (iii) push branches only, merge later — breaks "each builds on previous" for serial.
- **Risk flag**: the ff-publish changes what the owner sees on their LOCAL main worktree
  (commits land on origin/<default_branch>; local `main` lags until pulled). This is a
  visible UX change → candidate for owner ruling (post-dual-brain) before flip; under
  report-only/default-OFF it affects only opted-in pilot tasks.

### Q-F. Cleanup / orphan GC
- Terminal (`.done` / `.BLOCKED.md`): `git worktree remove` the task's worktree (force
  only if clean; if dirty/unmerged → KEEP + record `worktree-retained` reason, never
  destroy). Extend `prune.py` (it already janitors terminal sidecars).
- Orphan (session died, worktree leaked): `git worktree prune` (git's own GC of
  unreachable worktree admin dirs) + a handoff-level scan that removes worktree dirs for
  terminal tasks. Extend `handle_cleanup_orphan`.
- **Fail-safe**: any remove that would lose uncommitted/unmerged work is REFUSED +
  logged; manual intervention. "失败保留现场".

### Q-G. Graceful degrade (default-safe)
- Worktree creation is best-effort. If: not a git repo / `git worktree add` fails /
  base unresolvable / worktrees_root unwritable → **fall back to the current shared-tree
  workspace** (exact current behavior) + a LOUD `[dump] ⚠️ worktree isolation requested
  but unavailable (<reason>); falling back to shared tree` line. The dump NEVER bricks.
- Feature is **OFF unless explicitly opted in**, so the default path is byte-identical to
  today.

## 3. Opt-in surface (mirror v4 autoclose + mandate_projects)

Enabled when ANY of:
1. env `HANDOFF_WORKTREE_ISOLATION=1`
2. sentinel `$HANDOFF_HOME/worktree.enabled` (all projects)
3. sentinel `$HANDOFF_HOME/<project>/worktree.enabled` (one project)
4. config `worktree_projects: ["erp-system", ...]` (non-empty list; fail-OPEN here —
   unlike mandate, an accidental empty must NOT enable a tree-mutating feature for all).

`report-only` sub-mode: `HANDOFF_WORKTREE_ISOLATION=report` (or config
`worktree_mode: "report"`) → log what WOULD happen (compute worktree path, print the
`git worktree add` it would run) but DO NOT create it; workspace stays the shared tree.
This is the "report-only → flip" stage 1.

## 4. New config fields (config.py)

```
worktree_isolation_configured: bool      # any opt-in seen
worktree_mode: "off" | "report" | "on"   # resolved
worktrees_root: Path                      # default $HANDOFF_HOME/<project>/worktrees
worktree_branch_prefix: str = "handoff/"
worktree_link_venv: bool = True
worktree_venv_names: list[str] = [".venv"]
worktree_default_branch: str | None       # override for integration branch resolution
worktree_projects: list[str]              # per-project opt-in
```

## 5. Files touched (≥5 → 4-round dual-brain mandated)

- `src/handoff_fanout/worktree.py` (NEW) — create/remove/prune/list + resolve_mode.
- `src/handoff_fanout/config.py` — new fields + env/sentinel resolution helper.
- `src/handoff_fanout/dump.py` — `main()` active path: resolve mode → create worktree →
  substitute `workspace` → write `.worktree` metadata sidecar; degrade on failure.
- `src/handoff_fanout/prune.py` — terminal worktree removal.
- `src/handoff_fanout/templates.py` — worktree handoff `.md`: worktree banner + merge-back
  closure protocol (only when workspace is a worktree).
- `src/handoff_fanout/cli.py` — wire `handoff worktree {list,prune,gc}` subcommands.
- tests: `tests/test_worktree.py` (NEW) + extensions to prune/dump tests.
- `install/auto-continue.sh` — **no change needed** (already `code -r "$WORKSPACE"` +
  `[ -d "$WORKSPACE" ]` guard; a worktree path just works). Verify only.

## 6. Metadata sidecar (`.worktree`)

`queue/<task>.worktree` (or `ack/<task>.worktree`) JSON: `{path, branch, base_sha,
default_branch, created_at, link_venv}`. Lets prune/gc/fan-in find+reclaim the worktree
without re-deriving paths. Written BEFORE the `.uri` publish (same ordering rule as
old_ready).

## 7. Verification (spec §5 delivery redline)

- spawn → session in its own worktree; two concurrent sessions' `git stash` / `git reset
  --hard` do NOT touch each other's tree (real-machine 2-session test).
- venv / hooks / codegraph / gitnexus / alembic / Docker-DB each work in a worktree
  (per-item real test).
- merge-back (ff-publish) lands on integration branch cleanly; next dump branches from it.
- worktree leak → prune/gc reclaims; dirty/unmerged worktree is RETAINED not destroyed.
- full handoff-fanout regression: zero new failures vs baseline (463 pass / 1 known
  uv-shim artifact).
- R1–R4 dual-brain: no P0.

## 8. R1 dual-brain results + revised decisions (2026-06-03)

codex (impl) + Gemini 3 Pro (arch) audited independently; CC cross-validated. Both
**converged** on the core fixes (no owner coin-flip remained — the design is dual-brain
consensus, implemented within mandate). Findings → revised decisions:

### 8.1 Convergent (both brains)
- **R1-C1 (codex P1 + Gemini P1-3) — split the workspace role.** A blind "substitute
  workspace" (a) makes `_write_old_ready`'s `git rev-parse HEAD` read the successor's tree
  (wrong predecessor anchor) and (b) runs the retro/preflight gates against the wrong tree.
  → **`source_workspace`** (old session cwd: retro gate, preflight gate, old_ready predecessor
  metadata) vs **`spawn_workspace`** (the worktree: `detect_baseline`, handoff `cd`, `.uri`
  WORKSPACE=, `.queued` workspace=, `.worktree` sidecar). Thread both explicitly.
- **R1-C2 (codex P0-2 + Gemini P0-1) — engine-ENFORCED merge-back, not session discipline.**
  A missed ff-publish silently builds the successor on stale code. → at dump (under the
  existing lock), the engine **verifies `origin/<INT>` contains `source HEAD`**; if not →
  **BLOCK loud** ("publish HEAD to <INT> first"), never silently branch from stale. The
  worktree session's handoff `.md` instructs the closure ff-publish (`git push origin
  HEAD:<INT>`). The engine does NOT auto-push (keeps it simple); it gates.
- **R1-C3 (codex P1 + Gemini P1-4) — clean ≠ published.** A clean worktree can hold
  committed-unpushed/unmerged commits. → classify {dirty, HEAD==base, HEAD⊆INT, branch
  pushed} before remove/reuse; **retain+block clean-but-ahead; never auto-delete a branch**.

### 8.2 codex-unique criticals
- **R1-X1 (codex P0-3) — project identity.** The worktree lives at
  `$HANDOFF_HOME/<project>/worktrees/<task_id>`, so `workspace.name == <task_id> ≠ project`.
  Generated `handoff precheck` / `dump` / `audit-*` infer `project = workspace.name` → write
  evidence/queue/ack under a project NAMED AFTER THE TASK. → **every generated handoff
  command in a worktree handoff `.md` MUST pass `--project {project} --workspace {spawn_ws}`**;
  also write a `.worktree` sidecar + `HANDOFF_PROJECT`/`HANDOFF_SOURCE_WS` env marker.
- **R1-X2 (codex P0-1) — integration-branch resolution.** `git rev-parse --abbrev-ref HEAD`
  in a worktree returns `handoff/<task>`. → resolve `<INT>` as: config
  `worktree_default_branch` → `git symbolic-ref refs/remotes/origin/HEAD` → `git remote show
  origin` → `origin/main|origin/master` → local `main|master` → **else BLOCK**. NEVER infer
  from a `handoff/*` branch.
- **R1-X3 (codex P1) — shared `.venv` can defeat isolation.** An editable `.pth` in the
  shared venv points at the MAIN repo `src/`, so worktree pytest/entrypoints import main-tree
  code (the exact trap dogfooded around with `PYTHONPATH`). → `.venv` linking stays
  configurable (default on for run-ability) but **documented as a correctness risk** for
  editable-self-installed projects (recommend per-worktree venv there). ERP is NOT
  editable-self-installed (only handoff_fanout / external libs are), so the pilot is safe.

### 8.3 Gemini-unique + refinements
- **R1-G1 (Gemini P0-2) — link `.env`/`.claude`, not just `.venv`.** A fresh worktree lacks
  gitignored `.env` (DB creds → all E2E/alembic crash → instant BLOCK) + `.claude/`. →
  `worktree_link_files` config (default `[".env", ".claude"]`) + separate `worktree_link_venv`.
- **R1-G2/codex P1 — local ahead of origin.** Owner commits in main tree unpushed → branching
  from `origin/<INT>` skips them (regression vs shared tree). → detect local `<INT>` ahead of
  `origin/<INT>`; BLOCK with a clear "publish/pull first" message.
- **R1-R3 (codex P1) — degrade vs unsafe collision.** Do NOT degrade-to-shared-tree on a
  *dirty same-task worktree* collision (reintroduces the unsafe class). → environmental
  unavailability (not-a-repo / no-remote / `worktree add` fails) degrades to shared tree +
  loud warn; an unsafe collision **retains + BLOCKs**.
- **R1-G3 (Gemini P2-6) — degrade visible to successor.** Inject the degrade reason into the
  successor handoff `.md` so the new session knows it's on the shared tree (and may need
  `git pull --ff-only` if local main lags origin).
- **R1-X4 (codex P2) — report-only truly read-only.** No fetch / mkdir / symlink / ref-update;
  resolve paths from local metadata, print uncertainty where remote state would be needed.
- **R1-X5 (codex P2 + Gemini P1-4) — prune dry-run default** for worktree cleanup + print
  retained reasons.

### 8.4 Scope tightening from R1
- **No-remote repos**: worktree mode is **unavailable** (degrade to shared tree + warn) in v1
  — the published-integration-ref merge-back needs a bare remote. Honest boundary (ERP has
  GitHub → pilot works). No-remote local merge-back deferred.
- **Default-OFF byte-identical**: the `off` path must not evaluate any worktree code that
  fetches / mutates refs / changes emitted artifacts (codex no-issue caveat).
- Batch fan-out, Docker DB, alembic fork: unchanged honest boundaries (§1 OUT).

### 8.5 Revised single-task active flow (dump.main)
```
1. source_workspace = cwd/--workspace (as today)
2. run retro gate + preflight on source_workspace          # closure gates: old session's tree
3. mode = resolve_worktree_mode(cfg, project)               # off | report | on
4. if mode == on:
     INT = resolve_integration_branch(source_workspace, cfg)         # R1-X2; BLOCK if unresolved
     ensure source_HEAD ⊆ origin/INT  (and origin/INT not behind local INT)  # R1-C2 + R1-G2; BLOCK if not
     spawn_workspace, branch = create_worktree(origin/INT, handoff/<task>, root, link_files)  # R1-C1/X1/X3/G1
     on failure(env) → degrade: spawn_workspace = source_workspace + record reason   # R1-R3/G3
     on unsafe collision → retain + BLOCK
   elif mode == report: compute + log only; spawn_workspace = source_workspace        # R1-X4
   else: spawn_workspace = source_workspace                  # byte-identical
5. old_head = rev-parse HEAD of source_workspace            # R1-C1 (capture BEFORE substitution)
6. baseline = detect_baseline(spawn_workspace)              # successor sees worktree HEAD
7. write_active_dump(spawn_workspace, source_workspace=…, old_head=…, worktree_info=…)
     - handoff .md: cd spawn_workspace + worktree banner + merge-back closure protocol
       + generated handoff commands carry --project/--workspace (R1-X1)
     - .uri WORKSPACE = spawn_workspace ; .queued workspace = spawn_workspace
     - old_ready: commit_hash = old_head (source) ; workspace field = source_workspace
     - .worktree sidecar {path, branch, base_sha, INT, link_files}
```


## 9. R2–R4 dual-brain results + fixes (2026-06-03)

codex (impl/edge-cases) + Gemini 3 Pro (business-goal/concurrency) audited the
implemented code independently; CC cross-validated. Convergent + each-unique findings,
all fixed (tests in `tests/test_worktree.py` / `tests/test_worktree_dump.py`):

### 9.1 P0 (fixed)
- **R2-P0-A (both) — branch-only collision force-deleted unpublished WIP.** A lingering
  `handoff/<task>` branch (worktree dir manually removed) with committed-but-unpushed
  commits was `git branch -D`'d on re-dump. Fix: `create_worktree` resolves the branch
  SHA separately and BLOCKs if `branch_head` ⊄ `origin/<int>` — never deletes it.
- **R2-P0-B (codex) — dirty source not handled.** A closing worktree's uncommitted
  changes don't reach the successor (which branches from `origin/<int>`). Fix: **WARN**
  (not BLOCK — benign hook auto-edits routinely leave the tree dirty; a hard block would
  brick every real dump) + retain the source worktree (work preserved) + surface the
  advisory in the successor's handoff banner.
- **R2-P0-C (Gemini) — happy-path worktrees never reclaimed.** The serial relay never
  writes `A.done` (A closes by dumping B `--status active`), so a `.done`-gated GC leaked
  every worktree. Fix: GC reclaims on **session-gone** (absent/stale
  `queue/<task>.heartbeat`, `HEARTBEAT_LIVE_SEC`) — a LIVE heartbeat is skipped (never
  pulls a running tab's rug) — combined with the clean+published fail-safe.

### 9.2 P1 (fixed)
- **R2-P1-D (codex) — fetch unreliable + rc ignored.** Explicit refspec
  `+refs/heads/<int>:refs/remotes/origin/<int>` + rc check; a fetch failure only risks a
  SAFE false-BLOCK (push already updated the tracking ref), so warn + proceed.
- **R2-P1-E (both) — same-task add race degraded to shared tree.** On `worktree add`
  failure, "already exists / already used / already checked out" → BLOCK (retry), not
  degrade — never spawn a duplicate session onto the shared tree.
- **R2-P1-F (both) — GC claimed success on failed remove.** `remove_worktree` now checks
  both remove rc and returns `(False, …)` on failure, so the recovery sidecar is kept.
- **R2-P1-G (Gemini) — absolute `.env` symlink breaks Docker mounts.** `link_files` COPIES
  regular files (portable into a bind-mounted container) and SYMLINKS dirs (`.claude`,
  `.venv`). Orphan-branch GC: a *published* orphan branch is `branch -d`'d; an
  *unpublished* one is retained.

### 9.3 P2 (fixed)
- **R2-P2-H (both)** `.worktree` sidecar → `atomic.write_with_fsync` (crash-atomic).
- **R2-P2-I (codex)** default-OFF byte-identical: `old_head` is no longer precomputed
  unconditionally — read lazily inside `_write_old_ready` (source tree) only when retro
  evidence drives a dump; captured explicitly only when a worktree is actually created.

### 9.4 Pre-existing (FLAGGED, deferred — orthogonal to worktree, works in prod)
- **§0 template hardcodes `$HOME/.claude-handoff`** in `build_handoff_md` (ignores
  `{handoff_home}`). Correct for the real ERP deployment (home IS `~/.claude-handoff`),
  wrong for the library default / a non-default home. Not introduced by this bar; fixing
  it touches every handoff. → follow-up.
- **old_ready write-fail is swallowed** (warn + publish `.uri`) — a missing predecessor
  anchor could let the next session escape a forced audit follow-up. Pre-existing
  best-effort behavior; hardening (BLOCK on old_ready failure) is a separate decision.

### 9.5 Real-machine verification (design §5)
On a real bare-remote + clone with `HANDOFF_WORKTREE_ISOLATION=on`: spawn task-a (main
tree) → worktree A; A ff-publishes to `origin/main` → dump task-b → worktree B **sees A's
work**. In worktree A, `git stash` + `git reset --hard` reset **only A's** tree — **B's
uncommitted WIP + the source main tree's WIP both stayed intact**; per-worktree index
files confirmed. GC reclaimed clean+published A, **retained dirty B** (fail-safe).

**Honest boundary discovered (added to §1 OUT):** worktree isolates the **working tree**
(the catastrophic silent clobber of the incident) — but `refs/stash` is a **repo-global
ref**, so `git stash list`/`pop` is shared across worktrees. The destructive working-tree
wipe is fully isolated; the shared stash STACK is a lesser, *explicit-action* footgun
(`git stash pop` in one worktree can pop another's saved stash). Documented, not solved
in v1 — the incident was the silent working-tree wipe, which worktree fully prevents.

## 10. Report-only pilot runbook (2026-06-03)

The rollout is **report-only → flip** (design §2.3). A per-project `worktree.report`
sentinel (commit 90709cc) pilots ONE project in report-only without flipping the global
env/`worktree_mode`.

### Enable (pilot erp-system)
```bash
touch ~/.claude-handoff/erp-system/worktree.report     # scoped report-only for erp-system
```
Now every erp-system `handoff dump --status active` runs the report path: it LOGS the
worktree it WOULD create and mutates nothing (no fetch / mkdir / symlink / ref update).
The dump still proceeds on the shared tree exactly as before (byte-identical), plus:
- stdout: `[dump] [worktree:report] would run: git -C <repo> worktree add -b handoff/<task> <path> origin/<int>`
- the handoff `.md` carries a `🌿 worktree 隔离 report-only` banner.

### Observe (what to check over a few real dumps)
1. **Integration branch resolves correctly** — the `origin/<int>` in the planned command
   is `origin/main` (not a `handoff/*` task branch, not `<UNRESOLVED>`).
2. **Planned worktree path is sane** — `~/.claude-handoff/erp-system/worktrees/<task>`.
3. **No `integration branch unresolved` / degrade notes** in the report log.
4. Confirm the report path adds NO latency/side-effect to real dumps (it's local-only).

### Flip criteria → MODE_ON (after stable observation)
All of: int branch consistently `origin/main`; planned paths correct; no unresolved/degrade
surprises; the merge-back closure protocol (session ff-publishes `HEAD:main` before dumping
the successor — see the worktree handoff banner) is understood + acceptable; erp-system has
a working `origin` remote (it does: GitHub). Then:
```bash
rm  ~/.claude-handoff/erp-system/worktree.report
touch ~/.claude-handoff/erp-system/worktree.enabled    # → MODE_ON (creates real worktrees)
```

### Abort / pause
```bash
rm ~/.claude-handoff/erp-system/worktree.report         # back to OFF (byte-identical)
```

### Honest note
Report-only does NOT exercise the actual worktree create / merge-back / GC paths (those run
only under MODE_ON). It de-risks the **resolution + planning** layer (int branch, paths) and
surfaces the protocol to the owner before any tree is created — but the first real ON dump is
where create/merge-back/GC get their first production exercise (they ARE covered by the test
suite + the real-machine verification in §9.5).

## 11. R-ON: real-machine ON flip findings (2026-06-03)

Before flipping erp-system from report → ON, a controlled real-ON test on the actual
erp-system repo (not a tmp fixture) caught two issues the tmp tests missed — the value of
verifying at the user-experience layer:

- **R-ON-1 (GC leak)**: `link_files` symlinks `.claude`/`.venv` (dirs), but a project's
  `.gitignore` uses dir patterns (`.venv/`) that don't match a *symlink* named `.venv` →
  `git status` shows `?? .claude` `?? .venv` → a fresh worktree reads "dirty" → the
  remove/GC fail-safe RETAINS it → every ON worktree leaks. (`.env` is a copied file →
  matches `.env` → clean.) Fix: `is_dirty(workspace, ignore=...)` discounts the
  engine-linked names; threaded via `_link_names(cfg)` into classify/remove/gc/create
  (commit 6e0cc3d).
- **R-ON-2 (codex sanity P1/P2)**: the discount was status-code blind — it would discount a
  *tracked* `M .env` / `D .claude/settings.json` or a `' -> '` filename, making genuine WIP
  destroyable (redline). Tightened to discount ONLY untracked (`??`) link-named entries;
  any tracked change is WIP regardless of name (commit ed9ee36).

**Flip done**: `rm worktree.report` + `touch ~/.claude-handoff/erp-system/worktree.enabled`
→ erp-system = ON. End-to-end real-ON dump verified: worktree created, `.uri` → worktree,
handoff banner + `--project erp-system` injected, `.worktree` sidecar, venv works, ERP code
present, clean GC. Full suite 509 pass / 1 known uv-shim artifact.

**Known degradation (honest)**: `.codegraph` / `.gitnexus` are gitignored per-tree indexes —
NOT linked into worktrees (linking a shared SQLite graph across worktrees risks concurrent
re-index corruption). So worktree sessions get "CG/GN not initialized" and fall back to grep
(the routing rules explicitly support this). Per-worktree code-intel is a future option.
