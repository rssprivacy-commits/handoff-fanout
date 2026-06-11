import * as assert from "assert";
import { TabLike } from "../src/handoffClose";
import {
  AckIntent,
  PollReclaimDeps,
  ReclaimResult,
  parseWorktreeWorkerIdentity,
  pendingToParams,
  pollReclaimOnce,
} from "../src/handoffReclaim";

// §6c A-poll (2026-06-12): the producer writes `reclaim_pending.json`; THIS window's
// extension polls it, rebuilds the close params, and runs the unchanged handleReclaim
// core. These tests exercise the pure poll surface (identity parse + pending→params +
// one poll pass) so the four invariants are demonstrably preserved under pull:
//   nonce self-targeting, freshness (stale pending never closes a new window),
//   C7 fail-closed (no false done), dirty refusal — plus run_id idempotency.

const NONCE = "0123456789abcdef";
const OTHER_NONCE = "feedfacecafebeef";
const RUN_ID = "00aa11bb22cc33dd";
const T0 = Date.parse("2026-06-12T08:00:00.000Z");

const TITLE =
  "handoff-fanout · sw-w1 · worker · " + NONCE + " [worktree]${separator}${activeEditorShort}";

function tab(label: string, isDirty = false): TabLike {
  return { label, isDirty };
}

// A pending payload exactly as reclaim.py `_process_request` now writes it.
function pending(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    run_id: RUN_ID,
    request_id: "sw-w1",
    task: "sw-w1",
    project: "handoff-fanout",
    role: "worker",
    reason: "reclaim",
    nonce: NONCE,
    ack_timeout: 30,
    path: "merged",
    issued_at: new Date(T0 - 1000).toISOString(), // 1s ago — fresh
    deadline_epoch: T0 / 1000 + 29,
    evidence: { pinned_head_sha: "abc", canonical_int_sha: "def", fetched_at: "x" },
    ...overrides,
  };
}

interface Calls {
  closed: TabLike[][];
  acks: AckIntent[];
  logs: string[];
}

function makePollDeps(opts: {
  pending?: Record<string, unknown> | null;
  tabs?: TabLike[];
  title?: string | undefined;
  nowMs?: number;
  closeOk?: boolean;
  handled?: Set<string>;
}): { deps: PollReclaimDeps; calls: Calls } {
  const calls: Calls = { closed: [], acks: [], logs: [] };
  const deps: PollReclaimDeps = {
    getAllTabs: () => opts.tabs ?? [tab("a"), tab("b")],
    closeTabs: async (tabs) => {
      calls.closed.push(tabs);
      return opts.closeOk ?? true;
    },
    delay: async () => undefined,
    log: (msg) => calls.logs.push(msg),
    windowTitle: () => ("title" in opts ? opts.title : TITLE),
    now: () => opts.nowMs ?? T0,
    readPending: () => ("pending" in opts ? opts.pending ?? null : pending()),
    writeAck: (ack) => calls.acks.push(ack),
    handled: opts.handled ?? new Set<string>(),
  };
  return { deps, calls };
}

describe("parseWorktreeWorkerIdentity", () => {
  it("parses project/task from a worker worktree title", () => {
    assert.deepStrictEqual(parseWorktreeWorkerIdentity(TITLE), {
      project: "handoff-fanout",
      task: "sw-w1",
    });
  });

  it("returns null for a coordinator (🧭中枢·-prefixed) window — succession leg, not reclaim", () => {
    assert.strictEqual(parseWorktreeWorkerIdentity("🧭中枢·" + TITLE), null);
  });

  it("returns null for a non-worker role", () => {
    const t =
      "handoff-fanout · sw-w1 · supervisor_succession · " +
      NONCE +
      " [worktree]${separator}${activeEditorShort}";
    assert.strictEqual(parseWorktreeWorkerIdentity(t), null);
  });

  it("returns null when the [worktree] marker is absent (singlepane / non-worktree)", () => {
    assert.strictEqual(
      parseWorktreeWorkerIdentity("handoff-fanout · sw-w1 · worker · " + NONCE),
      null,
    );
  });

  it("returns null for a malformed nonce or undefined title", () => {
    assert.strictEqual(
      parseWorktreeWorkerIdentity("handoff-fanout · sw-w1 · worker · NOTHEX [worktree]"),
      null,
    );
    assert.strictEqual(parseWorktreeWorkerIdentity(undefined), null);
  });
});

