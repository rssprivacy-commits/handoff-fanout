# handoff-fanout

> Project-agnostic auto-handoff & parallel fan-out for AI coding sessions.
> 5-layer defense against orphan tasks, git index hijack, and stale handoffs.

[![CI](https://github.com/rssprivacy-commits/handoff-fanout/actions/workflows/ci.yml/badge.svg)](https://github.com/rssprivacy-commits/handoff-fanout/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**[中文文档 / Chinese README](README.zh.md)**

---

## The problem

You're running an AI coding agent (Claude Code / Cursor / Aider) across **multiple IDE tabs**, often across **multiple projects**, on a single workstation. Four pain points show up:

| # | Symptom | Root cause |
|---|---|---|
| 1 | A tab finishes its task; the next task never spawns. | No standard handoff protocol — every project re-invents an ad-hoc baton. |
| 2 | `git commit` from Tab B silently sweeps in Tab A's `git add`. | `.git/index` is a repo-shared file. Without a lock, `add → commit` is not atomic. |
| 3 | A session crashes mid-task; downstream sessions inherit a half-baked baseline. | No durable "last good baseline" record + no orphan detector. |
| 4 | Want to split one task into N parallel sub-tasks but can't coordinate the merge. | No fan-out/fan-in primitives that respect file ownership. |

`handoff-fanout` is a **small, zero-runtime-dependency Python toolkit** that solves all four. It was extracted from a year-old production ERP project where these failures cost real hours, and it's been hardened by three documented commit-hijack incidents (now blocked at four independent layers).

## What you get

- **`handoff dump`** — atomically write a queue file describing the next task; the IDE auto-spawn helper picks it up and launches a new tab.
- **`handoff dump --open-batch`** — fan-out: split one task into N sub-tasks, each with strict `file_ownership` boundaries; a fan-in tab consolidates results.
- **`handoff watchdog`** — fail-safe scanner: triggers fan-in when the last sub-task finishes; flags orphan tabs (e.g. a tab spawned by launchd but whose batch dir got `rm`-ed under it).
- **`handoff safe-commit`** — wraps `git commit` with cross-process `flock` + `HANDOFF_EXPECTED_FILES` invariant + pre-commit hook integration. No more hijack.
- **`handoff heartbeat`** — fan-in tab heartbeat daemon, Amdahl-speedup metrics, and runtime calibration so your next batch's split decision is data-driven.
- **`handoff git-guard`** — a `PATH`-injected `git` wrapper that **physically blocks** `commit/push/rebase/cherry-pick/reset/revert/tag/am/format-patch/merge` inside sub-task tabs. The fan-in tab is the only committer.

## 5-layer defense (the headline)

```
                ┌──────────────────────────────────────────────────┐
                │  Layer 1 — git-guard (PATH-injected git wrapper) │
                │  Sub-task tabs literally cannot run `git commit`.│
                └──────────────────────────────────────────────────┘
                                       │
                                       ▼
              ┌────────────────────────────────────────────────────┐
              │  Layer 2 — pre-commit hook (HANDOFF_EXPECTED_FILES)│
              │  Rejects commits whose staged set ≠ expected set.  │
              └────────────────────────────────────────────────────┘
                                       │
                                       ▼
          ┌─────────────────────────────────────────────────────────┐
          │  Layer 3 — safe-commit wrapper (flock + invariant)      │
          │  Cross-process lock on ~/.handoff/git-commit.lock.      │
          │  Verifies `git diff --cached --name-only` ⊆ expected.   │
          └─────────────────────────────────────────────────────────┘
                                       │
                                       ▼
       ┌────────────────────────────────────────────────────────────┐
       │  Layer 4 — atomic file primitives (queue / batch writes)   │
       │  atomic_create / write_with_fsync / acquire_dir_lock.      │
       │  No half-written queue files. No torn batch manifests.     │
       └────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
      ┌──────────────────────────────────────────────────────────────┐
      │  Layer 5 — watchdog (orphan / stale / heartbeat scan)        │
      │  Runs from launchd/cron. Re-triggers fan-in if last-one-out  │
      │  silently dies. Marks orphan sub-task tabs BLOCKED.          │
      └──────────────────────────────────────────────────────────────┘
```

Each layer is independent. Defeating one is not enough — for a hijack to land, **all four commit-path layers (1-4) must fail simultaneously**.

## Quickstart

```bash
pip install handoff-fanout

# Idempotent install: symlinks bin/ → ~/.local/bin, generates ~/.handoff/config.json,
# installs git hooks, optionally installs launchd plist (macOS).
curl -L https://raw.githubusercontent.com/rssprivacy-commits/handoff-fanout/main/install/install.sh | bash

# Dump the next task from your current project repo:
cd ~/Projects/my-repo
handoff dump \
    --task fix-bug-123 \
    --next "Fix the off-by-one in the discount calculation." \
    --status active \
    --tests "tests/test_discount.py"
```

A new file appears at `~/.handoff/my-repo/queue/fix-bug-123.md`, the launchd watcher picks it up within one second, and a fresh IDE tab opens already pointed at your repo with that handoff loaded.

## How it compares

| | **handoff-fanout** | Celery | Argo Workflows | Temporal |
|---|---|---|---|---|
| **Target environment** | One workstation, multiple IDE tabs | Distributed service, many workers | Kubernetes cluster | Distributed service |
| **Coordination unit** | An AI coding tab | A Python function | A pod | A workflow function |
| **State store** | Plain files under `~/.handoff/` | Redis / RabbitMQ broker + result backend | etcd + K8s objects | Cassandra / MySQL + Temporal server |
| **External dependencies** | None (zero-dep Python) | Broker + (often) Redis result backend | Full K8s cluster | Temporal server + DB |
| **Failure model** | Atomic file writes + file locks + watchdog | Broker durability + ack semantics | K8s controller reconciliation | Event-sourced durable execution |
| **Cross-process commit safety** | First-class (4 layers) | Out of scope | Out of scope | Out of scope |
| **Fan-out / fan-in** | Yes, with file-ownership boundaries | Yes (canvas: group/chord/chain) | Yes (DAG) | Yes (child workflows) |
| **Setup time** | `pip install` + `install.sh` | Hours (broker, workers, monitoring) | Days (cluster + manifests) | Hours (server + workers) |
| **Designed for** | AI coding-session orchestration | Background-job queues | CI/CD & ML pipelines | Long-running business workflows |

The right way to read this: handoff-fanout is **not** a competitor to Celery / Argo / Temporal — it occupies the corner of the design space those tools deliberately don't enter (one workstation, IDE-tab granularity, no broker, git-aware). If you need durable distributed workflow execution, pick one of those instead.

## Documentation

- **[docs/PROTOCOL.md](docs/PROTOCOL.md)** — queue file format, state machine, atomicity guarantees.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — full 5-layer defense walk-through with sequence diagrams.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — dev setup, test layout, PR conventions.
- **[CHANGELOG.md](CHANGELOG.md)** — version history & extraction roadmap.

## Status

`v0.1.0` — **public extraction in progress**. Source modules ported. Bilingual docs, installer, and CI matrix are part of the `v1.0.0` milestone (this branch). The original implementation has been in daily production use since 2024-Q2 across an ERP project with 70+ DB tables and 250+ tests.

Once `v1.0.0` ships, the ERP repo will migrate to a thin shim that delegates to this package.

## License

MIT — see [LICENSE](LICENSE).
