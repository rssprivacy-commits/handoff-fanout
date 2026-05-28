# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.0] — 2026-05-29

v4.1 single-task 529-detection symmetry. Plugs the gap where
`build_handoff_md` emitted a spawn prompt without any heartbeat instruction,
so when the new tab wedged on 529 / API Error there was nothing for the
watchdog to notice. Mirrors what `build_sub_task_handoff_md` Step 2 +
watchdog mode 4 already do for fan-out sub-tasks.

Root cause: 主人 5/29 'API Error 会话裸跑' incident. Sessions launched via the
single-task path could die silently because they never wrote a heartbeat
file.

### Added

- **`build_handoff_md` Step 1** — heartbeat daemon that touches
  `queue/<task>.heartbeat` every 60s, with a pidfile + kill hint. The
  existing Baseline step is renumbered to Step 2.
- **`watchdog.scan_single_task_heartbeats` (mode 6)** — cross-project sweep
  for `queue/<task>.heartbeat` files older than
  `SUB_TASK_HEARTBEAT_STALE_SECONDS` (5 min). When the task is still
  active (`.md` present, no `.done` / `.BLOCKED.md` / existing
  `.529-suspected`), atomic-creates `queue/<task>.529-suspected` and fires
  the same osascript notification mode 4 uses.
- **`tests/test_v41_heartbeat.py`** — 11 cases covering template injection
  (3) and watchdog mode 6 (8: stale-detect, fresh-skip, md-missing-skip,
  done-skip, BLOCKED-skip, idempotency, cross-project, special-dir-skip).

### Changed

- `watchdog.main` now reports `scanned N batches / M orphans / K stale v4.1
  heartbeats` in its summary line.

## [1.1.0] — 2026-05-29

v5.4 retro-evidence gate — Phase 4a tool layer. Adds the precheck CLI that
captures Phase 0 / Phase 1 evidence, and gates `handoff-dump` on that
evidence so AI sessions can no longer skip the closure protocol silently.

### Added

- **`handoff_fanout.handoff_precheck`** — new module / CLI
  (`handoff-precheck` entry point + `handoff precheck` subcommand). Builds
  `precheck/<task>.retro.evidence.json` with the 5 Phase 0 items
  (`memory`, `tests`, `audit`, `commit`, `code_review`) and 5 Phase 1
  items (`codex`, `claude_md`, `l2_memory`, `tests`, `prs`), each tagged
  ✅/⚠️/❌/skip. Hash is SHA-256 over canonical JSON with `evidence_hash`
  excluded (spec §7.5).
- **`handoff_fanout.retro_gate`** — gate module imported by `dump`.
  Implements the 7-tier exit code protocol (§7.1: 0/1/2/3/4/6 — exit 5 is
  intentionally unassigned), the stderr prefix grammar
  (`OK:` / `ERR-FATAL:` / `ERR-BLOCKED:` / `ERR-LOCKED:` / `ERR-RETRY:` /
  `ERR-BYPASS:`), the §7.2 attempt-counter state machine
  (`ack/<task>.retro.attempt_n.txt` with atomic write + corrupt-file
  quarantine), the §7.3 lock hierarchy (`precheck.lock` → `dump.lock` →
  `<task>.retro.attempt.lock` with deadlock-free ordering and stale
  cleanup), the §7.7 three-tier HEAD freshness gate (configurable via
  `handoff.config.json:head_freshness.head_stale_action ∈
  {retry, block, warn-ok}`), and the §7.4 BLOCKED.md artifact schema.
- **`handoff_fanout.dump --retro-evidence FILE`** — new flag activates the
  v5.4 gate. Also honours `HANDOFF_RETRO_BYPASS=1` (requires an
  `ack/<task>.retro.override.json` with `follow_up_retro_task_id` +
  ISO-8601 `follow_up_deadline`) and `HANDOFF_RETRO_MANDATE=1` (enforce
  even without the flag; intended for Phase 4b CLAUDE.md activation).
