# Runbook — unlock-pivot rollout (merge → enable → release)

> **Status:** OWNER-GATED. None of the steps below run autonomously. They are
> ordered by **irreversibility / blast-radius** (do reversible/low-risk first,
> irreversible/high-blast last) and each is behind its own gate. **Do not run out
> of order. Do not merge code that is still being audited. Do not enable
> password-injecting auto-unlock on the financial repo without the on-box
> validation in Step 2.**
>
> Scope: rolling out the VS Code unlock-pivot (PR #2, branch `feat/vscode-unlock`)
> + the autoclose removal, on top of `main` (v1.8.0). Companion: the MindPersist
> `mp-unlock` capability (`mindpersist` `95e47de`).
>
> Principle: **审计 → 修 → 合并 → 灰度上机验证 → 启用 ERP → （需要才）发布。**

---

## Gate −1 — verify the deployment topology FIRST (实地, don't assume)

Before touching anything, confirm how each artifact is actually consumed — this
decides *what* you sync/release in Steps 2–3.

- [ ] **Which launcher actually runs?** The production launcher is a *copy* at
  `~/.local/bin/auto-continue.sh` (run by `com.dharmaxis.auto-continue`), synced
  from the repo via `install.sh --sync-launcher`. Confirm:
  ```bash
  launchctl print "gui/$(id -u)/com.dharmaxis.auto-continue" 2>/dev/null | grep -A2 ProgramArguments
  shasum ~/.local/bin/auto-continue.sh ~/Projects/handoff-fanout/install/auto-continue.sh
  ```
  → the unlock path lives in this bash launcher, so **enabling unlock = syncing
  this copy**, NOT a PyPI release.
- [ ] **How is the `handoff` Python CLI consumed?** PyPI pin vs editable:
  ```bash
  pip show handoff-fanout 2>/dev/null | grep -E "Version|Location"
  grep -rn "handoff-fanout" ~/Projects/erp-system/scripts/*.sh ~/Projects/erp-system/pyproject.toml 2>/dev/null | grep -i "handoff-fanout[>=]"
  ```
  → ERP shims pin `handoff-fanout>=1.8.0` (PyPI). The unlock feature is in the
  launcher (bash), so it does **not** require a PyPI bump to function; PyPI is
  only for the `dump.py §3.7` CLI change + the downstream pin.
- [ ] **MP unlock CLI reachable as a bare command?** (see Step 2 — it is NOT, by
  default; needs a wrapper.)

---

## Gate 0 — comprehensive codex audit (must pass before merge)

You do not merge code you are about to audit.

- [ ] Run the full codex audit of `feat/vscode-unlock` (the 5-pass prompt in the
  audit-prompt artifact). `codex exec --sandbox read-only`, gpt-5.x high.
- [ ] Triage findings → fix all P0 + P1 on the branch → re-run affected tests +
  `uvx ruff@0.15.5 check/format --check`.
- [ ] Re-confirm CI green on PR #2 after fixes.
- [ ] **Exit criterion:** 0 open P0/P1; P2s either fixed or consciously deferred
  with a note.

---

## Step 1 — merge PR #2 (reversible: revertable on main; default-OFF so inert)

- [ ] Prereqs: Gate 0 clean · CI green · you have reviewed the diff.
- [ ] Merge:
  ```bash
  cd ~/Projects/handoff-fanout
  gh pr checks 2          # all green
  gh pr merge 2 --squash  # or --merge, your call
  git checkout main && git pull --ff-only
  ```
- [ ] Post-merge sanity: `pytest -q` green on main; `git log --oneline -3`.
- Note: merging does NOT enable unlock — it ships the code default-OFF. Safe.
- Rollback if needed: `git revert <merge-sha>` + push (it's main, fully reversible).

---

## Step 2 — staged enablement (HIGHEST RISK: injects login password + runs unattended on financial code)

**This is not a one-line `touch`.** Gate each sub-step.

### 2a. Build a launcher-invocable unlock wrapper (REQUIRED — integration gap)
The launcher shells a **bare command** (`$HANDOFF_UNLOCK_CMD`) with no `cd`/
`PYTHONPATH`, but `python -m src.agent.unlock_cli` only resolves with mindpersist
on the path. So a raw `… -m src.agent.unlock_cli` will fail `ModuleNotFoundError`.
Create a stable wrapper:
```bash
cat > ~/.local/bin/mp-unlock <<'SH'
#!/bin/bash
# cross-project entry to MindPersist's lock-control CLI (handoff unlock-pivot)
cd "$HOME/Projects/mindpersist" || exit 2
exec .venv/bin/python -m src.agent.unlock_cli "$@"
SH
chmod +x ~/.local/bin/mp-unlock
mp-unlock --status; echo "exit=$?"   # 0=unlocked / 1=locked / 2=config error
```
- [ ] Wrapper created + `--status` returns a clean 0/1 (NOT 2).
- [ ] (Optional, cleaner long-term: promote this wrapper into the mindpersist repo
  as `bin/mp-unlock` + register in its `## 对外提供能力`.)

### 2b. Sync the production launcher
- [ ] `cd ~/Projects/handoff-fanout && bash install/install.sh --sync-launcher`
  (copies the merged launcher → `~/.local/bin/auto-continue.sh` + records the
  canonical sha so the drift guard is quiet).

### 2c. On-box validation on a THROWAWAY / non-financial project (NOT ERP yet)
This is the unlock-path equivalent of the §2.2 pre-enable spike — it cannot be
settled by unit tests (they stub the lock). Pick/create a throwaway project dir
under `~/.claude-handoff/<scratch>/`, point the unlock envs at the wrapper, opt
it in, then physically lock the screen and watch one real spawn:
- [ ] Configure the launchd job env (the `com.dharmaxis.auto-continue` plist
  `EnvironmentVariables`) — or export for a manual launcher run:
  `HANDOFF_UNLOCK_CMD="$HOME/.local/bin/mp-unlock --unlock"`,
  `HANDOFF_RELOCK_CMD="$HOME/.local/bin/mp-unlock --lock"`.
- [ ] `touch ~/.claude-handoff/<scratch>/unlock.enabled`
- [ ] Queue one trivial `.uri` for `<scratch>`, **lock the screen**, wait a tick.
- [ ] Verify in `~/.claude-handoff/auto-continue.log`: `UNLOCK-OK` → tab spawned
  + submitted → screen **re-locked** (verify it actually locked). Confirm the
  `.unlock.lock` mutex released, no orphan `caffeinate`, no `.relock-failed`.
- [ ] Force the failure paths once: wrong Keychain password → confirm cooldown
  marker + manual-pause (rc=2 ⇒ effectively permanent); `mp-unlock --lock` broken
  → confirm `.relock-failed` + spawns halt. Then restore.
- [ ] **Exit criterion:** unlock→GUI→relock + every failure path behaves as
  designed, observed on a real locked screen.

### 2d. Enable ERP (only after 2c passes + you say go)
- [ ] `touch ~/.claude-handoff/erp-system/unlock.enabled`
- [ ] Watch the first 1–2 real overnight/locked runs in `auto-continue.log`.
- Brakes (always available): `touch ~/.claude-handoff/STOP_AUTO` (pause all) ·
  `rm ~/.claude-handoff/erp-system/unlock.enabled` (disable ERP unlock only) ·
  `rm ~/.claude-handoff/<proj>/.unlock-cooldown` (clear a stuck cooldown).

---

## Step 3 — version bump + PyPI release (IRREVERSIBLE; optional; decoupled from unlock)

PyPI cannot re-upload a version — get it right once. **Not required to use
unlock** (that's the synced bash launcher); this is for the `dump.py §3.7` CLI
change + the ERP `handoff-fanout>=` pin.

- [ ] Decide whether a release is even needed now (does any consumer need the
  dump §3.7 atomic fix shipped? if not, defer).
- [ ] **Verify the bundle scope** (v1.4.0 lesson): list exactly what commits land
  in the tag; confirm no unintended breaking change is hidden in the range:
  ```bash
  curl -s https://pypi.org/pypi/handoff-fanout/json | python3 -c "import sys,json;print('PyPI latest:',json.load(sys.stdin)['info']['version'])"
  git log --oneline <last-released-tag>..HEAD
  ```
- [ ] **SemVer call (owner):** unlock feature (Added) + autoclose removal (a
  Removed item, but it was opt-in + never enabled) → MINOR `1.9.0` is the
  defensible read; flag to owner if you think the autoclose removal warrants MAJOR.
- [ ] Bump `pyproject.toml` + `CHANGELOG [1.9.0]` (move the [Unreleased] block) →
  commit with explicit pathspec (`git commit -m "…" -- <files>`, `-m` before `--`).
- [ ] codex **release audit** (only the release process, not re-auditing audited code).
- [ ] **Pre-publish multi-tab check** (release lesson): no parallel handoff-fanout
  session mid-flight (`find ~/.claude-handoff -name '*.heartbeat' -mmin -10`).
- [ ] Tag + build + `twine upload` (Keychain token: account `api-token-handoff-fanout-scoped`, verify `pypi-` prefix — NOT 2FA recovery codes).
- [ ] Bump the ERP shim pin if the CLI change is needed downstream.

---

## Brakes & rollback (reference)

| Situation | Action | Reversible? |
|---|---|---|
| Pause all auto-continue | `touch ~/.claude-handoff/STOP_AUTO` | yes (`rm`) |
| Disable ERP unlock | `rm ~/.claude-handoff/erp-system/unlock.enabled` | yes |
| Stuck unlock cooldown | `rm ~/.claude-handoff/<proj>/.unlock-cooldown` | yes |
| Mac stuck unlocked | `.relock-failed` marker present → spawns already halted; lock manually | n/a |
| Bad merge on main | `git revert <sha>` + push | yes |
| Bad PyPI release | **cannot unpublish** — must ship a new fixed version | NO |

## Why this order (the professional point)
Reversible-first, irreversible-last; every step behind a gate. The audit gates
the merge; on-box validation gates the financial-repo enablement; bundle-scope +
SemVer review gate the irreversible PyPI publish. Enabling unlock and releasing
to PyPI are **decoupled** (launcher syncs independently of the package).
