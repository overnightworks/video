// Measures a script of this package when it runs under `mcr` (see the scripts in the CI jobs).
// MCR_LCOV_FILE names the lcov, which carries paths from the repository root.
import { readFileSync } from "node:fs";
import path from "node:path";

// The lowest percentage of lines and of branches a report may reach, rounded down from what CI measured.
const FLOORS = {
  "coverage-browser-pack.lcov": { lines: 96.87, branches: 75 },
  "coverage-browser-report.lcov": { lines: 92.15, branches: 58.33 },
  "coverage-browser.lcov": { lines: 100, branches: 100 },
};
const LCOV_COUNTS = { lines: ["LF", "LH"], branches: ["BRF", "BRH"] };

function sumOf(lcov, key) {
  return [...lcov.matchAll(new RegExp(String.raw`^${key}:(\d+)$`, "gm"))].reduce((sum, match) => sum + Number(match[1]), 0);
}

// Fails the job when the written lcov stands below its floor; an empty lcov fails too, as the lcov is what the gate reads.
export function enforceFloor(outputDirectory, lcovFile) {
  return () => {
    const lcov = readFileSync(path.join(outputDirectory, lcovFile), "utf8");
    const failures = Object.entries(FLOORS[lcovFile]).flatMap(([metric, floor]) => {
      const [totalKey, coveredKey] = LCOV_COUNTS[metric];
      const percentage = (100 * sumOf(lcov, coveredKey)) / sumOf(lcov, totalKey);
      return percentage >= floor ? [] : [`${metric} ${percentage.toFixed(2)}% below ${floor}%`];
    });
    if (failures.length > 0) {
      throw new Error(`${lcovFile}: ${failures.join(", ")}`);
    }
  };
}

const OUTPUT_DIRECTORY = "coverage-scripts";
export default {
  name: "Browser package scripts",
  outputDir: OUTPUT_DIRECTORY,
  reports: [["lcovonly", { file: process.env.MCR_LCOV_FILE, projectRoot: "../.." }]],
  // The config is measured where a script imports it.
  entryFilter: { "**/scripts/**": true, "**/mcr.config.js": true },
  sourceFilter: { "**/scripts/**": true, "**/mcr.config.js": true },
  onEnd: enforceFloor(OUTPUT_DIRECTORY, process.env.MCR_LCOV_FILE),
};
