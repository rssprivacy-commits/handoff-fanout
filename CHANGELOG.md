# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.5.1] — 2026-05-29

### Fixed

- **`__version__` no longer drifts from the release** — `__init__.py` hardcoded
  `1.4.0` and was never bumped, so the published 1.5.0 wheel reported itself as
  `1.4.0` via `handoff --version`. `__version__` now reads from installed
  package metadata (`importlib.metadata.version`), making pyproject the single
  source of truth.

## [1.5.0] — 2026-05-29

Queue-hygiene + auto-submit reliability release. Two new features (the `handoff
prune` janitor and the auto-continue Accessibility preflight) plus a heartbeat
leak fix, all surfaced by observing real erp-system queue/handoff behavior.
MINOR bump: backward-compatible additions, no contract changes.

### Added

- **`handoff prune`** (`eeedecc`) — new subcommand + `handoff-prune` console
  script. Scans every project queue and removes leftover `.heartbeat` /
  `.529-suspected` / `.uri` sidecars for *terminal* tasks (those with a
  `.done` / `.BLOCKED.md` marker), never touching history or active/unknown
  tasks. Dry-run by default; `--execute` to apply, `--project` to scope.
  Built after a real erp-system queue accumulated 81 `.md` / 8 heartbeat / 4
  stale `529` files with no command to clean them.
- **Accessibility preflight before auto-submit** (`428abc1`) —
  `install/auto-continue.sh` now runs a non-destructive `UI elements enabled`
  probe before the Enter keystroke. When the Accessibility grant is missing it
  skips the doomed keystroke, writes an `accessibility-missing` ack, and raises
  one rate-limited notification (once per run + once per 6h) instead of a
  silent per-task WARN. Spawn-loop `open` / frontmost / keystroke now route
  through the existing `HANDOFF_*_CMD` env overrides (were hardcoded), so the
  submit path is testable — covered by 4 new spawn-path tests.

### Fixed

- **Heartbeat leak on terminal states** (`6c88f79`) — `dump` left
  `queue/<task>.heartbeat` (and the sub-task batch heartbeat) on disk after a
  task went `done` / `blocked`, so the heartbeat read stale forever and
  watchdog mode 4/6 mis-flagged finished tasks as `529-suspected`. All four
  terminal paths (single-task done/blocked + batch done/blocked) now
  `unlink(missing_ok=True)` the heartbeat. 5 regression tests.

### Changed

- **Dev setup docs** (`258615b`) — `CONTRIBUTING.md` now documents the
  uv-native `uv sync --extra dev --extra lint` flow as the recommended path and
  warns that a bare `uv sync` (or `uv run --with pytest`) omits the dev extras.

## [1.4.0] — 2026-05-29

v5.4 Phase 4e codex audit fixes + watchdog mode 6 enforcement + precheck
reason enforcement + CJK path hardening. Bundles every change merged to
`main` since v1.3.0 (Phase 4d). MINOR bump: `357e2fd` adds a new
**required** reason field for non-`✅` retro statuses, a
backward-incompatible CLI contract change for callers that previously
submitted `⚠️`/`❌`/`skip` without a reason — so this is not patch-safe.

### Added

- **`watchdog` mode 6 auto-kill** (`5b5d9b7`) — the stale-task watcher
  now actively kills processes whose heartbeat went silent past the
  threshold and returns a structured `EnforceResult`, rather than only
  flagging `.529-suspected`. Complements the per-session `timeout`
  caveat below (active prevention) with passive recovery.
- **`templates` §第一步.5 timeout-wrap caveat** (`da45e9a`) — generated
  handoff prompts now instruct sessions to wrap long-running external
  CLI calls (codex / `claude -p` / `gh` / full `pytest`) in `timeout`,
  after a 19-minute codex hang froze a session and starved its
  heartbeat on 2026-05-29.
- **`precheck` reason enforcement** (`357e2fd`, `f2d8bd3`) — any non-`✅`
  retro status (`⚠️` / `❌` / `skip`) now requires a non-empty reason at
  both the CLI and gate layers; whitespace-only reasons are rejected.
  Defends against ceremonial evidence (8-sample audit found 7/8 with
  zero reasons = pass-in-name-only).

### Fixed

