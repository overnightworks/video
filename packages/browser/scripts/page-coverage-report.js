// Turns the raw V8 coverage the browser layer collected from Chromium into an lcov of the package's sources.
// Usage: node scripts/page-coverage-report.js <raw coverage directory>
// It writes coverage/coverage-browser.lcov, with paths from the repository root.
import { readFile, readdir, rm } from "node:fs/promises";
import path from "node:path";
import { CoverageReport } from "monocart-coverage-reports";
import { enforceFloor } from "../mcr.config.js";

const [rawDirectory] = process.argv.slice(2);
// The report empties its directory before it writes, so the directory is its own.
const REPORT_DIRECTORY = "coverage";
const LCOV_FILE = "coverage-browser.lcov";
// index.ts only re-exports, so it has no statements for the report to list.
const MEASURED = new Set(["src/watch.ts"]);
const BUILD_PATH = "/dist/";
const repositoryRoot = path.resolve("../..");
const report = new CoverageReport({
  name: "Browser layer",
  outputDir: REPORT_DIRECTORY,
  // The maps name sources relative to the package; the lcov names them from the repository root.
  reports: [["lcovonly", { file: LCOV_FILE, projectRoot: repositoryRoot }]],
  outputFilter: (file) => MEASURED.has(file.sourcePath),
  onEnd: enforceFloor(REPORT_DIRECTORY, LCOV_FILE),
});

// Chromium reports a script's coverage without its text, and the build's maps carry no sources, so both are read here.
async function withBuildAndMap(entry) {
  const built = path.join("dist", new URL(entry.url).pathname.slice(BUILD_PATH.length));
  const sourceMap = JSON.parse(await readFile(`${built}.map`, "utf8"));
  const sources = sourceMap.sources.map((source) => readFile(path.join(path.dirname(built), source), "utf8"));
  return {
    ...entry,
    source: await readFile(built, "utf8"),
    sourceMap: { ...sourceMap, sourcesContent: await Promise.all(sources) },
  };
}

// A report an earlier run left must not stand for this run when it fails.
await rm(path.join(REPORT_DIRECTORY, LCOV_FILE), { force: true });
const files = await readdir(rawDirectory);
// The report writes nothing and checks no floor for an empty data set, so an empty directory would pass unmeasured.
if (files.length === 0) {
  throw new Error(`${rawDirectory} holds no coverage: the browser layer collected none`);
}
const entriesByFile = await Promise.all(
  files.map(async (file) => {
    const { result } = JSON.parse(await readFile(path.join(rawDirectory, file), "utf8"));
    const built = result.filter((entry) => entry.url.startsWith("http") && new URL(entry.url).pathname.startsWith(BUILD_PATH));
    return Promise.all(built.map(withBuildAndMap));
  }),
);
await report.add(entriesByFile.flat());
await report.generate();
