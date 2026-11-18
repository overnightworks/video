# video

A service that gives any app a live, photoreal video face for its AI agents: the
app hands it an agent's spoken sentences as audio and a few facts, and each viewer
receives one live stream of picture and sound, with the model behind it chosen by
setting. The operator starts and configures it; an app developer reads the wire.

What runs today is the skeleton: one process that runs the provider
`VIDEO_PROVIDER` names (only `fake` exists), fetches each look's image once at
start, and answers `GET /health` with the provider's readiness, what it plays and
each look's readiness. Sessions, media and a real model are not built yet. The
service downloads no weights and has no image to deploy.

## Quick start

From the repository root, with [uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked --all-groups
VIDEO_PROVIDER=fake uv run video-service
curl http://127.0.0.1:8080/health
```

The service listens on `127.0.0.1:8080` and answers health at once; `ready` turns
true when the start is over. It stops on SIGINT or SIGTERM.

## Looks

A look is a name the apps know and the picture behind it. The operator names them
in a TOML file, `VIDEO_LOOKS_FILE`, one table per look; a name that holds a dot is
quoted:

```toml
[mozart]
url = "https://images.example/looks/mozart.png"
sha256 = "0f3a...64 lowercase hex characters"

["bach.v2"]
url = "https://images.example/looks/bach.png"
sha256 = "0f3a...64 lowercase hex characters"
```

A name matches `^[A-Za-z0-9._-]{1,64}$`. The look file is read at start: a file
that breaks this form stops the start naming `VIDEO_LOOKS_FILE`. With no look file
the service offers no look.

At start each look's image is fetched once from a URL under one of
`VIDEO_LOOK_SOURCES`, and kept only when its sha256 is the one the file names, it
is within `VIDEO_LOOK_MAX_BYTES` and within `VIDEO_LOOK_MAX_PIXELS` once its header
is read, and it decodes as PNG, JPEG or WebP. A look that fails any check, or that
the provider refuses, stays not ready in health and is logged with its `reason`;
the other looks serve. Nothing is fetched during a session.

## Settings

The process reads `VIDEO_*` from the environment; a missing or malformed value
stops the start naming the setting, never its value. [`docs/wire.md`](docs/wire.md)
holds the settings that shape the wire.

| Setting | Default | Meaning |
|---|---|---|
| `VIDEO_PROVIDER` | required | The provider this instance runs: `fake`. |
| `VIDEO_HOST` | `127.0.0.1` | The address to listen on. |
| `VIDEO_PORT` | `8080` | The port to listen on. |
| `VIDEO_LOOKS_FILE` | unset | The absolute path of the look file; unset offers no look. |
| `VIDEO_LOOK_SOURCES` | `[]` | A JSON list of the URL prefixes a look's image may come from. Each is http or https with a lowercase host, no user, query or fragment, and a path that starts and ends with `/` and holds only ASCII letters, digits and `-._~/`, with no empty, `.` or `..` segment. A redirect is never followed. |
| `VIDEO_LOOK_MAX_BYTES` | `8388608` | The most bytes an image may have. |
| `VIDEO_LOOK_MAX_PIXELS` | `16777216` | The most pixels (width times height) an image may have. |
| `VIDEO_MAX_SESSIONS` | `1` | The sessions the service admits, reported in health. |

The `fake` provider is a development dependency (`uv sync --all-groups` installs
it) that no image installs.

## Install the client

An app depends on the client distribution by the Git pin the release notes give,
for example:

```toml
dependencies = [
    "overnightworks-video-client @ git+https://github.com/overnightworks/video@vX.Y.Z#subdirectory=packages/client",
]
```

It holds the wire types and `fetch_health`. Its only dependency is pydantic.

## Release

Dispatch `.github/workflows/release.yml` with the version `X.Y.Z`, which every
`pyproject.toml` of the repository must stand at (`uv run python
scripts/check_release.py X.Y.Z` checks it). The release proves the client installs
by its Git pin at the gated commit and tags it.

## Documentation

- [`docs/VISION.md`](docs/VISION.md): what the service is for and what it is not.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): the building blocks, the trust model and the proofs.
- [`docs/wire.md`](docs/wire.md): the wire an app calls.
- [`AGENTS.md`](AGENTS.md): the commands and rules for an agent that changes the code.
