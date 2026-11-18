# video

## Commands

Run each from the repository root.

- `uv sync --locked --all-groups`: installs the workspace. After a new package directory or module appears, add `--reinstall-package <distribution>`, or its editable install stays empty.
- `uv run --locked ruff check` and `uv run --locked ruff format --check`: lint and format.
- `uv run --locked pytest -q <named tests>`: targeted tests; CI adds `-n auto --cov`.
- `uv run --locked python scripts/check_release.py [X.Y.Z]`: every `pyproject.toml` stands at one version, the given one if named.
- `quality check`: the shared static check. Run it after `git add` (it reads the index); `quality check --fix` records the debt baseline.

## Rules

- `packages/client` owns the wire's types and `docs/wire.md` its ruled behaviour; the service imports the types and copies none.
- A provider is reached only through the port in `providers/provider-contract`; the service fetches, checks and decodes every image, a provider never does.
- `looks.py` is the only module that fetches a look's image, and only at start; nothing fetches during a session.
- The `fake` provider is a development dependency: no image installs it, and nothing under `src/` imports it at module level.
- The service offers no `/docs`, `/redoc`, `/openapi.json`, look administration, upload or recording.
- A new mandatory CI job is added by its display name to `mandatory_jobs` in `quality/config.toml`; the required check is `quality / verdict`.
- A new root entry is declared in `quality/config.toml` with a reason, or lives in its owner's directory.
- `sonar-project.properties` carries only the keys the quality gate allows.
- Coverage reports are `coverage-<job>.xml` with paths relative to the repository root, as `[tool.coverage.run]` sets.

## Owners

- `packages/client` owns the wire's types.
- `docs/wire.md` owns the wire's ruled behaviour.

## Documents

- `README.md`: read when you start or configure the service.
- `docs/VISION.md`: read when you judge whether a change still serves the service's purpose.
- `docs/ARCHITECTURE.md`: read when you change a building block, the trust model or delivery.
- `docs/wire.md`: read when you change or call the wire.
