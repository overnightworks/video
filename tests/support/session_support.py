"""Driving a session at the WebSocket boundary: events in, events and refusals out."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, NamedTuple

import pytest
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from support.identity_support import valid_headers
from video_client.session_wire import (
    BYTES_PER_SAMPLE,
    ErrorEvent,
    ServerEvent,
    SessionCreated,
    SessionUpdated,
    parse_server_event,
)
from video_client.wire import LOOK_PARAM, SESSIONS_PATH, Refusal, RefusalCode

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from starlette.testclient import TestClient, WebSocketTestSession

EVENT_WITHIN_SECONDS: Final = 10.0
RATE: Final = 16000
ICE_SERVERS: Final = [
    {"urls": ["turn:relay.test:3478"], "username": "u", "credential": "SENTINEL-CRED"}
]
_reader = ThreadPoolExecutor(max_workers=4, thread_name_prefix="event-reader")


def session_url(look: str = "mozart") -> str:
    """The upgrade path of a session on `look`."""
    return f"{SESSIONS_PATH}?{LOOK_PARAM}={look}"


def connect(client: TestClient, look: str = "mozart") -> WebSocketTestSession:
    """Open the upgrade of a session on `look` with valid tokens."""
    return client.websocket_connect(session_url(look), headers=valid_headers())


class Denied(NamedTuple):
    """A refused upgrade: the status, the refusal's code and field, the headers."""

    status: int
    code: RefusalCode
    param: str | None
    headers: Mapping[str, str]


def denial(client: TestClient, url: str, headers: dict[str, str]) -> Denied:
    """What the service answers an upgrade it refuses."""
    with (
        pytest.raises(WebSocketDenialResponse) as refused,
        client.websocket_connect(url, headers=headers),
    ):
        pass
    answer = refused.value
    error = Refusal.model_validate_json(answer.content).error
    return Denied(answer.status_code, error.code, error.param, answer.headers)


def update_event(**fields: object) -> dict[str, object]:
    """A valid `session.update`, with `fields` set over it."""
    return {
        "type": "session.update",
        "session": {"ice_servers": ICE_SERVERS, "ice_transport_policy": "relay"},
    } | fields


def pcm(milliseconds: int, rate: int = RATE) -> bytes:
    """Silent PCM16 mono audio of `milliseconds`."""
    return bytes(BYTES_PER_SAMPLE * rate * milliseconds // 1000)


def receive(session: WebSocketTestSession) -> ServerEvent:
    """The next server event; the test fails when none comes."""
    pending = _reader.submit(session.receive_text)
    done, _ = wait([pending], timeout=EVENT_WITHIN_SECONDS)
    if not done:
        pytest.fail("no server event arrived")
    return parse_server_event(pending.result())


@contextmanager
def opened(client: TestClient, look: str = "mozart") -> Iterator[WebSocketTestSession]:
    """A session past its start: `session.created` and `session.updated` read."""
    with connect(client, look) as session:
        assert isinstance(receive(session), SessionCreated)
        session.send_json(update_event())
        assert isinstance(receive(session), SessionUpdated)
        yield session


def sentence_events(sentence: str, rate: int = RATE) -> tuple[dict, dict]:
    """The `sentence.start` and `sentence.end` of `sentence`."""
    return (
        {
            "type": "sentence.start",
            "sentence": sentence,
            "format": {"type": "audio/pcm", "rate": rate},
        },
        {"type": "sentence.end", "sentence": sentence},
    )


def failure_of(event: ServerEvent) -> ErrorEvent:
    """`event`, which must be an `error`."""
    assert isinstance(event, ErrorEvent)
    return event


def next_error(session: WebSocketTestSession) -> ErrorEvent:
    """The `error` event, read past the playback events that came before it."""
    while True:
        event = receive(session)
        if isinstance(event, ErrorEvent):
            return event


def written(output: str, event: str) -> list[dict]:
    """The JSON lines of `event` in `output`, the service's standard output."""
    records = (json.loads(line) for line in output.splitlines() if line.startswith("{"))
    return [record for record in records if record["event"] == event]


def close_code(session: WebSocketTestSession) -> int:
    """The code the service closes the session with; no event may precede it."""
    with pytest.raises(WebSocketDisconnect) as closed:
        receive(session)
    return closed.value.code
