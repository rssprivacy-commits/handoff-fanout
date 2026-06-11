// §6c worker-worktree reclaim — extension-side close handler (contract v4).
//
// Pure, vscode-free, fs-free: like handoffClose.ts, every side effect is an
// injected dependency. The ONE new side-effect class — the producer-visible ack
// file (contract C2/C6: the watchdog resolves its cross-tick pending state by
// reading `ack/<task>.reclaim_ack.json`) — is returned to the caller as a fully
// computed AckIntent; extension.ts (the impure glue) performs the actual write.
// That keeps the gemini "extension decision logic stays pure" boundary while
// making C3's `close-command-expired` rejection and C6's done/failed acks real.
//
// Gate order (C7: rejection precedes ANY side effect — including the close):
//   1. project/task slug validation (an invalid slug never reaches a filesystem
//      path, so a rejected slug produces NO ack — log only).
//   2. role × reason whitelist matrix:
//        worker + reclaim                      → THIS path.
//        supervisor_succession + close_predecessor → delegate to the legacy
//            handleAutoclose (§6 gates; untouched).
//        anything else                          → fail-closed + ack
//            `role-reason-rejected`.
//   3. nonce must be hex16 (the nonce IS the auth token — C3 gemini M2).
//   4. run_id/issued_at/ack_timeout: malformed or outside the freshness window →
//      ack `close-command-expired` BEFORE any close (C3 codex M4 — a producer
//      whose ack-timeout already released the lock must never have its stale URI
//      kill the window a NEW spawn now occupies).
//   5. window-local self-targeting: only the window whose own title carries the
//      spawn_nonce acts; every other window logs + stays silent (no ack — the URI
//      landed in the wrong window; the producer's deadline handles non-delivery).
//   6. dirty gate: ANY dirty tab → refuse the whole close + ack `dirty` (stricter
//      than succession's skip-dirty: a reclaim must never half-close a window
//      holding unsaved work).
//   7. close via the shared dirty-safe retry-once mechanics → ack `done`; a close
//      that mechanically fails produces NO ack (fail toward the producer's
//      ack-timeout, never a false `done`).

import {
  CloseDeps,
  HandoffCloseParams,
  closeNonDirtyWithRetry,
  isValidNonce,
  titleHasNonce,
} from "./handoffClose";

export const ROLE_WORKER = "worker";
export const ROLE_SUCCESSION = "supervisor_succession";
export const REASON_RECLAIM = "reclaim";
export const REASON_CLOSE_PREDECESSOR = "close_predecessor";

// Coordinator (🧭中枢) windows carry this title prefix and are NEVER §6c reclaim
// targets (they retire via the succession close_predecessor leg, not reclaim) — so
// the poller never arms for them. Must match worktree.py ``_COORDINATOR_TITLE_PREFIX``.
export const COORDINATOR_TITLE_PREFIX = "🧭中枢·";

// Producer-config ack timeout arrives in the URI; cap it so a forged/buggy URI
// can't declare itself fresh for hours (mirrors reclaim.EXT_ACK_TIMEOUT_CAP).
export const ACK_TIMEOUT_CAP_S = 600;
export const ACK_TIMEOUT_DEFAULT_S = 30;

const SLUG_RE = /^[a-z0-9][a-z0-9-]*[a-z0-9]$/;
const RUN_ID_RE = /^[0-9a-f]{16}$/;

/** A reclaim run_id is the CSPRNG hex16 the producer minted; the dedupe set (and any
 *  "never write an ack under a forged run" guard) keys on it. */
export function isValidRunId(runId: string | null | undefined): runId is string {
  return typeof runId === "string" && RUN_ID_RE.test(runId);
}

/** The producer-facing ack payload (subset of reclaim.py's 18-reason enum). */
export interface AckIntent {
  /** Path RELATIVE to the handoff root (~/.claude-handoff). Glue joins + writes. */
  relPath: string;
  payload: {
    task: string;
    run_id: string;
    result: "done" | "failed";
    reason?: string;
    detail?: string;
    closed_count?: number;
    ts: string;
  };
}

