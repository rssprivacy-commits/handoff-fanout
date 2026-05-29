import * as assert from "assert";
import {
  CloseDeps,
  TabLike,
  handleHandoffClose,
  isClosePath,
  isValidNonce,
  parseQuery,
} from "../src/handoffClose";

const VALID_NONCE = "0123456789abcdef"; // 16 hex chars, mirrors secrets.token_hex(8)

function tab(label: string, isDirty = false): TabLike {
  return { label, isDirty };
}

type CloseOutcome = boolean | "throw";

interface Calls {
  closeCount: number;
  closedTabs: TabLike[][];
  getAllTabsCount: number;
  delays: number[];
  logs: string[];
}

// Builds an injectable deps object with controllable getAllTabs snapshots and a
// closeTabs outcome sequence. `tabs` may be a single snapshot (reused for every
// getAllTabs call) or an array of snapshots (one per call; last reused once
// exhausted) to exercise re-fetch-on-retry behavior.
function makeDeps(opts: {
  tabs: TabLike[] | TabLike[][];
  closeResults: CloseOutcome[]; // one entry per closeTabs() call
}): { deps: CloseDeps; calls: Calls } {
  const calls: Calls = { closeCount: 0, closedTabs: [], getAllTabsCount: 0, delays: [], logs: [] };
  const snapshots: TabLike[][] = Array.isArray(opts.tabs[0])
    ? (opts.tabs as TabLike[][])
    : [opts.tabs as TabLike[]];
  const deps: CloseDeps = {
    getAllTabs: () => {
      const idx = Math.min(calls.getAllTabsCount, snapshots.length - 1);
      calls.getAllTabsCount += 1;
      return snapshots[idx];
    },
    closeTabs: async (tabs) => {
      const outcome = opts.closeResults[calls.closeCount] ?? true;
      calls.closeCount += 1;
      calls.closedTabs.push(tabs);
      if (outcome === "throw") {
        throw new Error("simulated close rejection");
      }
      return outcome;
    },
    delay: async (ms) => {
      calls.delays.push(ms);
    },
    log: (msg) => calls.logs.push(msg),
  };
  return { deps, calls };
}

describe("isValidNonce", () => {
  it("accepts a 16-char lowercase hex nonce", () => {
    assert.strictEqual(isValidNonce(VALID_NONCE), true);
  });
  it("rejects wrong length / uppercase / non-hex / null", () => {
    assert.strictEqual(isValidNonce("abc"), false);
    assert.strictEqual(isValidNonce("0123456789ABCDEF"), false);
    assert.strictEqual(isValidNonce("0123456789abcdeg"), false);
    assert.strictEqual(isValidNonce(null), false);
    assert.strictEqual(isValidNonce(undefined), false);
  });
});

describe("parseQuery / isClosePath (URI contract)", () => {
  it("accepts both /close and /autoclose paths, rejects others", () => {
    assert.strictEqual(isClosePath("/close"), true);
    assert.strictEqual(isClosePath("/autoclose"), true);
    assert.strictEqual(isClosePath("/open"), false);
  });
  it("parses task from `task` param", () => {
    const p = parseQuery("task=t1&nonce=" + VALID_NONCE + "&project=erp");
    assert.strictEqual(p.task, "t1");
    assert.strictEqual(p.nonce, VALID_NONCE);
    assert.strictEqual(p.project, "erp");
  });
  it("falls back to `task_id` alias when `task` absent", () => {
    const p = parseQuery("task_id=t2&nonce=" + VALID_NONCE);
    assert.strictEqual(p.task, "t2");
  });
  it("prefers `task` over `task_id` when both present", () => {
    const p = parseQuery("task=primary&task_id=alias");
    assert.strictEqual(p.task, "primary");
  });
});

