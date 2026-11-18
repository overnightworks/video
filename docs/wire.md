# The video wire

Audience: an app developer who asks the video service what it offers.

`video_client.wire` (the `overnightworks-video-client` distribution) owns the
paths, the `GET /health` answer and the refusal list; read them through it.
`video_client.health.fetch_health` makes the health call within a total deadline
and a byte cap and answers with a typed outcome; its module documents them.

The service listens on `VIDEO_HOST:VIDEO_PORT`, over HTTP on a TCP address.

## Version and refusals

**WIRE-1** `GET /health` is the one path outside `/v1`; every other path is under
`/v1`. `/v{n}` or `/v{n}/...` with `n` other than 1, on any method, is 400
`unsupported_version`. Any other path, and any method a path does not offer, is
404 `not_found`.

Every HTTP refusal is `Content-Type: application/json` in OpenAI's error form,
`{"error": {"message", "type", "param", "code"}}`. `message` is a fixed sentence
per code and never echoes the request. `type` is `invalid_request_error` for a
4xx and `server_error` for a 5xx. `param` is the request field at fault, or
`null`. Apps read `code`:

| `code` | Status | Header |
|---|---|---|
| `unsupported_version` | 400 | |
| `not_found` | 404 | |
| `unauthorized` | 401 | |
| `forbidden` | 403 | |
| `invalid_request` | 422 | |
| `too_large` | 413 | |
| `busy` | 429 | `Retry-After`: whole seconds, at least 1 |
| `unavailable` | 503 | |

## Health

**WIRE-12** `GET /health` needs no token and answers 200:

```json
{
  "ready": true,
  "provider": {"name": "fake", "ready": true, "plays": ["speak", "clear"],
               "video": {"width": 256, "height": 256, "frame_rate": 25}},
  "looks": {"mozart": {"ready": true}},
  "sessions": {"open": 0, "limit": 1},
  "card_memory_mb": 0
}
```

- `ready` and `provider.ready` are true once the start is over and the provider
  started. Before that, and when the provider failed, they are false, `plays` is
  empty and `video` is `null`.
- `provider.name` is the catalogue name `VIDEO_PROVIDER` names. `plays` lists the
  verbs the provider plays (`speak`, `clear`); `video` is the picture it draws.
- `looks` has one key per look the look file names, whatever its state. A look's
  `ready` is true exactly when a session for it would open now: the provider is
  ready and the look's image passed every start check
  ([ARCHITECTURE](ARCHITECTURE.md#looks)). A look that failed stays false and the
  other looks serve.
- `sessions.limit` is `VIDEO_MAX_SESSIONS`.
- `card_memory_mb` is the memory in use on GPU 0, or 0 where no card can be
  queried.
- A reader ignores the keys it does not know.

## Not offered

**WIRE-13** The service offers no look administration or upload, no recording, and
no `/docs`, `/redoc` or `/openapi.json`; each of those paths is 404 `not_found`.
The looks are what the operator's look file names.

## Settings that shape the wire

| Setting | Default | Meaning |
|---|---|---|
| `VIDEO_MAX_SESSIONS` | `1` | `sessions.limit`, at least 1. |
