"""While no sentence plays, the provider's idle frame is drawn on the frame clock."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from video_provider_fake import FakeProvider

from support.clock_support import FakeClock
from support.scripted_session import FRAME_RATE, play, played, scripted
from support.session_support import EVENT_WITHIN_SECONDS, sentence_events
from video_client.session_wire import ErrorEvent, SessionCreated, SessionUpdated
from video_client.wire import RefusalCode
from video_service.runtime import Pacing

if TYPE_CHECKING:
    from support.scripted_socket import ScriptedSocket
    from video_client.session_wire import ServerEvent

_FRAME_SECONDS = 1 / FRAME_RATE
_IDLE_FRAMES = 6
_IDLE_RENDER_SECONDS = 0.1
_MAX_LAG_MS = 150


def test_idle_frames_leave_one_frame_period_apart_while_the_session_is_open() -> None:
    clock = FakeClock()
    left_at: list[float] = []
    enough = asyncio.Event()

    async def note_each_frame_leaving(seconds: float) -> None:
        await clock.sleep(seconds)
        left_at.append(clock.now())
        if len(left_at) == _IDLE_FRAMES:
            enough.set()

    async def once_idle_frames_have_left(_socket: ScriptedSocket) -> None:
        async with asyncio.timeout(EVENT_WITHIN_SECONDS):
            await enough.wait()

    played(
        [once_idle_frames_have_left],
        sentences=0,
        provider=FakeProvider(),
        pacing=Pacing(now=clock.now, sleep=note_each_frame_leaving),
        max_lag_ms=_MAX_LAG_MS,
    )

    assert left_at[:_IDLE_FRAMES] == pytest.approx(
        [step * _FRAME_SECONDS for step in range(_IDLE_FRAMES)]
    )


def test_idle_frames_keep_leaving_while_a_sentence_lacks_audio_for_a_frame() -> None:
    clock = FakeClock()
    left = asyncio.Condition()
    frames_left = 0

    async def note_each_frame_leaving(seconds: float) -> None:
        nonlocal frames_left
        await clock.sleep(seconds)
        async with left:
            frames_left += 1
            left.notify_all()

    async def once_idle_frames_have_left_after_the_short_audio(
        _socket: ScriptedSocket,
    ) -> None:
        async with asyncio.timeout(EVENT_WITHIN_SECONDS), left:
            expected = frames_left + _IDLE_FRAMES
            await left.wait_for(lambda: frames_left >= expected)

    start, end = sentence_events("short")
    one_sample = bytes(2)

    played(
        [start, one_sample, once_idle_frames_have_left_after_the_short_audio, end],
        sentences=1,
        provider=FakeProvider(),
        pacing=Pacing(now=clock.now, sleep=note_each_frame_leaving),
        max_lag_ms=_MAX_LAG_MS,
    )


def test_a_late_idle_frame_ends_a_silent_session_unavailable() -> None:
    clock = FakeClock()
    provider = FakeProvider(
        idle_render_seconds=_IDLE_RENDER_SECONDS, sleep=clock.advance
    )

    events = played(
        [], sentences=1, provider=provider, pacing=clock.pacing, max_lag_ms=_MAX_LAG_MS
    )

    failure = events[-1]
    assert isinstance(failure, ErrorEvent)
    assert failure.error.code is RefusalCode.UNAVAILABLE


def test_the_session_is_updated_only_after_the_first_idle_frame_is_drawn() -> None:
    clock = FakeClock()
    socket = scripted([], sentences=0)
    sent_while_drawing: list[ServerEvent] = []

    def hold_the_first_idle_frame(_seconds: float) -> None:
        if not sent_while_drawing:
            sent_while_drawing.extend(socket.sent)

    events = play(
        socket,
        provider=FakeProvider(
            idle_render_seconds=_IDLE_RENDER_SECONDS, sleep=hold_the_first_idle_frame
        ),
        pacing=clock.pacing,
        max_lag_ms=_MAX_LAG_MS * 1000,
    )

    assert [type(event) for event in sent_while_drawing] == [SessionCreated]
    assert any(isinstance(event, SessionUpdated) for event in events)
