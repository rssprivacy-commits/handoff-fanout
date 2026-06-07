// Pure, vscode-free close logic for the handoff-helper extension.
//
// This module deliberately imports nothing from `vscode` so it can be unit
// tested with plain mocha (no @vscode/test-electron download / display server).
// extension.ts is the thin glue that wires the real vscode APIs into these
// injectable dependencies.

/** Minimal shape of a vscode.Tab that this logic needs. */
export interface TabLike {
  readonly isDirty: boolean;
  readonly label: string;
}

/** Parsed query params from the handoff close URI. */
export interface HandoffCloseParams {
  task: string | null;
  nonce: string | null;
  project: string | null;
}

// Canonical URI contract (settled in D-2):
//   vscode://dharmaxis.handoff-helper/autoclose?task_id=<id>&nonce=<hex>&project=<slug>
//
// D-1 provisionally accepted both `/close`|`/autoclose` paths and `task`|`task_id`
// params to avoid a cross-component break before the contract was pinned. D-2
// canonicalizes to the single form the launchd watcher actually emits — see
// install/auto-continue.sh `try_autoclose`, which hardcodes
// `/autoclose?task_id=…&nonce=…&project=…`. The legacy `/close` path and `task`
// param are dropped: there is no producer of those forms (the watcher is the
// sole emitter), so tightening the receiver to match the sender removes
// unreachable surface rather than breaking any caller.
export const AUTOCLOSE_PATH = "/autoclose";

export function isClosePath(path: string): boolean {
  return path === AUTOCLOSE_PATH;
}

export function parseQuery(query: string): HandoffCloseParams {
  const p = new URLSearchParams(query);
  return {
    task: p.get("task_id"),
    nonce: p.get("nonce"),
    project: p.get("project"),
  };
}

// A6 (master decision 2026-05-29): nonce = secrets.token_hex(8) → exactly 16
// lowercase hex chars (64-bit entropy). D-1 validates the FORMAT only; matching
// the nonce against ack/<task>.submitted content is D-2 (needs ack/config
// plumbing that is out of scope here).
const NONCE_RE = /^[0-9a-f]{16}$/;

export function isValidNonce(nonce: string | null | undefined): boolean {
  return typeof nonce === "string" && NONCE_RE.test(nonce);
}

/** Injected side effects, so the core logic stays pure and testable. */
export interface CloseDeps {
  /** All tabs across all tab groups, already flattened. */
  getAllTabs: () => TabLike[];
  /** Close the given tabs; resolves true on success (mirrors vscode tabGroups.close). */
  closeTabs: (tabs: TabLike[]) => Promise<boolean>;
  /** Delay helper (injected so tests don't wait real time). */
  delay: (ms: number) => Promise<void>;
  /** Structured log sink. */
  log: (msg: string) => void;
}

export type CloseReason =
  | "closed"
  | "nothing-to-close"
  | "invalid-nonce"
  | "missing-params"
  | "close-failed";

export interface CloseResult {
  ok: boolean;
  reason: CloseReason;
  closedCount: number;
  skippedDirty: number;
  retried: boolean;
}

// A3 (master decision): one delayed retry (500ms) then give up.
export const RETRY_DELAY_MS = 500;

/**
 * Core handoff-close handler.
 *
 * Flow: validate params → validate nonce format → flatten tabs → skip dirty
 * (A4) → close the rest → on `close()===false`, wait 500ms and retry once (A3).
 *
 * Targeting is intentionally coarse for the D-1 MVP: it closes every non-dirty
 * tab in the activated window. Narrowing to the specific stale task tab (by
 * nonce/session fingerprint) is D-2 work — see checklist #2/#8.
 */
