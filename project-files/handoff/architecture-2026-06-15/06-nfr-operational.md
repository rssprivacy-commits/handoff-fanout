# 06 — Cross-cutting Non-Functional / Operational (NFR) View

> ⚠️ **快照时效声明（务必先读）**：本文是 **2026-06-15 晨的架构快照**，勘察锚点 git HEAD `5e8d7b2`（p27 baseline），但实际随 commit `5527ce1` 入库——其间 **p28/p29 已闭多个本文标为「缺口/未修」的项**：GAP §F **#1**（install.sh 反向卸载 live 扩展 → `6f8c2c8`）、**#3**（C1 回程 helper 无 wall-clock timeout → `c641b28`）、**#4**（C2 spawn_lock stale-break 竞态 → `0aad8f4`）、**#2**（24GB 零应用级备份 → `359e650`），并订正了 `codex_audit.py`/`retro_gate.py` 的 mandate-OFF/dormant 注释（`5b4eb20`）。
> **据此读本文**：凡标「🔴 P1 未修 / CONFIRMED REAL / No heartbeat exists / 提议修法」且涉及 **C1/C2/install-A3/备份** 的，**均为快照态、现已修复**；行号 / LOC / 日志计数 / exit-code 等具体值为快照时刻、可能已漂移。**当前权威状态以 [GAP-ANALYSIS.md](GAP-ANALYSIS.md) §F（状态列已更新）+ 现行代码为准**。逐图 refresh-to-HEAD 待后续 doc 包（外审 punch-list：`~/.claude-handoff/handoff-fanout/audits/p29-submap-audit-workflow-findings.json`）。

> System class: **internal macOS CLI / automation** (handoff & parallel fan-out for AI coding sessions). NOT a commercial web product. `handoff-fanout` @ git HEAD `5e8d7b2`.
> State root `$HANDOFF_HOME = ~/.claude-handoff` (config default `~/.handoff`; runtime uses `~/.claude-handoff`). Cross-cutting cousins live in `dharmaxis/scripts/vscode-spaces/` and `~/.claude/scripts/`.

This is the operational-health chapter: it spans every subsystem (dump / spawn / watchdog / audit-gate / unlock-pivot / worktree). Each dimension is rated 有✅ / 半成品🟡 / 无❌ with one sentence grounded in `file:line` or a concrete absence.

---

## Commercial-NFR dimensions that are N/A (one-line exemptions)

- **Payments / dunning / invoices / chargebacks** — N/A: no money flows through this tool (it spawns coding sessions; it never charges anyone).
- **GDPR / DSAR / data-deletion / cross-border** — N/A: single-user, single-Mac, no third-party PII; the only "personal data" is the owner's own task text/transcripts on their own disk.
- **WCAG 2.2 AA / EU-EAA accessibility** — N/A: CLI + notifications, no public UI; the lone "accessibility" code (`test_accessibility_preflight.py`) is about the macOS **Accessibility API permission** for keystroke injection, not a11y for users.
- **SOC2 / PCI / penetration test / SBOM** — N/A: not a hosted service, no external attack surface (no network listener), no customer data custody.
- **i18n / SEO / multi-currency / growth metrics** — N/A: no web frontend, single operator, single locale (zh/en mixed by design).

What an internal automation tool of this class actually needs — and what this map scores — is: **deployment safety, observability, credential safety, failure-mode discipline, state durability, concurrency safety, test/CI, supply chain.**

---

## 1. 部署 / install 安全 — 🟡 半成品

Rating **🟡** — install path itself is hardened, but the launcher uses a *copy-not-symlink* deploy model whose drift is only closed by an opt-in auto-sync hook + a startup drift guard, and that hook is itself audit-gated (a chicken-and-egg risk under brain-down).