- **v5.4 Phase 4e codex R1 P0/P1 hardening** (`2b7942f`) — autoclose &
  follow-up overdue scanner hardened: timezone-aware deadline parsing
  (no lexical string compare), `follow_up` task-id path-traversal guard,
  and URI-nonce validation.
- **v5.4 Phase 4e P0-2 defense-in-depth** (`6d2998a`) — the retro gate
  rejects non-kebab-case `follow_up` task ids as a second layer.
- **v5.4 Phase 4e R2 P2 observability** (`8ac6e02`) — no silent failures
  in the autoclose watcher; every skipped/aborted path is logged.
- **safe-commit CJK path normalization** (`89d3fe4`) — `git` invocations
  use `core.quotepath=false` so CJK file paths no longer trip false
  positives in the segment-5 hijack check.
- **install-hook file-path contract + CJK + dual-mode** (`bb983f9`) —
  the installed pre-commit hook accepts safe-commit's file-path
  `HANDOFF_EXPECTED_FILES` contract (alongside the legacy colon form)
  and handles CJK paths under both modes.

## [1.3.0] — 2026-05-29

v5.4 Phase 4d — D-3 `old_ready` writer + D-4 autoclose & follow-up
overdue scanner. Closes the loop between v5.4 retro-evidence gate and v4
path-D autoclose, so a successful retro-gated dump now leaves enough
durable state on disk for the launchd watcher to (a) close the old tab
via a helper-extension URI without depending on PIDs or window titles,
and (b) hard-fail subsequent dumps in the same project when a
`HANDOFF_RETRO_BYPASS=1` promise to follow up retro misses its
ISO-8601 deadline. Phase 4c text in the handoff prompt is also flipped
to reflect that `HANDOFF_RETRO_MANDATE=1` is live system-wide.

### Added

- **`dump._write_old_ready`** — when the retro gate ran with a valid
  evidence file, the active dump now writes
  `ack/<task>.old_ready` per spec §7.6 with the full v5.4.1 schema:
  `schema_version`, `task_id`, `nonce`, `session_id`,
  `session_id_kind` (claude-uuid | fallback-fingerprint),
  `commit_hash`, `push_completed_at`, `tests_passed`,
  `memory_updated`, `dump_success`, `retro_evidence_hash` (sha256 of
  file bytes per §7.5), `retro_evidence_path` (relative to
  `~/.claude-handoff/<project>/`, portable across machines), and
  `retro_evidence_path_absolute` (local fast-path lookup).
  Atomic-written via the same `write_with_fsync` path the rest of the
  ack/queue artifacts trust.
