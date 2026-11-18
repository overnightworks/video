"""A provider that falls behind real time ends the session (WIRE-7)."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from video_provider_fake import FakeProvider

from support.clock_support import FakeClock
from support.scripted_session import played
from support.session_support import (
    EVENT_WITHIN_SECONDS,
    pcm,
    sentence_events,
)
from video_client.session_wire import (
    ErrorEvent,
    OutputAudioBufferStopped,
)
from video_client.wire import RefusalCode

if TYPE_CHECKING:
    from support.scripted_socket import ScriptedSocket

_FRAME_MS = 40
_RENDER_SECONDS = 0.118
_MAX_LAG_MS = 150
_PARTIAL_FRAME_MS = 50
_END_DURING_FRAME_SECONDS = 0.09
_SLOW_CLIENT_MAX_LAG_MS = 1000


def _sentences(*ids: str) -> list[dict[str, object] | bytes]:
    script: list[dict[str, object] | bytes] = []
    for sentence in ids:
        start, end = sentence_events(sentence)
        script += [start, pcm(_FRAME_MS), end]
    return script


def test_queued_audio_keeps_the_lag_the_frames_before_it_built_up() -> None:
    clock = FakeClock()
    provider = FakeProvider(render_seconds=_RENDER_SECONDS, sleep=clock.advance)

    events = played(
        _sentences("a", "b"),
        sentences=2,
        provider=provider,
        pacing=clock.pacing,
        max_lag_ms=_MAX_LAG_MS,
    )

    failure = events[-1]
    assert isinstance(failure, ErrorEvent)
    assert failure.error.code is RefusalCode.UNAVAILABLE


def test_a_sentence_ended_while_its_first_frame_draws_keeps_that_frames_lag() -> None:
    clock = FakeClock()
    drawing = threading.Event()
    ended = threading.Event()

    def draw(seconds: float) -> None:
        if drawing.is_set():
            clock.advance(seconds)
            return
        clock.advance(_END_DURING_FRAME_SECONDS)
        drawing.set()
        ended.wait(EVENT_WITHIN_SECONDS)
        clock.advance(seconds - _END_DURING_FRAME_SECONDS)

    async def while_the_first_frame_draws(_socket: ScriptedSocket) -> None:
        await asyncio.to_thread(drawing.wait, EVENT_WITHIN_SECONDS)

    async def once_ended(_socket: ScriptedSocket) -> None:
        ended.set()

    start, end = sentence_events("a")
    events = played(
        [
            start,
            pcm(_PARTIAL_FRAME_MS),
            while_the_first_frame_draws,
            end,
            once_ended,
        ],
        sentences=1,
        provider=FakeProvider(render_seconds=_RENDER_SECONDS, sleep=draw),
        pacing=clock.pacing,
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
