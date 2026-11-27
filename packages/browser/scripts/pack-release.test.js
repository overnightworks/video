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

function printedIntegrity(stdout) {
  return stdout.match(/^integrity=(.+)$/m)[1];
}

// openssl digests the tarball independently of the script's own hashing.
function independentIntegrity(tarball) {
  const digest = spawnSync("sh", ["-c", 'openssl dgst -sha512 -binary "$1" | base64 -w0', "sh", tarball], { encoding: "utf8" });
  return `sha512-${digest.stdout}`;
}

describe("pack-release.js", () => {
  it("prints the tarball's file name and integrity for a package that installs", async () => {
    const destination = await temporaryDirectory();
    const result = runScript(await fixturePackage(), [destination]);

    assert.equal(result.status, 0);
    assert.match(result.stdout, /^file=fixture-package-1\.2\.3\.tgz$/m);
    assert.match(result.stdout, /^integrity=sha512-[A-Za-z0-9+/]+=*$/m);
    assert.equal(printedIntegrity(result.stdout), independentIntegrity(path.join(destination, "fixture-package-1.2.3.tgz")));
  });

  it("fails without a destination and prints nothing", async () => {
    const result = runScript(await fixturePackage(), []);

    assert.notEqual(result.status, 0);
    assert.doesNotMatch(result.stdout, /^(file|integrity)=/m);
  });

  const refusedTarballs = {
    "a file beyond package.json and dist": {
      overrides: { files: ["dist", "stray.txt"] },
      extraFiles: ["stray.txt"],
    },
    "an export target that is missing": {
      overrides: { exports: { ".": { default: "./dist/missing.js" } } },
    },
  };

  for (const [behaviour, fixture] of Object.entries(refusedTarballs)) {
    it(`refuses a tarball with ${behaviour}`, async () => {
      const result = runScript(await fixturePackage(fixture), [await temporaryDirectory()]);

      assert.notEqual(result.status, 0);
      assert.doesNotMatch(result.stdout, /^integrity=/m);
    });
  }
});
