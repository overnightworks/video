"""A provider that falls behind real time ends the session (WIRE-7)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from video_provider_fake import FakeProvider

from support.clock_support import FakeClock
from support.scripted_socket import ScriptedSocket
from support.session_support import (
    EVENT_WITHIN_SECONDS,
    pcm,
    sentence_events,
    update_event,
)
from video_client.session_wire import ErrorEvent, ServerEvent
from video_client.wire import RefusalCode, VideoFormat
from video_service.runtime import Pacing, Place, SessionLimits
from video_service.session import Session

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

_FRAME_MS = 40
_RENDER_SECONDS = 0.1
_MAX_LAG_MS = 150


def _played(
    script: list[dict[str, object] | bytes],
    sentences: int,
    *,
    provider: FakeProvider,
    pacing: Pacing,
    max_lag_ms: int,
) -> list[ServerEvent]:
    """The events a session sends while it plays `sentences` over `provider`."""
    place = Place(
        session=provider.open_session("mozart"),
        video=VideoFormat(width=256, height=256, frame_rate=25),
        limits=SessionLimits(max_queued_audio_seconds=30, max_lag_ms=max_lag_ms),
        pacing=pacing,
        draining=asyncio.Event(),
        release=lambda: None,
    )
    socket = ScriptedSocket([update_event(), *script], sentences)

    async def run() -> None:
        async with asyncio.timeout(EVENT_WITHIN_SECONDS):
            await Session(
                session_id="s",
                look="mozart",
                provider="fake",
                socket=cast("WebSocket", socket),
                place=place,
            ).run()

    asyncio.run(run())
    return socket.sent


def _sentences(*ids: str) -> list[dict[str, object] | bytes]:
    script: list[dict[str, object] | bytes] = []
    for sentence in ids:
        start, end = sentence_events(sentence)
        script += [start, pcm(_FRAME_MS), end]
    return script


def test_queued_audio_keeps_the_lag_the_frames_before_it_built_up() -> None:
    clock = FakeClock()
    provider = FakeProvider(render_seconds=_RENDER_SECONDS, sleep=clock.advance)

    events = _played(
        _sentences("a", "b"),
        sentences=2,
        provider=provider,
        pacing=clock.pacing,
        max_lag_ms=_MAX_LAG_MS,
    )

    failure = events[-1]
    assert isinstance(failure, ErrorEvent)
    assert failure.error.code is RefusalCode.UNAVAILABLE