export type ReclaimReason =
  | "closed" // done ack
  | "delegate-legacy" // succession row → caller runs handleAutoclose
  | "invalid-params" // bad slugs — no ack possible
  | "role-reason-rejected"
  | "nonce-mismatch"
  | "close-command-expired"
  | "not-this-window" // title doesn't carry the nonce — silent fail-closed
  | "dirty"
  | "close-failed"; // mechanics failed — no ack (producer times out)

export interface ReclaimResult {
  ok: boolean;
  reason: ReclaimReason;
  closedCount: number;
  ack: AckIntent | null;
}

export interface ReclaimDeps extends CloseDeps {
  /** This window's configured window.title (self-targeting gate). */
  windowTitle: () => string | undefined;
  /** Injected clock (ms epoch) so expiry tests don't depend on wall time. */
  now: () => number;
}

function ackIntent(
  project: string,
  task: string,
  runId: string,
  nowMs: number,
  payload: Omit<AckIntent["payload"], "task" | "run_id" | "ts">,
): AckIntent {
  return {
    relPath: `${project}/ack/${task}.reclaim_ack.json`,
    payload: {
      task,
      run_id: runId,
      ts: new Date(nowMs).toISOString(),
      ...payload,
    },
  };
}

function result(
  reason: ReclaimReason,
  ok: boolean,
  ack: AckIntent | null = null,
  closedCount = 0,
): ReclaimResult {
  return { ok, reason, closedCount, ack };
}

/** Effective freshness window in ms: producer value, defaulted + capped. */
export function effectiveAckTimeoutMs(ackTimeout: string | null | undefined): number {
  const parsed = Number(ackTimeout);
  const s =
    Number.isFinite(parsed) && parsed > 0 ? Math.min(parsed, ACK_TIMEOUT_CAP_S) : ACK_TIMEOUT_DEFAULT_S;
  return s * 1000;
}

