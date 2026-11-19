# video

## Commands

Run each from the repository root.

- `uv sync --locked --all-groups`: installs the workspace. After a new package directory or module appears, add `--reinstall-package <distribution>`, or its editable install stays empty.
- `uv run --locked ruff check` and `uv run --locked ruff format --check`: lint and format.
- `uv run --locked pytest -q <named tests>`: targeted tests; CI adds `-n auto --cov`, and `--no-build` to every uv command, so only the packages `[tool.uv] no-binary-package` names are built from source.
- `uv run --locked --project providers/flashhead-lite pytest -q providers/flashhead-lite/tests`: the FlashHead lite driver (Python 3.10).
- `uv run --locked python scripts/check_release.py [X.Y.Z]`: every `pyproject.toml` and `packages/*/package.json` stands at one version, the given one if named.
- In `packages/browser`: `pnpm install --frozen-lockfile`, `pnpm run check` (types) and `pnpm run build` (emits `dist`, which the browser layer loads, so build first); `node scripts/pack-release.js <empty directory>` packs the tarball and proves it installs into an empty project.
- `uv run --locked playwright install chromium` once, then `probe-run uv run --locked pytest -q -m browser tests`: the browser layer in headless Chromium, one worker. The default run deselects the `browser` marker.
- With `VIDEO_BROWSER_COVERAGE_DIR` naming a directory, the browser layer writes Chromium's page coverage there; `node scripts/page-coverage-report.js <that directory>` in `packages/browser` turns it into `coverage/coverage-browser.lcov`.
- `quality check`: the static check. Run it after `git add` (it reads the index); `quality check --fix` records the debt baseline.

## Rules

- `packages/client` owns the wire's types and `docs/wire.md` its ruled behaviour; the service imports the types and copies none.
- A provider is reached only through the port in `providers/provider-contract`, in a child process that only `src/video_service/provider_process.py` starts; the service fetches, checks and decodes every image, a provider never does.
- `looks.py` is the only module that fetches a look's image, and only at start; nothing fetches during a session.
- The `fake` provider is a development dependency: no image installs it, and nothing under `src/` imports it at module level.
- The service offers no `/docs`, `/redoc`, `/openapi.json`, look administration, upload or recording.
- A new mandatory CI job is added by its display name to `mandatory_jobs` in `quality/config.toml`; the required check is `quality / verdict`.
- A new root entry is declared in `quality/config.toml` with a reason, or lives in its owner's directory.
- `sonar-project.properties` carries only the keys the quality gate allows.
- `packages/browser` receives no token and has no runtime dependency; the app's route relays the offer (`docs/wire.md`, WIRE-14).
- The browser layer's lcov is `coverage-browser.lcov`, with paths from the repository root; the CI `coverage` job merges it with `coverage-root.xml` for the gate. The two scripts in `packages/browser/scripts` are measured where CI runs them, as `pnpm exec mcr -c mcr.config.js node scripts/<script>.js …` with `MCR_LCOV_FILE` naming the lcov (`coverage-browser-pack.lcov`, `coverage-browser-report.lcov`), and no coverage exclusion hides them.
- Coverage reports are `coverage-<job>.xml`, written without `relative_files`: its `<source>` is the checkout's absolute path, which SonarCloud's module per uv workspace member resolves each file against; a relative `<source>` leaves the other packages' files unresolved.

## Owners

- `packages/client` owns the wire's types.
- `packages/browser` owns the browser side of a viewer, `watch`.
- `docs/wire.md` owns the wire's ruled behaviour.

## Documents

- `README.md`: read when you start or configure the service.
- `docs/VISION.md`: read when you judge whether a change still serves the service's purpose.
- `docs/ARCHITECTURE.md`: read when you change a building block, the trust model or delivery.
- `docs/wire.md`: read when you change or call the wire.
