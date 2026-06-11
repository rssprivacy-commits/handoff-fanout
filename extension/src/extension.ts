import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import {
  AutocloseDeps,
  SinglePaneDeps,
  StartupDeps,
  TabLike,
  handleAutoclose,
  handleSinglePane,
  isClosePath,
  isSinglePanePath,
  parseQuery,
  runStartupSinglePane,
} from "./handoffClose";
import {
  AckIntent,
  ReclaimDeps,
  handleReclaim,
  isValidRunId,
  parseWorktreeWorkerIdentity,
  pollReclaimOnce,
} from "./handoffReclaim";

// §6c A-poll: how often each worker-worktree window polls its own reclaim_pending
// file. 7s sits inside the contract's 5–10s band and gives ~4 attempts inside the
// 30s default reclaim_ack_timeout (the freshness window the producer grants), so a
// non-throttled background window reliably self-closes before its deadline.
const RECLAIM_POLL_INTERVAL_MS = 7_000;

// §6c reclaim ack sink (contract C2/C6): the watchdog producer resolves its
// cross-tick pending state by reading `~/.claude-handoff/<project>/ack/
// <task>.reclaim_ack.json`. The DECISION logic stays pure (handoffReclaim.ts
// returns a fully computed AckIntent); this glue only lands the bytes —
// temp + rename so the producer can never read a torn ack. relPath components
// were slug-validated by the pure layer before the intent was ever produced.
function writeReclaimAck(intent: AckIntent, log: (msg: string) => void): void {
  try {
    const root = path.join(os.homedir(), ".claude-handoff");
    const target = path.join(root, intent.relPath);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    const tmp = `${target}.tmp-${process.pid}`;
    fs.writeFileSync(tmp, JSON.stringify(intent.payload) + "\n");
    fs.renameSync(tmp, target);
    log(`[handoff-helper] reclaim ack written: ${target} (${intent.payload.result})`);
  } catch (err) {
    log(`[handoff-helper] reclaim ack write failed: ${String(err)}`);
  }
}

