# Design Spec — Terminal-Native (iTerm2) Attended Auto-Continue

> Status: **V1 DRAFT** (brainstorming-approved; Checkpoints A/B/C confirmed by owner 2026-05-30; pre-codex)
>
> Author: Claude Code (集团中枢) · 2026-05-30
> Engine: **handoff-fanout** — developed under the ERP project per the 铁律
> (`~/Projects/handoff-fanout/src/...`, commit/push in that repo, lessons land in
> ERP memory). NOT a standalone spawn target.
> Scope: **new `terminal_daemon.py`** (iTerm2 AutoLaunch) + a per-project routing
> branch in `install/auto-continue.sh` + a `terminal.enabled` sentinel + install
> wiring + tests. No change to dump/precheck/retro/audit **gate semantics** (they
> are出口-agnostic CLI gates and are inherited verbatim).
>
> Sibling design: `docs/design-headless-fallback-display-off.md` (headless, no
> window, `claude -p`, lock-screen fallback). This spec is its **attended**
> complement (visible iTerm tab). The two are **mutually exclusive per project**
> and share all common machinery (atomic / ack / sentinel / guard semantics /
> retro+audit CLI gates).

---

## 0. TL;DR

Today's auto-continue relay is physically VS-Code-bound: its weakest link is the
`osascript … keystroke return` synthetic keystroke, which macOS forbids against a
locked session — so an unattended, display-asleep Mac dead-stalls (the root cause
the headless design also targets).

This spec adds a **terminal-native出口**: when a leg closes for a
`terminal.enabled` project, a resident **iTerm2 AutoLaunch daemon** opens a
**visible new iTerm tab**, `cd`s to the workspace, and runs `claude "<prompt>"`.
Because the prompt is delivered as a **CLI argument over the tty** (iTerm2
`async_send_text`, Apple-Events injection — NOT a GUI keystroke), submission
**needs no synthetic Enter** and is therefore not blocked by the lock screen.
That is the core value: an attended-yet-unattended-capable relay.

The daemon owns `terminal.enabled` projects **unconditionally** (locked or
unlocked — 方案 A). `auto-continue.sh` simply skips those projects, so its
existing two routes (unlocked→VS Code GUI, locked+opted-in→headless) serve only
**non-terminal** projects. Default **OFF**, per-project opt-in, fail-visible,
enabled only after on-box spikes pass.

---

## 1. Why (problem background — Phase 0, evidence-backed)

The deployed relay (`install/auto-continue.sh`, 859 lines) is VS-Code-bound at
three points:
- **Guard 4/5**: VS Code must be running + `code` CLI must exist (the GUI路径前置).
- **Step 1**: `code -r "$WORKSPACE"` activates the project window.
- **Step 2/3**: `open vscode://…` pastes the prompt, then
  `osascript … keystroke return` synthesizes Enter.

The **synthetic Enter is the unattended killer**: display sleep →
`screenLock=immediate` → frontmost becomes `loginwindow` → `is_frontmost_code`
aborts → Enter never pressed → tab open but unsubmitted → relay dead-stalls.

**Terminal-native escape (verified facts, this machine, 2026-05-30):**
- `claude "<prompt>"` launches and auto-runs the prompt immediately — **no
  synthetic Enter needed**. This sidesteps the lock-screen death-spot.
- iTerm2 `async_send_text` is **Apple-Events tty injection**, not GUI focus /
  synthetic keys.
- autoclose is simpler: each iTerm session has a stable `session_id`;
  `session.async_close()` closes it — no `dharmaxis.handoff-helper` VS Code
  extension, no nonce, no helper URI.

---

## 2. Verified preconditions (no assumptions — probed 2026-05-30)

1. **iTerm2 installed and is the active terminal.** `/Applications/iTerm.app`
   present; this session runs in it (`TERM_PROGRAM=iTerm.app`).
