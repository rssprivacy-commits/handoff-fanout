# CLAUDE.md — `handoff-fanout` project instructions

Project-specific rules for this repo. Cross-project rules live in `~/.claude/CLAUDE.md` (global) and take precedence; this file holds only what is true *here* and not derivable from the code.

## What this is

`handoff-fanout` is the **dispatch / hand-off / supervision hub** that lets the owner run an AI org across many desktops and many sessions, unattended. It produces a "baton" (a handoff queue file + spawn URI), a launchd/cron watchdog spawns the next VS Code window from it, and a layered gate stack governs whether a session may close out and advance (for this repo the always-on guard is the **pre-push** delivery-audit hook + the new-session §0 self-audit — the dump-time mandate is scoped out, see PROTOCOL §13.3). It is a **single-user, single-machine, internal CLI/automation tool** — no network listener, no funds, no third-party PII (so the commercial NFR dimensions — payments / GDPR / WCAG / SOC2 / i18n — are N/A).

**Authoritative docs** (read these, don't re-derive):
- Wire format + gate protocol → [`docs/PROTOCOL.md`](docs/PROTOCOL.md) (Part I = v1.0 base; Part II = the v5.4 governance gate layer — the SOT the in-code docstrings point to).
- Architecture → [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (5-layer defense + post-v1.0 layers).
- Comprehensive snapshot → [`project-files/handoff/architecture-2026-06-15/`](project-files/handoff/architecture-2026-06-15/) (`ARCHITECTURE-OVERVIEW.md` + 6 subsystem maps + `GAP-ANALYSIS.md`). Each dated map carries a snapshot banner; current authoritative gap status is `GAP-ANALYSIS.md §F`.
- Operational state / open loops → coordinator memory `~/.claude/projects/-Users-chenmingzhong-Projects-handoff-fanout/memory/` (`MEMORY.md` index + `open-loops.md` + `lesson-sw-coord-p*` history).

## 🔴 Business red-lines (project-specific invariants — violating these breaks live operation)

1. **Launcher = deployment COPY (post-commit deploy trap).** Only two files are deployed copies of repo sources: `install/auto-continue.sh` → `~/.local/bin/auto-continue.sh` and `install/dump-handoff.py` → `~/.local/bin/dump-handoff.py`. A `post-commit` hook auto-syncs them when committed. **Before committing a change to either**, `export HANDOFF_INSTALL_SH=/nonexistent` (skips the auto-deploy), verify the live launcher SHA is unchanged, and only after the gate is GREEN run `bash install/install.sh --sync-launcher` / `--sync-dump` deliberately. `install.sh` itself, `backup-handoff-state.sh`, and everything under `src/` (editable install — `src/` *is* live) are **not** on the deploy manifest — editing them is safe.

2. **`<project>/.spawn.lock` is an EMPTY directory — never write a file inside it.** Three actors (`spawn_lock.py`, `reclaim.py`, the bash `try_autoclose`) all `rmdir` it as an empty dir; a file inside wedges the whole project. The held-lock heartbeat refreshes mtime via `os.utime` only (see `spawn_lock.py` + PROTOCOL §12). Fencing data goes in *sibling* files.

3. **Every push to `main` (docs-only included) needs GREEN delivery-audit evidence.** The `pre-push` hook runs `handoff audit-check`; produce evidence with the dual-brain runner (`--evidence-repo <repo> --evidence-range <base>..<head> --out audits/…`), brief must end with `Verdict: GREEN|RED`. A RED verdict is released only by the owner via interactive-tty `handoff audit-override`. Don't teach sessions to self-bypass (emergency paths live in the owner runbook, not here). PROTOCOL §16.

4. **Don't touch other chains.** This repo is operated by a supervisor-coordinator chain (`sw-coord-pN`). A session here must not modify other projects' files/branches/sessions (erp / xunyin / fateforge / styleforge / wilde-hexe / sdgf), and must not touch the owner's dirty `dharmaxis` cc-global tree beyond its own scope.

5. **High-risk steps are owner-in-the-loop.** GC `--execute` (moves files) and real cross-desktop E2E (switches desktops) require the owner present; do not run them unattended.

## Test conventions (non-obvious — a wrong invocation gives false failures)

```bash
# Run the suite with the shim python (NOT bare python3):
#   bare python3 = Homebrew 3.14 lacks package metadata → spurious failures.
PYTHONPATH=src DX_SPAWN_SH=$HOME/Projects/dharmaxis/scripts/dx-spawn-session.sh \
  ~/.claude/skills/tob-modern-python/hooks/shims/python -m pytest -q
```
- `PYTHONPATH=src` — test the in-tree source (an editable install also works; a worktree needs this to avoid testing the live editable copy).
- `DX_SPAWN_SH` unset → the cross-repo spawn tests skip (environmental, not a failure).
- Baseline: **1670 passed / 3 skipped / 0 failed** (Python) + **82 passing** (VS Code extension).

## `$HANDOFF_HOME`

Defaults to `~/.handoff` in code (`config.DEFAULT_HOME`); the deployed install overrides it to `~/.claude-handoff` via the env var. On-disk paths resolve under `~/.claude-handoff/handoff-fanout/` in practice.

## 代码图谱状态

CodeGraph index present (SessionStart `codegraph-freshness.py` auto-syncs). Per RFC v7.1: use `codegraph_explore` for lookup/understanding; **grep-first** for caller/impact (DI/dynamic dispatch blinds the graph — never use "0 callers" as proof of no impact).

<!-- DO NOT EDIT MANUALLY between START_AUTO and END_AUTO -->
<!-- START_AUTO -->
- **CG**: FRESH
<!-- END_AUTO -->
