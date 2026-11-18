// Measures a script of this package when it runs under `mcr` (see the scripts in the CI jobs).
// MCR_LCOV_FILE names the lcov, which carries paths from the repository root.
export default {
  name: "Browser package scripts",
  outputDir: "coverage-scripts",
  reports: [["lcovonly", { file: process.env.MCR_LCOV_FILE, projectRoot: "../.." }]],
  entryFilter: { "**/scripts/**": true },
  sourceFilter: { "**/scripts/**": true },
};
