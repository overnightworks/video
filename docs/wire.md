# The video wire

Audience: an app developer who asks the video service what it offers and opens a
session on a look.

`video_client.wire` (the `overnightworks-video-client` distribution) owns the
paths, the caller headers, the `GET /health` answer and the refusal list;
`video_client.session_wire` owns the session's events; read them through those.
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
| `unauthorized` | 401 | `WWW-Authenticate: Bearer` |
| `forbidden` | 403 | |
| `invalid_request` | 422 | |
| `too_large` | 413 | |
| `busy` | 429 | `Retry-After`: whole seconds, at least 1 |
| `unavailable` | 503 | |

A refused WebSocket upgrade is answered with that same HTTP refusal; an upgrade at
a path the service does not offer is refused as a request there would be.

## Callers

**WIRE-2** A session upgrade carries the app's workload token in
`Video-Workload-Token` and the person's token as `Authorization: Bearer <token>`;
`video_client.wire.caller_headers` builds them. The service answers only when both
tokens are valid and belong together:

- Both come from the configured issuer, name the service in `aud` (its own
  audience, never another service's), and are unexpired; the service allows no
  clock skew.
- The workload token carries the configured realm role and comes from a
  registered app's client (`azp` and `client_id`).
- The person's token has the same `azp`, a different subject, membership of
  `/apps/<app>` or a group below it, and membership of exactly the configured
  service group (no subgroup of it).

An agent with its own account is the person: its app exchanges the agent's token for
one of the service's audience through the issuer's token exchange. The check runs
before anything else of the request is read: a request with bad tokens is refused
for its tokens whatever else is wrong with it. A token or an issuer fault is
`unauthorized` (missing, malformed, unverifiable or expired token), `forbidden` (a
valid token the policy above does not admit) or `unavailable` (the issuer's key set
cannot be fetched). Nothing taken from a token is logged.

## Session

**WIRE-3** `WS /v1/sessions?look=<name>` opens a session on a look. The upgrade is
refused, in this order and with the refusal's HTTP status, for the first of:

1. the tokens (WIRE-2);
2. `look` is absent, does not match `^[A-Za-z0-9._-]{1,64}$` or is not in the look
   file: `invalid_request`, `param` `look`;
3. the provider is not ready, the look is not ready or the service is draining
   (WIRE-11): `unavailable`;
4. `VIDEO_MAX_SESSIONS` sessions are open: `busy` with `Retry-After: 5`;
5. the provider cannot open the session: `unavailable`.

A session holds its place until it ends, whoever ends it. Every refusal writes a
`session.ended` line with outcome `refused` and its code as `reason`.

**WIRE-4** Every server event is one JSON text message with a `type` and a fresh
`event_id`. A client event is one JSON text message with its `type`, an optional
`event_id` that an `error` about it returns, and no member not listed here. The
exchange starts like this:

- The service sends `session.created` `{session: {id, look, video: {width, height,
  frame_rate}}}` first.
- The client's first event must be `session.update`
  `{session: {ice_servers, ice_transport_policy}, traceparent?}`, with both
  members in the shape of W3C `RTCConfiguration` (https://www.w3.org/TR/webrtc/):
  `ice_servers` a list of `{urls, username?, credential?}` with `urls` one string or
  a list of them, and `ice_transport_policy` `relay` or `all`. HopIn sends `relay`;
  a one-host test sends `all`. The service keeps the configuration for the viewers'
  media and never logs it; a credential appears in no line and no error.
- The service answers `session.updated` once the provider has drawn the look's first
  idle frame; a first idle frame not drawn within `VIDEO_MAX_LAG_MS` ends the session
  with `unavailable` (WIRE-7).
- A first client event of another type (a binary message included), or a second
  `session.update`, ends the session with an `error` (`invalid_request`, `param`
  `type`).

**WIRE-5** A sentence is `sentence.start`
`{sentence, format: {"type": "audio/pcm", "rate": 16000 | 22050 | 24000 | 48000},
traceparent?}`, then binary messages of PCM16 little-endian mono at `rate`, then
`sentence.end` `{sentence}`. `sentence` is the app's id, one to 128 characters, and
is the `response_id` of the sentence's playback events. One sentence is open at a
time; a sentence stays queued until it has played or been cleared, and its id may
not start again while it is queued. Sentences play in order and back to back, the
next one's first frame following the last frame of the one before. A sentence's
audio may still arrive while it plays. A sentence with no audio, a binary message of
an odd length or outside an open sentence (`param` `audio`), a `sentence.end` that
does not name the open sentence (`param` `sentence`), a `sentence.start` while one
is open (`param` `type`) and a format outside the set (`param` `format.rate` or
`format.type`) end the session with `invalid_request`. A text message above 64 KiB
and queued audio above `VIDEO_MAX_QUEUED_AUDIO_SECONDS` end it with `too_large`. The
queued audio is the audio received of the queued sentences that has not yet left as
frames, the playing sentence's included.

**WIRE-6** `output_audio_buffer.clear` drops the playing sentence and every queued
one, the sentence still open included, and the provider returns the face to idle by
its own idle rendering on silent audio. The playing sentence gets
`output_audio_buffer.cleared` `{response_id, audio_end_ms}` with the milliseconds of
its audio whose frames had left, in whole frames, and each other dropped sentence one
with `audio_end_ms` 0, in the order the sentences were queued. A dropped sentence
gets no `started` or `stopped` after its `cleared`. A clear with nothing queued sends
nothing; the audio and `sentence.end` of a dropped open sentence are refused as
outside an open sentence.

**WIRE-7** `output_audio_buffer.started` `{response_id}` is sent when a sentence's
first frame leaves for the viewers, and `output_audio_buffer.stopped`
`{response_id, audio_end_ms}` when its last frame has left, with the sentence's whole
audio as `audio_end_ms`. A failure sends `error`
`{error: {type, code, message, param, event_id}}`, where `code` is a code of the
refusal list, `param` the field at fault or `null`, and `event_id` the client event's
own id or `null`, and closes the session with `1000`. A provider that falls behind
real time by more than `VIDEO_MAX_LAG_MS`, or that fails to draw, ends it with
`unavailable`. The lag is how far a frame is produced after its time on the audio's
clock; a client whose audio arrives late restarts the clock and is not a lag, while
audio already queued keeps the lag the frames before it built up, and the time a
client takes to end a sentence is none either. A provider call that has not returned
within `VIDEO_MAX_LAG_MS` ends the session with `unavailable` too. A client that disconnects gets no event.

**Trace.** The session's lines (`session.ended`) are written in the trace of
`session.update`'s `traceparent`, and a sentence's line (`sentence.ended`, with
`audio_ms`, `frame_count`, `late_frame_count` and `first_output_ms`, the time from
its first audio to its first frame) in the trace of its `sentence.start`'s
`traceparent`; a missing or malformed `traceparent` starts a trace of its own. HopIn
sends the trace of the turn that spoke, so the service's lines join it.

## Drain

**WIRE-11** On SIGTERM or SIGINT the service reports `ready` false at once, refuses
every new session as `unavailable` (WIRE-3), lets the playing sentence of each open
session finish, then closes the session with `1012` (Service Restart,
`DRAIN_CLOSE_CODE` in `video_client.wire`) and no `error`. A sentence still queued is
dropped without events; a session with no sentence playing closes at once; a clear
during the drain ends the session after its `cleared`. The process exits when the
last session has ended, or `VIDEO_DRAIN_SECONDS` after the signal at the latest; the
sessions still open then, one stuck in a provider call included, are closed with
`1012` by the stop, which starts at most one second before that time.

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
  empty and `video` is `null`. From the drain's start (WIRE-11) `ready` is false
  while `provider.ready` is not changed.
- `provider.name` is the catalogue name `VIDEO_PROVIDER` names. `plays` lists the
  verbs the provider plays (`speak`, `clear`); `video` is the picture it draws.
- `looks` has one key per look the look file names, whatever its state. A look's
  `ready` is true exactly when a session for it would open now: the provider is
  ready and the look's image passed every start check
  ([ARCHITECTURE](ARCHITECTURE.md#looks)) and the service is not draining. A look
  that failed stays false and the other looks serve.
- `sessions.open` is the number of sessions open now; `sessions.limit` is
  `VIDEO_MAX_SESSIONS`.
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
| `VIDEO_MAX_SESSIONS` | `1` | `sessions.limit`, at least 1; one more is `busy` (WIRE-3). |
| `VIDEO_MAX_QUEUED_AUDIO_SECONDS` | `30` | The most queued audio a session holds, above 0 (WIRE-5). |
| `VIDEO_MAX_LAG_MS` | `500` | The most a frame may be late, and the longest a provider call may take, before the session ends `unavailable`, at least 1 (WIRE-7). |
| `VIDEO_DRAIN_SECONDS` | `30` | The longest from the stop to the exit, the cut of the sessions left included, above 0 (WIRE-11). |
