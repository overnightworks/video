# video

A service that gives any app a live, photoreal video face for its AI agents: the
app hands it an agent's spoken sentences as audio and a few facts, and each viewer
receives one live stream of picture and sound, with the model behind it chosen by
setting. The operator starts and configures it; an app developer reads the wire.

What runs today: one process that runs the provider `VIDEO_PROVIDER` names (only
`fake` exists), fetches each look's image once at start, answers `GET /health` with
the provider's readiness, what it plays and each look's readiness, and serves
sessions over `WS /v1/sessions`: an app opens one on a look with two tokens, queues
sentences of audio, clears them, and reads when each starts and stops. The session's
opener adds viewers over `POST /v1/sessions/{id}/viewers` with a WebRTC offer and gets
one Opus and VP8 stream of the provider's picture and sound, with the idle picture
and silence between sentences. The browser package shows that stream in a page. A real
model is not built. The service downloads no weights and has no image to deploy.

## Quick start

From the repository root, with [uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked --all-groups
VIDEO_PROVIDER=fake VIDEO_CONFIG_FILE=/abs/path/callers.toml uv run video-service
curl http://127.0.0.1:8080/health
```

The service listens on `127.0.0.1:8080` and answers health at once; `ready` turns
true when the start is over. On SIGINT or SIGTERM it drains: it reports not ready,
refuses new sessions and viewers, lets each playing sentence finish and exits, at the latest
`VIDEO_DRAIN_SECONDS` after the signal, the last second of it spent cutting the
sessions left.

## Callers

A session needs two tokens ([`docs/wire.md`](docs/wire.md#callers)), and the
service starts only with `VIDEO_CONFIG_FILE`, an absolute path to a TOML file that
says whom it trusts. `[identity]` names the issuer and the one group the people
must belong to; each `[apps.<name>]` table registers an app by the client id of its
workload token, and its people belong to the group `/apps/<name>`. Unknown keys stop
the start, which names the field and never its value.

```toml
[identity]
issuer = "https://identity.example/realms/apps"
jwks_url = "https://identity.example/realms/apps/protocol/openid-connect/certs"  # https only
audience = "video-service"
workload_role = "video-workload"
group_claim = "groups"
service_group = "/services/video-service"

[apps.hopin]
workload_client = "hopin-workload"
```

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
| `VIDEO_CONFIG_FILE` | required | The absolute path of the caller file ([Callers](#callers)). |
| `VIDEO_MAX_SESSIONS` | `1` | The sessions the service admits, reported in health. |
| `VIDEO_MAX_VIEWERS` | `5` | The most viewers a session admits. |
| `VIDEO_MAX_QUEUED_AUDIO_SECONDS` | `30` | The most audio a session may queue. |
| `VIDEO_MAX_LAG_MS` | `500` | The most a frame may be late, and the longest a provider call may take, before the session ends. |
| `VIDEO_DRAIN_SECONDS` | `30` | The longest from the stop to the exit, the cut of the sessions left included. |

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

It holds the wire types (health, refusals, the session's events and the viewer
paths) and `fetch_health`. Its only dependency is pydantic.

## Install the browser package

A page shows a session with `overnightworks-video-browser`, which has no runtime
dependency. The release notes give its tarball URL and integrity; add it to the app's
front end as pnpm writes it into the lockfile:

```sh
pnpm add https://github.com/overnightworks/video/releases/download/vX.Y.Z/overnightworks-video-browser-X.Y.Z.tgz
```

```ts
import { watch } from "overnightworks-video-browser";

const watching = watch(host, "/video/viewers", iceServers, (state) => {
  // "loading", then "drawn" once a frame is painted; "ended" for good on any failure
});
watching.stop(); // ends the viewer and deletes it
```

`watch` posts the browser's offer to the app's own route `path`, so the app relays it
to the service with the session's tokens and the browser never holds one. The route's
contract is [WIRE-14](docs/wire.md#the-apps-route). A page needs the user to have
interacted with it before `watch` plays sound; otherwise the browser refuses the
playback and the state is `ended`.

## Release

Dispatch `.github/workflows/release.yml` with the version `X.Y.Z`, which every
`pyproject.toml` and `packages/*/package.json` of the repository must stand at
(`uv run python scripts/check_release.py X.Y.Z` checks it). The release proves the
client installs by its Git pin at the gated commit, packs the browser package as a
tarball release file and proves it installs, and tags the commit.

## Documentation

- [`docs/VISION.md`](docs/VISION.md): what the service is for and what it is not.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): the building blocks, the trust model and the proofs.
- [`docs/wire.md`](docs/wire.md): the wire an app calls.
- [`AGENTS.md`](AGENTS.md): the commands and rules for an agent that changes the code.