// §6c A-poll (2026-06-12): start the per-window reclaim poller. If THIS window is a
// worker WORKTREE window (its title parses to a worker identity), it polls its OWN
// `~/.claude-handoff/<project>/ack/<task>.reclaim_pending.json` every interval; when
// the producer authorizes a reclaim (writes that pending), the window self-closes via
// the unchanged handleReclaim core and lands the ack the watchdog consumes. A
// `busy` guard makes overlapping ticks a no-op (a slow close never double-runs); the
// shared `handled` set (with the legacy URI leg) makes each run close exactly once.
// Returns a Disposable that clears the interval on deactivate. Non-worker/coordinator
// windows get an inert disposable — the poller never arms for them.
function startReclaimPoller(
  makeReclaimDeps: () => ReclaimDeps,
  reclaimHandled: Set<string>,
  log: (msg: string) => void,
): vscode.Disposable {
  const title = vscode.workspace.getConfiguration("window").get<string>("title");
  const identity = parseWorktreeWorkerIdentity(title);
  if (!identity) {
    log("[handoff-helper] reclaim poll: not a worker worktree window — poller inert");
    return { dispose() {} };
  }
  const pendingPath = path.join(
    os.homedir(),
    ".claude-handoff",
    identity.project,
    "ack",
    `${identity.task}.reclaim_pending.json`,
  );
  log(
    `[handoff-helper] reclaim poll: ${identity.project}/${identity.task} watching ` +
      `${pendingPath} every ${RECLAIM_POLL_INTERVAL_MS}ms`,
  );
  let busy = false;
  const tick = async (): Promise<void> => {
    if (busy) return; // a slow close must not overlap the next interval
    busy = true;
    try {
      await pollReclaimOnce({
        ...makeReclaimDeps(),
        handled: reclaimHandled,
        readPending: () => {
          let raw: string;
          try {
            raw = fs.readFileSync(pendingPath, "utf8");
          } catch {
            return null; // absent/unreadable → nothing to do this tick
          }
          try {
            return JSON.parse(raw);
          } catch {
            return null; // torn write mid-rename → next tick re-reads
          }
        },
        writeAck: (ack) => writeReclaimAck(ack, log),
      });
    } catch (err) {
      log(`[handoff-helper] reclaim poll error: ${String(err)}`);
    } finally {
      busy = false;
    }
  };
  const timer = setInterval(() => {
    void tick();
  }, RECLAIM_POLL_INTERVAL_MS);
  return {
    dispose() {
      clearInterval(timer);
    },
  };
}

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Handoff Helper");
  const log = (msg: string) => output.appendLine(msg);

  // §6c reclaim run_ids already acted on. SHARED between the poll path (primary, since
  // the A-poll revision) and the legacy URI path (now producerless — the engine polls
  // instead of pushing), so a run is closed + acked by EXACTLY one path. The poll
  // re-reads the lingering pending every interval until the producer consumes the ack,
  // so without this it would re-fire; and a stray URI for an already-handled run no-ops.
  const reclaimHandled = new Set<string>();
  const makeReclaimDeps = (): ReclaimDeps => ({
    getAllTabs: () =>
      vscode.window.tabGroups.all.flatMap((g) => g.tabs) as unknown as TabLike[],
    closeTabs: (tabs) =>
      Promise.resolve(vscode.window.tabGroups.close(tabs as unknown as vscode.Tab[], true)),
    delay: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    log,
    windowTitle: () => vscode.workspace.getConfiguration("window").get<string>("title"),
    now: () => Date.now(),
  });

  // SINGLE-PANE ON STARTUP (onStartupFinished): if THIS window is a handoff worktree, collapse its side bars as
  // soon as it finishes loading — so a cold-spawned window is single-pane on load, not after the submit. Guarded
  // to `.handoff.code-workspace`, so the owner's normal windows are never touched. (codex+gemini audit → owner's
  // choice 2026-06-06.) `void` because activate() must stay synchronous; the close runs as a microtask.
  const startupDeps: StartupDeps = {
    workspaceFile: () => vscode.workspace.workspaceFile?.fsPath,
    executeCommand: (command) => Promise.resolve(vscode.commands.executeCommand(command)),
    log: (msg) => output.appendLine(msg),
  };
  void runStartupSinglePane(startupDeps);

  const handler: vscode.UriHandler = {
    async handleUri(uri: vscode.Uri): Promise<void> {
      const params = parseQuery(uri.query);

      // /singlepane — collapse the side bars natively (cold-spawn single-pane).
      if (isSinglePanePath(uri.path)) {
        const deps: SinglePaneDeps = {
          workspaceFile: () => vscode.workspace.workspaceFile?.fsPath,
          executeCommand: (command) =>
            Promise.resolve(vscode.commands.executeCommand(command)),
          log: (msg) => output.appendLine(msg),
        };
        try {
          await handleSinglePane(params, deps);
        } catch (err) {
          output.appendLine(`[handoff-helper] singlepane internal error: ${String(err)}`);
        }
        return;
      }

      // /autoclose with a `reason` param — the §6c role×reason-matrixed path (contract
      // v4 C3/C7). worker+reclaim → the nonce-self-targeting reclaim close;
      // succession+close_predecessor → delegated to the legacy handleAutoclose below;
      // any other combo → fail-closed + role-reason-rejected ack. A URI WITHOUT a
      // reason keeps the pre-§6c legacy route byte-identical.
      //
      // A-poll note (2026-06-12): the engine no longer PUSHES this URI for reclaim (it
      // writes a pending the window polls instead), so this leg is now reached in
      // production only by the succession delegate (close_predecessor). It is kept for
      // that delegation + as a defence-in-depth receiver for any stray reclaim URI,
      // sharing `reclaimHandled` with the poll path so a run is never double-closed.
      if (isClosePath(uri.path) && params.reason != null) {
        if (isValidRunId(params.runId) && reclaimHandled.has(params.runId)) {
          log(`[handoff-helper] reclaim URI: run ${params.runId} already handled (poll) — ignoring`);
          return;
        }
        const deps = makeReclaimDeps();
        try {
          const res = await handleReclaim(params, deps);
          if (res.reason !== "delegate-legacy") {
            if (isValidRunId(params.runId)) reclaimHandled.add(params.runId);
            if (res.ack) writeReclaimAck(res.ack, deps.log);
            return;
          }
          // fall through: succession+close_predecessor rides the legacy §6 gates below.
        } catch (err) {
          output.appendLine(`[handoff-helper] reclaim internal error: ${String(err)}`);
          return;
        }
      }

      // /autoclose — role-gated supervisor-succession close (Phase 4). worker → never
      // close; supervisor_succession → close ONLY if THIS window's own window.title
      // carries predecessor_nonce (window-local self-targeting; fail-closed otherwise).
      if (isClosePath(uri.path)) {
        const deps: AutocloseDeps = {
          getAllTabs: () =>
            vscode.window.tabGroups.all.flatMap((g) => g.tabs) as unknown as TabLike[],
          closeTabs: (tabs) =>
            Promise.resolve(
              vscode.window.tabGroups.close(tabs as unknown as vscode.Tab[], true),
            ),
          delay: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
          log: (msg) => output.appendLine(msg),
          // This window's configured title (the .handoff.code-workspace sets it to
          // project·task·role·spawn_nonce…); getConfiguration returns the literal
          // template, so substring-matching predecessor_nonce against it works.
          windowTitle: () => vscode.workspace.getConfiguration("window").get<string>("title"),
        };
        try {
          await handleAutoclose(params, deps);
        } catch (err) {
          output.appendLine(`[handoff-helper] internal error: ${String(err)}`);
        }
        return;
      }

      output.appendLine(`[handoff-helper] ignoring unknown path: ${uri.path}`);
    },
  };

  // §6c A-poll: arm the reclaim poller for THIS window (inert unless it is a worker
  // worktree window). Disposed via subscriptions → its interval is cleared on
  // deactivate / window close.
  const reclaimPoller = startReclaimPoller(makeReclaimDeps, reclaimHandled, log);

  context.subscriptions.push(
    vscode.window.registerUriHandler(handler),
    reclaimPoller,
    output,
  );
  output.appendLine("[handoff-helper] activated, URI handler + reclaim poller registered");
}

export function deactivate(): void {
  /* no-op */
}
