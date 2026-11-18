"""A provider that falls behind real time or never answers ends the session (WIRE-7)."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, NamedTuple, cast

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
from video_service.runtime import REAL_TIME, Pacing, Place, SessionLimits
from video_service.session import Session

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

_FRAME_MS = 40
_RENDER_SECONDS = 0.1
_MAX_LAG_MS = 150
_STUCK_LAG_MS = 50


class _Played(NamedTuple):
    events: list[ServerEvent]
    released: bool


def _played(
    script: list[dict[str, object] | bytes],
    sentences: int,
    *,
    provider: FakeProvider,
    pacing: Pacing,
    max_lag_ms: int,
) -> _Played:
    """What a session plays of `sentences` over `provider`, and whether it let go."""
    released: list[bool] = []
    place = Place(
        session=provider.open_session("mozart"),
        video=VideoFormat(width=256, height=256, frame_rate=25),
        limits=SessionLimits(max_queued_audio_seconds=30, max_lag_ms=max_lag_ms),
        pacing=pacing,
        draining=asyncio.Event(),
        release=lambda: released.append(True),
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
    return _Played(socket.sent, bool(released))


def _sentences(*ids: str) -> list[dict[str, object] | bytes]:
    script: list[dict[str, object] | bytes] = []
    for sentence in ids:
        start, end = sentence_events(sentence)
        script += [start, pcm(_FRAME_MS), end]
    return script


def test_queued_audio_keeps_the_lag_the_frames_before_it_built_up() -> None:
    clock = FakeClock()
    provider = FakeProvider(render_seconds=_RENDER_SECONDS, sleep=clock.advance)

    played = _played(
        _sentences("a", "b"),
        sentences=2,
        provider=provider,
        pacing=clock.pacing,
        max_lag_ms=_MAX_LAG_MS,
    )

    failure = played.events[-1]
    assert isinstance(failure, ErrorEvent)
    assert failure.error.code is RefusalCode.UNAVAILABLE


def test_a_provider_that_never_answers_ends_the_session_and_lets_go_of_its_place() -> (
    None
):
    held = threading.Event()
    try:
        played = _played(
            _sentences("a"),
            sentences=1,
            provider=FakeProvider(finish_gate=held),
            pacing=REAL_TIME,
            max_lag_ms=_STUCK_LAG_MS,
        )
    finally:
        held.set()

    failure = played.events[-1]
    assert isinstance(failure, ErrorEvent)
    assert failure.error.code is RefusalCode.UNAVAILABLE
    assert played.released
