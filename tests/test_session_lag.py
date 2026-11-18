"""The lag of a provider that falls behind real time (WIRE-7), on a moved clock."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from video_provider_fake import FakeProvider

from support.clock_support import FakeClock
from support.scripted_socket import ScriptedSocket
from support.session_support import pcm, sentence_events, update_event
from video_client.session_wire import ErrorEvent, ServerEvent
from video_client.wire import RefusalCode, VideoFormat
from video_service.runtime import Place, SessionLimits
from video_service.session import Session

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

_FRAME_MS = 40
_RENDER_SECONDS = 0.1
_MAX_LAG_MS = 150


def _played(
    script: list[dict[str, object] | bytes], sentences: int
) -> list[ServerEvent]:
    """The events of a session that plays `sentences` on a provider slower than real."""
    clock = FakeClock()
    provider = FakeProvider(render_seconds=_RENDER_SECONDS, sleep=clock.advance)
    place = Place(
        session=provider.open_session("mozart"),
        video=VideoFormat(width=256, height=256, frame_rate=25),
        limits=SessionLimits(max_queued_audio_seconds=30, max_lag_ms=_MAX_LAG_MS),
        pacing=clock.pacing,
        draining=asyncio.Event(),
        release=lambda: None,
    )
    socket = ScriptedSocket([update_event(), *script], sentences)

    async def run() -> None:
        await Session(
            session_id="s",
            look="mozart",
            provider="fake",
            socket=cast("WebSocket", socket),
            place=place,
        ).run()

    asyncio.run(run())
    return socket.sent


def test_queued_audio_keeps_the_lag_the_frames_before_it_built_up() -> None:
    script: list[dict[str, object] | bytes] = []
    for sentence in ("a", "b"):
        start, end = sentence_events(sentence)
        script += [start, pcm(_FRAME_MS), end]

    events = _played(script, sentences=2)

    failure = events[-1]
    assert isinstance(failure, ErrorEvent)
    assert failure.error.code is RefusalCode.UNAVAILABLE