export async function handleHandoffClose(
  params: HandoffCloseParams,
  deps: CloseDeps,
): Promise<CloseResult> {
  if (!params.task || !params.nonce) {
    deps.log(
      `[handoff-helper] reject: missing params (task=${params.task}, nonce=${params.nonce ? "set" : "null"})`,
    );
    return base("missing-params", false);
  }

  if (!isValidNonce(params.nonce)) {
    deps.log(`[handoff-helper] reject: invalid nonce format for task=${params.task}`);
    return base("invalid-nonce", false);
  }

  const allTabs = deps.getAllTabs();
  const dirty = allTabs.filter((t) => t.isDirty);
  let closeable = allTabs.filter((t) => !t.isDirty);

  if (closeable.length === 0) {
    deps.log(
      `[handoff-helper] nothing to close for task=${params.task} (total=${allTabs.length}, dirty=${dirty.length})`,
    );
    return { ok: true, reason: "nothing-to-close", closedCount: 0, skippedDirty: dirty.length, retried: false };
  }

  let attempted = closeable.length;
  let ok = await tryClose(deps, closeable);
  let retried = false;

  if (!ok) {
    retried = true;
    deps.log(`[handoff-helper] close() failed, retrying once after ${RETRY_DELAY_MS}ms`);
    await deps.delay(RETRY_DELAY_MS);

    // Re-fetch and re-filter before the retry: closed Tab handles become
    // invalid after close(), and a tab may have turned dirty during the delay.
    // A4 forbids ever closing a dirty tab, so we must not reuse the stale set.
    const freshTabs = deps.getAllTabs();
    closeable = freshTabs.filter((t) => !t.isDirty);
    if (closeable.length === 0) {
      const freshDirty = freshTabs.filter((t) => t.isDirty).length;
      deps.log(
        `[handoff-helper] nothing left to close after retry delay (total=${freshTabs.length}, dirty=${freshDirty})`,
      );
      return { ok: true, reason: "nothing-to-close", closedCount: 0, skippedDirty: freshDirty, retried: true };
    }
    attempted = closeable.length;
    ok = await tryClose(deps, closeable);
  }

  const result: CloseResult = {
    ok,
    reason: ok ? "closed" : "close-failed",
    closedCount: ok ? attempted : 0,
    skippedDirty: dirty.length,
    retried,
  };
  deps.log(`[handoff-helper] task=${params.task} result=${JSON.stringify(result)}`);
  return result;
}

// Treats a rejected close (not just a `false` resolution) as a failed attempt,
// so a transient throw still triggers the single retry instead of bubbling up
// as an unhandled error.
async function tryClose(deps: CloseDeps, tabs: TabLike[]): Promise<boolean> {
  try {
    return await deps.closeTabs(tabs);
  } catch (err) {
    deps.log(`[handoff-helper] closeTabs threw: ${String(err)}`);
    return false;
  }
}

function base(reason: CloseReason, ok: boolean): CloseResult {
  return { ok, reason, closedCount: 0, skippedDirty: 0, retried: false };
}

// ── Single-pane (close side bars natively) — 2026-06-06 ───────────────────────
// The cold-spawn launcher wants a freshly spawned worktree window to end up as a
// SINGLE editor pane (no left/right side bars). Earlier launcher attempts sent
// OS-level keystrokes (Cmd+B, then a custom cmd+ctrl+alt+9 chord via osascript)
// which were fragile: option-mutated characters, a keybindings.json dependency,
// toggle state (a toggle REOPENS an already-closed bar), and keyboard-focus races
// (the Claude chat input could swallow the key). The robust mechanism is to call
// VS Code's OWN explicit close commands from the extension host — no keystroke, no
// keybinding, cannot be eaten by a focused text input, idempotent. The launcher
// fires `vscode://dharmaxis.handoff-helper/singlepane?task_id=<task>` AFTER the
// prompt tab is open + submitted, so whatever the Claude URI re-opened (the chat
// side bar) is closed last.
export const SINGLEPANE_PATH = "/singlepane";

export function isSinglePanePath(path: string): boolean {
  return path === SINGLEPANE_PATH;
}

// EXPLICIT (not toggle) close commands — idempotent: an already-closed bar stays
// closed, never reopens. closeSidebar = primary side bar; closeAuxiliaryBar =
// secondary/auxiliary side bar. Together they cover both, regardless of which is
// on the left/right (layout-independent).
export const SINGLEPANE_COMMANDS = [
  "workbench.action.closeSidebar",
  "workbench.action.closeAuxiliaryBar",
] as const;

