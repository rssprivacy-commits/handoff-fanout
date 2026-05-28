# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- [/] 30-second demo gif (recording script committed; gif asset capture pending)
- [ ] ERP-side migration: `scripts/dump-handoff.py` → thin wrapper around `handoff dump`
- [ ] v1.0.0 tag + GitHub Release

## [0.1.0] — 2026-05-29

### Added

- Initial scaffold: `pyproject.toml`, `LICENSE` (MIT), `.gitignore`, README placeholder
- Source package skeleton at `src/handoff_fanout/`
- Extraction roadmap above
