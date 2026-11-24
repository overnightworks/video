# The video wire

Audience: an app developer who asks the video service what it offers, opens a
session on a look and lets a browser watch it.

`video_client.wire` (the `overnightworks-video-client` distribution) owns the
paths (the viewer paths and the offer cap included), the caller headers, the `GET /health` answer and the refusal list;
`video_client.session_wire` owns the session's events; read them through those.
`video_client.health.fetch_health` makes the health call within a total deadline
and a byte cap and answers with a typed outcome; its module documents them.

The service listens on `VIDEO_HOST:VIDEO_PORT`, over HTTP on a TCP address.

## Version and refusals

- WIRE-1: `GET /health` is the one path outside `/v1`; every other path is under
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

- WIRE-2: A session upgrade carries the app's workload token in
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

- WIRE-3: `WS /v1/sessions?look=<name>` opens a session on a look. The upgrade is
refused, in this order and with the refusal's HTTP status, for the first of:

1. the tokens (WIRE-2);
2. `look` is absent, does not match `^[A-Za-z0-9._-]{1,64}$` or is not in the look
   file: `invalid_request`, `param` `look`;
3. the provider is not ready, the look is not ready or the service is draining
   (WIRE-11): `unavailable`;
4. `VIDEO_MAX_SESSIONS` sessions are open: `busy` with `Retry-After: 5`;
5. the provider cannot open the session: `unavailable`.

A session holds its place until it ends, whoever ends it. An upgrade that finds every
place taken waits for a place of a session that has ended and is refused `busy` only
if every place is held by a session that has not. Every refusal writes a
`session.ended` line with outcome `refused` and its code as `reason`.

- WIRE-4: Every server event is one JSON text message with a `type` and a fresh
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

- WIRE-5: A sentence is `sentence.start`
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

- WIRE-6: `output_audio_buffer.clear` drops the playing sentence and every queued
one, the sentence still open included, and the face returns to idle: the idle
frame on silence follows (WIRE-10). The playing sentence gets
`output_audio_buffer.cleared` `{response_id, audio_end_ms}` with the milliseconds of
its audio whose frames had left, in whole frames, and each other dropped sentence one
with `audio_end_ms` 0, in the order the sentences were queued. A dropped sentence
gets no `started` or `stopped` after its `cleared`. A clear with nothing queued sends
nothing; the audio and `sentence.end` of a dropped open sentence are refused as
outside an open sentence.

- WIRE-7: `output_audio_buffer.started` `{response_id}` is sent when a sentence's
first frame leaves for the viewers, and `output_audio_buffer.stopped`
`{response_id, audio_end_ms}` when its last frame has left, with the sentence's whole
audio as `audio_end_ms`. A failure sends `error`
`{error: {type, code, message, param, event_id}}`, where `code` is a code of the
refusal list, `param` the field at fault or `null`, and `event_id` the client event's
own id or `null`, and closes the session with `1000`. A provider that falls behind
real time by more than `VIDEO_MAX_LAG_MS`, or that fails to draw, ends it with
`unavailable`. The lag is how far a frame is produced after its time on the frame clock,
which a sentence's audio sets while it plays and which runs on its own while the
face is idle: a late idle frame counts like a sentence's, so a session can end
`unavailable` during silence. A client whose audio arrives late restarts the clock
and is not a lag, while audio already queued keeps the lag the frames before it built
up, and the time a client takes to end a sentence is none either. A provider call that has not returned
within `VIDEO_MAX_LAG_MS` ends the session with `unavailable` too. A client that disconnects gets no event.

**Trace.** The session's lines (`session.ended`, with `max_behind_ms`, the most a frame
fell behind its clock and 0 before the clock starts, `max_idle_call_ms`, the longest idle
provider call that returned, and for an `unavailable` end `ended_by`, one of
`late_frame`, `call_timeout` and `provider_failed`) are written in the trace of
`session.update`'s `traceparent`, and a sentence's line (`sentence.ended`, with
`audio_ms`, `frame_count`, `late_frame_count` and `first_output_ms`, the time from
its first audio to its first frame) in the trace of its `sentence.start`'s
`traceparent`; a missing or malformed `traceparent` starts a trace of its own. HopIn
sends the trace of the turn that spoke, so the service's lines join it.

