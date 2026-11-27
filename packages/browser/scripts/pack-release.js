// Packs the package into <destination> and proves the tarball installs into an empty project.
// Usage: node scripts/pack-release.js <destination>
// It prints the tarball's file name and its integrity as file=… and integrity=… lines.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const run = (command, args, options = {}) =>
  execFileSync(command, args, { encoding: "utf8", stdio: ["ignore", "pipe", "inherit"], ...options });
const targets = (exported) =>
  typeof exported === "string" ? [exported] : Object.values(exported).flatMap(targets);

// The work stands in a function because V8 reports the branches of a failing module body wrongly.
async function packRelease(destination) {
  if (!destination) throw new Error("usage: node scripts/pack-release.js <destination>");

  const { name, version } = JSON.parse(await readFile("package.json", "utf8"));
  run("pnpm", ["pack", "--pack-destination", destination]);

  const tarball = `${name}-${version}.tgz`;
  const tarballPath = path.join(destination, tarball);

  const stray = run("tar", ["-tzf", tarballPath])
    .split("\n")
    .filter((entry) => entry && entry !== "package/package.json" && !entry.startsWith("package/dist/"));
  if (stray.length > 0) throw new Error(`the tarball holds files beyond package.json and dist: ${stray.join(", ")}`);

  const host = await mkdtemp(path.join(tmpdir(), "pack-release-"));
  try {
    await writeFile(path.join(host, "package.json"), JSON.stringify({ name: "host", private: true }));
    run("pnpm", ["add", path.resolve(tarballPath)], { cwd: host });
    const installed = path.join(host, "node_modules", name);
    const { exports } = JSON.parse(await readFile(path.join(installed, "package.json"), "utf8"));
    for (const target of targets(exports)) {
      if (!existsSync(path.join(installed, target))) throw new Error(`export target ${target} is missing`);
    }
    // From the host, the name resolves the installed copy; from this directory it would resolve the source package.
    run("node", ["--input-type=module", "-e", `import ${JSON.stringify(name)}`], { cwd: host });
  } finally {
    await rm(host, { recursive: true, force: true });
  }

  const integrity = createHash("sha512").update(await readFile(tarballPath)).digest("base64");
  console.log(`file=${tarball}`);
  console.log(`integrity=sha512-${integrity}`);
}

await packRelease(process.argv[2]);
