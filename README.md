# handoff-fanout

> Project-agnostic auto-handoff & parallel fan-out for AI coding sessions.
> 5-layer defense against orphan tasks, git index hijack, and stale handoffs.

[![CI](https://github.com/rssprivacy-commits/handoff-fanout/actions/workflows/ci.yml/badge.svg)](https://github.com/rssprivacy-commits/handoff-fanout/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

---

**⚠️ Status: scaffolding phase (v0.1.0).** Core extraction in progress — see [CHANGELOG.md](CHANGELOG.md).

## What is this?

When you run multiple AI coding sessions (Claude Code / Cursor / Aider tabs) in parallel across projects, you hit four pain points:

1. **Orphan tabs** — a tab finishes but the next task never spawns.
2. **Git index hijack** — Tab A's `git add` gets swept into Tab B's `git commit` because `.git/index` is repo-shared state.
3. **Stale handoffs** — a session crashes mid-task, downstream sessions inherit a half-baked baseline.
4. **No fan-out / fan-in primitives** — you can't easily split one task into parallel sub-tasks with a clean barrier.

`handoff-fanout` is a small, dependency-free Python toolkit (extracted and battle-tested from a 1-year-old production ERP project) that gives you:

- `handoff dump` — atomic handoff queue files with launchd / cron auto-spawn of the next tab
- `handoff fan-out` — split one task into N parallel sub-tasks with file_ownership boundaries
- `handoff watchdog` — fail-safe scanner: triggers fan-in if last-one-out fires; flags 529-stalled tabs
- `handoff safe-commit` — wraps `git commit` with cross-process file-lock + expected-files invariant + pre-commit hook integration
- `handoff git-guard` — PATH-injected `git` wrapper that blocks sub-task tabs from `commit`/`push`/`rebase` (fan-in tab is the only committer)

## Quickstart

```bash
pip install handoff-fanout
handoff --help
```

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the queue file format and [examples/config.yaml](examples/config.yaml) for configuration.

## Documentation

- [Protocol spec](docs/PROTOCOL.md) — queue file format, state machine, atomicity guarantees
- [Architecture](docs/ARCHITECTURE.md) — 5-layer defense diagram, comparison with Celery / Argo / Temporal
- [中文文档](README.zh.md) — Chinese docs (coming in v1.0)

## Status

This repo is being extracted from a battle-tested production ERP system (1+ year of multi-tab Claude Code usage, 3 documented commit-hijack incidents resolved). Target: **v1.0.0 stable release** with full bilingual docs, CI matrix, and demo gif.

Track progress in [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