## Viewers and media

- WIRE-9: `POST /v1/sessions/{id}/viewers` adds a viewer to the session `{id}`
(`session.created` names it) by the WHEP exchange: the body is a recvonly SDP offer
with exactly one audio and one video section, `Content-Type: application/sdp`, at
most `MAX_OFFER_BYTES` (16 KiB) in `video_client.wire`. It answers 201 with the
`application/sdp` answer, which holds every candidate (no trickle), and `Location`,
the viewer's path (`viewer_path`). The request carries the same two tokens as the
session's upgrade (WIRE-2), relayed by the app; the caller must be the session's
opener, the same app's client and the same person. It is refused, in this order, for
the first of:

1. the tokens (WIRE-2), checked before anything else of the request is read;
2. the session id is unknown or the session has ended: `not_found`;
3. the caller is not the session's opener, whether another person or another
   registered app: `forbidden`;
4. the service is draining (WIRE-11), or the session has not yet received its
   `session.update` and so has no ICE configuration (WIRE-4), or its ICE servers
   cannot be parsed into a peer: `unavailable`;
5. `Content-Type` other than `application/sdp`: `invalid_request`;
6. a body over `MAX_OFFER_BYTES`: `too_large`; the body is never read past the cap;
7. a body that is not SDP, an offer that is not recvonly, that has other than one
   audio and one video section, that offers no Opus or no VP8, or that the peer
   cannot take: `invalid_request`;
8. the session already has `VIDEO_MAX_VIEWERS` viewers: `busy` with `Retry-After: 5`.

`DELETE` on `Location`, with the same two tokens and the same opener check, ends the
viewer and answers 200; an unknown or ended viewer is `not_found`. PATCH, like any
other method a path does not offer, is `not_found` (WIRE-1). A viewer also ends when
its peer closes (DTLS close or ICE failure), and with its session, whatever ends
the session. A connected peer that goes silent without a DTLS close, a browser
killed for one, ends its viewer through ICE consent expiry (RFC 7675), as WHEP
requires of a server; its timing is the ICE library's, about 39 s at worst with
aioice 0.13.0. The service sets no deadline on a connection that is never
established: a viewer whose peer never applies the answer holds its place until it
is deleted or its session ends.

- WIRE-10: A viewer receives one Opus audio track and one VP8 video track in one
stream with one CNAME, so the browser keeps sound and picture together (RFC 3550,
section 6). The answer offers only Opus and VP8, whatever the offer lists first. Both
tracks are stamped from the session's frame count: video timestamps advance by one
frame period per frame, and the audio of each frame period goes out with its frame.
Between sentences the stream shows the provider's idle frame on silence, for as long
as the session is open and whether or not a viewer watches. Each viewer's peer is
configured with exactly the session's `ice_servers` and `ice_transport_policy`
(WIRE-4): an empty list stays empty, and no default STUN server is added, so the
service reaches the network only through the TURN servers the app hands over. The
media is DTLS-SRTP.

**Trace.** A viewer's line, `viewer.ended`, is written once for each POST, in the
trace of the POST's `traceparent`: when the viewer ends, with outcome `ok` and
`ended_by` `deleted`, `peer_closed` or `session_ended`, or at once when the POST is
refused, with outcome `refused` and the refusal's code as `reason`. The session's
line carries `viewer_count`, the viewers admitted over its life. The offer, the
answer, the candidates, the ICE credentials and the tokens are never logged, at any
`LOG_LEVEL`.

## The app's route

- WIRE-14: The browser package's `watch(host, path, iceServers, show)` calls the
app's own route at `path`, never the service, so no token reaches a browser; the
route relays to WIRE-9 with the session's tokens. Its contract:

