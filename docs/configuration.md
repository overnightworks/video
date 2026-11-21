# Configuration

What the operator sets: the `VIDEO_*` environment, the look file and the caller file.
Each rule carries the ID the test that proves it cites. The settings that shape the wire
([`wire.md`](wire.md#settings-that-shape-the-wire)) are listed there and not here.

## Settings

**CONFIG-1** The process reads `VIDEO_*` from the environment. A missing or malformed
setting stops the start, naming the setting and never its value.

| Setting | Default | Meaning |
|---|---|---|
| `VIDEO_PROVIDER` | required | The provider this instance runs: `fake` or `flashhead-lite`. |
| `VIDEO_PROVIDER_ROOT` | unset | The absolute path of the directory that holds each provider's own environment (CONFIG-2). |
| `VIDEO_CARD_MEMORY_MB` | unset | The memory in MiB the provider may use on the card (CONFIG-3). |
| `VIDEO_HOST` | `127.0.0.1` | The address to listen on. |
| `VIDEO_PORT` | `8080` | The port to listen on, 1 to 65535. |
| `VIDEO_CONFIG_FILE` | required | The absolute path of the caller file (CONFIG-6). |
| `VIDEO_LOOKS_FILE` | unset | The absolute path of the look file (CONFIG-4); unset offers no look. |
| `VIDEO_LOOK_SOURCES` | `[]` | A JSON list of the URL prefixes a look's image may come from (CONFIG-5). |
| `VIDEO_LOOK_MAX_BYTES` | `8388608` | The most bytes an image may have, at least 1. |
| `VIDEO_LOOK_MAX_PIXELS` | `16777216` | The most pixels (width times height) an image may have, at least 1. |

The `fake` provider is a development dependency: `uv sync --all-groups` installs it and
the image does not.

## Provider and card

**CONFIG-2** The provider runs as `<VIDEO_PROVIDER_ROOT>/<provider>/.venv/bin/video-<provider>-worker`;
with no root it runs as `video-<provider>-worker` beside the service's interpreter. No
other location is searched.

**CONFIG-3** `VIDEO_CARD_MEMORY_MB` is an integer of at least 1. It is required for
`flashhead-lite` and optional for `fake`. It binds torch's allocator, not the card's
context; whether the card holds it is the worker's judgement at start, and a cap too large
leaves the provider failed. The child receives this value and no other.

## Look file

**CONFIG-4** `VIDEO_LOOKS_FILE` is a TOML file with one table per look, named by
`^[A-Za-z0-9._-]{1,64}$` (a name with a dot is quoted), each holding exactly `url` and
`sha256` (64 lowercase hex characters). A file that breaks this form or cannot be read
stops the start naming `VIDEO_LOOKS_FILE`. An empty file offers no look.

```toml
[mozart]
url = "https://images.example/looks/mozart.png"
sha256 = "0f3a...64 lowercase hex characters"

["bach.v2"]
url = "https://images.example/looks/bach.png"
sha256 = "0f3a...64 lowercase hex characters"
```

**CONFIG-5** Each prefix of `VIDEO_LOOK_SOURCES` is http or https with a lowercase host,
no user, query or fragment, and a path that starts and ends with `/`, holds only ASCII
letters, digits and `-._~/` and has no empty, `.` or `..` segment. At start each look's
image is fetched once, only from a URL under such a prefix, without following a redirect.
It is kept when its sha256 is the file's, it is within `VIDEO_LOOK_MAX_BYTES` and, by its
header, `VIDEO_LOOK_MAX_PIXELS`, and it decodes as PNG, JPEG or WebP. A look that fails a
check, or that the provider refuses, stays not ready in health and is logged with its
`reason`; the other looks serve.

## Caller file

**CONFIG-6** `VIDEO_CONFIG_FILE` is a TOML file that says whom the service trusts.
`[identity]` names the issuer and the one group the people must belong to; each
`[apps.<name>]` registers an app by the client id of its workload token, which no other app
shares, and its people belong to the group `/apps/<name>`. `jwks_url` is https, and each
group is an absolute path without an empty segment. An unknown key, a file that breaks this
form or one that cannot be read stops the start, naming the field and never its value.
The wire's use of the two tokens is [`wire.md`](wire.md#callers).

```toml
[identity]
issuer = "https://identity.example/realms/apps"
jwks_url = "https://identity.example/realms/apps/protocol/openid-connect/certs"
audience = "video-service"
workload_role = "video-workload"
group_claim = "groups"
service_group = "/services/video-service"

[apps.hopin]
workload_client = "hopin-workload"
```