describe("handleHandoffClose", () => {
  it("success: closes all non-dirty tabs on first try", async () => {
    const { deps, calls } = makeDeps({
      tabs: [tab("claude-old"), tab("readme.md")],
      closeResults: [true],
    });
    const res = await handleHandoffClose(
      { task: "t1", nonce: VALID_NONCE, project: "erp-system" },
      deps,
    );
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.reason, "closed");
    assert.strictEqual(res.closedCount, 2);
    assert.strictEqual(res.retried, false);
    assert.strictEqual(calls.closeCount, 1);
  });

  it("dirty skip: dirty tabs are excluded from the close set", async () => {
    const { deps, calls } = makeDeps({
      tabs: [tab("claude-old"), tab("unsaved.md", true)],
      closeResults: [true],
    });
    const res = await handleHandoffClose(
      { task: "t1", nonce: VALID_NONCE, project: "erp-system" },
      deps,
    );
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.closedCount, 1);
    assert.strictEqual(res.skippedDirty, 1);
    // Only the non-dirty tab was passed to closeTabs.
    assert.deepStrictEqual(
      calls.closedTabs[0].map((t) => t.label),
      ["claude-old"],
    );
  });

  it("retry: close()===false first, then succeeds on the single retry (after 500ms)", async () => {
    const { deps, calls } = makeDeps({
      tabs: [tab("claude-old")],
      closeResults: [false, true],
    });
    const res = await handleHandoffClose(
      { task: "t1", nonce: VALID_NONCE, project: "erp-system" },
      deps,
    );
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.retried, true);
    assert.strictEqual(res.closedCount, 1);
    assert.strictEqual(calls.closeCount, 2);
    assert.deepStrictEqual(calls.delays, [500]); // exactly one 500ms delay
  });

  it("retry on rejection: first close throws, retry resolves true", async () => {
    const { deps, calls } = makeDeps({
      tabs: [tab("claude-old")],
      closeResults: ["throw", true],
    });
    const res = await handleHandoffClose(
      { task: "t1", nonce: VALID_NONCE, project: "erp-system" },
      deps,
    );
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.retried, true);
    assert.strictEqual(calls.closeCount, 2);
  });

  it("re-filters on retry: tab turned dirty during delay -> not closed", async () => {
    // First getAllTabs: one clean tab. Retry getAllTabs: same tab now dirty.
    const { deps, calls } = makeDeps({
      tabs: [[tab("claude-old", false)], [tab("claude-old", true)]],
      closeResults: [false, true],
    });
    const res = await handleHandoffClose(
      { task: "t1", nonce: VALID_NONCE, project: "erp-system" },
      deps,
    );
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.reason, "nothing-to-close");
    assert.strictEqual(res.retried, true);
    assert.strictEqual(res.closedCount, 0);
    // Only the first (failed) close was attempted; no second close on a dirty tab.
    assert.strictEqual(calls.closeCount, 1);
  });

  it("retry exhausted: both attempts fail -> close-failed, no third try", async () => {
    const { deps, calls } = makeDeps({
      tabs: [tab("claude-old")],
      closeResults: [false, false],
    });
    const res = await handleHandoffClose(
      { task: "t1", nonce: VALID_NONCE, project: "erp-system" },
      deps,
    );
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.reason, "close-failed");
    assert.strictEqual(res.closedCount, 0);
    assert.strictEqual(calls.closeCount, 2);
  });

  it("invalid nonce: rejected before any close attempt", async () => {
    const { deps, calls } = makeDeps({ tabs: [tab("claude-old")], closeResults: [true] });
    const res = await handleHandoffClose(
      { task: "t1", nonce: "bad", project: "erp-system" },
      deps,
    );
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.reason, "invalid-nonce");
    assert.strictEqual(calls.closeCount, 0);
  });

  it("missing task: rejected as missing-params", async () => {
    const { deps, calls } = makeDeps({ tabs: [tab("claude-old")], closeResults: [true] });
    const res = await handleHandoffClose(
      { task: null, nonce: VALID_NONCE, project: "erp-system" },
      deps,
    );
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.reason, "missing-params");
    assert.strictEqual(calls.closeCount, 0);
  });

  it("nothing to close: all tabs dirty -> ok with nothing-to-close", async () => {
    const { deps, calls } = makeDeps({
      tabs: [tab("unsaved.md", true)],
      closeResults: [true],
    });
    const res = await handleHandoffClose(
      { task: "t1", nonce: VALID_NONCE, project: "erp-system" },
      deps,
    );
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.reason, "nothing-to-close");
    assert.strictEqual(res.skippedDirty, 1);
    assert.strictEqual(calls.closeCount, 0);
  });
});
