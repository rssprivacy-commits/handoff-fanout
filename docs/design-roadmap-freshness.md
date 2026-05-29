# Design Spec — Roadmap Freshness / Git-Derived Phase Status

> Status: **PROPOSED (立项)** — 2026-05-29. Awaiting design decision before implementation.
> Origin: ERP-system handoff `nrv-service-phase2b` stale-premise incident (2nd recurrence).

## Problem

`get_roadmap_excerpt()` ([src/handoff_fanout/dump.py](../src/handoff_fanout/dump.py)) injects phase
sections verbatim from a **hand-maintained** roadmap file (configured via `roadmap.path`). It blindly
trusts the file's `✅ / ⏳ / 待启动` markers. When a maintainer forgets to update those markers after
shipping the work, the generator injects a **stale "phase pending" snapshot** into the handoff prompt,
which spawns an auto-continue session against an **already-completed phantom task** — burning tokens and
risking re-implementation of done work.

### Evidence (2 independent recurrences)

1. **2026-05-29 06:24 — commit `91aca85` (erp-system)**: diagnosed the stale-premise, but fixed the
   *wrong* file (`project-files/v310-followup-roadmap.md`) — not the file the generator actually reads
   (a memory file). Recurrence not prevented.
2. **2026-05-29 15:56 → 16:09**: the same `nrv-service-phase2b` phantom task spawned *again* from the
   still-stale injection source. Fixed at data level (marked phase ✅) + an authoritative-source warning
   header was added to the source file + the `section_regex` was hardened with a line-start anchor
   (`(?m)^####`). Those are mitigations, not a structural fix.

**Root structural gap**: the generator has no notion of *freshness*. A phase marked `⏳` whose described
artifacts already exist in git is indistinguishable, to the generator, from genuine pending work.

## Current behavior (baseline)

```python
# dump.py get_roadmap_excerpt() — simplified
matches = re.finditer(section_regex, content, re.DOTALL)   # config-driven section extraction
slice_  = matches[-max_sections:]                          # last N phase sections
return "\n\n".join(m.group(0)[:max_chars] for m in slice_) # injected verbatim, no validation
```

No cross-check against the repo's actual git state.

## Proposed approaches (to be chosen)

| # | Approach | Mechanism | Strictness | Cost |
|---|----------|-----------|------------|------|
| **A** | **Git-ancestor validation** | Phase entries that cite a commit (`HEAD=<sha>` / inline sha) → `git merge-base --is-ancestor <sha> HEAD`. If the cited commit is already in history but the phase is marked `⏳`, inject a **staleness banner**. | Warn (non-blocking) | Low–Med |
| **B** | **Artifact-existence probe** | If a `⏳`/pending phase names a file/symbol (e.g. `nrv_service.py`) that already exists in the worktree, flag "phase marked pending but artifacts exist". | Warn | Med (needs artifact extraction) |
| **C** | **HEAD-drift banner** | Roadmap file records a reference HEAD; if current `git HEAD` is N commits ahead, prepend "⚠️ roadmap may be stale (recorded HEAD=X, current=Y, +N commits)". | Warn | Low |
| **D** | **Explicit freshness assertion (gate-style)** | Dumping session must pass `--roadmap-verified` asserting they reconciled roadmap vs git — mirrors the existing v5.4 retro-evidence gate. | Hard-block | Low (reuses gate infra) |

Approaches compose: **C** (cheap drift banner) + **A** (commit-ancestor check) gives strong signal with
no false-positive hard-blocks. **D** adds human accountability but raises dump friction.

### Recommendation (for review)

Start with **C + A as non-blocking banners** (zero false-block risk, immediately useful), defer **D**
unless recurrence continues after C+A ship. **B** only if A's commit-citation coverage proves too sparse.

## Open design questions (decide before implementation)

1. **Warn vs block?** Banner-only (C/A) or hard-block the dump (D)? — affects dump friction.
2. **"Phase done" signal of record**: cited commit sha? named artifact existing? explicit `✅`? combination?
3. **Where does the banner go** — top of the roadmap excerpt, or a dedicated `## ⚠️ Freshness` section in the handoff prompt?
4. **Config surface**: new `roadmap.freshness: {mode: off|warn|block, ...}` block (opt-in, default `off` for backward-compat across all consumer projects)?
5. **Cross-project applicability**: must degrade gracefully for consumers whose roadmap files don't cite commits (no-op, not crash).

## Acceptance criteria

- [ ] A roadmap phase marked pending but whose cited commit is an ancestor of HEAD → handoff prompt carries a visible staleness warning.
- [ ] Default config behavior unchanged for existing consumers (feature opt-in).
- [ ] No crash / graceful degrade when git unavailable, no commits cited, or detached HEAD.
- [ ] Unit tests: stale-detected / fresh-clean / no-citation-degrade / git-absent. 
- [ ] CHANGELOG + PROTOCOL.md updated.

## Non-goals

- Auto-*editing* the roadmap file (generator stays read-only on the roadmap source).
- Replacing the hand-maintained roadmap with a fully git-derived one (too lossy — prose context matters).
