"""How a session starts (WIRE-4): created, update, the first idle frame."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from starlette.websockets import WebSocketDisconnect
from video_provider_fake import FakeProvider

from support.session_support import (
    ICE_SERVERS,
    connect,
    failure_of,
    opened,
    receive,
    sentence_events,
    update_event,
)
from video_client.session_wire import SessionCreated, SessionUpdated
from video_client.wire import RefusalCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

    from video_service.runtime import Runtime


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def client(
    provider: FakeProvider,
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
) -> TestClient:
    return wire(started(provider))


def test_the_first_event_names_the_session_its_look_and_the_picture(
    client: TestClient,
) -> None:
    with connect(client) as session:
        created = receive(session)

    assert isinstance(created, SessionCreated)
    assert created.session.look == "mozart"
    assert created.session.video.frame_rate > 0
    assert created.session.id


def test_the_first_event_names_the_queued_audio_limit_the_service_enforces(
    provider: FakeProvider,
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
) -> None:
    client = wire(started(provider, max_queued_audio_seconds=7.5))
    with connect(client) as session:
        created = receive(session)

    assert isinstance(created, SessionCreated)
    assert created.session.max_queued_audio_seconds == 7.5


def test_every_server_event_and_session_gets_a_fresh_id(client: TestClient) -> None:
    with connect(client) as first, connect(client) as second:
        events = [receive(first), receive(second)]

    assert isinstance(events[0], SessionCreated)
    assert isinstance(events[1], SessionCreated)
    assert events[0].session.id != events[1].session.id
    assert events[0].event_id != events[1].event_id


@pytest.mark.parametrize("policy", ["relay", "all"])
def test_the_session_takes_the_ice_servers_in_the_webrtc_shape(
    client: TestClient, policy: str
) -> None:
    update = update_event(
        session={
            "ice_servers": [*ICE_SERVERS, {"urls": "stun:stun.test"}],
            "ice_transport_policy": policy,
        },
        traceparent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
    )

    with connect(client) as session:
        receive(session)
        session.send_json(update)
        updated = receive(session)

    assert isinstance(updated, SessionUpdated)


@pytest.mark.parametrize(
    ("first", "param"),
    [
        (sentence_events("s")[0], "type"),
        ({"type": "output_audio_buffer.clear"}, "type"),
        ({"type": "no.such.event"}, "type"),
        ({"no": "type"}, "type"),
        (
            {
                "type": "sentence.start",
                "sentence": "s",
                "format": {"type": "audio/pcm", "rate": 8000},
            },
            "type",
        ),
        (update_event(extra=1), "extra"),
        (update_event(session={"ice_servers": []}), "session.ice_transport_policy"),
        (
            update_event(
                session={"ice_servers": [], "ice_transport_policy": "everything"}
            ),
            "session.ice_transport_policy",
        ),
        (
            update_event(
                session={"ice_servers": [{"urls": 1}], "ice_transport_policy": "all"}
            ),
            "session.ice_servers.0.urls",
        ),
    ],
    ids=[
        "a-sentence",
        "a-clear",
        "unknown-type",
        "no-type",
        "a-sentence-that-is-no-valid-sentence",
        "unknown-field",
        "no-policy",
        "bad-policy",
        "bad-url",
    ],
)
def test_a_first_event_that_is_not_a_valid_session_update_ends_the_session(
    client: TestClient, first: dict[str, object], param: str
) -> None:
    with connect(client) as session:
        receive(session)
        session.send_json(first)
        error = failure_of(receive(session))
        with pytest.raises(WebSocketDisconnect):
            session.receive_text()

    assert error.error.code is RefusalCode.INVALID_REQUEST
    assert error.error.param == param


def test_a_first_message_that_is_not_text_ends_the_session(client: TestClient) -> None:
    with connect(client) as session:
        receive(session)
        session.send_bytes(b"\x00\x00")
        error = failure_of(receive(session))

    assert (error.error.code, error.error.param) == (
        RefusalCode.INVALID_REQUEST,
        "type",
    )


def test_a_second_session_update_ends_the_session_naming_the_event(
    client: TestClient,
) -> None:
    with opened(client) as session:
        session.send_json(update_event(event_id="client-event-7"))
        error = failure_of(receive(session))

    assert error.error.code is RefusalCode.INVALID_REQUEST
    assert error.error.param == "type"
    assert error.error.event_id == "client-event-7"
    assert error.event_id != error.error.event_id


@pytest.mark.parametrize(
    "second",
    [update_event(extra=1), update_event(session={"ice_servers": []})],
    ids=["unknown-field", "no-policy"],
)
def test_a_second_session_update_names_its_type_whatever_else_is_wrong_with_it(
    client: TestClient, second: dict[str, object]
) -> None:
    with opened(client) as session:
        session.send_json(second)
        error = failure_of(receive(session))

    assert error.error.param == "type"


def test_a_session_that_leaves_before_its_first_event_closes_its_provider_session(
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
    provider: FakeProvider,
) -> None:
    client = wire(started(provider, max_sessions=1))
    with connect(client) as session:
        receive(session)
    with connect(client) as again:
        receive(again)
        first_closed = provider.sessions[0].closed

    assert first_closed
