import * as vscode from "vscode";
import {
  CloseDeps,
  SinglePaneDeps,
  StartupDeps,
  TabLike,
  handleHandoffClose,
  handleSinglePane,
  isClosePath,
  isSinglePanePath,
  parseQuery,
  runStartupSinglePane,
} from "./handoffClose";

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

      // /autoclose — legacy stale-tab close (dormant: no current producer).
      if (isClosePath(uri.path)) {
        const deps: CloseDeps = {
          getAllTabs: () =>
            vscode.window.tabGroups.all.flatMap((g) => g.tabs) as unknown as TabLike[],
          closeTabs: (tabs) =>
            Promise.resolve(
              vscode.window.tabGroups.close(tabs as unknown as vscode.Tab[], true),
            ),
          delay: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
          log: (msg) => output.appendLine(msg),
        };
        try {
          await handleHandoffClose(params, deps);
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