export async function handleReclaim(
  params: HandoffCloseParams,
  deps: ReclaimDeps,
): Promise<ReclaimResult> {
  const { task, project, nonce, role, reason, runId, issuedAt, ackTimeout } = params;

  // 1. slugs first: an invalid project/task must never be interpolated into an ack
  //    path (path-traversal hardening) — reject with log only.
  if (!task || !project || !SLUG_RE.test(task) || !SLUG_RE.test(project)) {
    deps.log(
      `[handoff-helper] reclaim reject: invalid project/task slug (project=${project}, task=${task})`,
    );
    return result("invalid-params", false);
  }

  // 2. role × reason matrix (C7) — checked before EVERY other gate/side effect.
  if (role === ROLE_SUCCESSION && reason === REASON_CLOSE_PREDECESSOR) {
    return result("delegate-legacy", true); // §6 gates own that row (handleAutoclose)
  }
  const nowMs = deps.now();
  if (role !== ROLE_WORKER || reason !== REASON_RECLAIM) {
    deps.log(
      `[handoff-helper] reclaim reject: role×reason not whitelisted (role=${role}, reason=${reason}) task=${task}`,
    );
    const ack = RUN_ID_RE.test(runId ?? "")
      ? ackIntent(project, task, runId as string, nowMs, {
          result: "failed",
          reason: "role-reason-rejected",
          detail: `role=${role} reason=${reason}`,
        })
      : null; // un-attributable run → no ack (never write under a forged run id)
    return result("role-reason-rejected", false, ack);
  }

  // 3. the nonce is the auth token — malformed ⇒ the command is unauthenticated.
  if (!isValidNonce(nonce)) {
    deps.log(`[handoff-helper] reclaim reject: malformed nonce task=${task}`);
    const ack = RUN_ID_RE.test(runId ?? "")
      ? ackIntent(project, task, runId as string, nowMs, {
          result: "failed",
          reason: "nonce-mismatch",
          detail: "nonce not hex16",
        })
      : null;
    return result("nonce-mismatch", false, ack);
  }

  // 4. freshness (C3 codex M4): BEFORE any side effect. Malformed timing credentials
  //    are treated exactly like expiry — the command's timing cannot be trusted.
  if (!runId || !RUN_ID_RE.test(runId)) {
    deps.log(`[handoff-helper] reclaim reject: malformed run_id task=${task}`);
    return result("close-command-expired", false);
  }
  const issuedMs = Date.parse(issuedAt ?? "");
  const windowMs = effectiveAckTimeoutMs(ackTimeout);
  if (!Number.isFinite(issuedMs) || Math.abs(nowMs - issuedMs) > windowMs) {
    deps.log(
      `[handoff-helper] reclaim reject: close-command-expired (issued_at=${issuedAt}, window=${windowMs}ms) task=${task}`,
    );
    return result(
      "close-command-expired",
      false,
      ackIntent(project, task, runId, nowMs, {
        result: "failed",
        reason: "close-command-expired",
        detail: `issued_at=${issuedAt ?? "missing"}`,
      }),
    );
  }

  // 5. window-local self-targeting: only the worker window whose title carries the
  //    spawn_nonce acts. A miss is SILENT (no ack): the URI landed in some other
  //    window; an ack from here would terminate the producer's wait with a verdict
  //    about the WRONG window.
  const title = deps.windowTitle();
  if (!titleHasNonce(title, nonce)) {
    deps.log(
      `[handoff-helper] reclaim: this window is not the target (title=${title ?? "none"}) — fail-closed. task=${task}`,
    );
    return result("not-this-window", false);
  }

  // 6. dirty gate: a reclaim never closes (or half-closes) a window with unsaved work.
  const tabs = deps.getAllTabs();
  const dirtyCount = tabs.filter((t) => t.isDirty).length;
  if (dirtyCount > 0) {
    deps.log(`[handoff-helper] reclaim reject: ${dirtyCount} dirty tab(s) task=${task}`);
    return result(
      "dirty",
      false,
      ackIntent(project, task, runId, nowMs, {
        result: "failed",
        reason: "dirty",
        detail: `${dirtyCount} dirty tab(s)`,
      }),
    );
  }

  // 7. confirmed target + clean → shared close mechanics, then the done ack.
  deps.log(`[handoff-helper] reclaim: confirmed target window → closing task=${task}`);
  const close = await closeNonDirtyWithRetry(deps);
  deps.log(`[handoff-helper] reclaim task=${task} close=${JSON.stringify(close)}`);
  if (!close.ok) {
    // No ack: a false `done` would let the producer force-remove a worktree whose
    // window is still open; the producer's ack-timeout is the honest fallback.
    return result("close-failed", false, null, 0);
  }
  return result(
    "closed",
    true,
    ackIntent(project, task, runId, nowMs, {
      result: "done",
      closed_count: close.closedCount,
    }),
    close.closedCount,
  );
}

// ── A-poll revision (2026-06-12): the producer no longer PUSHES a close URI ───────
//
// Root cause it fixes: `open vscode://…` is delivered to ONE window (VS Code routes
// it to the active/focused window), so a worker window on another desktop never
// received its reclaim and the producer always timed out `ack-timeout`. The fix
// reverses push→pull: the producer writes `ack/<task>.reclaim_pending.json` (its
// post-gate authorization), and THIS window's extension polls its OWN pending file,
// rebuilds the same close params, and runs the UNCHANGED `handleReclaim` decision
// core. Window targeting is now intrinsic — an extension only ever reads its own
// task's pending — so it works regardless of which desktop the window is on.
//
// All four §6c invariants are preserved because the poll path funnels into the same
// `handleReclaim`: (C3) nonce self-targeting against the window title; (C3 M4)
// freshness — a pending polled past `issued_at + ack_timeout` is rejected
// `close-command-expired`, so a STALE pending can never close a window a new spawn
// now occupies (and even a lingering pending carries the OLD nonce, which a new
// window's title won't match → `not-this-window`); (C7) fail-closed — a mechanically
// failed close writes NO `done`, so the producer's `ack-timeout` still decides; the
// dirty gate still refuses any window with unsaved work.

/** This window's identity, parsed from its title, IFF it is a worker WORKTREE window
 *  (the only §6c reclaim target). */
export interface WorktreeWorkerIdentity {
  project: string;
  task: string;
}

