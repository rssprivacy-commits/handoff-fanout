import * as assert from "assert";
import { TabLike } from "../src/handoffClose";
import {
  ACK_TIMEOUT_CAP_S,
  ACK_TIMEOUT_DEFAULT_S,
  ReclaimDeps,
  ReclaimResult,
  effectiveAckTimeoutMs,
  handleReclaim,
} from "../src/handoffReclaim";

const NONCE = "0123456789abcdef";
const OTHER_NONCE = "feedfacecafebeef";
const RUN_ID = "00aa11bb22cc33dd";
const T0 = Date.parse("2026-06-11T08:00:00.000Z");

// The engine-written worker worktree title: title_for(...) + the [worktree] suffix.
const TITLE =
  "handoff-fanout · sw-w1 · worker · " + NONCE + " [worktree]${separator}${activeEditorShort}";

function tab(label: string, isDirty = false): TabLike {
  return { label, isDirty };
}

interface Calls {
  closed: TabLike[][];
  logs: string[];
}

function makeDeps(opts: {
  tabs?: TabLike[];
  title?: string | undefined;
  nowMs?: number;
  closeOk?: boolean;
}): { deps: ReclaimDeps; calls: Calls } {
  const calls: Calls = { closed: [], logs: [] };
  const deps: ReclaimDeps = {
    getAllTabs: () => opts.tabs ?? [tab("a"), tab("b")],
    closeTabs: async (tabs) => {
      calls.closed.push(tabs);
      return opts.closeOk ?? true;
    },
    delay: async () => undefined,
    log: (msg) => calls.logs.push(msg),
    windowTitle: () => ("title" in opts ? opts.title : TITLE),
    now: () => opts.nowMs ?? T0,
  };
  return { deps, calls };
}

function params(overrides: Record<string, string | null> = {}) {
  return {
    task: "sw-w1",
    project: "handoff-fanout",
    nonce: NONCE,
    role: "worker",
    reason: "reclaim",
    runId: RUN_ID,
    issuedAt: new Date(T0 - 1000).toISOString(), // issued 1s ago — fresh
    ackTimeout: "30",
    predecessorNonce: null,
    ...overrides,
  };
}

function assertNoSideEffects(res: ReclaimResult, calls: Calls): void {
  assert.strictEqual(res.ok, false);
  assert.strictEqual(calls.closed.length, 0, "no close may have happened");
}

describe("effectiveAckTimeoutMs", () => {
  it("uses the producer value when sane", () => {
    assert.strictEqual(effectiveAckTimeoutMs("45"), 45_000);
  });
  it("caps a huge value at the receiver-side ceiling", () => {
    assert.strictEqual(effectiveAckTimeoutMs("99999"), ACK_TIMEOUT_CAP_S * 1000);
  });
  it("falls back to the default on garbage / missing / non-positive", () => {
    for (const v of ["banana", "", null, undefined, "-5", "0"]) {
      assert.strictEqual(effectiveAckTimeoutMs(v as never), ACK_TIMEOUT_DEFAULT_S * 1000);
    }
  });
});

describe("handleReclaim — role×reason matrix (C7: rejection precedes side effects)", () => {
  it("worker+reclaim on the target window → closes + done ack", async () => {
    const { deps, calls } = makeDeps({});
    const res = await handleReclaim(params(), deps);
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.reason, "closed");
    assert.strictEqual(res.closedCount, 2);
    assert.ok(res.ack, "done ack expected");
    assert.strictEqual(res.ack?.payload.result, "done");
    assert.strictEqual(res.ack?.payload.run_id, RUN_ID);
    assert.strictEqual(res.ack?.relPath, "handoff-fanout/ack/sw-w1.reclaim_ack.json");
  });

  it("succession+close_predecessor → delegate-legacy (no close, no ack here)", async () => {
    const { deps, calls } = makeDeps({});
    const res = await handleReclaim(
      params({ role: "supervisor_succession", reason: "close_predecessor" }),
      deps,
    );
    assert.strictEqual(res.reason, "delegate-legacy");
    assert.strictEqual(res.ack, null);
    assert.strictEqual(calls.closed.length, 0);
  });

  it("worker+close_predecessor → role-reason-rejected + ack, no close", async () => {
    const { deps, calls } = makeDeps({});
    const res = await handleReclaim(params({ reason: "close_predecessor" }), deps);
    assert.strictEqual(res.reason, "role-reason-rejected");
    assertNoSideEffects(res, calls);
    assert.strictEqual(res.ack?.payload.reason, "role-reason-rejected");
  });

  it("succession+reclaim → role-reason-rejected, no close", async () => {
    const { deps, calls } = makeDeps({});
    const res = await handleReclaim(params({ role: "supervisor_succession" }), deps);
    assert.strictEqual(res.reason, "role-reason-rejected");
    assertNoSideEffects(res, calls);
  });

  it("missing role / unknown reason → role-reason-rejected, no close", async () => {
    for (const p of [params({ role: null }), params({ reason: "banana" })]) {
      const { deps, calls } = makeDeps({});
      const res = await handleReclaim(p, deps);
      assert.strictEqual(res.reason, "role-reason-rejected");
      assertNoSideEffects(res, calls);
    }
  });

  it("matrix reject with an UNPARSEABLE run_id writes NO ack (never under a forged id)", async () => {
    const { deps } = makeDeps({});
    const res = await handleReclaim(params({ reason: "banana", runId: "zzz" }), deps);
    assert.strictEqual(res.reason, "role-reason-rejected");
    assert.strictEqual(res.ack, null);
  });
});

