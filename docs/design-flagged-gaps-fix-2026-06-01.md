# Design — fix 3 reconcile-flagged pre-existing gaps (2026-06-01)

Task `fix-handoff-engine-flagged-gaps`. Source of the flags:
`reconcile-handoff-cli-v54` (Gemini R2) — see ERP memory
`lesson-reconcile-handoff-cli-v54-2026-06-01` §🚩flagged + ERP
`project-files/non-stage-cleanup-orchestration.md` §审计日志「③闭环」🚩.

**Constraint (red line):** these are atomicity / hygiene / traceability fixes,
**NOT** changes to mandate determination semantics. Do **not** touch
`retro_gate._run_retro_gate` mandate logic. Zero regression target.

All edits land in `src/handoff_fanout/dump.py` (+ a 1-line jq projection in
`templates.py` for gap 3 visibility). Dev happens under the ERP session context
(editable install); commit lands in the handoff-fanout repo.

---

## Gap 1 — batch fan-out writes launcher-visible files non-crash-atomically

### Problem
The single-task active path writes its launcher-visible files with
`atomic.atomic_replace` (temp + `os.replace`) precisely because the launchd
WatchPaths watcher tails `queue/*.uri` and the spawned session reads
`queue/<task>.md` — a reader must never observe a truncated/partial file
(`dump.py:500-506`, `:586-588`, with an explicit rationale comment).

The **batch** path (`handle_open_batch`, `trigger_fan_in_if_ready`) instead uses
`atomic.write_with_fsync` (in-place `O_CREAT|O_WRONLY|O_TRUNC`) for the same
launcher-visible `.uri`/`.md` files. `write_with_fsync` is durable but exposes a
window where a concurrent reader sees a truncated file (the `O_TRUNC` empties it
before the new bytes land). In batch mode, launchd can wake on the `.uri` the
instant it's `O_TRUNC`-emptied → torn read.

### Fix (scope = launcher-visible `.uri` + `.md` only)
Convert exactly these 4 call sites from `write_with_fsync` → `atomic_replace`:

| dump.py line | file | role |
|---|---|---|
| 810 | `queue/<sub_id>.md` | sub-task description (spawned session reads) |
| 821 | `queue/<sub_id>.uri` | sub-task launchd WatchPaths trigger |
| 889 | `queue/<fan_in_task>.md` | fan-in description |
| 892 | `queue/<fan_in_task>.uri` | fan-in launchd WatchPaths trigger |

The atomic_replace temp name (`.{name}.tmp.<pid>.<ns>`) never matches the
launcher's `*.uri`/`*.md` glob, so an early WatchPaths wake on the temp is a
harmless no-op (same property the single-task path relies on).

### Deliberately OUT of scope (kept as `write_with_fsync`) — with rationale
- `env_path` (`write_role_env`, :446) — `batches/<id>/<sub>.env`, **not** in
  `queue/`, never globbed by the launcher.
- `manifest.json` (:778) — `batches/<id>/`, not launcher-watched.
- `.done` / `.blocked` (`handle_batch_done` :930 / `handle_batch_blocked` :965)
  — live in `batch_dir`, read by `trigger_fan_in_if_ready` via
  `batch_dir.glob("*.done")` using **`.stem` (filename) only, never content**.
  A torn O_TRUNC write can only make the file momentarily empty, which does not
  change the existence-based "this sub-task finished" signal → no correctness bug.
- `.BLOCKED.md` (`handle_batch_done`/`handle_batch_blocked` error paths
  :918 / :954) — in `queue/`, but the launcher only **existence-checks** it
  (`count_global_active_tabs` :378 + launchd skip). Its content is for humans /
  §0 audit. The single-task path itself writes BLOCKED.md with plain
  `write_text` (:521, no fsync), so making the batch BLOCKED.md atomic would make
  batch *stricter* than single-task — an inconsistency for marginal value. Left
  as-is to stay in the narrow flagged scope.

**Open question for R1:** is leaving `.BLOCKED.md` (918/954) non-atomic
acceptable, or should batch+single-task BLOCKED.md both be hardened (separate,
broader change)?

---

## Gap 2 — `handle_cleanup_orphan` misses `.old_ready` + `.heartbeat`

### Problem
`find_orphans` builds a per-orphan dict and `handle_cleanup_orphan --apply`
unlinks `[spawned, submitted, queued, blocked_md]` + launched txts (`dump.py:1054`).
It omits two residue files that the rest of the engine creates per task:
- `ack/<task>.old_ready` (written by `_write_old_ready`, :677) — accumulates as
  stale orphan audit metadata.
- `queue/<task>.heartbeat` (the per-session heartbeat the handoff baseline starts;
  the single-task done/blocked paths already unlink it at :515/:532) — a leaked
  heartbeat keeps ticking and watchdog **mode 6** mis-flags it as 529-suspected.

### Fix
1. In `find_orphans` add to the dict (mirroring the existing keys):
   - `"old_ready_path": ack_dir / f"{task_id}.old_ready"`
   - `"heartbeat_path": queue_dir / f"{task_id}.heartbeat"`
