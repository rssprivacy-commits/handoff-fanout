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

// URI contract: vscode://dharmaxis.handoff-helper/<path>?task=..&nonce=..&project=..
//
// The D-1 brief documents path `/close` with param `task`; the v4 design docs
// (and the D-2 watcher in auto-continue.sh) use `/autoclose` with `task_id`.
// To avoid a cross-component break before D-2 canonicalizes the contract, we
// accept BOTH path spellings and BOTH param names. The discrepancy is flagged
// for the master to settle in D-2.
export const CLOSE_PATHS: ReadonlySet<string> = new Set(["/close", "/autoclose"]);

export function isClosePath(path: string): boolean {
  return CLOSE_PATHS.has(path);
}

export function parseQuery(query: string): HandoffCloseParams {
  const p = new URLSearchParams(query);
  return {
    task: p.get("task") ?? p.get("task_id"),
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