**真实情况:**
- `install.sh` is idempotent, `set -euo pipefail`, supports `--uninstall`, validates asset-dir layout before acting (`install/install.sh:33,78-82,136`). Curl-pipe mode auto-clones to a temp dir with `trap rm -rf` cleanup (`install.sh:67-73`).
- **The launcher is NOT editable/symlinked — it is a deployed COPY.** `~/.local/bin/auto-continue.sh` (run by `com.dharmaxis.auto-continue`) and `~/.local/bin/dump-handoff.py` are byte-copies of `install/auto-continue.sh` / `install/dump-handoff.py`, kept current only by `install.sh --sync-launcher` / `--sync-dump` (`install.sh:84-133`). The Python package *is* editable (`pip install -e`, src/ is live), but the shell launcher is not — this asymmetry is the standing deploy hazard.
- **Drift is closed by two backstops:** (a) `post-commit` hook auto-fires the matching `--sync-*` when a commit touches the canonical asset (`install/git-hooks/post-commit:64-76`), and (b) a startup drift guard inside `auto-continue.sh` (records/compares `.auto-continue.canonical.sha`). Owner-pain root cause is documented inline: forgetting the manual sync left the runtime running OLD logic (`post-commit:10-13`).
- **Deploy-ordering hazard, explicit:** the auto-sync deploy is itself gated by `handoff audit-check` (`post-commit:33-62`) — if the `handoff` CLI is unavailable it fails-closed and **skips deploy** (still exit 0, never fails the commit). Pushing to `main` is blocked by the same gate in `pre-push` (`install/git-hooks/pre-push:41-53`), which **fails-closed and refuses the push** if `handoff` is missing. `post-merge` is warn-only and writes `.audit_pending` (`post-merge:26-35`).
- **`ff-merge does not trigger post-commit`** → a fast-forward merge that brings in a launcher change does not auto-deploy; the runbook requires a manual `install.sh --sync-launcher` + `launchctl kickstart -k` to clear drift (documented operational footgun, per memory热数据).

---

## 2. 可观测性 / 日志 — 🟡 半成品

Rating **🟡** — there is logging everywhere, but it is **plain append-only text with no rotation**, no metrics/traces, and the one structured diagnostic channel is silently capped.

**真实情况:**
- **Logs (all flat text, no rotation):**
  - `auto-continue.log` — the launcher relay log; `$LOG = $HANDOFF_ROOT/auto-continue.log` (`install/auto-continue.sh:29-30`), ~50+ `log` call sites; no size cap, no logrotate.
  - `watchdog.log` — launchd `StandardOut/ErrorPath` (`install/launchd/com.handoff-fanout.watchdog.plist`), watchdog runs **every 60s** (`StartInterval 60`); append-forever, no rotation.
  - `~/.vscode-spaces/router.log` — the desktop-routing wrapper log (`dharmaxis/scripts/vscode-spaces/code-router.sh:14`); every focus-jump / goto appends; no cap.
  - `~/.claude/state/coord-identity-diag.log` — coordinator-identity resolver diagnostics (`~/.claude/scripts/dx_session_role.py:45`). **CAPPED at 5MB, and on overflow it STOPS WRITING (no rotation) and silently swallows write errors** (`dx_session_role.py:46 DIAG_MAX_BYTES = 5*1024*1024`; `:66 if getsize > DIAG_MAX_BYTES: return`; docstring `:36-39` "「>5MB 停写不轮转 / 写失败静默吞」"). This is the #1 observability trap — see §半实现陷阱.
- **Metrics:** there IS a narrow metrics channel — fan-in writes `metrics.jsonl` (n_sub_tasks, estimated vs actual minutes) with O_APPEND+fsync (`src/handoff_fanout/heartbeat.py:182-208`), and a calibration reader (`heartbeat.py:214+`). This is task-estimation calibration, not system observability.
- **Traces:** ❌ none. No OpenTelemetry, no spans, no correlation IDs across the dump→spawn→router→submit chain (grep for `import logging|opentelemetry|prometheus|statsd` in src/ returns only doc-comment hits, no real instrumentation).
- **Audit evidence as observability:** `audits/*.evidence.json` sidecars (dual-brain runner output) are the closest thing to a structured event log of "what was reviewed" — durable, hash-bound, and read by the gate.

---

## 3. 安全 / 凭据接触面 — 🟡 (mechanism safe, but a credentialed bypass path is LIVE)

