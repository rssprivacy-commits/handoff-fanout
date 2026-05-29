import * as fs from "fs";
import * as path from "path";
import Mocha from "mocha";

// Runs inside the VS Code extension host. Discovers compiled *.test.js under
// this suite dir and runs them with mocha.
export function run(): Promise<void> {
  const mocha = new Mocha({ ui: "bdd", color: true, timeout: 20000 });
  const suiteDir = __dirname;
  for (const file of fs.readdirSync(suiteDir)) {
    if (file.endsWith(".test.js")) {
      mocha.addFile(path.join(suiteDir, file));
    }
  }
  return new Promise((resolve, reject) => {
    try {
      mocha.run((failures) => {
        if (failures > 0) {
          reject(new Error(`${failures} integration test(s) failed.`));
        } else {
          resolve();
        }
      });
    } catch (err) {
      reject(err as Error);
    }
  });
}
