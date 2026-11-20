# video

A service that gives any app a live, photoreal video face for its AI agents: the
app hands it an agent's spoken sentences as audio and a few facts, and each viewer
receives one live stream of picture and sound, with the model behind it chosen by
setting. The operator starts and configures it; an app developer reads the wire.

What runs today: one process that runs the provider `VIDEO_PROVIDER` names (`fake`, and `flashhead-lite`, which needs a card) in a child process of its own, fetches each look's image once at start, answers `GET /health` with
the provider's readiness, what it plays and each look's readiness, and serves
sessions over `WS /v1/sessions`: an app opens one on a look with two tokens, queues
sentences of audio, clears them, and reads when each starts and stops. The session's
opener adds viewers over `POST /v1/sessions/{id}/viewers` with a WebRTC offer and gets
one Opus and VP8 stream of the provider's picture and sound, with the idle picture
and silence between sentences. The browser package shows that stream in a page. No image exists to deploy, and the service downloads no weights.

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
| `VIDEO_PROVIDER` | required | The provider this instance runs: `fake` or `flashhead-lite`. |
| `VIDEO_PROVIDER_ROOT` | unset | The absolute path of the directory that holds each provider's own environment. The provider runs as `<root>/<provider>/.venv/bin/video-<provider>-worker`; unset, it runs as `video-<provider>-worker` beside the service's interpreter. No other location is searched, and a worker that is missing or not executable makes the provider fail to start. |
| `VIDEO_HOST` | `127.0.0.1` | The address to listen on. |
| `VIDEO_PORT` | `8080` | The port to listen on. |
| `VIDEO_CARD_MEMORY_MB` | unset | The memory, in mebibytes, the provider may use on the card: an integer of at least 1, required for `flashhead-lite`, optional for `fake`. It binds torch's allocator, not the card's context; whether the card holds it is the worker's judgement at start, and a cap too large leaves the provider failed. The child receives this value and no other. |
| `VIDEO_LOOKS_FILE` | unset | The absolute path of the look file; unset offers no look. |
| `VIDEO_LOOK_SOURCES` | `[]` | A JSON list of the URL prefixes a look's image may come from. Each is http or https with a lowercase host, no user, query or fragment, and a path that starts and ends with `/` and holds only ASCII letters, digits and `-._~/`, with no empty, `.` or `..` segment. A redirect is never followed. |
| `VIDEO_LOOK_MAX_BYTES` | `8388608` | The most bytes an image may have. |
| `VIDEO_LOOK_MAX_PIXELS` | `16777216` | The most pixels (width times height) an image may have. |
| `VIDEO_CONFIG_FILE` | required | The absolute path of the caller file ([Callers](#callers)). |
| `VIDEO_MAX_SESSIONS` | `1` | The sessions the service admits, reported in health. |
| `VIDEO_MAX_VIEWERS` | `5` | The most viewers a session admits. |
| `VIDEO_MAX_QUEUED_AUDIO_SECONDS` | `30` | The most audio a session may queue. |
| `VIDEO_MAX_LAG_MS` | `500` | The most a frame may be late, and the longest a provider call may take, before the session ends. It also bounds how far the player draws ahead of the frames, so each session's frame memory. A provider that renders in chunks needs at least two renders plus one frame period. |
| `VIDEO_DRAIN_SECONDS` | `30` | The longest from the stop to the exit, the cut of the sessions left included. |

The `fake` provider is a development dependency (`uv sync --all-groups` installs
it, and its `video-fake-worker` runs beside the interpreter) that no image installs.

To run `flashhead-lite` from source, check out the submodule, create the provider's own environment
(`git submodule update --init`, then `uv sync --locked --all-groups --project providers/flashhead-lite`),
fetch the weights with `uv run --locked --project providers/flashhead-lite video-flashhead-lite-fetch-weights`,
and set `VIDEO_PROVIDER_ROOT` to the repository's `providers` directory and `VIDEO_CARD_MEMORY_MB`.
The fetch downloads about 8.2 GB into `providers/flashhead-lite/weights`: the FlashHead lite
transformer and VAE, and the wav2vec2 audio encoder, each from its Hugging Face repository at a
pinned commit and kept only when its sha256 matches the pin. A file already there with its digest
is not downloaded again, and a failure ends the command non-zero naming the file. The licence
notices of the components (FlashHead's model card, LTX-Video's licence text for the VAE and
wav2vec2's model card) land in `weights/notices`; keep them with the files if you pass them on.
On a host without a card the worker refuses: the provider is
failed, health shows it not ready and the service keeps running.

A provider runs as a child process of the service, with its own interpreter and
packages, so a model may pin a Python and libraries the service does not. A child
that dies, hangs past a bound or breaks the private frame protocol makes the provider
failed until the service restarts: health shows `provider.ready` false, every look
not ready, and every open session ends `unavailable`. A killed service closes the
child's pipe, and the child ends.

## Proofs

`uv run --locked python scripts/prove_card.py --dry-run --audio speech.wav --record record.json`
is the card proof's drive. It starts the service from the checkout with the `fake` provider,
under a throwaway issuer and look server it serves over HTTPS beneath a certificate authority
of its own, and drives sessions through the wire as an app does: sentences with idle between
them, a clear inside a sentence, one session on each of two looks, a lag far too small, and
viewers, which are receive-only aiortc peers attached through the viewer route with the
session's tokens. `--audio` is a mono 16-bit WAV of at least a second, at a rate the wire
carries; the command checks it, and that `--record` names a file in an existing writable
directory, before any service starts, and ends with one line when either fails. `--cells`
names the cells to run (`sentences`, `clear`, `looks`, `viewer`, `viewers`, `lag`); each
group of them runs as one service under `probe-run`, which the command calls itself. The
service starts under `setpriv --pdeathsig KILL` (the host needs `setpriv`, as `probe-run`
does), so a kill of the group's process ends it.
The command waits for the service's readiness as long as the service's own bounds for
the provider's start and each look's preparation allow, plus a slack. Every exit stops
the service first, which reaps its provider worker. A service that has not exited within
the stop bound is killed, the groups of the workers that are its children at that moment
first and its own group after, so the service and its workers are gone within the stop
bound and a margin of the interrupt, also for a service that cannot answer its stop; the
command itself ends after it has unwound its cells. The command marks itself a child
subreaper, so an orphan of a group's process stays below it, and once a group's `probe-run`
has ended, however the group ended, it sends SIGKILL to every process still below it (each
checked through a pidfd by its own and its parents' ids and start times, passes until none
is alive or `KILL_WITHIN_SECONDS` has passed). The service's entry in the record holds
`processes_left_after_stop`, how many it found (0 when the group stopped its service
cleanly), and `processes_alive_after_kill`, how many were still alive past the bound; a kill
that raised, or met a process it could not read, makes both `not_measured` with the reason
`kill_failed`, and the command writes the record, starts no later group and exits non-zero.
A service that had exited before its stop has `stop_to_exit_ms` `not_measured` with
`exited_before_stop`. An interrupt (INT or TERM) ends the command with one line, after any
kill that is running has ended. Each group's service log is written beside the record from
the service's start, so it is there however the group ends (cells done, a failed cell, an
interrupt, a kill), and the command prints its path. A kill of the command itself is not
trapped: `probe-run`'s parent-death signal stops the group's process, which stops its
service, and the cap ends what stays. It writes no record and leaves the throwaway
identity's keys in its `prove-card-record-*` directory under `TMPDIR`; any other end of the
command removes them. A service that dies by a signal ends its worker, which reads its
closed input on a thread of its own, also while the provider loads. That does not cover the
time before the worker's `run_worker` runs (FlashHead lite imports torch first), a load step
that holds the interpreter lock, a worker not built on `run_worker`, nor the compiler's
helper processes, which no input thread ends.

The record is written however a group ends, from the fragment each group's process
rewrites after every cell and once more when the group ends (whole or absent, never half
written). A cell that raises ends its group: it is `not_measured` with the reason
`cell_failed`, each later cell of the group `group_ended`, and the command exits non-zero
after it writes the record. A failed group ends the command: each cell of a group not
started is `earlier_group_failed`. A group whose process ended without a fragment, or with
one it did not end (the cap, a kill), keeps the cells its fragment holds, and every cell
and every figure of its service that the fragment lacks is `no_fragment`. An interrupt
(INT or TERM) writes no record.

The record is JSON. Its figures come from the service's own log lines, the session's events
and what the viewers receive: per sentence the first output, the frames against the audio's
length and the late frames; the time from a clear to its confirmation and to the last loud
audio frame a viewer receives; per viewer (one, and each of five) the frames per second at
the peer in a sentence and between sentences, the loud audio frames between sentences, the
gaps in the video timestamps and the frame size the provider drew; the service's CPU, read
from its own process's accounting, at no, one and five viewers, and the encoders' share at
five; each session's opening time; each service's start, the provider's load and each look's
preparation as the service logged them (outcome, the reason when one is logged,
duration), and its stop's time to exit; and that a lag far too small ends the session as
`unavailable`. Beside the record the command writes one viewer's audio and video as `<record>.viewer.mp4`, contact sheets of it as
`<record>.viewer-sheet-<n>.png` for the judge of the picture, and each group's service log as
`<record>.<group>.log`; the record names them and the command prints their paths. A dry run
shows that the drive works, and its figures are the fake's (a 256x256 picture on whatever
host runs it), so a figure judged against a line of the program plan (first output, clear to
silence, start, frames per second, the encoders' two cores) holds its value and the verdict
`not_measured` with the reason. It cannot show a card's timing or memory, nor the picture's
likeness to the idle look: the video is judged by eye. Against `fake`, a quiet audio file
gives an all-black recording.

Without `--dry-run` the command drives `flashhead-lite` with the operator's
`VIDEO_PROVIDER_ROOT` and `VIDEO_CARD_MEMORY_MB`, passed to the service unchanged and
checked first against the service's own settings, and ends with one line naming the setting
when either is missing or invalid. `--memory-floor-mb` is the free card memory in MiB the
run keeps beside the provider's cap (default 1024, the program plan's "free memory at least
1 GB"); every memory figure is in MiB. The command samples the card itself, from outside the
group, and refuses a group, before it starts or while its service does not yet exist, if
the card's free memory minus the cap is below the floor, or a sample fails: nothing of the
group starts, and its cells are `not_measured` with the reason `memory_refused`. From the
first sample that finds the group's service the watchdog samples every half second; free
memory below the floor, a failed sample or a failure of the watchdog itself is a breach, and
the command prints one line on standard error, sends SIGKILL to every process below it as
the end-of-group kill does, and records the breach under `memory_stop` in the group's
service entry (its reason, the sample, the floor, the cap and the seconds to no process
left): the group's unfinished cells and every later group are `not_measured` with the reason
`watchdog_breach`, no later group starts and the command exits non-zero. A group's entry
holds the card's used memory before, the most used and the least free while the service
existed, the used memory after, their difference and what the run added at its peak beyond
the cap; the least free is judged against the line "free card memory at least 1024 MiB at
the peak". The watchdog cannot stop an allocation faster than one interval (the provider's
cap does), memory outside the cap (the CUDA context, compiler peaks), which it sees only
afterwards, the voice's own growth, nor a driver or `nvidia-smi` that reports wrong figures;
it reads GPU 0 only. A provider that failed to load, or a look that failed to prepare, ends
its group: each cell is `not_measured` with the reason the service logged.

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
  // "loading", then "drawn" once a frame is painted; "ended" for good on any failure,
  // which deletes the viewer the route made
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
