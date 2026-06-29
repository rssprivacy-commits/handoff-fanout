# DELTA — `autoclose-audited-workers.py` (live driver) reconcile patch

> 🔴 Red-line #1: this driver is a **deploy-audited COPY** under `~/.claude-handoff/supervisor-monitor/`
> (non-git, run-in-place). This worker produces the **complete patched file** as a repo artifact
> (`docs/req3-reconcile-driver/autoclose-audited-workers.py`); the **coordinator** deploys it via
> `deploy-audited.py` after the gate is GREEN. The worker NEVER writes the live file.

## What changed (additive only — zero edits to existing logic)

The patch is **purely additive glue**. No existing function body, signature, or control-flow line
was modified. All judgement stays in `handoff_fanout.autoclose_gate` (the driver carries none).

| # | Change | Lines |
|---|---|---|
| 1 | **Module docstring** — `discharged tasks`→`candidate tasks`; document the new `--reconcile` mode under `Modes:`. | docstring only |
| 2 | **New `run_reconcile(cfg, project, *, execute, idle_threshold, now_epoch=None, projects_root=None, windows=None) -> dict`** — gate every OPEN worker window via `autoclose_gate.reconcile_open_worker_windows` (which runs `gate_task_git_terminal` per window and returns only the `close_ok` decisions, each annotated with `.task`), bind each to its WID with the **existing** `resolve_wid`, and (on execute) close via the **existing** `invoke_close_tool`. Mirrors `run()`; writes the same durable JSONL log (records tagged `"mode": "reconcile"`). | +~60 (new fn) |
| 3 | **New `--reconcile` flag** added to the existing mutually-exclusive mode group (`--task` / `--sweep` / `--reconcile`). | +4 |
| 4 | **New reconcile branch in `main()`** — placed AFTER the kill-switch check and the `execute = requested_execute and opt_in_enabled(...)` computation, BEFORE the `--sweep`/`--task` branch. Gated on `_gate.reconcile_enabled(cfg, project)`: OFF ⇒ log a no-op skip and `return 0` (not even dry-run). ON ⇒ call `run_reconcile`. | +16 |

Total functional delta: **one new function + one new arg + one new `main()` branch.** Verify with
`diff -u ~/.claude-handoff/supervisor-monitor/autoclose-audited-workers.py docs/req3-reconcile-driver/autoclose-audited-workers.py`.

## Switch semantics (defense in depth — two independent switches)

- **`worker-autoclose-reconcile.enabled`** (NEW, DEFAULT-OFF, `autoclose_gate.reconcile_enabled`) —
  gates whether `--reconcile` runs **at all**. OFF ⇒ the mode is a no-op (so an hourly `--reconcile`
  daemon can be deployed **inert** until the owner flips this). Sources: env
  `HANDOFF_WORKER_AUTOCLOSE_RECONCILE=1` / fleet `$HANDOFF_HOME/worker-autoclose-reconcile.enabled` /
  per-project `$HANDOFF_HOME/<project>/worker-autoclose-reconcile.enabled`.
- **`worker-autoclose.enabled`** (EXISTING, DEFAULT-OFF) — still the prerequisite for `--execute` to
  take effect (else forced dry-run), in reconcile mode too. So a real close needs **BOTH** switches on.
- **`.worker-autoclose-off`** kill-switch — unchanged; present ⇒ the driver does nothing, including
  reconcile.

## NOT changed (explicitly out of scope per brief §2.2)

- The **sweep wrapper / launchd plist** are untouched. The coordinator separately points the hourly
  daemon at `--reconcile` (a plist `ProgramArguments` edit, routed to dx per red-line #4) when ready.
- `coord-close-windows.py` (the mechanical close tool) is untouched.

## Deployment (coordinator, post-GREEN)

```bash
# byte-for-byte deploy of the audited artifact to the live run-in-place driver:
~/.claude/scripts/deploy-audited.py record-audit \
    --dest   ~/.claude-handoff/supervisor-monitor/autoclose-audited-workers.py \
    --audited-file docs/req3-reconcile-driver/autoclose-audited-workers.py \
    --evidence <audit>.evidence.json --brief <audit-brief>
~/.claude/scripts/deploy-audited.py deploy \
    --src  docs/req3-reconcile-driver/autoclose-audited-workers.py \
    --dest ~/.claude-handoff/supervisor-monitor/autoclose-audited-workers.py
```

## Verification done by this worker (no live close)

- Artifact `py_compile`-clean.
- Driver-glue behavior-verified in isolation (mocked gate + mocked close tool + injected windows —
  NO real winlist/close): reconcile-switch OFF → no-op; ON → dispatch + WID-bind + invoke; `--execute`
  honored only with BOTH switches; kill-switch wins. (See report.)
- All reconcile **judgement** is unit-tested in `tests/test_autoclose_gate.py` (the gate functions).
  `tests/test_autoclose_driver.py` is intentionally left testing the LIVE (un-patched) driver and is
  NOT made to depend on the un-deployed reconcile mode (brief §2.3).
