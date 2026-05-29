import * as vscode from "vscode";
import { CloseDeps, TabLike, handleHandoffClose, isClosePath, parseQuery } from "./handoffClose";

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Handoff Helper");

  const handler: vscode.UriHandler = {
    async handleUri(uri: vscode.Uri): Promise<void> {
      if (!isClosePath(uri.path)) {
        output.appendLine(`[handoff-helper] ignoring unknown path: ${uri.path}`);
        return;
      }

      const params = parseQuery(uri.query);
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
    },
  };

  context.subscriptions.push(vscode.window.registerUriHandler(handler), output);
  output.appendLine("[handoff-helper] activated, URI handler registered");
}

export function deactivate(): void {
  /* no-op */
}
