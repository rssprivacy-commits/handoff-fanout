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

// Producer-config ack timeout arrives in the URI; cap it so a forged/buggy URI
// can't declare itself fresh for hours (mirrors reclaim.EXT_ACK_TIMEOUT_CAP).
export const ACK_TIMEOUT_CAP_S = 600;
export const ACK_TIMEOUT_DEFAULT_S = 30;

const SLUG_RE = /^[a-z0-9][a-z0-9-]*[a-z0-9]$/;
const RUN_ID_RE = /^[0-9a-f]{16}$/;

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