2. In `handle_cleanup_orphan` extend the unlink list with
   `o["old_ready_path"], o["heartbeat_path"]`.

`unlink(missing_ok=True)` already tolerates absent files, so adding them is safe
for orphans that never produced an old_ready/heartbeat.

---

## Gap 3 — `old_ready` only anchors workspace HEAD (cross-repo blind spot)

### Problem
`_write_old_ready` records `commit_hash = git rev-parse HEAD` of the **workspace**
(the consumer project, e.g. erp-system). For a dual-repo task (engine code in
`handoff-fanout` + docs/config in erp-system), the engine-side commit is invisible
to the §0 new-session predecessor audit — traceability escapes.

### Key finding (Phase 0)
The engine **already** has validated cross-repo plumbing: `audit-close --code-repo`
→ `codex_audit._build_code_repo_keys` puts `code_repo` (abs path) +
`code_repo_head` (sha) into the `codex_audit` block (`codex_audit.py:529, 557-618`),
and the gate binds G0 to that HEAD. `_write_old_ready` already surfaces
`codex_audit_hash` / `codex_audit_mode` / `next_session_forced_task` from that
block (:664-674) — it simply does **not** surface `code_repo` / `code_repo_head`.

So gap 3's fix is **additive surfacing of an already-computed, already-validated
value** — not new git logic, not a new flag.

### Fix
In `_write_old_ready`, inside the existing `if isinstance(codex_audit, dict):`
block, after the forced-task handling, add:

```python
code_repo = codex_audit.get("code_repo")
code_repo_head = codex_audit.get("code_repo_head")
if (
    isinstance(code_repo, str) and code_repo
    and isinstance(code_repo_head, str) and code_repo_head
):
    old_ready["code_repo"] = code_repo
    old_ready["code_repo_head"] = code_repo_head
```

Plus (visibility) add `code_repo_head` to the §0 audit `jq` projection in
`templates.py` so a new session on a cross-repo task sees the engine HEAD.

### Backward-compat / schema
- `old_ready` is **unhashed metadata** — no consumer hashes the file; consumers
  (`templates.py` §0 jq, `handoff_precheck`) read named fields. Adding optional
  fields is harmless to all readers.
- The fields are **only present** when the codex_audit block carries them (i.e.
  audit-close was run with `--code-repo`). Same-repo dumps → fields absent →
  old_ready byte-stable.
- **No schema_version bump.** `OLD_READY_SCHEMA_VERSION = EVIDENCE_SCHEMA_VERSION`
  is shared; bumping it would falsely signal an *evidence* schema change (the
  evidence/codex_audit schema is unchanged — code_repo_head already exists there)
  and break unrelated `== "5.5.0"` assertions. Additive optional fields on
  unhashed metadata don't require a bump.

**Open question for R1:** is "no schema bump for additive optional old_ready
fields" the right call, or should there be a dedicated old_ready version separate
from EVIDENCE_SCHEMA_VERSION?

### Effectiveness honesty
This closes the gap **only when the dispatcher declares the cross-repo via
`--code-repo`** (the engine cannot guess a task is cross-repo). That is the
correct design — the dispatcher knows; the engine records what it's told (already
validated). This very task will dogfood it: its closing dump runs
`audit-close --code-repo ~/Projects/handoff-fanout`, so its own old_ready will
record the handoff-fanout HEAD.

---

## Tests (new — none of these functions had tests for the changed behavior)
- **Gap 1:** monkeypatch `atomic.write_with_fsync` + `atomic.atomic_replace` to
  record call paths; run `handle_open_batch` (and trigger fan-in) on a real git
  workspace + manifest; assert every `queue/*.uri` and `queue/*.md` went through
  `atomic_replace`, while `.env`/`manifest.json` went through `write_with_fsync`.
  Plus a no-`.tmp.`-residue assertion on queue_dir.
- **Gap 2:** extend `test_cleanup_orphan_apply_deletes_all` (or add a sibling) to
  create `.old_ready` + `.heartbeat` and assert `--apply` removes them.
- **Gap 3:** unit-test `_write_old_ready` with an evidence payload whose
  codex_audit block has `code_repo`/`code_repo_head` → asserts they appear in
  old_ready; and a same-repo payload (no keys) → asserts they're absent
  (byte-stability of the common case).

## Regression
Run in clean env to avoid the known uv-shim subprocess false-failures:
`env -u HANDOFF_AUDIT_MANDATE -u HANDOFF_RETRO_MANDATE PATH=/usr/bin:$PATH .venv/bin/python -m pytest tests/`
Expect the existing 457 green + new cases, zero regression.

---

## R1 dual-brain review resolution (codex + Gemini independent + CC cross-validation)

- **codex P1 — VALID, in-scope symmetric gap.** `watchdog._dump_degraded_fan_in`
  (`watchdog.py:297, 300`) is also a batch fan-in producer of launcher-visible
  `queue/<fan_in_task>.md` + `.uri`, still using `write_with_fsync`. Same
  torn-read window. **Gap 1 scope expanded from 4 → 6 call sites** (add these 2)
  per #1 立法 (symmetric gaps fixed in one pass). The watchdog `.BLOCKED.md` /
  `.529-suspected` markers (`watchdog.py:237, 373`) stay `write_with_fsync`
  (existence-checked, consistent with the BLOCKED.md decision).

