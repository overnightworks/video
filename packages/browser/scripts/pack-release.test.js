// Runs pack-release.js as a process against fixture packages built in temporary directories.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, describe, it } from "node:test";
import { fileURLToPath } from "node:url";

const SCRIPT = fileURLToPath(new URL("./pack-release.js", import.meta.url));
const directories = [];

after(() => Promise.all(directories.map((directory) => rm(directory, { recursive: true, force: true }))));

async function temporaryDirectory() {
  const directory = await mkdtemp(path.join(tmpdir(), "pack-release-test-"));
  directories.push(directory);
  return directory;
}

// A package that exports dist/index.js; `overrides` change its manifest and `extraFiles` are written next to it.
async function fixturePackage({ overrides = {}, extraFiles = [] } = {}) {
  const directory = await temporaryDirectory();
  const manifest = {
    name: "fixture-package",
    version: "1.2.3",
    type: "module",
    exports: { ".": { default: "./dist/index.js" } },
    files: ["dist"],
    ...overrides,
  };
  await writeFile(path.join(directory, "package.json"), JSON.stringify(manifest));
  await mkdir(path.join(directory, "dist"));
  await writeFile(path.join(directory, "dist", "index.js"), "export const answer = 42;\n");
  await Promise.all(extraFiles.map((file) => writeFile(path.join(directory, file), "stray\n")));
  return directory;
}

function runScript(packageDirectory, args) {
  return spawnSync("node", [SCRIPT, ...args], { cwd: packageDirectory, encoding: "utf8" });
}

describe("pack-release.js", () => {
  it("prints the tarball's file name and integrity for a package that installs", async () => {
    const destination = await temporaryDirectory();
    const result = runScript(await fixturePackage(), [destination]);

    assert.equal(result.status, 0);
    assert.match(result.stdout, /^file=fixture-package-1\.2\.3\.tgz$/m);
    assert.match(result.stdout, /^integrity=sha512-[A-Za-z0-9+/]+=*$/m);
  });

  it("fails without a destination and prints nothing", async () => {
    const result = runScript(await fixturePackage(), []);

    assert.notEqual(result.status, 0);
    assert.doesNotMatch(result.stdout, /^(file|integrity)=/m);
  });

  it("refuses a tarball that holds a file beyond package.json and dist", async () => {
    const packageDirectory = await fixturePackage({
      overrides: { files: ["dist", "stray.txt"] },
      extraFiles: ["stray.txt"],
    });
    const result = runScript(packageDirectory, [await temporaryDirectory()]);

    assert.notEqual(result.status, 0);
    assert.doesNotMatch(result.stdout, /^integrity=/m);
  });

  it("refuses a tarball whose export target is missing", async () => {
    const packageDirectory = await fixturePackage({
      overrides: { exports: { ".": { default: "./dist/missing.js" } } },
    });
    const result = runScript(packageDirectory, [await temporaryDirectory()]);

    assert.notEqual(result.status, 0);
    assert.doesNotMatch(result.stdout, /^integrity=/m);
  });
});