2. **`claude` is a zsh function** (`~/.zshrc:27`):
   `claude() { python3 ~/.openclaw/scripts/claude-rc.py --dangerously-skip-permissions "$@"; }`.
   ⇒ the spawned tab MUST be an **interactive login zsh** that sources `.zshrc`
   (iTerm2's default profile is exactly that); never invoke a bare `claude`
   binary. The wrapper **already carries `--dangerously-skip-permissions`**, so
   the spawned session's permission posture equals the owner's daily terminal —
   攻击面与现状一致 (not new).
3. **`claude-rc.py` does NOT caffeinate** (grep clean) ⇒ the terminal path must
   wrap the spawned process in `caffeinate -i` itself (§4 spike #3 / §3.2).
4. **iTerm2 Python API is currently OFF** (`EnableAPIServer` unset) — must be
   enabled (GUI toggle or `defaults write com.googlecode.iterm2 EnableAPIServer
   -bool true` + restart). First hard prerequisite.
5. **iTerm2 managed Python runtime not yet installed** (`iterm2env` absent) —
   the first AutoLaunch script triggers iTerm2 to download its own runtime +
   `iterm2` module (one-time, networked, may prompt). We do NOT manage pip.
6. **`~/Library/Application Support/iTerm2/Scripts/AutoLaunch/` is empty** —
   greenfield, no collision.
7. **`auto-continue.sh` already merged the headless fallback** — it is no longer
   single-path. Current routing: unlocked→GUI; locked+headless-opted-in→
   `dispatch_headless` (writes `headless-req/<task>.req` for the launchd headless
   runner). Terminal is therefore a **3rd route**, integrated into the existing
   lock-aware spawn loop, not a naive pre-guard.
8. handoff-fanout modules to reuse: `atomic`, `config.home_dir()`, `templates`
   (BLOCKED md), `handoff_precheck`/`retro_gate`/`codex_audit` (gates), the ack /
   sentinel / `launched/` atomic-claim conventions.

---

## 3. Design

### 3.0 Components (Checkpoint A)

| Component | Role | State |
|---|---|---|
| `dump` (micro-adjust) | writes `queue/<task>.md` + `.uri` (unchanged); a terminal-enabled project needs no new dump flag — the `.uri` is the trigger | reuse existing |
| **`terminal_daemon.py` (NEW)** | iTerm2 AutoLaunch daemon: connect iTerm2 API, poll queues of `terminal.enabled` projects, atomic-claim `.uri`, ensure project window, create tab → `cd` → `caffeinate -i claude "<prompt>"`, capture `session_id`, write ack/old_ready, autoclose old sessions | **this spec** |
| `install/auto-continue.sh` (1 branch) | at top of per-project loop: `terminal_enabled_for_project` → `continue` (skip GUI **and** headless for that project; leave `.uri` for the daemon) | small edit |
| `terminal.enabled` sentinel | per-project opt-in (mirrors `headless.enabled` / `autoclose.enabled`) | new |

### 3.1 Routing & coexistence (Checkpoint B-① / 方案 A)

`queue/<task>.uri` is the single trigger. Exactly **one** consumer must claim it.

- **`auto-continue.sh`**: a new `terminal_enabled_for_project()` check
  (`HANDOFF_TERMINAL_ENABLED=1` env, or `~/.claude-handoff/terminal.enabled`
  global, or `<project>/terminal.enabled`) is evaluated at the **top of the
  per-project loop**, BEFORE lock detection / GUI / headless dispatch. If true →
  `continue` (the launcher never touches that project's `.uri`, GUI or headless).
- **`terminal_daemon.py`**: owns `terminal.enabled` projects **unconditionally**
  (locked or unlocked — 方案 A, owner-decided). It claims the `.uri` via the
  **same atomic mv** to `launched/<task>-<ts>.txt`. Because the launcher skips
  these projects entirely, the daemon is the sole claimer — no cross-process
  race with the launcher.
- **Non-terminal projects**: launcher behavior **byte-unchanged** (unlocked→GUI,
  locked+opted-in→headless, locked+not-opted-in→defer).

Routing truth table (per project):

| project sentinel | screen | who handles |
|---|---|---|
| `terminal.enabled` | unlocked | **terminal daemon** (visible tab) |
| `terminal.enabled` | locked | **terminal daemon** (visible tab, CLI auto-submit; gated by spike #1) |
| not terminal | unlocked | VS Code GUI (unchanged) |
| not terminal | locked + headless opt-in | headless runner (unchanged) |
| not terminal | locked + no opt-in / unknown | defer + notify (unchanged) |

**Mutual exclusivity (anti-double-spawn invariant):** a project is EITHER
terminal-enabled (daemon-owned) OR not (launcher-owned). The launcher's `continue`
+ the shared `launched/` atomic claim guarantee a `.uri` is consumed once.

### 3.2 Spawn flow — daemon (Checkpoint B-②)

The daemon is an iTerm2 **AutoLaunch** script using the async API
(`iterm2.run_forever` + `async_get_app`). Main loop:

1. **Trigger — poll** `~/.claude-handoff/*/queue/*.uri` every ~2 s (no extra dep;
   simpler & steadier than fsevents; parity with the launcher's 1 s/60 s cadence).
   Process only `terminal.enabled` projects. Honor the **same guards**: global /
   project `STOP_AUTO` & `done`; per-task `<task>.done` & `<task>.BLOCKED.md`.
2. **Atomic claim**: `mv .uri → launched/<task>-<ts>.txt` (same primitive as the
   bash launcher; sole-claimer guaranteed by §3.1). Lost race ⇒ skip.
3. **Parse** `WORKSPACE=` from the claimed file; read `queue/<task>.md` (the
   prompt — `build_handoff_md` output).
4. **Ensure project window**: the daemon keeps `{project → window_id}` (persisted
   to a sidecar JSON so it survives a daemon restart). Missing/closed window ⇒
   `create_window`. **One window per project** = workspace-routing parity with
   `code -r`.
5. **Create tab + feed prompt**: `create_tab` in that window →
   `session.async_send_text("cd <workspace> && caffeinate -i claude \"$(cat '<abs>/<task>.md')\"\n")`.
   - `cd` fixes cwd (no reliance on tab inheriting it).
   - `caffeinate -i` keeps the **system** awake while claude runs, lets the
     display sleep (§4 spike #3; claude-rc.py doesn't self-caffeinate).
   - `"$(cat '…')"` avoids stuffing a ~40 KB prompt through `send_text` and
     avoids quote/backtick re-evaluation (zsh treats it literally inside `"$(…)"`).
   - `\n` is a literal tty newline (Apple-Events injection) — NOT a GUI keystroke,
     so it submits even while locked (the core advantage; spike #1 gates it).
6. **Record**: capture `session.session_id`; write `ack/<task>.spawned`,
   `ack/<task>.submitted` (CLI auto-submit is one-shot — no separate keystroke
   step), and `ack/<task>.old_ready` (autoclose evidence; **same schema** as the
   bash path). Persist `{task → session_id}` to the sidecar.
7. **Fail-visible** (never silent, never silent-fallback to VS Code): iTerm not
   running / API not authorized / window-or-tab create fails / API timeout →
   write `queue/<task>.BLOCKED.md` (via `templates.build_blocked_md`) +
   `display notification` + `ack/<task>.failed`. The claimed `launched/` file is
   the durable record; do not silently restore-and-loop.

### 3.3 Fan-out (Checkpoint B-③, P2)

`dump --split-into` still produces N sub-task `.uri` files (dump unchanged). The
daemon opens **N tabs** in the project's window — one `claude "<sub-prompt>"` per
sub-task — and `export HANDOFF_ROLE=sub-task` in each tab **before** launching
claude (parity with the VS Code fan-out's `git_guard` physical constraint, so a
sub-task tab cannot run a bare commit). The fan-in heartbeat
(`handoff-fanin-heartbeat`) is CLI-level and **unchanged**.

### 3.4 autoclose (Checkpoint B-③, P3)

The daemon holds `{task → session_id}` from spawn time. When an old leg's
`ack/<task>.old_ready` appears AND passes the **same evidence gate** as the bash
autoclose (verify `schema_version` ∈ known set + recompute `retro_evidence_hash`
and match) → `session.async_close()` on the recorded old session.

- **Simpler than VS Code**: no helper extension, no nonce, no helper URI — the
  daemon closes a session it owns by `session_id`.
- **Safety**: the daemon **only** closes (and only `send_text`s to) sessions it
  itself created and recorded. It NEVER touches an owner-opened tab.
- **Opt-in**: still gated by the existing `autoclose.enabled` sentinel/env
  (default OFF), for parity and caution.
- **"dirty tab never closed" parity**: close only after the evidence gate passes
  (= the task reached a clean terminal handoff). A running/busy session has no
  `old_ready` yet, so it is never closed mid-run.

### 3.5 STOP / lifecycle / safety (Checkpoint C policy)

- **STOP semantics (attended-specialized, owner-decided):** `暂停`/`永久停`
  (`STOP_AUTO`/`done`, global or project) make the daemon **stop opening new
  tabs**, but do **NOT kill** already-running visible tabs (attended — the owner
  may want to read them and Ctrl-C manually). This intentionally differs from the
  headless runner, which SIGTERMs the process group on STOP (no window to read).
- **Safety boundary:** the daemon only `send_text`/`close`es sessions it created
  and recorded in the sidecar.
- **Failure visibility:** every failure path writes a visible `BLOCKED.md` +
  notification (§3.2-7).
- **Gate inheritance:** retro + codex-audit mandates are enforced inside
  `handoff dump`/`precheck` (CLI), which the spawned `claude` runs at leg close —
  identical to every other出口. A mandate hard-fail surfaces as the spawned
  session's own dump exiting nonzero (and the next leg simply never being
  dispatched); the relay parks, it does not silently advance.

---

## 4. Pre-enable spike gates (BLOCKING — no enable without evidence)

Mirrors the headless §2.2 discipline: docs cannot settle these; capture evidence
in `docs/terminal-validation-evidence.md`. Any **hard-gate** failing ⇒ terminal
stays OFF (VS Code + headless remain the zero-regression default). A
non-hard-gate failure degrades the affected sub-feature only.

| # | Spike | Hard gate? | If it fails |
|---|---|---|---|
| **1** | **Locked-screen `send_text`** — with the screen actually locked (display-off→immediate lock), `create_tab` + `async_send_text` opens a tab AND `claude` produces output | **YES** (gates 方案 A) | fall back to 方案 B (terminal only when unlocked; locked → headless) — a design change requiring owner re-decision |
| **2** | **AutoLaunch daemon survives display-off / lock** — iTerm2 keeps the script alive and polling/spawning while the display sleeps | **YES** | terminal path cannot be unattended; keep OFF |
| **3** | **system-sleep / caffeinate** — `caffeinate -i`-wrapped child keeps the system awake for its lifetime; confirm no idle-sleep freeze mid-run | no | document bounded-degraded (resumes on wake), like headless §3.3 |
| **4** | **API enable + AutoLaunch trust persistence** — API on, AutoLaunch script trusted (no cookie), survives iTerm2/Mac restart | **YES** | daemon can't connect; keep OFF |
| **5** | **`claude` function resolves in the spawned tab** — default profile = interactive login zsh that sourced `.zshrc` | **YES** | `command not found`; fix profile or invoke `claude-rc.py` directly |
| **6** | **`session_id` stability + `async_close` + sidecar recovery** (autoclose) | no (P3) | ship P1/P2 without autoclose; close manually |

A separate one-time **setup** (not a spike, but a prerequisite): enable the API,
trigger the managed-runtime install, drop the AutoLaunch script, grant any first
-run prompt. Documented in the install runbook.

---

## 5. Failure-mode matrix

| Scenario | Behavior |
|---|---|
| terminal-enabled, unlocked, `.uri` queued | daemon opens visible tab; `caffeinate -i claude "<prompt>"` auto-runs; chain continues |
| terminal-enabled, locked, `.uri` queued | same (CLI auto-submit; gated by spike #1) |
| iTerm not running / API unauthorized | `BLOCKED.md` + notification + `ack/.failed`; no silent VS Code fallback |
| window/tab create fails | `BLOCKED.md` + notification + `ack/.failed` |
| `claude` unresolved in tab | tab shows error; spike #5 must pass before enable |
| STOP_AUTO/done during run | daemon stops new tabs; running tabs left for the owner (attended) |
| daemon restart mid-relay | sidecar restores `{project→window_id}` + `{task→session_id}`; pending `.uri` re-claimed on next poll |
| system fully asleep before claim | resumes on wake (bounded-degraded; documented, not "fixed") |
| autoclose evidence invalid | do NOT close; leave tab + log (fail-closed) |
| non-terminal project | launcher path byte-identical (zero regression) |

---

## 6. Test plan (parity with `tests/test_headless_runner.py` etc.)

Daemon tested with the iTerm2 API **stubbed** (a fake app/window/session
recording `create_tab`/`send_text`/`close` calls) so CI needs no real iTerm2:

- routing: `terminal.enabled` ⇒ launcher `continue`s (no GUI/headless side
  effects); not-enabled ⇒ existing path unchanged.
- atomic claim: two daemon ticks claim a `.uri` once (mv-to-`launched/`).
- spawn: asserts `create_window` once per project, `create_tab` per task,
  `send_text` contains `cd <workspace>` + `caffeinate -i` + `cat <task>.md` + `\n`;
  `ack/.spawned`+`.submitted`+`.old_ready` written; sidecar updated.
- guards: STOP_AUTO/done ⇒ no new tab; `<task>.done`/`.BLOCKED.md` ⇒ skip.
- fan-out: N `.uri` ⇒ N tabs, each with `HANDOFF_ROLE=sub-task` exported.
- autoclose: `old_ready` with valid hash ⇒ `async_close` on recorded session;
  invalid hash ⇒ not closed; never closes a non-recorded session.
- fail-visible: stubbed API error ⇒ `BLOCKED.md` + `ack/.failed`, no fallback.
- STOP attended semantics: running (recorded) session NOT closed on STOP.
- regression: all existing handoff-fanout tests green; launcher byte-identical
  for non-terminal projects.

On-box (manual, gated, recorded in `docs/terminal-validation-evidence.md`): the
§4 spikes #1–#6.

---

## 7. Phasing (owner-decided: full parity, staged)

- **P1 — single-leg MVP:** daemon + routing branch + sentinel + spawn flow +
  fail-visible + tests; spikes #1/#2/#4/#5 pass → enable for one project.
- **P2 — fan-out:** N-tab spawn + `HANDOFF_ROLE` parity + tests.
- **P3 — autoclose:** `session_id` close + evidence gate + sidecar + spike #6.

Each phase: feature branch + codex audit (architecture upgrade ≥5 files ⇒ 4-round)
+ default OFF + on-box spike pass before enable + lesson into ERP memory.

---

## 8. Out of scope

- Removing/altering the VS Code GUI path or the headless runner (both kept).
- Non-iTerm2 terminals (Terminal.app/tmux/WezTerm) — iTerm2 only (owner-decided).
- Waking a fully-asleep Mac (`pmset` scheduled wake / WoL).
- Engine **gate semantics** (dump/precheck/retro/audit) — inherited verbatim.
- A standing always-on power-assertion daemon (only per-run `caffeinate -i`).