Rating **🟡** — the credential design is correct (Keychain-only, no on-disk password, shell-out isolation), AND the highest-risk path (CGEvent login-password injection) is **dialed ON for erp-system right now**. The blast radius is the Mac login password — but it never touches this repo's disk, so the owner red-line (don't下放 Live/DB/root creds to AI) is honored.

**真实情况:**
- **The unlock path = CGEvent HID injection of the Mac login password.** `auto-continue.sh` shells out to MindPersist's venv (`HANDOFF_UNLOCK_CMD`, `auto-continue.sh:71`) which reads the password from **Keychain `mindpersist-login-password`** (design `docs/design-unlock-pivot-and-autoclose-removal-2026-05-31.md:36-45`). **Verified live:** `security find-generic-password -s mindpersist-login-password` returns a `genp` item in `login.keychain-db` (acct=`mindpersist`). The password is **never written to disk by handoff-fanout** — confirmed by grep: src/ has zero password/secret-on-disk writes (only succession *tokens*, which are 0600/120s ephemeral nonces, `src/handoff_fanout/succession_authority.py:12,36`).
- **Dependency hygiene:** handoff-fanout does NOT import pyobjc / Quartz; it shells out to MP's venv (`design …:117-119`). Single source of truth for the unlock logic = MP's `idle.py`; this repo only routes to it.
- **Opt-in is the ONLY enabler and it is LIVE for one project:** per-project `<project>/unlock.enabled` sentinel; the global `HANDOFF_UNLOCK_ENABLED` env enabler was **deliberately removed** so a stray export can't arm password injection everywhere (`auto-continue.sh:1060-1070`). **Verified:** `~/.claude-handoff/erp-system/unlock.enabled` exists → the CGEvent password-injection path is armed for erp-system (and only erp-system).
- **Three concurrency/safety guards around the credentialed action** (codex R1 P0s, all wired): global `.unlock.lock` mutex so a 2nd tick can't inject into an already-unlocked / wrong window (`design …:218-226`); wrong-password **cooldown marker** after N=2 consecutive fails to avoid a macOS account-lockout retry storm (`auto-continue.sh:77,1130-1142`); `caffeinate -d -i` + re-probe-before-Enter (`auto-continue.sh:76`).
- **Blast radius:** if the Keychain entry leaked, an attacker gets the Mac login password (full local account). Mitigations: it lives only in Keychain (OS-protected), is injected only while the screen is being auto-unlocked to run a *visible* session, and the feature is off by default. **No Live-DB / root / SSH credential is ever下放 to the AI** — the gate keeps the last live step (sync, audit-override) on a human key. Owner consciously accepted this physical-risk class (design §6 B2, same class as MP's WeChat sender).

---

## 4. 失败模式 — ✅ 有 (disciplined fail-open vs fail-closed split)

Rating **✅** — failure modes are explicitly designed, with a deliberate split: **routing/UX paths fail-OPEN (never block opening a window), correctness/security paths fail-CLOSED.** auto-continue.sh has 11 fail-open + 15 fail-closed annotations; src/ has fail-open/closed reasoning in 11 modules.

**真实情况:**
- **launchd not running** → no relay at all (the watchdog/launcher simply don't tick); failure is *silent stall*, not corruption. The watchdog is a separate launchd job (`StartInterval 60`) so a crashed relay is still scanned.
- **VS Code not installed / `code` CLI missing** → after an unlock, GUI prerequisites are re-probed and a failure **defers** the `.uri` rather than claiming it (`design …:228-234` P1-4); the router wrapper `exec`s real `code` regardless (fail-open, `code-router.sh:4,57`).
- **Desktop-routing probe (winlist/Quartz) unavailable** (e.g. no Screen Recording permission) → `code-router.sh` is **zero-侵入 fail-open**: any step failure `exec`s the real `code` and just doesn't route (`code-router.sh:2-4,22-27,46`).
- **Lock probe unreliable (modern macOS ioreg)** → LOUD warning + notification, refuses to trust ioreg when an unlock cmd is configured → returns UNKNOWN → defers fail-closed (`auto-continue.sh:975-1047`).
- **codex/gemini brain down** → the audit gate verdict goes `ERROR`, which is **fail-closed** (push refused) unless the one-time `audit_unavailable` bypass door is used (`src/handoff_fanout/audit_evidence.py:32-37,43,253`). RED verdicts fail-closed; the only door out is the owner's tty `handoff audit-override` (no tty for AI sessions).
- **Lock contention** → `project_spawn_lock` raises `LockHeld` immediately (default `wait=0.0`) for singlepane hard-reject / watchdog skip; legitimate parallel workers pass `wait>0` to queue (`src/handoff_fanout/spawn_lock.py:33-40`).
- **Disk full / power loss** → atomic writes (`atomic_replace` = temp+fsync+os.replace) mean a reader sees full-old-or-full-new, never partial (`src/handoff_fanout/atomic.py:63-94`); a crashed lock holder is auto-released by the kernel (`flock`, `atomic.py:153-159`).
- **Network down for push** → `git push` fails locally; the pre-push gate runs *before* network, so a brain-down state blocks the push attempt itself (fail-closed). No silent partial-publish.
- **Caveat:** an *alive-but-hung* lock holder is **never force-broken** (`atomic.py:171-174`) — hang recovery is delegated to the watchdog / timeouts, not the lock. A hung holder can wedge a project's spawns until the watchdog acts.

---

## 5. 状态持久化 / 备份 — 🟡 半成品 (durable writes, but NO app-level backup + unbounded accumulation)

Rating **🟡** — every write is crash-atomic and the protocol is filesystem-native by design, but **there is no backup/recovery story for the 24GB state tree, and several sentinel classes accumulate without pruning.**

**真实情况:**
- **All durable state is under `$HANDOFF_HOME = ~/.claude-handoff`**: per-project `queue/`, `ack/`, `singlepane/`, `audits/`, `worktrees/`, `authority/` (succession tokens), `launched/`, plus `metrics.jsonl`, `auto-continue.log`, `watchdog.log`, and `.auto-continue.canonical.sha`. **Verified live: 24GB total** (`du -sh ~/.claude-handoff` = 24G), 102 top-level entries.
- **Durability is solid:** writes go through `atomic_create` (O_CREAT|O_EXCL), `atomic_replace` (temp+fsync+os.replace), `write_with_fsync` — all fsync file **and** parent dir for power-loss durability (`src/handoff_fanout/atomic.py:7,28-94`). State files **must live on local disk** (NFS/SMB/FUSE break the atomicity guarantees — documented `atomic.py:9-11`).
- **Source of truth:** the filesystem state tree IS the operational source of truth (queue/ack/audits drive the watchdog and gate). The org-memory (`MEMORY.md` / `open-loops.md`) is the *coordination* source of truth (what's闭环 / backlog), NOT runtime state — they are different layers. ARCHITECTURE.md §"Why not a database?" defends the filesystem-as-DB choice (`docs/ARCHITECTURE.md:260`).
- **❌ NO app-level backup / recovery:** `~/.claude-handoff` is **not version-controlled** (no `.git`) and has no export/restore tooling. It is **Time-Machine-Included** (`tmutil isexcluded` → `[Included]`), so it rides on whatever TM the owner runs — but if the dir is lost and TM isn't current, in-flight handoffs (queue/.uri, deferred markers, audit evidence, succession tokens) are gone with no recovery path. Recovery story = "re-spawn from scratch."
- **🟡 Unbounded sentinel accumulation, no GC for most classes:** `prune.py` only reconciles queue `*.done`/`*.blocked` (`src/handoff_fanout/prune.py:50-51`). Verified accumulation: **1804 ack files** in `erp-system/ack/`, **52 `.auto-continue.drift-notified.*` markers** (one per sha, forever), plus `_mplr_deploy_backup_*` / `_mplr_e2e_backup` dirs left in place. Worktree GC exists but is **manual, dry-run-by-default** (`handoff worktree gc`, `worktree.py:1350,1455`) — not run by launchd/cron, so orphan worktrees accumulate until someone runs it. This is the largest contributor to the 24GB.

---

## 6. 并发安全 — ✅ 有 (recently hardened; one historical gap closed at HEAD)

Rating **✅** — two complementary lock primitives, atomic file ops, kernel-fenced flock, and the `.uri`/`create_worktree` race that the global legislation flagged was closed in p21 (`dump.py:729`).

**真实情况:**
- **Two lock primitives, distinct jobs:**
  - `project_spawn_lock` (atomic `mkdir`, TTL-break, bounded retry, `wait` budget) — covers spawn-intent + serializes parallel-worktree git mutations (`src/handoff_fanout/spawn_lock.py:23-85`). Crash-free under its own race by contract: a lost stale-break sees the winner's fresh lock → clean `LockHeld`, never an uncaught `FileExistsError` (`spawn_lock.py:5-11,49-77`).
  - `acquire_dir_lock` (`fcntl.flock`) — **kernel-fenced**, auto-released on holder death, re-entrant via a pid-keyed registry, O_CLOEXEC so it never leaks into the `git` subprocesses dump spawns (`atomic.py:145-214`). No staleness heuristic → roots out the acquire/stale-clear TOCTOU (`atomic.py:153-159`).
- **The flagged `.uri` / `create_worktree` race is closed at HEAD:** dump's worktree creation now runs **under `project_spawn_lock`** (`src/handoff_fanout/dump.py:729` `with project_spawn_lock(project, root=cfg.home):`), symmetric with `spawn.py`. This was the p21 `dump-lock-fix` (memory热数据: "消并发 3/8 spurious 失败 / N-并发测试 8/8"). All single-task `.md`/`.uri` writes use `atomic_replace` (`dump.py:871,971,1037`).
- **Legacy-lock fail-closed migration:** an old mkdir-era `*.lockdir` blocking a new flock file raises `LockMigrationError` (operator removes manually) rather than auto-rmdir, which would reintroduce TOCTOU (`atomic.py:114-122,228-233`).
- **Residual:** alive-but-hung holders are not force-broken (§4 caveat); concurrent same-path flock from multiple *threads* is unsupported (consumers are single-threaded CLI, so safe in practice — `atomic.py:163-167`).

---

## 7. 测试 / CI — ✅ 有 (strong, but coverage is unit/integration-heavy; live E2E is human-driven)

Rating **✅** for unit/CI; **🟡** sub-note: the highest-risk live behaviors (cross-desktop focus-jump, real CGEvent unlock, real Enter submit) are validated by human/crafted-window E2E, not CI.

**真实情况:**
- **67 test files, 1459 `def test_` functions** (verified `grep 'def test_' tests/*.py`; the "1664" figure includes pytest parametrize expansion). Covers atomicity, spawn-lock concurrency, worktree lifecycle, succession authority, audit-gate phases A–D, unlock routing, focus-drift, singlepane, retro mandate.
- **CI matrix is real:** GitHub Actions on push+PR to main, **3 OSes × 3 Pythons** (ubuntu+macos × 3.11/3.12/3.13), `pytest -v`, console-script smoke tests, **idempotent installer smoke test**, plus separate `ruff check` + `ruff format --check` and an sdist/wheel build job gated on test+lint (`.github/workflows/ci.yml:18-111`).
- **ruff is pinned exactly** (`ruff==0.15.5`) with an inline comment explaining why an unpinned ruff drifted the tree red (`pyproject.toml [project.optional-dependencies].lint`).
- **Gap (acknowledged in memory):** real-screen unlock, real cross-desktop focus-jump, and real osascript Enter cannot run in CI (no GUI / no lock). These are validated by **crafted-window / human live E2E** (lessons p14/p15/p20/p25) — i.e. the riskiest behaviors have the weakest *automated* coverage, by necessity. Tests pin lock=unlocked / stub the routing CLIs (`design …:184-191`).

---

## 8. 依赖 / 供应链 — ✅ 有 (minimal, clean — the strongest dimension)

Rating **✅** — the Python package has **zero runtime dependencies**; external behavior is via shell-out to host CLIs, not vendored code.

**真实情况:**
- **Python package: `dependencies = []`** (`pyproject.toml [project]`) — pure stdlib at runtime. Dev/lint extras only: pytest≥8 / pytest-asyncio≥0.23 (floor-pinned), ruff==0.15.5 (exact-pinned). `uv.lock` present.
- **VS Code extension: zero runtime deps** — `dependencies: {}`, all are devDeps (typescript, esbuild, mocha, @vscode/* — build/test only), `engines.vscode ^1.85.0`, `package-lock.json` committed (`extension/package.json`). No npm runtime supply-chain surface ships to the user.
- **External CLI deps (shell-out, host-provided, version-unpinned):** `osascript` (47 refs), `codex` (41), `ioreg` (10), `caffeinate` (9), `vscode-spaces.py` (4), `gemini` (2), `winlist` (1) in auto-continue.sh; plus `dual-brain-runner.py`, `code`, MindPersist venv. **These are the real supply-chain surface** — they are not pinned (host-version), but each call is fail-open or env-overridable for tests, so a missing/changed CLI degrades rather than corrupts. `winlist` (SkyLight z-order probe) and `vscode-spaces.py` (desktop routing) live in dharmaxis, not pip-installed — cross-repo runtime coupling that no manifest captures.

---

## 🔴 半实现陷阱 / 虚假就绪 (NFR things that LOOK done but aren't)

1. **The coordinator diagnostic log is a silent black hole at 5MB.** `coord-identity-diag.log` looks like a real diagnostic channel, but `dx_session_role.py:46,66` caps it at 5MB and then **stops writing with no rotation** — and write failures are **silently swallowed** (docstring `:36-39`). Once the cap is hit, every subsequent ambiguous coordinator-identity event (the exact case you'd want to debug) is invisible. Looks observable; goes blind under load. (Cross-repo, but it's the audit/identity backbone the whole handoff fan-out trusts.)

2. **"Backed up" is an illusion of the host, not the system.** The 24GB state tree is Time-Machine-*Included*, which reads like "it's backed up" — but there is **no app-level backup, no `.git`, no export/restore** (§5). If TM isn't current when the dir is lost, in-flight queue/.uri, audit evidence, and succession tokens are unrecoverable. The durability of individual *writes* (fsync everywhere) masks the absence of *state recovery*.

3. **The audit-gate's deploy arm can fail-OPEN-into-no-deploy that looks like success.** The `post-commit` auto-sync is fail-closed (skips deploy on brain-down) but **still exits 0** (`post-commit:48-62`) — a commit that touched the launcher can "succeed" while the runtime stays stale, with only a stderr WARN. Combined with "ff-merge doesn't trigger post-commit," the live launcher can silently lag the committed source. Looks deployed; isn't.

4. **Unlock opt-in is "default OFF" — but it's dialed ON in production for erp-system.** The design repeatedly emphasizes DEFAULT-OFF posture as the safety story (`design …:198-201`). Reality: `~/.claude-handoff/erp-system/unlock.enabled` exists, so the **CGEvent login-password injection path is live right now**. "Default off" is true of the *mechanism* but the owner has consciously armed it — the safety budget is spent, not held in reserve. (This is accepted, not a bug — but reading "default OFF" as "credential injection is dormant" would be wrong.)

5. **Sentinel accumulation masquerades as harmless.** `prune.py` only GCs queue done/blocked; meanwhile ack/ holds 1804 files, drift-notified markers grow one-per-sha forever (52 now), and worktree GC is manual-dry-run-default. None of this corrupts anything — so it looks fine — but it is the source of the 24GB tree and means `ls`/scan operations over `ack/` get linearly slower with age, with no automatic floor.

6. **ARCHITECTURE.md is the "5-layer defense" doc and is now ~60% of the system.** `docs/ARCHITECTURE.md` (May 29) documents only git-guard / pre-commit / safe-commit / atomic / watchdog (`:1,9,47,86,118,157`). It has **zero mentions** of the unlock-pivot, the delivery-audit machine gate, coordinator/succession authority, singlepane, per-session worktree isolation, or winlist desktop routing — every dominant subsystem added since. A reader trusting it as "the architecture" would miss the entire credential surface and the entire audit/concurrency story. Looks like the architecture doc; describes a third of it.

---

## 承重事实 file:line 清单 (load-bearing, no fabrication)

1. `install/install.sh:84-133` — launcher/dump are deployed COPIES, synced only by `--sync-launcher`/`--sync-dump` (drift model).
2. `install/git-hooks/pre-push:41-53` — push to main fails-CLOSED (refused) if `handoff` CLI unavailable; gate range = pushed commits.
3. `install/git-hooks/post-commit:33-62` — auto-deploy of launcher copy is itself audit-gated; brain-down → skip deploy, still exit 0.
4. `install/git-hooks/post-merge:26-35` — warn-only, writes `audits/.audit_pending`; never blocks local merge.
5. `~/.claude/scripts/dx_session_role.py:46` — `DIAG_MAX_BYTES = 5 * 1024 * 1024`; `:66` stops writing past cap (no rotation, errors swallowed).
6. `install/auto-continue.sh:29-30` — `$LOG = $HANDOFF_ROOT/auto-continue.log`, flat append, no rotation.
7. `dharmaxis/scripts/vscode-spaces/code-router.sh:14` — `router.log`; `:2-4,46` zero-侵入 fail-open (always `exec` real code).
8. `docs/design-unlock-pivot-and-autoclose-removal-2026-05-31.md:36-45` — unlock = CGEvent injection of Mac login password read from Keychain `mindpersist-login-password`.
9. `install/auto-continue.sh:1060-1070` — per-project `unlock.enabled` is the ONLY enabler; global env enabler deliberately removed.
10. `install/auto-continue.sh:77,1130-1142` — wrong-password cooldown (N=2 fails → manual-only) to prevent macOS account-lockout retry storm.
11. `src/handoff_fanout/atomic.py:63-94` — `atomic_replace` temp+fsync+os.replace (no partial-read window); `:7,97-107` fsync file+parent dir.
12. `src/handoff_fanout/atomic.py:153-159,171-174` — `flock` kernel-fenced, auto-release on death, NO force-break of alive-hung holder.
13. `src/handoff_fanout/spawn_lock.py:23-85` — `project_spawn_lock` atomic mkdir + TTL-break + bounded retry + wait budget; crash-free under its own race.
14. `src/handoff_fanout/dump.py:729` — `create_worktree` now runs under `project_spawn_lock` (the flagged `.uri`/worktree race, closed in p21).
15. `src/handoff_fanout/audit_evidence.py:32-37,43` — verdict ruling: GREEN passes; RED/MIXED/ERROR fail-closed; only doors = `audit_unavailable` bypass or tty owner red-override.
16. `src/handoff_fanout/heartbeat.py:182-208` — `metrics.jsonl` O_APPEND+fsync (the only real metrics channel; task-estimation calibration, not system telemetry).
17. `src/handoff_fanout/prune.py:50-51` — prune only reconciles queue `*.done`/`*.blocked`; ack/drift/worktree sentinels not GC'd here.
18. `src/handoff_fanout/worktree.py:1350,1455` — `gc` is a manual CLI, dry-run by default; not launchd/cron-driven.
19. `.github/workflows/ci.yml:18-22,64-83` — CI matrix ubuntu+macos × py3.11/3.12/3.13, pytest + ruff check + ruff format-check + build.
20. `pyproject.toml [project] dependencies = []` / `extension/package.json dependencies: {}` — zero runtime deps both sides; external CLIs (codex/gemini/osascript/winlist) are unpinned shell-outs.

*(Live-verified facts: `security find-generic-password -s mindpersist-login-password` → genp item exists; `~/.claude-handoff/erp-system/unlock.enabled` exists; `du -sh ~/.claude-handoff` = 24G; `tmutil isexcluded ~/.claude-handoff` = [Included]; `~/.claude-handoff` has no `.git`; erp-system/ack/ = 1804 files; 52 drift-notified markers.)*
