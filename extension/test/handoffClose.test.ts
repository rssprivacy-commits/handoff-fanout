import * as assert from "assert";
import {
  CloseDeps,
  SINGLEPANE_COMMANDS,
  SinglePaneDeps,
  StartupDeps,
  TabLike,
  handleHandoffClose,
  handleSinglePane,
  isClosePath,
  isHandoffWorktreeWorkspace,
  isSinglePanePath,
  isValidNonce,
  parseQuery,
  runStartupSinglePane,
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
  it("accepts only the canonical /autoclose path, rejects legacy /close and others", () => {
    assert.strictEqual(isClosePath("/autoclose"), true);
    assert.strictEqual(isClosePath("/close"), false); // legacy form dropped in D-2
    assert.strictEqual(isClosePath("/open"), false);
  });
  it("parses task_id (the canonical param) into params.task", () => {
    const p = parseQuery("task_id=t1&nonce=" + VALID_NONCE + "&project=erp");
    assert.strictEqual(p.task, "t1");
    assert.strictEqual(p.nonce, VALID_NONCE);
    assert.strictEqual(p.project, "erp");
  });
  it("ignores the legacy `task` param (no longer accepted)", () => {
    const p = parseQuery("task=legacy&nonce=" + VALID_NONCE);
    assert.strictEqual(p.task, null);
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

// ── single-pane (close side bars natively) ────────────────────────────────────

const HANDOFF_WS = "/Users/me/.../worktrees/t1/.handoff.code-workspace";

interface SPCalls {
  commands: string[];
  logs: string[];
}

function makeSinglePaneDeps(opts: {
  workspaceFile?: string | undefined;
  failOn?: string; // command name that throws
}): { deps: SinglePaneDeps; calls: SPCalls } {
  const calls: SPCalls = { commands: [], logs: [] };
  const deps: SinglePaneDeps = {
    workspaceFile: () => opts.workspaceFile,
    executeCommand: async (command) => {
      calls.commands.push(command);
      if (opts.failOn && command === opts.failOn) {
        throw new Error("simulated command failure");
      }
      return undefined;
    },
    log: (msg) => calls.logs.push(msg),
  };
  return { deps, calls };
}

describe("isSinglePanePath / isHandoffWorktreeWorkspace", () => {
  it("accepts only /singlepane", () => {
    assert.strictEqual(isSinglePanePath("/singlepane"), true);
    assert.strictEqual(isSinglePanePath("/autoclose"), false);
    assert.strictEqual(isSinglePanePath("/open"), false);
  });
  it("recognizes a .handoff.code-workspace file, rejects others/undefined", () => {
    assert.strictEqual(isHandoffWorktreeWorkspace(HANDOFF_WS), true);
    assert.strictEqual(isHandoffWorktreeWorkspace("/Users/me/proj/normal.code-workspace"), false);
    assert.strictEqual(isHandoffWorktreeWorkspace(undefined), false);
  });
});

describe("handleSinglePane", () => {
  it("success: runs closeSidebar then closeAuxiliaryBar in order on a handoff worktree window", async () => {
    const { deps, calls } = makeSinglePaneDeps({ workspaceFile: HANDOFF_WS });
    const res = await handleSinglePane({ task: "t1", nonce: null, project: null }, deps);
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.reason, "closed");
    assert.strictEqual(res.ran, 2);
    assert.deepStrictEqual(calls.commands, [...SINGLEPANE_COMMANDS]);
  });

  it("wrong-window: a normal (non-handoff) workspace is rejected, no commands run", async () => {
    const { deps, calls } = makeSinglePaneDeps({
      workspaceFile: "/Users/me/proj/normal.code-workspace",
    });
    const res = await handleSinglePane({ task: "t1", nonce: null, project: null }, deps);
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.reason, "wrong-window");
    assert.strictEqual(res.ran, 0);
    assert.strictEqual(calls.commands.length, 0);
  });

  it("wrong-window: no workspace file (single folder / empty) is rejected", async () => {
    const { deps, calls } = makeSinglePaneDeps({ workspaceFile: undefined });
    const res = await handleSinglePane({ task: "t1", nonce: null, project: null }, deps);
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.reason, "wrong-window");
    assert.strictEqual(calls.commands.length, 0);
  });

  it("missing-task: rejected before any command", async () => {
    const { deps, calls } = makeSinglePaneDeps({ workspaceFile: HANDOFF_WS });
    const res = await handleSinglePane({ task: null, nonce: null, project: null }, deps);
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.reason, "missing-task");
    assert.strictEqual(calls.commands.length, 0);
  });

  it("command-failed: a throwing executeCommand is reported, ran reflects progress", async () => {
    const { deps, calls } = makeSinglePaneDeps({
      workspaceFile: HANDOFF_WS,
      failOn: "workbench.action.closeAuxiliaryBar",
    });
    const res = await handleSinglePane({ task: "t1", nonce: null, project: null }, deps);
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.reason, "command-failed");
    assert.strictEqual(res.ran, 1); // closeSidebar ran, closeAuxiliaryBar threw
    assert.deepStrictEqual(calls.commands, [...SINGLEPANE_COMMANDS]);
  });
});

describe("runStartupSinglePane (onStartupFinished)", () => {
  function makeStartupDeps(opts: { workspaceFile?: string | undefined; failOn?: string }): {
    deps: StartupDeps;
    calls: SPCalls;
  } {
    const calls: SPCalls = { commands: [], logs: [] };
    const deps: StartupDeps = {
      workspaceFile: () => opts.workspaceFile,
      executeCommand: async (command) => {
        calls.commands.push(command);
        if (opts.failOn && command === opts.failOn) throw new Error("simulated startup command failure");
        return undefined;
      },
      log: (msg) => calls.logs.push(msg),
    };
    return { deps, calls };
  }

  it("handoff worktree window: closes both side bars on startup", async () => {
    const { deps, calls } = makeStartupDeps({ workspaceFile: HANDOFF_WS });
    const res = await runStartupSinglePane(deps);
    assert.strictEqual(res.ran, true);
    assert.strictEqual(res.reason, "closed");
    assert.deepStrictEqual(calls.commands, [...SINGLEPANE_COMMANDS]);
  });

  it("normal (non-handoff) window: does nothing on startup", async () => {
    const { deps, calls } = makeStartupDeps({ workspaceFile: "/Users/me/proj/normal.code-workspace" });
    const res = await runStartupSinglePane(deps);
    assert.strictEqual(res.ran, false);
    assert.strictEqual(res.reason, "not-handoff-worktree");
    assert.strictEqual(calls.commands.length, 0);
  });

  it("no workspace file (single folder / empty window): does nothing", async () => {
    const { deps, calls } = makeStartupDeps({ workspaceFile: undefined });
    const res = await runStartupSinglePane(deps);
    assert.strictEqual(res.ran, false);
    assert.strictEqual(res.reason, "not-handoff-worktree");
    assert.strictEqual(calls.commands.length, 0);
  });

  it("command failure on startup is reported, not thrown", async () => {
    const { deps } = makeStartupDeps({ workspaceFile: HANDOFF_WS, failOn: "workbench.action.closeSidebar" });
    const res = await runStartupSinglePane(deps);
    assert.strictEqual(res.ran, false);
    assert.strictEqual(res.reason, "command-failed");
  });
});
