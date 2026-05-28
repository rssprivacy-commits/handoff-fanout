# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Extraction roadmap (v0.1.0 → v1.0.0)

- [x] Repo scaffolding (pyproject.toml, LICENSE, .gitignore, README placeholder)
- [ ] Extract `git_guard/git` shell wrapper (PATH-injected git blocker for sub-task tabs)
- [ ] Extract `safe_commit` (mkdir-based cross-process lock + expected-files invariant)
- [ ] Extract `dump` core (queue file generation, baseline detection, IDE spawn URI)
- [ ] Extract `watchdog` (orphan/stale/timeout/heartbeat fan-in trigger)
- [ ] Extract `heartbeat` (fan-in tab heartbeat daemon + metrics + Amdahl calibration)
- [ ] Port 23 tests (orphan defense + hijack defense) with project-agnostic fixtures
- [ ] Generic `~/.handoff-fanout/config.yaml` schema + loader
- [ ] Bilingual README (EN + 中文) with 5-layer defense diagram & Celery/Argo/Temporal comparison
- [ ] `docs/PROTOCOL.md` queue file format spec
- [ ] `install/install.sh` idempotent installer (bin/ + launchd plist + git hooks + config)
- [ ] GitHub Actions CI (Python 3.11/3.12/3.13 × ubuntu/macos)
- [ ] 30-second demo gif
- [ ] v1.0.0 tag + GitHub Release

## [0.1.0] — 2026-05-29

### Added

- Initial scaffold: `pyproject.toml`, `LICENSE` (MIT), `.gitignore`, README placeholder
- Source package skeleton at `src/handoff_fanout/`
- Extraction roadmap above
