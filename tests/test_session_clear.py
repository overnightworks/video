"""Clearing the buffer: the playing and every queued sentence are dropped."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from video_provider_fake import FakeProvider

from support.chunk_support import (
    ENOUGH_LAG_MS,
    chunked,
    record_leaves,
    waiting,
)
from support.clock_support import FakeClock
from support.scripted_session import play, played, scripted
from support.session_support import (
    EVENT_WITHIN_SECONDS,
    failure_of,
    opened,
    pcm,
    receive,
    sentence_events,
)
from video_client.session_wire import (
    ErrorEvent,
    OutputAudioBufferCleared,
    OutputAudioBufferStarted,
    OutputAudioBufferStopped,
    SessionCreated,
    SessionUpdated,
)
from video_client.wire import RefusalCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient
    from starlette.testclient import WebSocketTestSession

    from support.scripted_socket import ScriptedSocket
    from video_service.runtime import Runtime

_LONG_MS = 10_000
_START_UP_SECONDS = 3.0
_SENTENCE_MS = 3840
_ONE_FRAME_MS = 40
_CLEAR_AFTER_SECONDS = 1.5
_AFTER_THE_CLEAR_SECONDS = 2.0
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
    """WIRE-6."""
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
    """WIRE-6."""
    with opened(client) as session:
        _queue(session, "playing", _LONG_MS)
        started = receive(session)
        assert isinstance(started, OutputAudioBufferStarted)
        frame_rate = client.get("/health").json()["provider"]["video"]["frame_rate"]

        session.send_json(_CLEAR)
        (playing,) = _cleared(session, 1)

    assert 0 <= playing.audio_end_ms < _LONG_MS
    assert playing.audio_end_ms % (1000 // frame_rate) == 0


def test_a_cleared_sentence_never_stops_and_the_provider_forgets_it(
    client: TestClient, provider: FakeProvider
) -> None:
    """WIRE-6."""
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


def test_a_sentence_cleared_before_any_audio_is_dropped_with_nothing_played(
    client: TestClient,
) -> None:
    """WIRE-6."""
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
    """WIRE-6."""
    with opened(client) as session:
        session.send_json(_CLEAR)
        _queue(session, "next", 80)
        first = receive(session)

    assert isinstance(first, OutputAudioBufferStarted)


def test_the_end_of_a_cleared_open_sentence_is_refused(client: TestClient) -> None:
    """WIRE-6."""
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
    """WIRE-6."""
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


def test_a_clear_while_a_chunk_renders_drops_every_frame_of_the_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WIRE-6."""
    clock = FakeClock()
    chunked_provider = chunked(clock)
    leaves = record_leaves(monkeypatch, clock)
    cleared_at: list[float] = []

    async def note_the_clear(_socket: object) -> None:
        cleared_at.append(clock.now())

    start, end = sentence_events("a")
    events = played(
        [
            waiting(clock, _START_UP_SECONDS),
            start,
            pcm(_SENTENCE_MS),
            end,
            waiting(clock, _CLEAR_AFTER_SECONDS),
            note_the_clear,
            _CLEAR,
            waiting(clock, _AFTER_THE_CLEAR_SECONDS),
        ],
        sentences=0,
        provider=chunked_provider,
        pacing=clock.pacing,
        worker=clock.worker(),
        max_lag_ms=ENOUGH_LAG_MS,
    )

    (clear,) = cleared_at
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert any(leave.speech and leave.at < clear for leave in leaves)
    assert not any(leave.speech and leave.at >= clear for leave in leaves)
    assert chunked_provider.sessions[0].clears == 1
    resumed = min(leave.at for leave in leaves if not leave.speech and leave.at > clear)
    assert resumed - clear <= ENOUGH_LAG_MS / 1000


def test_a_clear_queued_in_front_of_a_stop_leaves_neither_stop_nor_error() -> None:
    """WIRE-6."""
    clock = FakeClock()
    start, end = sentence_events("last")

    async def when_the_start_is_held(socket: ScriptedSocket) -> None:
        await socket.held.wait()

    socket = scripted(
        [
            start,
            pcm(_ONE_FRAME_MS),
            end,
            when_the_start_is_held,
            _CLEAR,
            waiting(clock, _AFTER_THE_CLEAR_SECONDS),
        ],
        sentences=0,
    )
    socket.hold(OutputAudioBufferStarted, until_received=_CLEAR["type"])

    events = play(
        socket,
        provider=FakeProvider(),
        pacing=clock.pacing,
        worker=clock.worker(),
        max_lag_ms=ENOUGH_LAG_MS,
    )

    assert [type(event) for event in events] == [
        SessionCreated,
        SessionUpdated,
        OutputAudioBufferStarted,
        OutputAudioBufferCleared,
    ]
    assert isinstance(events[-1], OutputAudioBufferCleared)
    assert events[-1].response_id == "last"