describe("pendingToParams", () => {
  it("rebuilds the full close-param set from a pending payload", () => {
    const params = pendingToParams(pending());
    assert.ok(params);
    assert.strictEqual(params?.task, "sw-w1");
    assert.strictEqual(params?.project, "handoff-fanout");
    assert.strictEqual(params?.role, "worker");
    assert.strictEqual(params?.reason, "reclaim");
    assert.strictEqual(params?.nonce, NONCE);
    assert.strictEqual(params?.runId, RUN_ID);
    assert.strictEqual(params?.ackTimeout, "30");
  });

  it("returns null when a required field (run_id/task/project) is missing", () => {
    assert.strictEqual(pendingToParams(pending({ run_id: undefined })), null);
    assert.strictEqual(pendingToParams(pending({ task: 42 })), null);
    assert.strictEqual(pendingToParams(null), null);
    assert.strictEqual(pendingToParams("not an object"), null);
  });
});

describe("pollReclaimOnce — happy path + idempotency", () => {
  it("a fresh pending for THIS window → closes + done ack + marks run handled", async () => {
    const handled = new Set<string>();
    const { deps, calls } = makePollDeps({ handled });
    const res = (await pollReclaimOnce(deps)) as ReclaimResult;
    assert.strictEqual(res.reason, "closed");
    assert.strictEqual(res.ok, true);
    assert.strictEqual(calls.acks.length, 1);
    assert.strictEqual(calls.acks[0].payload.result, "done");
    assert.strictEqual(calls.acks[0].payload.run_id, RUN_ID);
    assert.ok(handled.has(RUN_ID), "run must be marked handled");
  });

  it("a second pass with the SAME run already handled → no-op (no re-close, no ack)", async () => {
    const handled = new Set<string>([RUN_ID]);
    const { deps, calls } = makePollDeps({ handled });
    const res = await pollReclaimOnce(deps);
    assert.strictEqual(res, null);
    assert.strictEqual(calls.closed.length, 0);
    assert.strictEqual(calls.acks.length, 0);
  });

  it("no pending file → null, nothing happens", async () => {
    const { deps, calls } = makePollDeps({ pending: null });
    const res = await pollReclaimOnce(deps);
    assert.strictEqual(res, null);
    assert.strictEqual(calls.closed.length, 0);
  });
});

describe("pollReclaimOnce — the four §6c invariants under pull", () => {
  it("FRESHNESS: a pending polled past issued_at + ack_timeout → close-command-expired, NO close", async () => {
    // Producer wrote the pending at T0; this window only polls it 31s later (> 30s
    // window) — by then the producer has timed out, so a stale pending must NEVER close
    // a window a new spawn might now occupy.
    const { deps, calls } = makePollDeps({
      nowMs: T0 + 31_000,
      pending: pending({ issued_at: new Date(T0).toISOString(), ack_timeout: 30 }),
    });
    const res = (await pollReclaimOnce(deps)) as ReclaimResult;
    assert.strictEqual(res.reason, "close-command-expired");
    assert.strictEqual(calls.closed.length, 0, "a stale pending must not close the window");
    assert.strictEqual(calls.acks[0].payload.result, "failed");
    assert.strictEqual(calls.acks[0].payload.reason, "close-command-expired");
  });

  it("NONCE: a lingering pending whose nonce ≠ this window's title → not-this-window, NO close", async () => {
    // The window was re-spawned with a new nonce (title carries OTHER_NONCE); a leftover
    // pending naming the OLD nonce must not close the new occupant.
    const { deps, calls } = makePollDeps({
      title:
        "handoff-fanout · sw-w1 · worker · " +
        OTHER_NONCE +
        " [worktree]${separator}${activeEditorShort}",
      pending: pending({ nonce: NONCE }),
    });
    const res = (await pollReclaimOnce(deps)) as ReclaimResult;
    assert.strictEqual(res.reason, "not-this-window");
    assert.strictEqual(res.ack, null);
    assert.strictEqual(calls.closed.length, 0);
  });

  it("DIRTY: any dirty tab → dirty ack, the window is never even partially closed", async () => {
    const { deps, calls } = makePollDeps({ tabs: [tab("clean"), tab("wip", true)] });
    const res = (await pollReclaimOnce(deps)) as ReclaimResult;
    assert.strictEqual(res.reason, "dirty");
    assert.strictEqual(calls.acks[0].payload.reason, "dirty");
    assert.strictEqual(calls.closed.length, 0);
  });

  it("C7 FAIL-CLOSED: a mechanically failed close writes NO ack (producer ack-timeout decides)", async () => {
    const { deps, calls } = makePollDeps({ closeOk: false });
    const res = (await pollReclaimOnce(deps)) as ReclaimResult;
    assert.strictEqual(res.reason, "close-failed");
    assert.strictEqual(res.ack, null);
    assert.strictEqual(calls.acks.length, 0, "never a false done ack");
  });

  it("a malformed nonce in the pending → nonce-mismatch, NO close", async () => {
    const { deps, calls } = makePollDeps({ pending: pending({ nonce: "SHOUTING" }) });
    const res = (await pollReclaimOnce(deps)) as ReclaimResult;
    assert.strictEqual(res.reason, "nonce-mismatch");
    assert.strictEqual(calls.closed.length, 0);
  });
});