describe("handleReclaim — identity gates", () => {
  it("invalid project slug → invalid-params, NO ack (path-traversal hardening)", async () => {
    const { deps, calls } = makeDeps({});
    const res = await handleReclaim(params({ project: "../etc" }), deps);
    assert.strictEqual(res.reason, "invalid-params");
    assert.strictEqual(res.ack, null);
    assertNoSideEffects(res, calls);
  });

  it("malformed nonce → nonce-mismatch ack, no close", async () => {
    const { deps, calls } = makeDeps({});
    const res = await handleReclaim(params({ nonce: "SHOUTING" }), deps);
    assert.strictEqual(res.reason, "nonce-mismatch");
    assert.strictEqual(res.ack?.payload.reason, "nonce-mismatch");
    assertNoSideEffects(res, calls);
  });

  it("this window's title does not carry the nonce → silent fail-closed (no ack, no close)", async () => {
    const { deps, calls } = makeDeps({
      title:
        "handoff-fanout · sw-w1 · worker · " +
        OTHER_NONCE +
        " [worktree]${separator}${activeEditorShort}",
    });
    const res = await handleReclaim(params(), deps);
    assert.strictEqual(res.reason, "not-this-window");
    assert.strictEqual(res.ack, null);
    assertNoSideEffects(res, calls);
  });

  it("undefined window title → fail-closed", async () => {
    const { deps, calls } = makeDeps({ title: undefined });
    const res = await handleReclaim(params(), deps);
    assert.strictEqual(res.reason, "not-this-window");
    assertNoSideEffects(res, calls);
  });
});

// ── P0 #5 (contract SHOULD-必做清单): ack-timeout 后迟到送达的 close 必须被
// close-command-expired 拒绝 —— 在任何关窗副作用之前。
describe("handleReclaim — close-command-expired (P0 #5)", () => {
  it("a LATE close (now - issued_at > ack_timeout) is rejected BEFORE any close", async () => {
    const { deps, calls } = makeDeps({ nowMs: T0 + 31_000 });
    const res = await handleReclaim(
      params({ issuedAt: new Date(T0).toISOString(), ackTimeout: "30" }),
      deps,
    );
    assert.strictEqual(res.reason, "close-command-expired");
    assert.strictEqual(res.ack?.payload.result, "failed");
    assert.strictEqual(res.ack?.payload.reason, "close-command-expired");
    assertNoSideEffects(res, calls);
  });

  it("a fresh close within the window proceeds", async () => {
    const { deps } = makeDeps({ nowMs: T0 + 5_000 });
    const res = await handleReclaim(params({ issuedAt: new Date(T0).toISOString() }), deps);
    assert.strictEqual(res.reason, "closed");
  });

  it("a FUTURE-dated issued_at beyond the window is rejected too (clock forgery)", async () => {
    const { deps, calls } = makeDeps({ nowMs: T0 });
    const res = await handleReclaim(
      params({ issuedAt: new Date(T0 + 120_000).toISOString(), ackTimeout: "30" }),
      deps,
    );
    assert.strictEqual(res.reason, "close-command-expired");
    assertNoSideEffects(res, calls);
  });

  it("malformed issued_at / run_id → rejected as expired (timing credential untrusted)", async () => {
    for (const p of [params({ issuedAt: "yesterday" }), params({ issuedAt: null })]) {
      const { deps, calls } = makeDeps({});
      const res = await handleReclaim(p, deps);
      assert.strictEqual(res.reason, "close-command-expired");
      assertNoSideEffects(res, calls);
    }
    const { deps, calls } = makeDeps({});
    const res = await handleReclaim(params({ runId: "not-hex" }), deps);
    assert.strictEqual(res.reason, "close-command-expired");
    assert.strictEqual(res.ack, null); // unattributable run → no ack file
    assertNoSideEffects(res, calls);
  });

  it("a forged huge ack_timeout is capped — still expired past the ceiling", async () => {
    const { deps, calls } = makeDeps({ nowMs: T0 + (ACK_TIMEOUT_CAP_S + 10) * 1000 });
    const res = await handleReclaim(
      params({ issuedAt: new Date(T0).toISOString(), ackTimeout: "999999" }),
      deps,
    );
    assert.strictEqual(res.reason, "close-command-expired");
    assertNoSideEffects(res, calls);
  });
});

describe("handleReclaim — dirty gate + close mechanics", () => {
  it("ANY dirty tab → refuse the WHOLE close + dirty ack (stricter than succession)", async () => {
    const { deps, calls } = makeDeps({ tabs: [tab("clean"), tab("wip", true)] });
    const res = await handleReclaim(params(), deps);
    assert.strictEqual(res.reason, "dirty");
    assert.strictEqual(res.ack?.payload.reason, "dirty");
    assert.strictEqual(calls.closed.length, 0, "a dirty window is never even partially closed");
  });

  it("close mechanics fail → close-failed with NO ack (producer's ack-timeout decides)", async () => {
    const { deps } = makeDeps({ closeOk: false });
    const res = await handleReclaim(params(), deps);
    assert.strictEqual(res.reason, "close-failed");
    assert.strictEqual(res.ack, null);
  });

  it("an empty window (no tabs) acks done with closed_count 0", async () => {
    const { deps } = makeDeps({ tabs: [] });
    const res = await handleReclaim(params(), deps);
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.ack?.payload.result, "done");
    assert.strictEqual(res.ack?.payload.closed_count, 0);
  });
});