/**
 * Parse a worker-worktree identity from the window title, or null if this window is
 * not a §6c reclaim target. The engine's `worktree.inject_vscode_workspace` writes
 * the title as `spawn_nonce.title_for` + the worktree marker:
 *   "<project> · <task> · <role> · <nonce> [worktree]${separator}${activeEditorShort}"
 * Coordinator windows are `🧭中枢·`-prefixed (succession leg, not reclaim) → null.
 * Non-worker roles, non-worktree titles, or malformed slugs/nonce → null (no poller).
 * Pure: only the identity needed to LOCATE the pending file; the nonce auth check
 * stays in `handleReclaim` (re-read from the live title at close time).
 */
export function parseWorktreeWorkerIdentity(
  title: string | undefined,
): WorktreeWorkerIdentity | null {
  if (typeof title !== "string") return null;
  if (title.startsWith(COORDINATOR_TITLE_PREFIX)) return null; // 🧭中枢 → not a worker
  if (!title.includes("[worktree]")) return null; // worktree-isolation windows only
  const segs = title.split(" · ");
  if (segs.length < 4) return null;
  const project = segs[0];
  const task = segs[1];
  const role = segs[2];
  const nonce = (segs[3] ?? "").split(/\s/)[0] ?? ""; // strip " [worktree]…" suffix
  if (role !== ROLE_WORKER) return null;
  if (!SLUG_RE.test(project) || !SLUG_RE.test(task) || !isValidNonce(nonce)) return null;
  return { project, task };
}

/**
 * Reconstruct close params from a polled `reclaim_pending` payload, or null if it is
 * missing a field the gate chain needs (a torn write / a pre-A-poll pending). The
 * pending is the producer's authorization signal; it carries the exact param set the
 * removed URI used to (role/reason/nonce/run_id/issued_at/ack_timeout) so the poll
 * path runs `handleReclaim`'s FULL matrix + freshness + nonce + dirty unchanged.
 */
export function pendingToParams(pending: unknown): HandoffCloseParams | null {
  if (!pending || typeof pending !== "object") return null;
  const p = pending as Record<string, unknown>;
  const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
  const task = str(p.task);
  const project = str(p.project);
  const runId = str(p.run_id);
  if (!task || !project || !runId) return null;
  return {
    task,
    project,
    nonce: str(p.nonce),
    role: str(p.role),
    reason: str(p.reason),
    runId,
    issuedAt: str(p.issued_at),
    ackTimeout: p.ack_timeout != null ? String(p.ack_timeout) : null,
    predecessorNonce: null,
  };
}

/** Injected side effects for one poll pass — keeps the loop logic pure/testable
 *  (no fs, no vscode, no timers). The glue supplies the real reads/writes. */
export interface PollReclaimDeps extends ReclaimDeps {
  /** The parsed `reclaim_pending.json` for THIS window's task, or null if absent. */
  readPending: () => unknown | null;
  /** Land the producer-facing ack bytes (temp+rename in the glue). */
  writeAck: (ack: AckIntent) => void;
  /** run_ids already acted on — shared with the legacy URI leg so a run is closed
   *  by EXACTLY one path (the pending lingers until the producer consumes the ack,
   *  so the next poll would otherwise re-fire). */
  handled: Set<string>;
}

/**
 * One poll pass: if THIS window's pending names a not-yet-handled run, run the close
 * decision and land its ack. Returns the ReclaimResult, or null when there is nothing
 * to do (no pending / unparseable / already handled). Idempotent and re-entrancy-safe
 * to call repeatedly. The `handled` set is updated BEFORE the (async) close so an
 * overlapping pass can never double-fire the same run.
 */
export async function pollReclaimOnce(deps: PollReclaimDeps): Promise<ReclaimResult | null> {
  const pending = deps.readPending();
  if (pending == null) return null;
  const params = pendingToParams(pending);
  if (!params || !isValidRunId(params.runId)) return null;
  if (deps.handled.has(params.runId)) return null; // already closed by poll or URI
  deps.handled.add(params.runId);
  const res = await handleReclaim(params, deps);
  deps.log(`[handoff-helper] reclaim poll: run=${params.runId} → ${res.reason}`);
  if (res.ack) deps.writeAck(res.ack);
  return res;
}
