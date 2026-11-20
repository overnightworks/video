# video

## Commands

Run each from the repository root.

- `uv sync --locked --all-groups`: installs the workspace. After a new package directory or module appears, add `--reinstall-package <distribution>`, or its editable install stays empty.
- `uv run --locked ruff check` and `uv run --locked ruff format --check`: lint and format.
- `uv run --locked pytest -q <named tests>`: targeted tests; CI adds `-n auto --cov`, and `--no-build` to every uv command, so only the packages `[tool.uv] no-binary-package` names are built from source.
- `git submodule update --init`, then `uv sync --locked --all-groups --project providers/flashhead-lite` and `CUDA_VISIBLE_DEVICES= uv run --locked --project providers/flashhead-lite pytest -q providers/flashhead-lite/tests`: the FlashHead lite provider (Python 3.14); then `CUDA_VISIBLE_DEVICES= uv run --locked pytest -q -m flashhead_lite tests` starts its real worker through the service.
- `uv run --locked python scripts/check_release.py [X.Y.Z]`: every `pyproject.toml` and `packages/*/package.json` stands at one version, the given one if named.
- In `packages/browser`: `pnpm install --frozen-lockfile`, `pnpm run check` (types) and `pnpm run build` (emits `dist`, which the browser layer loads, so build first); `node scripts/pack-release.js <empty directory>` packs the tarball and installs it into an empty project.
- `uv run --locked playwright install chromium` once, then `probe-run uv run --locked pytest -q -m browser tests`: the browser layer in headless Chromium, one worker.
- With `VIDEO_BROWSER_COVERAGE_DIR` naming a directory, the browser layer writes Chromium's page coverage there; `node scripts/page-coverage-report.js <that directory>` in `packages/browser` turns it into `coverage/coverage-browser.lcov`.
- `quality check`: the static check. Run it after `git add` (it reads the index); `quality check --fix` records the debt baseline.

## Rules

- The service imports the wire's types from `packages/client` and copies none.
- A provider is reached only through the port in `providers/provider-contract`, in a child process that only `src/video_service/provider_process.py` starts; the service fetches, checks and decodes every image, a provider never does.
- `looks.py` is the only module that fetches a look's image, and only at start; nothing fetches during a session.
- The `fake` provider is a development dependency: no image installs it, and nothing under `src/` imports it at module level.
- The submodule `providers/flashhead-lite/soulx-flashhead` stays unchanged; every adaptation lives in the provider's own modules.
- The service offers no `/docs`, `/redoc`, `/openapi.json`, look administration, upload or recording.
- A new mandatory CI job is added by its display name to `mandatory_jobs` in `quality/config.toml`; the required check is `quality / verdict`.
- A new root entry is declared in `quality/config.toml` with a reason, or lives in its owner's directory.
- `sonar-project.properties` carries only the keys the quality gate allows.
- The browser layer's lcov is `coverage-browser.lcov`, with paths from the repository root; the CI `coverage` job merges it with `coverage-root.xml`. CI measures the two scripts in `packages/browser/scripts` as `pnpm exec mcr -c mcr.config.js node scripts/<script>.js …` with `MCR_LCOV_FILE` naming `coverage-browser-pack.lcov` or `coverage-browser-report.lcov`; no coverage exclusion hides them.
- Coverage reports are `coverage-<job>.xml`, written without `relative_files`: SonarCloud's module per uv workspace member resolves each file against the absolute `<source>`.

## Owners

- `packages/browser` owns the browser side of a viewer, `watch`.
- `docs/wire.md` owns the wire's ruled behaviour.

## Documents

- `README.md`: read when you start or configure the service.
- `docs/VISION.md`: read when you judge whether a change still serves the service's purpose.
- `docs/ARCHITECTURE.md`: read when you change a building block, the trust model or delivery.
- `docs/wire.md`: read when you change or call the wire.
