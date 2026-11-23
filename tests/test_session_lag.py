"""A provider that falls behind real time ends the session (WIRE-7)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from logline.testing import configured
from video_provider_fake import FakeProvider

from support.chunk_support import (
    ENOUGH_LAG_MS,
    TOO_LITTLE_LAG_MS,
    play_chunked,
    waiting,
)
from support.clock_support import FakeClock
from support.scripted_session import played
from support.session_support import (
    pcm,
    sentence_events,
    written,
)
from video_client.session_wire import (
    ErrorEvent,
    OutputAudioBufferStopped,
)
from video_client.wire import RefusalCode
from video_service.runtime import REAL_TIME

if TYPE_CHECKING:
    from support.scripted_socket import ScriptedSocket

_FRAME_MS = 40
_RENDER_SECONDS = 0.118
_MAX_LAG_MS = 150
_PARTIAL_FRAME_MS = 50
_END_DURING_FRAME_SECONDS = 0.09
_SLOW_CLIENT_MAX_LAG_MS = 1000
_CALL_SECONDS = 0.3
_CALL_LAG_MS = 100
_CLEAR = {"type": "output_audio_buffer.clear"}
_CHUNK_AUDIO_MS = 1920
# The audio arrives while the provider draws no chunk and between two frames of
# the face at rest, so its render starts at once, and the clear follows as it begins.
_AUDIO_AFTER_IDLE_SECONDS = 0.54
_CLEAR_DURING_FIRST_RENDER_SECONDS = 0.01
_AFTER_THE_FACE_AT_REST_SECONDS = 2.0


def _sentences(*ids: str) -> list[dict[str, object] | bytes]:
    script: list[dict[str, object] | bytes] = []
    for sentence in ids:
        start, end = sentence_events(sentence)
        script += [start, pcm(_FRAME_MS), end]
    return script


def test_queued_audio_keeps_the_lag_the_frames_before_it_built_up() -> None:
    clock = FakeClock()
    provider = FakeProvider(render_seconds=_RENDER_SECONDS, sleep=clock.render)

    events = played(
        _sentences("a", "b"),
        sentences=2,
        provider=provider,
        pacing=clock.pacing,
        worker=clock.worker(),
        max_lag_ms=_MAX_LAG_MS,
    )

    failure = events[-1]
    assert isinstance(failure, ErrorEvent)
    assert failure.error.code is RefusalCode.UNAVAILABLE


def test_a_sentence_ended_while_its_first_frame_draws_keeps_that_frames_lag() -> None:
    clock = FakeClock()
    start, end = sentence_events("a")
    events = played(
        [
            start,
            pcm(_PARTIAL_FRAME_MS),
            waiting(clock, _END_DURING_FRAME_SECONDS),
            end,
        ],
        sentences=1,
        provider=FakeProvider(render_seconds=_RENDER_SECONDS, sleep=clock.render),
        pacing=clock.pacing,
        worker=clock.worker(),
        max_lag_ms=_MAX_LAG_MS,
    )

    failure = events[-1]
    assert isinstance(failure, ErrorEvent)
    assert failure.error.code is RefusalCode.UNAVAILABLE


def test_a_client_that_ends_a_sentence_after_its_frames_left_adds_no_lag() -> None:
    clock = FakeClock()

    async def stall_once_playing(socket: ScriptedSocket) -> None:
        await socket.started.wait()
        clock.advance(2 * _SLOW_CLIENT_MAX_LAG_MS / 1000)

    start, end = sentence_events("a")
    events = played(
        [start, pcm(_FRAME_MS), stall_once_playing, end],
        sentences=1,
        provider=FakeProvider(),
        pacing=clock.pacing,
        max_lag_ms=_SLOW_CLIENT_MAX_LAG_MS,
    )

    stopped = events[-1]
    assert isinstance(stopped, OutputAudioBufferStopped)
    assert stopped.audio_end_ms == _FRAME_MS


@pytest.mark.parametrize(
    ("max_lag_ms", "ends_unavailable"),
    [(ENOUGH_LAG_MS, False), (TOO_LITTLE_LAG_MS, True)],
    ids=["above-the-worst-case", "below-the-worst-case"],
)
def test_a_clear_that_waits_for_two_renders_ends_the_session_only_below_the_worst_case(
    max_lag_ms: int, *, ends_unavailable: bool
) -> None:
    clock = FakeClock()
    start, end = sentence_events("a")

    events = play_chunked(
        clock,
        [
            waiting(clock, _AUDIO_AFTER_IDLE_SECONDS),
            start,
            pcm(_CHUNK_AUDIO_MS),
            end,
            waiting(clock, _CLEAR_DURING_FIRST_RENDER_SECONDS),
            _CLEAR,
            waiting(clock, _AFTER_THE_FACE_AT_REST_SECONDS),
        ],
        sentences=0,
        max_lag_ms=max_lag_ms,
    )

    failed = isinstance(events[-1], ErrorEvent)
    assert failed is ends_unavailable


@pytest.mark.parametrize(
    ("render_seconds", "fails_to_draw", "max_lag_ms", "ended_by"),
    [
        (_RENDER_SECONDS, False, _MAX_LAG_MS, "late_frame"),
        (_CALL_SECONDS, False, _CALL_LAG_MS, "call_timeout"),
        (0.0, True, _MAX_LAG_MS, "provider_failed"),
    ],
    ids=["a-frame-past-the-limit", "a-call-past-the-limit", "the-provider-failing"],
)
def test_a_session_ended_unavailable_names_the_path_that_ended_it(
    render_seconds: float,
    max_lag_ms: int,
    capsys: pytest.CaptureFixture[str],
    *,
    fails_to_draw: bool,
    ended_by: str,
) -> None:
    clock = FakeClock()
    # Only a frame's lateness runs on the fake clock; a call's timeout is real time.
    on_fake_clock = ended_by == "late_frame"
    provider = FakeProvider(
        render_seconds=render_seconds,
        fails_to_draw=fails_to_draw,
        sleep=clock.render if on_fake_clock else time.sleep,
    )

    with configured("video"):
        played(
            _sentences("a", "b"),
            sentences=2,
            provider=provider,
            pacing=clock.pacing if on_fake_clock else REAL_TIME,
            worker=clock.worker() if on_fake_clock else None,
            max_lag_ms=max_lag_ms,
        )

    (line,) = written(capsys.readouterr().out, "session.ended")
    assert line["ended_by"] == ended_by
    if ended_by == "late_frame":
        assert line["max_behind_ms"] > max_lag_ms


def test_an_idle_call_that_times_out_is_not_counted_in_the_longest_idle_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A call's timeout is real time, so the call sleeps in real time too.
    provider = FakeProvider(idle_render_seconds=_CALL_SECONDS, sleep=time.sleep)

    with configured("video"):
        played(
            [],
            sentences=0,
            provider=provider,
            pacing=REAL_TIME,
            max_lag_ms=_CALL_LAG_MS,
        )

    (line,) = written(capsys.readouterr().out, "session.ended")
    assert line["ended_by"] == "call_timeout"
    assert line["max_idle_call_ms"] == 0


def test_a_late_frame_within_the_limit_is_logged_as_the_most_a_frame_fell_behind(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = FakeClock()
    provider = FakeProvider(render_seconds=_RENDER_SECONDS, sleep=clock.render)

    with configured("video"):
        played(
            _sentences("a", "b"),
            sentences=2,
            provider=provider,
            pacing=clock.pacing,
            worker=clock.worker(),
            max_lag_ms=_SLOW_CLIENT_MAX_LAG_MS,
        )

    (line,) = written(capsys.readouterr().out, "session.ended")
    assert "ended_by" not in line
    assert 0 < line["max_behind_ms"] <= _SLOW_CLIENT_MAX_LAG_MS
