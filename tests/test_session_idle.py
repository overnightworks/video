"""While no sentence plays, the provider's idle frame is drawn on the frame clock."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from video_provider_fake import FakeProvider

from support.clock_support import FakeClock
from support.scripted_session import FRAME_RATE, played
from support.session_support import EVENT_WITHIN_SECONDS
from video_client.session_wire import ErrorEvent
from video_client.wire import RefusalCode
from video_service.runtime import Pacing

if TYPE_CHECKING:
    from support.scripted_socket import ScriptedSocket

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
