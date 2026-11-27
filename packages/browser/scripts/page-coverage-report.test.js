// Runs page-coverage-report.js as a process against raw V8 coverage of a fixture build in temporary directories.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, describe, it } from "node:test";
import { fileURLToPath } from "node:url";

const SCRIPT = fileURLToPath(new URL("./page-coverage-report.js", import.meta.url));
const SOURCE = "export function answer(flag) {\n  if (flag) {\n    return 42;\n  }\n  return 0;\n}\n";
const FUNCTION_START = SOURCE.indexOf("function");
const BRANCH_START = SOURCE.indexOf("{", SOURCE.indexOf("if"));
const BRANCH_END = SOURCE.indexOf("  return 0");
const directories = [];

after(() => Promise.all(directories.map((directory) => rm(directory, { recursive: true, force: true }))));

async function temporaryDirectory() {
  const directory = await mkdtemp(path.join(tmpdir(), "page-coverage-report-test-"));
  directories.push(directory);
  return directory;
}

// A package whose build is its source line for line, so one map segment per line names it exactly.
async function fixturePackage() {
  const directory = await temporaryDirectory();
  await mkdir(path.join(directory, "src"));
  await mkdir(path.join(directory, "dist"));
  await writeFile(path.join(directory, "src", "watch.ts"), SOURCE);
  await writeFile(path.join(directory, "dist", "watch.js"), SOURCE);
  await writeFile(
    path.join(directory, "dist", "watch.js.map"),
    JSON.stringify({ version: 3, file: "watch.js", sources: ["../src/watch.ts"], names: [], mappings: "AAAA;AACA;AACA;AACA;AACA;AACA" }),
  );
  return directory;
}

// The raw coverage Chromium writes for dist/watch.js; `branchCount` is how often the if branch ran of the two calls the function got.
async function rawCoverage(branchCount) {
  const directory = await temporaryDirectory();
  const entry = {
    scriptId: "1",
    url: "http://localhost/dist/watch.js",
    functions: [
      { functionName: "", isBlockCoverage: true, ranges: [{ startOffset: 0, endOffset: SOURCE.length, count: 1 }] },
      {
        functionName: "answer",
        isBlockCoverage: true,
        ranges: [
          { startOffset: FUNCTION_START, endOffset: SOURCE.length - 1, count: 2 },
          { startOffset: BRANCH_START, endOffset: BRANCH_END, count: branchCount },
        ],
      },
    ],
  };
  await writeFile(path.join(directory, "page.json"), JSON.stringify({ result: [entry] }));
  return directory;
}

function runScript(packageDirectory, rawDirectory) {
  return spawnSync("node", [SCRIPT, rawDirectory], { cwd: packageDirectory, encoding: "utf8" });
}

const lcovOf = (packageDirectory) => path.join(packageDirectory, "coverage", "coverage-browser.lcov");

describe("page-coverage-report.js", () => {
  it("writes the lcov of a measured source that the page ran in full", async () => {
    const packageDirectory = await fixturePackage();
    const result = runScript(packageDirectory, await rawCoverage(1));

    assert.equal(result.status, 0);
    assert.ok(existsSync(lcovOf(packageDirectory)));
  });

  it("fails through the floor when a measured line did not run", async () => {
    const packageDirectory = await fixturePackage();
    const result = runScript(packageDirectory, await rawCoverage(0));

    assert.notEqual(result.status, 0);
    assert.ok(existsSync(lcovOf(packageDirectory)));
  });
});
