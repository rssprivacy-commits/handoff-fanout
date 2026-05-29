import * as path from "path";
import { runTests } from "@vscode/test-electron";

// Gated integration entry point (NOT part of `npm test`). Downloads a VS Code
// build and launches the extension host. Run manually with `npm run
// test:integration`. Kept out of the default test run so the unit suite stays
// fast and headless-safe (see §第一步.5 long-running-CLI caveat).
async function main(): Promise<void> {
  try {
    const extensionDevelopmentPath = path.resolve(__dirname, "../../../");
    const extensionTestsPath = path.resolve(__dirname, "./suite/index");
    await runTests({ extensionDevelopmentPath, extensionTestsPath });
  } catch (err) {
    console.error("integration tests failed:", err);
    process.exit(1);
  }
}

void main();