- **codex P2 — VALID scope note.** `find_orphans` scans all `ack/*.spawned`,
  which can include batch sub-tasks whose heartbeat lives in
  `batches/<batch>/<sub>.heartbeat` (NOT `queue/`). Gap 2's
  `queue/<task>.heartbeat` + `ack/<task>.old_ready` cleanup is therefore
  **explicitly scoped to single-task orphan residue** (the originally-flagged
  shape — `.old_ready` is only ever written for single-task retro dumps, never
  for sub-tasks). Batch sub-task heartbeats are tied to the batch_dir lifecycle
  and already unlinked on terminal by `handle_batch_done`/`blocked` (:937/:975).
  `unlink(missing_ok=True)` makes the single-task cleanup a harmless no-op for
  sub-task orphans. Documented; not broadened (5/24 boundary).

- **Gemini P1 — FALSE POSITIVE (CC arbitrated via code).** Gemini claimed
  `handle_batch_done`/`blocked` heartbeat cleanup (:937/:975,
  `batch_dir / f"{sub_task_id}.heartbeat"`) is wrong and should be `queue_dir`.
  `templates.py:365` proves the sub-task heartbeat baseline writes to
  `batches/{batch_id}/{sub_task_id}.heartbeat` = `batch_dir`. The current code is
  **correct**; "fixing" it would introduce a regression. NOT changed.

- **Gemini P2 — out-of-contract / low / OUT OF SCOPE.** `removesuffix("-done")`
  brittleness. `templates.py:418` shows the template always invokes
  `--task {sub_task_id}-done`, so removesuffix is correct under the real calling
  contract (even for ids ending in `-done`, since the template appends another).
  Pre-existing, different class, non-blocking → flagged for owner, not fixed here.

- **Both brains agree:** `.done`/`.blocked` (existence/stem-globbed) and
  `.BLOCKED.md` (existence-checked, single-task precedent uses plain write_text)
  correctly stay `write_with_fsync`; no `old_ready` schema bump; no retro_gate
  mandate semantics change.

### Final Gap 1 scope = 6 call sites
`dump.py` 810 (`<sub_id>.md`), 821 (`<sub_id>.uri`), 889 (`<fan_in_task>.md`),
892 (`<fan_in_task>.uri`); `watchdog.py` 297 (`<fan_in_task>-watchdog.md`),
300 (`<fan_in_task>-watchdog.uri`).

---

## R2 dual-brain review resolution (implementation, on the actual diff)

**Verdict: the 3-gap fix is CLEAN. No P0/P1 on the changed code.** 464 tests pass
in clean env (457 existing + 7 new), zero regression.

- **codex R2 (reviewed `git diff -- src/ tests/`):** no P0/P1. Confirms all 6
  atomic_replace conversions correct + complete; gap2 dict-keys match + unlink is
  `missing_ok`-safe; gap3 type-guard correct + omits for same-repo; no retro_gate
  mandate change; new tests are not vacuous. One **P2 process note**: the new
  `tests/test_batch_atomic_writes.py` is untracked → must be `git add`-ed
  explicitly at commit (handled).

- **Gemini R2:** surfaced 7 findings, **all in pre-existing code outside this
  diff** (verified): `_find_stuck_pids` pgrep matching (watchdog.py:489),
  Gate-A prefix/glob blind spot (`expand_ownership`), `_fanin_triggered`
  crash-window state machine, global `/tmp/heartbeat-<task>.pid` namespacing
  (templates — I touched only the jq line), Mode-6 never-started blind spot,
  `acquire_lock` unlink race, 529-marker empty-shell. **None relate to the
  3-gap fix.** Per the 5/24 boundary (non-blocking, different class,
  pre-existing) these are FLAGGED for the owner / a future task, not absorbed
  here. (The 529-marker empty-shell is the same family as gap1 but is an
  existence-checked marker — consistent with the documented `.BLOCKED.md`
  decision to keep `write_with_fsync`.)

### 🚩 flagged for owner (Gemini R2 — pre-existing, out of this task's scope)
1. `watchdog._find_stuck_pids` mode-6 kill relies on `pgrep -fa` matching the
   heartbeat path in the subshell cmdline — may not match reliably (contested).
2. `expand_ownership` Gate-A overlap check is disk-scan based → two sub-tasks
   declaring the same not-yet-created `prefix`/`glob` dir aren't caught.
3. `_fanin_triggered` is atomic-created before the slow `detect_baseline` + `.uri`
   write → a crash in that window can wedge the batch until the 3h timeout.
4. `/tmp/heartbeat-<task>.pid` lacks project/batch namespacing → same-named tasks
   across projects can clobber each other's PID file.
5. Mode-6 single-task heartbeat scan misses the "heartbeat never written" case.
6. `watchdog.acquire_lock` stale-lock `unlink()` should be `missing_ok=True`.
7. 529-marker `atomic_create` + `write_with_fsync` can leave a 0-byte marker on
   mid-write crash.