export interface SinglePaneDeps {
  /** fsPath of the active window's .code-workspace file, or undefined. */
  workspaceFile: () => string | undefined;
  /** Run a VS Code command (injected for testability). */
  executeCommand: (command: string) => Promise<unknown>;
  /** Structured log sink. */
  log: (msg: string) => void;
}

export type SinglePaneReason =
  | "closed"
  | "missing-task"
  | "wrong-window"
  | "command-failed";

export interface SinglePaneResult {
  ok: boolean;
  reason: SinglePaneReason;
  ran: number;
}

// Guard: only collapse side bars on a HANDOFF WORKTREE window — one whose
// workspace file is a `.handoff.code-workspace` (the engine-injected cold-spawn
// workspace). A stray vscode:// from anywhere else, or one that lands on the
// owner's normal window, is rejected — so this can never collapse side bars on a
// window the owner is actively using (multi-window red line).
export function isHandoffWorktreeWorkspace(workspaceFile: string | undefined): boolean {
  return (
    typeof workspaceFile === "string" &&
    workspaceFile.endsWith(".handoff.code-workspace")
  );
}

export async function handleSinglePane(
  params: HandoffCloseParams,
  deps: SinglePaneDeps,
): Promise<SinglePaneResult> {
  if (!params.task) {
    deps.log("[handoff-helper] singlepane reject: missing task");
    return { ok: false, reason: "missing-task", ran: 0 };
  }
  const wf = deps.workspaceFile();
  if (!isHandoffWorktreeWorkspace(wf)) {
    deps.log(
      `[handoff-helper] singlepane reject: active window is not a handoff worktree (workspaceFile=${wf ?? "none"}) task=${params.task}`,
    );
    return { ok: false, reason: "wrong-window", ran: 0 };
  }
  let ran = 0;
  try {
    for (const cmd of SINGLEPANE_COMMANDS) {
      await deps.executeCommand(cmd);
      ran += 1;
    }
  } catch (err) {
    deps.log(`[handoff-helper] singlepane command failed after ran=${ran}: ${String(err)}`);
    return { ok: false, reason: "command-failed", ran };
  }
  deps.log(
    `[handoff-helper] singlepane closed side bars (ran=${ran}) task=${params.task} workspace=${wf}`,
  );
  return { ok: true, reason: "closed", ran };
}

// ── Single-pane on STARTUP (window load) — 2026-06-06 ─────────────────────────
// Owner complaint: closing the side bars only AFTER the submit makes the spawned window stay 3-column for too
// long ("等那么久干嘛"). Fix (codex+gemini dual-brain audit → owner chose this): when a HANDOFF WORKTREE window
// finishes loading (the extension activates on `onStartupFinished`), collapse the side bars immediately — so the
// window becomes single-pane on load instead of after the readiness-gate. No launcher timing, window-local, uses
// VS Code's own commands. Same `.handoff.code-workspace` guard so it never touches the owner's normal windows.
export interface StartupDeps {
  /** fsPath of the active window's .code-workspace file, or undefined. */
  workspaceFile: () => string | undefined;
  /** Run a VS Code command (injected for testability). */
  executeCommand: (command: string) => Promise<unknown>;
  /** Structured log sink. */
  log: (msg: string) => void;
}

export type StartupReason = "closed" | "not-handoff-worktree" | "command-failed";

export interface StartupResult {
  ran: boolean;
  reason: StartupReason;
}

export async function runStartupSinglePane(deps: StartupDeps): Promise<StartupResult> {
  const wf = deps.workspaceFile();
  if (!isHandoffWorktreeWorkspace(wf)) {
    deps.log(
      `[handoff-helper] startup: not a handoff worktree (workspaceFile=${wf ?? "none"}) — skip single-pane`,
    );
    return { ran: false, reason: "not-handoff-worktree" };
  }
  try {
    for (const cmd of SINGLEPANE_COMMANDS) {
      await deps.executeCommand(cmd);
    }
  } catch (err) {
    deps.log(`[handoff-helper] startup single-pane command failed: ${String(err)}`);
    return { ran: false, reason: "command-failed" };
  }
  deps.log(`[handoff-helper] startup: closed side bars (single-pane) for handoff worktree ${wf}`);
  return { ran: true, reason: "closed" };
}