- **§7.8 fingerprint algorithm — revised for D-1 probe results.** The
  previous spec referenced `VSCODE_MACHINE_ID` / `VSCODE_WORKSPACE_FILE`
  env vars, neither of which Claude Code on macOS exposes to subprocess
  env. The new fallback fingerprint uses `ioreg -rd1 -c
  IOPlatformExpertDevice` for the machine UUID and `os.getcwd()` for the
  workspace path, joined with ASCII unit-separator and SHA-256-truncated
  to 128 bits. `CLAUDE_CODE_SESSION_ID` remains the primary key when
  exposed (confirmed exposed in the 2026-05-29 D-1 probe).
- **`tests/test_retro.py`** — 14 single-axis (R-01..R-14) + 4 combination
  (C-01..C-04) cases covering the full §7.11 retro matrix, plus 4
  library-level sanity checks for hash / fingerprint / session-id
  resolution. Subprocess-based R-14 verifies the 5-tab race converges to
  1 winner + 4 `ERR-LOCKED` losers.

### Changed

- **Bumped to v1.1.0** (minor — backward compatible; ERP shim's legacy
  `--task --next --status active` invocation continues to work because
  the gate is skipped when neither `--retro-evidence` nor the two env
  switches are set).
- **`handoff_fanout.cli`** — added `precheck` subcommand to the unified
  dispatcher.

## [1.0.0] — 2026-05-29

First stable release. Engine extracted from a year-old production ERP project,
hardened by three documented commit-hijack incidents (now blocked at four
independent layers) and ported to a project-agnostic API with bilingual docs,
CI matrix, an idempotent installer, and a 30-second demo GIF.

### Added — Phase A4 (release)

- **`docs/demo/handoff-fanout-demo.gif`** — 30 s VHS-captured demo
  (864 KB, well under the 2 MB budget). Covers `dump` → `.uri` sidecar →
  handoff markdown → Layer 2 hijack rejection.
- **`docs/demo/demo.tape`** — committed VHS tape so the GIF stays
  reproducible across future releases.
- **README badges + demo embed** — top-of-page GIF in both
  [README.md](README.md) and [README.zh.md](README.zh.md).
- **ERP-side thin-shim migration** (downstream consumer) —
  `dharmaxis-group/erp-system@54ab453` replaces 4 hand-rolled handoff
  scripts (1292 + 388 + 383 + 225 = 2288 lines) with ~25-line shims that
  import `handoff_fanout`. ERP-specific behaviour (`V3.6` redlines,
  `主人立法`, `docker compose alembic current` baseline hook, roadmap
  excerpt path) now lives in the consumer's `~/.claude-handoff/config.json`.
  Validates the project-agnostic split.

### Added — Phase A3 (docs / install / CI)

- **Bilingual README** ([README.md](README.md) + [README.zh.md](README.zh.md))
  - 5-layer defense ASCII diagram
  - Comparison table vs Celery / Argo Workflows / Temporal
  - Quickstart, status, license
- **[docs/PROTOCOL.md](docs/PROTOCOL.md)** — `schema_version 2` wire-format spec
  - Directory layout under `$HANDOFF_HOME`
  - `task-id` / `batch-id` regex + length cap
  - Single-task `.md` + `.uri` sidecar + terminal markers
  - Fan-out `manifest.json` full JSON schema with examples
  - `file_ownership` 3 spec types (`exact` / `prefix` / `glob`)
  - Spawn-storm guards (N_max=3, GLOBAL_ACTIVE_LIMIT=5, STAGGER=30 s)
  - Role env contract, lifecycle markers, state machine
  - Atomicity guarantees, watchdog scan modes, ACK protocol
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — 5-layer defense walk-through
  - Layer 1 git-guard (PATH-injected wrapper)
  - Layer 2 pre-commit hook (`HANDOFF_EXPECTED_FILES` invariant)
  - Layer 3 safe-commit (flock + invariant + `git --only` + post-condition)
  - Layer 4 atomic primitives (`os.replace` + `fsync(dir)`)
  - Layer 5 watchdog (separate scheduler, idempotent, 60s tick)
  - Hijack scenario sequence diagram, orphan recovery timeline
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — ground rules, dev setup, test layout, PR conventions, release process
- **[install/install.sh](install/install.sh)** — idempotent installer
  - `$HANDOFF_HOME` tree + `config.json` from template
  - Per-repo git pre-commit symlink (backs up existing hook, respects `core.hooksPath`)
  - macOS launchd plist with placeholder substitution
  - `--uninstall` reverses everything
  - Curl-pipe mode auto-clones to tmp dir
