"""Clearing the buffer (WIRE-6): the playing and every queued sentence are dropped."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from video_provider_fake import FakeProvider

from support.session_support import (
    EVENT_WITHIN_SECONDS,
    failure_of,
    opened,
    pcm,
    receive,
    sentence_events,
)
from video_client.session_wire import (
    OutputAudioBufferCleared,
    OutputAudioBufferStarted,
    OutputAudioBufferStopped,
)
from video_client.wire import RefusalCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient
    from starlette.testclient import WebSocketTestSession

    from video_service.runtime import Runtime

_LONG_MS = 10_000
_IDLE_FRAMES_AT_START = 1
_CLEAR = {"type": "output_audio_buffer.clear"}


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


def _queue(session: WebSocketTestSession, sentence: str, milliseconds: int) -> None:
    start, end = sentence_events(sentence)
    session.send_json(start)
    session.send_bytes(pcm(milliseconds))
    session.send_json(end)


def _cleared(
    session: WebSocketTestSession, count: int
) -> list[OutputAudioBufferCleared]:
    events = [receive(session) for _ in range(count)]
    assert all(isinstance(event, OutputAudioBufferCleared) for event in events)
    return events  # type: ignore[return-value]


def test_a_clear_drops_the_playing_sentence_and_every_queued_one(
    client: TestClient,
) -> None:
    with opened(client) as session:
        _queue(session, "playing", _LONG_MS)
        assert isinstance(receive(session), OutputAudioBufferStarted)
        _queue(session, "queued", _LONG_MS)
        start, _ = sentence_events("open")
        session.send_json(start)
        session.send_bytes(pcm(100))

        session.send_json(_CLEAR)
        playing, queued, still_open = _cleared(session, 3)

    assert [event.response_id for event in (playing, queued, still_open)] == [
        "playing",
        "queued",
        "open",
    ]
    assert (queued.audio_end_ms, still_open.audio_end_ms) == (0, 0)


def test_the_playing_sentence_reports_the_audio_that_had_left_in_whole_frames(
    client: TestClient,
) -> None:
    with opened(client) as session:
        _queue(session, "playing", _LONG_MS)
        started = receive(session)
        assert isinstance(started, OutputAudioBufferStarted)
        frame_rate = client.get("/health").json()["provider"]["video"]["frame_rate"]

        session.send_json(_CLEAR)
        (playing,) = _cleared(session, 1)

    assert 0 <= playing.audio_end_ms < _LONG_MS
    assert playing.audio_end_ms % (1000 // frame_rate) == 0


def test_a_cleared_sentence_never_stops_and_the_face_returns_to_idle(
    client: TestClient, provider: FakeProvider
) -> None:
    with opened(client) as session:
        _queue(session, "playing", _LONG_MS)
        receive(session)
        session.send_json(_CLEAR)
        _cleared(session, 1)
        _queue(session, "next", 80)
        after = [receive(session), receive(session)]

    assert [type(event) for event in after] == [
        OutputAudioBufferStarted,
        OutputAudioBufferStopped,
    ]
    assert provider.sessions[0].clears == 1
    assert provider.sessions[0].idle_frames == _IDLE_FRAMES_AT_START + 1


def test_a_sentence_cleared_before_any_audio_is_dropped_with_nothing_played(
    client: TestClient,
) -> None:
    with opened(client) as session:
        session.send_json(sentence_events("empty")[0])
        session.send_json(_CLEAR)
        (cleared,) = _cleared(session, 1)
        _queue(session, "next", 80)
        after = receive(session)

    assert (cleared.response_id, cleared.audio_end_ms) == ("empty", 0)
    assert isinstance(after, OutputAudioBufferStarted)
    assert after.response_id == "next"


def test_a_clear_with_nothing_queued_sends_nothing(client: TestClient) -> None:
    with opened(client) as session:
        session.send_json(_CLEAR)
        _queue(session, "next", 80)
        first = receive(session)

    assert isinstance(first, OutputAudioBufferStarted)


def test_the_end_of_a_cleared_open_sentence_is_refused(client: TestClient) -> None:
    with opened(client) as session:
        start, end = sentence_events("a")
        session.send_json(start)
        session.send_bytes(pcm(100))
        session.send_json(_CLEAR)
        _cleared(session, 1)
        session.send_json(end)
        error = failure_of(receive(session))

    assert (error.error.code, error.error.param) == (
        RefusalCode.INVALID_REQUEST,
        "sentence",
    )


def test_a_clear_while_the_last_frames_are_drawn_leaves_no_stop_behind(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    gate = threading.Event()
    provider = FakeProvider(finish_gate=gate)
    client = wire(started(provider))
    with opened(client) as session:
        frame_rate = client.get("/health").json()["provider"]["video"]["frame_rate"]
        _queue(session, "last", 1000 // frame_rate)
        assert isinstance(receive(session), OutputAudioBufferStarted)
        assert provider.sessions[0].finishing.wait(EVENT_WITHIN_SECONDS)
        session.send_json(_CLEAR)
        _cleared(session, 1)
        gate.set()
        _queue(session, "next", 80)
        after = receive(session)

    assert isinstance(after, OutputAudioBufferStarted)
    assert after.response_id == "next"
