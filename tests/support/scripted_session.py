"""A session run to its end over a scripted client socket and a test's provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from aiortc import RTCPeerConnection

from support.scripted_socket import ScriptedSocket
from support.session_support import EVENT_WITHIN_SECONDS, update_event
from video_client.wire import VideoFormat
from video_service.callers import Opener
from video_service.runtime import Place, ProviderThread, SessionLimits
from video_service.session import Session

if TYPE_CHECKING:
    from starlette.websockets import WebSocket
    from video_provider_contract import Provider

    from support.scripted_socket import Step
    from video_client.session_wire import ServerEvent
    from video_service.runtime import Pacing

FRAME_RATE = 25


def scripted(
    script: list[dict[str, object] | bytes | Step], sentences: int
) -> ScriptedSocket:
    """A client that opens with a valid update, then plays `script`."""
    return ScriptedSocket([update_event(), *script], sentences)


def play(
    socket: ScriptedSocket,
    *,
    provider: Provider,
    pacing: Pacing,
    max_lag_ms: int,
    worker: ProviderThread | None = None,
) -> list[ServerEvent]:
    """The events a session sends over `socket` once it has run to its end.

    The provider is called on `worker`, a thread of its own unless a test hands one.
    """
    place = Place(
        session=provider.open_session("mozart"),
        worker=worker or ProviderThread(),
        video=VideoFormat(width=256, height=256, frame_rate=FRAME_RATE),
        limits=SessionLimits(
            max_queued_audio_seconds=30, max_lag_ms=max_lag_ms, max_viewers=1
        ),
        pacing=pacing,
        peers=RTCPeerConnection,
        draining=asyncio.Event(),
        end=lambda: None,
        release=lambda: None,
    )

    async def run() -> None:
        async with asyncio.timeout(EVENT_WITHIN_SECONDS):
            await Session(
                session_id="s",
                look="mozart",
                provider="fake",
                socket=cast("WebSocket", socket),
                place=place,
                opener=Opener("client", "subject"),
            ).run()

    asyncio.run(run())
    return socket.sent


def played(  # noqa: PLR0913 - the script, the provider and what a scenario varies of the session
    script: list[dict[str, object] | bytes | Step],
    sentences: int,
    *,
    provider: Provider,
    pacing: Pacing,
    max_lag_ms: int,
    worker: ProviderThread | None = None,
) -> list[ServerEvent]:
    """The events a session sends while it plays `sentences` over `provider`.

    The session ends once the script is spent and `sentences` have stopped.
    """
    return play(
        scripted(script, sentences),
        provider=provider,
        pacing=pacing,
        max_lag_ms=max_lag_ms,
        worker=worker,
    )
