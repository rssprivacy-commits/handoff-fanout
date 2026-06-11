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
import { AckIntent, ReclaimDeps, handleReclaim } from "./handoffReclaim";

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

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Handoff Helper");

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
      // v4 C3/C7). worker+reclaim → the nonce-self-targeting reclaim close (expiry-
      // checked BEFORE any side effect; acks written for the producer's state machine);
      // succession+close_predecessor → delegated to the legacy handleAutoclose below;
      // any other combo → fail-closed + role-reason-rejected ack. A URI WITHOUT a
      // reason keeps the pre-§6c legacy route byte-identical.
      if (isClosePath(uri.path) && params.reason != null) {
        const deps: ReclaimDeps = {
          getAllTabs: () =>
            vscode.window.tabGroups.all.flatMap((g) => g.tabs) as unknown as TabLike[],
          closeTabs: (tabs) =>
            Promise.resolve(
              vscode.window.tabGroups.close(tabs as unknown as vscode.Tab[], true),
            ),
          delay: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
          log: (msg) => output.appendLine(msg),
          windowTitle: () => vscode.workspace.getConfiguration("window").get<string>("title"),
          now: () => Date.now(),
        };
        try {
          const res = await handleReclaim(params, deps);
          if (res.ack) {
            writeReclaimAck(res.ack, deps.log);
          }
          if (res.reason !== "delegate-legacy") {
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

  context.subscriptions.push(vscode.window.registerUriHandler(handler), output);
  output.appendLine("[handoff-helper] activated, URI handler registered");
}

export function deactivate(): void {
  /* no-op */
}
