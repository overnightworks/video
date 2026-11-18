"""The drain (WIRE-11): not ready at once, no new sessions, the playing one ends."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from starlette.websockets import WebSocketDisconnect

from support.identity_support import valid_headers
from support.session_support import (
    denial,
    opened,
    pcm,
    receive,
    sentence_events,
    session_url,
)
from video_client.session_wire import (
    OutputAudioBufferCleared,
    OutputAudioBufferStarted,
    OutputAudioBufferStopped,
)
from video_client.wire import DRAIN_CLOSE_CODE, Health, RefusalCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient
    from starlette.testclient import WebSocketTestSession

    from video_service.runtime import Runtime


@pytest.fixture
def runtime(started: Callable[..., Runtime]) -> Runtime:
    return started(max_sessions=1)


@pytest.fixture
def client(runtime: Runtime, wire: Callable[[Runtime], TestClient]) -> TestClient:
    return wire(runtime)


def _begin_drain(client: TestClient, runtime: Runtime) -> None:
    client.portal.call(runtime.begin_drain)


def _close_code(session: WebSocketTestSession) -> int:
    with pytest.raises(WebSocketDisconnect) as closed:
        receive(session)
    return closed.value.code


def _speak(session: WebSocketTestSession, milliseconds: int) -> None:
    start, end = sentence_events("playing")
    session.send_json(start)
    session.send_bytes(pcm(milliseconds))
    session.send_json(end)


def test_an_idle_session_closes_with_service_restart_when_the_drain_begins(
    client: TestClient, runtime: Runtime
) -> None:
    with opened(client) as session:
        _begin_drain(client, runtime)

        code = _close_code(session)

    assert code == DRAIN_CLOSE_CODE


def test_a_draining_service_is_not_ready_and_refuses_new_sessions(
    client: TestClient, runtime: Runtime
) -> None:
    _begin_drain(client, runtime)

    health = Health.model_validate(client.get("/health").json())
    denied = denial(client, session_url(), valid_headers())

    assert not health.ready
    assert not health.looks["mozart"].ready
    assert (denied.status, denied.code) == (
        HTTPStatus.SERVICE_UNAVAILABLE,
        RefusalCode.UNAVAILABLE,
    )


def test_the_playing_sentence_finishes_before_the_session_closes(
    client: TestClient, runtime: Runtime
) -> None:
    with opened(client) as session:
        _speak(session, 200)
        assert isinstance(receive(session), OutputAudioBufferStarted)
        start, end = sentence_events("queued")
        session.send_json(start)
        session.send_bytes(pcm(200))
        session.send_json(end)
        _begin_drain(client, runtime)

        stopped = receive(session)
        code = _close_code(session)

    assert isinstance(stopped, OutputAudioBufferStopped)
    assert (stopped.response_id, stopped.audio_end_ms) == ("playing", 200)
    assert code == DRAIN_CLOSE_CODE


def test_a_clear_during_the_drain_ends_the_session_at_once(
    client: TestClient, runtime: Runtime
) -> None:
    with opened(client) as session:
        _speak(session, 10_000)
        assert isinstance(receive(session), OutputAudioBufferStarted)
        _begin_drain(client, runtime)
        session.send_json({"type": "output_audio_buffer.clear"})

        cleared = receive(session)
        code = _close_code(session)

    assert isinstance(cleared, OutputAudioBufferCleared)
    assert code == DRAIN_CLOSE_CODE


def test_the_service_is_idle_once_the_drained_sessions_have_ended(
    client: TestClient, runtime: Runtime
) -> None:
    with opened(client) as session:
        _begin_drain(client, runtime)
        _close_code(session)

    client.portal.call(runtime.wait_idle)