- **`install/auto-continue.sh` v5.4 D-4 sections** — every external
  dependency (`open`, `osascript`, `shasum`, `code`) is now overridable
  via env vars so the launchd watcher can be fully exercised in tests:
  - **autoclose segment** — iterates `ack/*.submitted`, validates the
    matching `old_ready` (schema_version whitelist, retro_evidence_hash
    file integrity per §7.5, BLOCKED.md / failed-marker / done-marker
    short-circuits per v4 improvement #4-#5), acquires a per-task
    `locks/<task>.autoclose.lock` (mkdir-based, 5-min stale TTL,
    TOCTOU re-check after lock per v4 #4), and fires the helper URI
    `vscode://dharmaxis.handoff-helper/autoclose?task_id=…&nonce=…&project=…`.
    Default OFF — opt in via `HANDOFF_AUTOCLOSE_ENABLED=1` or a sentinel
    file at the global or project level (v4 改进 #6).
  - **follow-up overdue scanner** — every invocation scans
    `ack/*.retro.override.json` per spec §7.9: when the
    `follow_up_deadline` is past and the matching
    `precheck/<follow_task>.retro.evidence.json` is absent, stamps
    `ack/<task>.retro_overdue.txt` (idempotent — exists check before
    notify) and fires one `osascript display notification`. When the
    follow-up evidence appears later, unlinks both the marker and the
    original override and appends a closing line to the audit jsonl.
- **`tests/test_handoff_autoclose.py`** — 19 cases:
  - **A-01 .. A-12** (autoclose state machine): happy path, nonce
    propagation, no-submitted skip, helper-failed marker short-circuit
    (no_candidate / multiple_candidates / is_active_tab), per-task
    lock serialization under concurrent runs, stale-lock recycling,
    retro_evidence_hash tamper rejection, missing-evidence rejection,
    BLOCKED.md skip, unknown schema_version rejection, default-OFF
    guard.
  - **V-01 .. V-04** (follow-up overdue scanner): past-deadline marker,
    follow-up evidence clears marker + override, future-deadline
    no-marker, marker idempotency across runs.
  - **D-3 round-trip**: dump.main end-to-end produces a v5.4.1
    `old_ready` whose `retro_evidence_hash` matches `sha256(file_bytes)`
    and whose schema fields all map to §7.6 correctly; legacy path (no
    `--retro-evidence`) writes no `old_ready`.

### Changed

- **`templates.build_handoff_md`** — §-1 closing prompt now reports
  Phase 4c flipped ✅ and points at the three env paths that carry
  `HANDOFF_RETRO_MANDATE=1` system-wide (`~/.zshenv`,
  `launchctl setenv`, `auto-continue.plist EnvironmentVariables`).
  §7.13 enum reconcile note updated to point at the runtime as the
  authoritative source.
- **`install/auto-continue.sh` env contract** — `HANDOFF_ROOT`,
  `HANDOFF_OPEN_CMD`, `HANDOFF_OSASCRIPT_CMD`, `HANDOFF_SHA256_CMD`,
  `HANDOFF_CODE_BIN`, `HANDOFF_SKIP_SPAWN`, `HANDOFF_VSCODE_CHECK` and
  `HANDOFF_AUTOCLOSE_ENABLED` are honoured; previously hard-coded
  paths kept their original defaults so production launchd behaviour
  is unchanged.

### Notes

- Source-of-truth for `auto-continue.sh` now lives in
  `install/auto-continue.sh`; the previous ad-hoc copies under
  `~/.local/bin/` and ERP `scripts/` are re-synced from this file.
- Schema bump is `v5.4.1` (matches retro evidence schema_version), so
  watchers built against 1.1.0/1.2.x stay compatible — the gate's
  whitelist already accepts it.

## [1.2.1] — 2026-05-29

Bug fix: `handoff dump --status active` no longer pollutes the user's
clipboard during pytest runs.

Root cause (主人 2026-05-29 03:50+): the active path in `dump.main`
unconditionally piped the rendered handoff markdown into `pbcopy`.
`tests/test_retro.py` exercises `dump.main(argv)` end-to-end against
tmpdir fixtures with `project=demo` / `task=demo-task`. Every test run
silently replaced whatever the user had on the clipboard with the
fixture handoff text. 主人 hit this while pasting a handwritten
`v310-sub4-r5-p0-fixes` BLOCKED report and got `project=demo` /
`task=demo-task` sample text instead — within a hair of executing the
wrong path from a hijacked paste.

### Fixed

- **`dump._maybe_pbcopy(content)`** — new helper wraps the existing
  `subprocess.Popen(["pbcopy"], …)` call site. Skips the real call when
  either env var is set:
  - `PYTEST_CURRENT_TEST` (auto-set by pytest for every running test —
    zero-config protection for any future test that exercises
    `dump.main()`).
  - `HANDOFF_NO_PBCOPY` (manual opt-out for CI, headless sessions, or
    scripted callers).

  Preserves the existing `FileNotFoundError` / `OSError` swallow so
  non-macOS hosts keep working.
- **`tests/test_no_clipboard_pollution.py`** — 10 cases: 7 unit (both
  env guards skip when set, both env guards skip when set to empty
  string, both unset still copies, missing-binary soft-fail, EPIPE
  soft-fail) + 2 integration through `dump.main()` (active path with
  retro evidence does not pipe pbcopy under pytest /
  `HANDOFF_NO_PBCOPY`) + 1 sanity (pytest auto-sets
  `PYTEST_CURRENT_TEST`). Guard uses `"VAR" in os.environ` (presence,
  not truthiness) so empty string still suppresses — matches the
  documented contract.
- **`tests/conftest.py` autouse fixture `no_pbcopy_during_tests`** —
  session-wide sentinel that wraps `subprocess.Popen` and raises
  `AssertionError` if any test lets a `pbcopy` command escape (defence
  in depth — catches future regressions in unrelated tests).

### Notes

- Non-pytest, non-`HANDOFF_NO_PBCOPY` callers (the real launchd /
  auto-continue path the user spawns) keep the original clipboard copy
  behaviour — this only fences off test runs.

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
  the consumer replaces 4 hand-rolled handoff
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
