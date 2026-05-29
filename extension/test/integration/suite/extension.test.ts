import * as assert from "assert";
import * as vscode from "vscode";

// Integration smoke test (extension host). Verifies the extension is present,
// activates without throwing, and contributes the onUri activation event so the
// handoff close URI can reach it. Real tab-closing is exercised manually during
// the master's D-5 acceptance (live VS Code), not here.
describe("handoff-helper extension (integration)", () => {
  it("is installed and discoverable", () => {
    const ext = vscode.extensions.getExtension("dharmaxis.handoff-helper");
    assert.ok(ext, "extension dharmaxis.handoff-helper not found");
  });

  it("activates without error", async () => {
    const ext = vscode.extensions.getExtension("dharmaxis.handoff-helper");
    assert.ok(ext);
    await ext!.activate();
    assert.strictEqual(ext!.isActive, true);
  });

  it("declares the onUri activation event", () => {
    const ext = vscode.extensions.getExtension("dharmaxis.handoff-helper");
    const events: string[] = ext!.packageJSON.activationEvents ?? [];
    assert.ok(events.includes("onUri"), "expected onUri activation event");
  });
});