- **[install/git-hooks/pre-commit](install/git-hooks/pre-commit)** — Layer 2 hook
  - POSIX-portable (awk-based, works under bash 3.2)
  - 9 regression tests in `tests/test_install_hook.py`
- **[install/launchd/com.handoff-fanout.watchdog.plist](install/launchd/com.handoff-fanout.watchdog.plist)** — Layer 5 LaunchAgent template
- **[install/examples/config.json](install/examples/config.json)** — annotated config template covering all `Config` fields
- **[.github/workflows/ci.yml](.github/workflows/ci.yml)** — Python 3.11/3.12/3.13 × ubuntu/macos matrix
  - `test` job: pytest, console-script smoke tests, installer idempotency smoke test
  - `lint` job: `ruff check` + `ruff format --check`
  - `build` job: sdist + wheel, uploaded as artifact
- **[docs/demo/RECORDING.md](docs/demo/RECORDING.md)** — VHS tape script for the README demo GIF (asset capture deferred to v1.0.0 release)

### Changed

- Codebase reformatted under `ruff format` (PEP 8, 100-char lines per `pyproject.toml`)
- `ruff check` cleanups (B904 raise-from, SIM105 contextlib.suppress, SIM117 with-merge, I001 import sort)

### Roadmap (v0.1.0 → v1.0.0)

- [x] Repo scaffolding (pyproject.toml, LICENSE, .gitignore, README placeholder)
- [x] Extract `git_guard/git` shell wrapper (PATH-injected git blocker for sub-task tabs) — 15 tests
- [x] Extract `atomic` primitives (atomic_create, write_with_fsync, acquire_dir_lock) — 10 tests
- [x] Extract `safe_commit` (4-layer hijack defense documented honestly) — 9 tests
- [x] Extract `dump` core (queue file generation, baseline detection, IDE spawn URI)
- [x] Extract `watchdog` (orphan/stale/timeout/heartbeat fan-in trigger)
- [x] Extract `heartbeat` (fan-in tab heartbeat daemon + metrics + Amdahl calibration)
- [x] Port 23 tests (orphan defense + hijack defense) with project-agnostic fixtures
- [x] Generic `~/.handoff/config.json` schema + loader
- [x] Bilingual README (EN + 中文) with 5-layer defense diagram & Celery/Argo/Temporal comparison
- [x] `docs/PROTOCOL.md` queue file format spec
- [x] `docs/ARCHITECTURE.md` 5-layer walk-through
- [x] `CONTRIBUTING.md` + example config
- [x] `install/install.sh` idempotent installer (bin/ + launchd plist + git hooks + config)
- [x] GitHub Actions CI (Python 3.11/3.12/3.13 × ubuntu/macos)
- [x] Layer 2 pre-commit hook regression tests (9 cases)
- [x] 30-second demo gif (captured via VHS, 864 KB)
- [x] ERP-side migration: `scripts/dump-handoff.py` → thin wrapper around `handoff dump`
- [x] v1.0.0 tag + GitHub Release

## [0.1.0] — 2026-05-29

### Added

- Initial scaffold: `pyproject.toml`, `LICENSE` (MIT), `.gitignore`, README placeholder
- Source package skeleton at `src/handoff_fanout/`
- Extraction roadmap above