- `POST path` with `Content-Type: application/sdp` and the browser's non-trickle
  offer as body (every candidate is in it) answers 201 with the SDP answer as body
  and `Location`, the app's own path of the viewer. The route bounds the body as
  WIRE-9 does (`MAX_OFFER_BYTES`) and rewrites the service's `Location`.
- `DELETE` on that `Location` ends the viewer and answers 2xx, relayed to WIRE-9.
- An answer other than 201 (the service's `busy` or `unavailable` relayed, or the
  app's own refusal), a 201 with no `Location` or an answer that does not set, a
  route that cannot be reached, a refused `play()` (the autoplay policy) and a peer
  connection that fails or closes each end the viewer: `show` gets `ended`, once,
  and `watch` throws nothing. `show` otherwise gets `loading`, then `drawn` once a
  frame is painted. A refused `play()` is `ended`, never a muted picture.
- Every end after the route has answered 201, a failure as much as `stop()`, sends
  the `DELETE` to its `Location`, so the viewer frees its place without the app.
- `stop()` sends the `DELETE` and reports `ended` at once. A `stop()` before the POST
  has answered waits for the answer, then deletes: an aborted POST may already have
  made a viewer, which would hold one of the session's `VIDEO_MAX_VIEWERS` places
  until it is deleted or its session ends (WIRE-9).
- `watch` sets no deadline on the POST: a route that never answers stays `loading`
  until the app calls `stop()`.
- `watch` waits for ICE gathering to complete before the POST, with no deadline
  either: with STUN or TURN servers that do not answer, the page stays `loading`
  until the browser gives up on them, about 40 s in Chromium.

## Drain

- WIRE-11: On SIGTERM or SIGINT the service reports `ready` false at once, refuses
every new session and every new viewer as `unavailable` (WIRE-3, WIRE-9), lets the
playing sentence of each open
session finish, then closes the session with `1012` (Service Restart,
`DRAIN_CLOSE_CODE` in `video_client.wire`) and no `error`. A sentence still queued is
dropped without events; a session with no sentence playing closes at once; a clear
during the drain ends the session after its `cleared`. The process exits when the
last session has ended, or `VIDEO_DRAIN_SECONDS` after the signal at the latest; the
sessions still open then, one stuck in a provider call included, are closed with
`1012` by the stop, which starts at most one second before that time. A session's
viewers end with it.

## Health

- WIRE-12: `GET /health` needs no token and answers 200:

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
  ([CONFIG-5](configuration.md#look-file)) and the service is not draining. A look
  that failed stays false and the other looks serve.
- `sessions.open` is the number of sessions open now; `sessions.limit` is
  `VIDEO_MAX_SESSIONS`.
- `card_memory_mb` is the memory in use on GPU 0, or 0 where no card can be
  queried.
- A reader ignores the keys it does not know.

## Not offered

- WIRE-13: The service offers no look administration or upload, no recording, and
no `/docs`, `/redoc` or `/openapi.json`; each of those paths is 404 `not_found`.
The looks are what the operator's look file names.

## Settings that shape the wire

| Setting | Default | Meaning |
|---|---|---|
| `VIDEO_MAX_SESSIONS` | `1` | `sessions.limit`, at least 1; one more is `busy` (WIRE-3). |
| `VIDEO_MAX_VIEWERS` | `5` | The most viewers a session admits, at least 1; one more is `busy` (WIRE-9). |
| `VIDEO_MAX_QUEUED_AUDIO_SECONDS` | `30` | The most queued audio a session holds, above 0 (WIRE-5). |
| `VIDEO_MAX_LAG_MS` | `500` | The most a frame may be late, and the longest a provider call may take, before the session ends `unavailable`, at least 1 (WIRE-7). |
| `VIDEO_DRAIN_SECONDS` | `30` | The longest from the stop to the exit, the cut of the sessions left included, above 0 (WIRE-11). |
