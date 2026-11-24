"""Sentences and their playback and the failures that end a session."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from logline.testing import configured
from starlette.websockets import WebSocketDisconnect
from video_provider_fake import FakeProvider

from support.chunk_support import (
    FRAME_SECONDS,
    RENDER_SECONDS,
    gaps,
    play_chunked,
    record_leaves,
    waiting,
)
from support.clock_support import FakeClock
from support.scripted_session import FRAME_RATE
from support.session_support import (
    failure_of,
    next_error,
    opened,
    pcm,
    receive,
    sentence_events,
    written,
)
from video_client.session_wire import (
    MAX_EVENT_BYTES,
    OutputAudioBufferStarted,
    OutputAudioBufferStopped,
)
from video_client.wire import RefusalCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient
    from starlette.testclient import WebSocketTestSession

    from video_service.runtime import Runtime

_SHORT_MS = 80
_STALL_SECONDS = 1.0
_CHUNK_MS = 40
_PARTIAL_FRAME_MS = 50
_FRAME_SECONDS = 0.01
_START_UP_SECONDS = 3.0
_TWO_CHUNKS_MS = 1920
_LONG_MESSAGE_MS = 5_000


@pytest.fixture
def client(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> TestClient:
    return wire(started())


def _speak(
    session: WebSocketTestSession, sentence: str, milliseconds: int, rate: int = 16000
) -> None:
    start, end = sentence_events(sentence, rate)
    session.send_json(start)
    session.send_bytes(pcm(milliseconds, rate))
    session.send_json(end)


def _playback(
    session: WebSocketTestSession, sentences: int
) -> list[tuple[str, str, int | None]]:
    """The started and stopped events of `sentences` sentences: kind, id, audio end."""
    seen: list[tuple[str, str, int | None]] = []
    for _ in range(2 * sentences):
        event = receive(session)
        if isinstance(event, OutputAudioBufferStarted):
            seen.append(("started", event.response_id, None))
        else:
            assert isinstance(event, OutputAudioBufferStopped)
            seen.append(("stopped", event.response_id, event.audio_end_ms))
    return seen


@pytest.mark.parametrize("rate", [16000, 22050, 24000, 48000])
def test_a_sentence_starts_with_its_first_frame_and_stops_with_its_last(
    client: TestClient, rate: int
) -> None:
    """WIRE-5."""
    with opened(client) as session:
        _speak(session, "s1", _SHORT_MS, rate)

        playback = _playback(session, 1)

    assert playback == [("started", "s1", None), ("stopped", "s1", _SHORT_MS)]


def test_audio_in_several_frames_is_one_sentence(client: TestClient) -> None:
    """WIRE-5."""
    with opened(client) as session:
        start, end = sentence_events("s1")
        session.send_json(start)
        for _ in range(3):
            session.send_bytes(pcm(_SHORT_MS // 2))
        session.send_json(end)

        playback = _playback(session, 1)

    assert playback[-1] == ("stopped", "s1", 3 * _SHORT_MS // 2)


def test_sentences_play_in_order_back_to_back(client: TestClient) -> None:
    """WIRE-5."""
    with opened(client) as session:
        for sentence in ("a", "b", "c"):
            _speak(session, sentence, _SHORT_MS)

        playback = _playback(session, 3)

    assert [(kind, sentence) for kind, sentence, _ in playback] == [
        ("started", "a"),
        ("stopped", "a"),
        ("started", "b"),
        ("stopped", "b"),
        ("started", "c"),
        ("stopped", "c"),
    ]


def test_a_sentence_whose_audio_comes_late_is_not_a_provider_lag(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    """WIRE-7."""
    clock = FakeClock()
    client = wire(started(max_lag_ms=500, pacing=clock.pacing))
    with opened(client) as session:
        start, end = sentence_events("s1")
        session.send_json(start)
        session.send_bytes(pcm(_CHUNK_MS))
        assert isinstance(receive(session), OutputAudioBufferStarted)
        # The client's audio stalls for longer than the lag the service bounds.
        clock.advance(_STALL_SECONDS)
        session.send_bytes(pcm(_CHUNK_MS))
        session.send_json(end)

        stopped = receive(session)

    assert isinstance(stopped, OutputAudioBufferStopped)
    assert stopped.audio_end_ms == 2 * _CHUNK_MS


def test_a_sentence_that_the_client_ends_late_is_not_a_provider_lag(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    """WIRE-7."""
    clock = FakeClock()
    provider = FakeProvider(render_seconds=_FRAME_SECONDS, sleep=clock.advance)
    client = wire(started(provider, max_lag_ms=500, pacing=clock.pacing))
    with opened(client) as session:
        start, end = sentence_events("s1")
        session.send_json(start)
        session.send_bytes(pcm(_PARTIAL_FRAME_MS))
        assert isinstance(receive(session), OutputAudioBufferStarted)
        # The client takes longer to end the sentence than the lag the service bounds.
        clock.advance(_STALL_SECONDS)
        session.send_json(end)

        stopped = receive(session)

    assert isinstance(stopped, OutputAudioBufferStopped)
    assert stopped.audio_end_ms == _PARTIAL_FRAME_MS


@pytest.mark.parametrize(
    ("events", "param"),
    [
        ([sentence_events("a")[0]] * 2, "type"),
        ([sentence_events("a")[0], sentence_events("b")[1]], "sentence"),
        ([sentence_events("a")[1]], "sentence"),
        ([*sentence_events("a")], "sentence"),
        (
            [
                {
                    "type": "sentence.start",
                    "sentence": "a",
                    "format": {"type": "audio/pcm", "rate": 8000},
                }
            ],
            "format.rate",
        ),
        (
            [
                {
                    "type": "sentence.start",
                    "sentence": "a",
                    "format": {"type": "audio/wav", "rate": 16000},
                }
            ],
            "format.type",
        ),
        (
            [
                {
                    "type": "sentence.start",
                    "format": {"type": "audio/pcm", "rate": 16000},
                }
            ],
            "sentence",
        ),
    ],
    ids=[
        "second-start-while-open",
        "end-names-another-sentence",
        "end-without-start",
        "empty-sentence",
        "unsupported-rate",
        "unsupported-type",
        "no-sentence-id",
    ],
)
def test_a_sentence_event_out_of_order_or_not_valid_ends_the_session(
    client: TestClient, events: list[dict[str, object]], param: str
) -> None:
    """WIRE-5."""
    with opened(client) as session:
        for event in events:
            session.send_json(event)
        error = failure_of(receive(session))

    assert error.error.code is RefusalCode.INVALID_REQUEST
    assert error.error.param == param


def test_a_sentence_id_that_is_still_queued_cannot_start_again(
    client: TestClient,
) -> None:
    """WIRE-5."""
    with opened(client) as session:
        start, end = sentence_events("a")
        session.send_json(start)
        session.send_bytes(pcm(10_000))
        session.send_json(end)
        session.send_json(start)
        error = next_error(session)

    assert error.error.param == "sentence"


@pytest.mark.parametrize(
    "frame", [b"\x00\x00", b"\x00"], ids=["no-sentence", "odd-length"]
)
def test_audio_outside_an_open_sentence_or_of_an_odd_length_ends_the_session(
    client: TestClient, frame: bytes
) -> None:
    """WIRE-5."""
    with opened(client) as session:
        if len(frame) == 1:
            session.send_json(sentence_events("a")[0])
        session.send_bytes(frame)
        error = failure_of(receive(session))

    assert (error.error.code, error.error.param) == (
        RefusalCode.INVALID_REQUEST,
        "audio",
    )


def test_queued_audio_above_the_bound_ends_the_session_too_large(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    """WIRE-5."""
    client = wire(started(max_queued_audio_seconds=1))
    with opened(client) as session:
        session.send_json(sentence_events("a")[0])
        session.send_bytes(pcm(2000))
        error = failure_of(receive(session))
        with pytest.raises(WebSocketDisconnect):
            session.receive_text()

    assert error.error.code is RefusalCode.TOO_LARGE
    assert error.error.type == "invalid_request_error"


def test_audio_the_provider_already_played_no_longer_counts_as_queued(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    """WIRE-5."""
    client = wire(started(max_queued_audio_seconds=0.3))
    with opened(client) as session:
        for sentence in ("a", "b", "c"):
            _speak(session, sentence, 200)
            _playback(session, 1)

        _speak(session, "d", 200)
        playback = _playback(session, 1)

    assert playback[-1] == ("stopped", "d", 200)


def test_an_event_above_the_size_bound_ends_the_session_too_large(
    client: TestClient,
) -> None:
    """WIRE-5."""
    with opened(client) as session:
        session.send_json({"type": "sentence.start", "sentence": "x" * MAX_EVENT_BYTES})
        error = failure_of(receive(session))

    assert error.error.code is RefusalCode.TOO_LARGE


@pytest.mark.parametrize(
    "fails",
    [{"render_seconds": 0.2}, {"fails_to_draw": True}],
    ids=["behind-real-time", "draw-fails"],
)
def test_a_provider_that_falls_behind_or_fails_ends_the_session_unavailable(
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
    fails: dict[str, object],
) -> None:
    """WIRE-7."""
    clock = FakeClock()
    provider = FakeProvider(**fails, sleep=clock.advance)
    client = wire(started(provider, max_lag_ms=50, max_sessions=1, pacing=clock.pacing))
    with opened(client) as session:
        _speak(session, "a", _SHORT_MS)
        error = failure_of(receive(session))
        with pytest.raises(WebSocketDisconnect):
            session.receive_text()

    assert error.error.code is RefusalCode.UNAVAILABLE
    assert error.error.type == "server_error"
    with opened(client):
        pass


def test_a_provider_that_never_answers_ends_the_session_unavailable_and_frees_the_place(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    """WIRE-7."""
    held = threading.Event()
    client = wire(
        started(FakeProvider(finish_gate=held), max_lag_ms=50, max_sessions=1)
    )
    try:
        with opened(client) as session:
            _speak(session, "a", _CHUNK_MS)
            error = next_error(session)
            with pytest.raises(WebSocketDisconnect):
                session.receive_text()

        with opened(client):
            pass
    finally:
        held.set()

    assert error.error.code is RefusalCode.UNAVAILABLE


def test_a_client_that_leaves_while_a_sentence_plays_frees_the_place(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    """WIRE-5."""
    client = wire(started(max_sessions=1))
    with opened(client) as session:
        _speak(session, "a", 10_000)
        assert isinstance(receive(session), OutputAudioBufferStarted)

    with opened(client):
        pass


def test_chunked_sentences_back_to_back_leave_with_no_frame_between_and_none_late(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """WIRE-7."""
    clock = FakeClock()
    leaves = record_leaves(monkeypatch, clock)
    script = [waiting(clock, _START_UP_SECONDS)]
    for sentence in ("a", "b"):
        start, end = sentence_events(sentence)
        script += [start, pcm(_TWO_CHUNKS_MS), end]

    with configured("video"):
        play_chunked(clock, script, sentences=2)

    lines = written(capsys.readouterr().out, "sentence.ended")
    kinds = [leave.speech for leave in leaves if leave.at >= _START_UP_SECONDS]
    assert [line["late_frame_count"] for line in lines] == [0, 0]
    assert kinds == sorted(kinds)
    assert sum(kinds) == 2 * _TWO_CHUNKS_MS * FRAME_RATE // 1000
    assert gaps(leaves, since=_START_UP_SECONDS) == pytest.approx(
        [FRAME_SECONDS] * len(gaps(leaves, since=_START_UP_SECONDS))
    )


def test_a_message_of_any_length_never_makes_a_call_longer_than_one_render() -> None:
    """WIRE-7."""
    clock = FakeClock()
    worker = clock.worker()
    start, end = sentence_events("long")

    events = play_chunked(
        clock,
        [waiting(clock, _START_UP_SECONDS), start, pcm(_LONG_MESSAGE_MS), end],
        sentences=1,
        worker=worker,
    )

    stopped = events[-1]
    assert isinstance(stopped, OutputAudioBufferStopped)
    assert stopped.audio_end_ms == _LONG_MESSAGE_MS
    assert worker.longest_call == RENDER_SECONDS
