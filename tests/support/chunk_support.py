"""A provider that renders in chunks longer than a frame period, and its frames."""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, NamedTuple

from video_provider_fake import FakeProvider

from support.scripted_session import played
from video_service.media import Media

if TYPE_CHECKING:
    import pytest

    from support.clock_support import ClockedWorker, FakeClock
    from support.scripted_socket import ScriptedSocket, Step
    from video_client.session_wire import ServerEvent

FRAME_SECONDS = 0.04
CHUNK_FRAMES = 24
RENDER_SECONDS = 0.52
ENOUGH_LAG_MS = 1200
TOO_LITTLE_LAG_MS = 1000


def chunked(clock: FakeClock) -> FakeProvider:
    """A provider that draws a chunk of frames in a render, the face at rest too."""
    return FakeProvider(
        chunk_frames=CHUNK_FRAMES,
        render_seconds=RENDER_SECONDS,
        idle_render_seconds=RENDER_SECONDS,
        sleep=clock.render,
    )


class Leave(NamedTuple):
    """A frame that left for the viewers, when and whether it was a sentence's."""

    at: float
    speech: bool


def record_leaves(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> list[Leave]:
    """Every frame the sessions of this test let leave, in order, by `clock`."""
    leaves: list[Leave] = []
    send, send_idle = Media.send, Media.send_idle

    def note_speech(media: Media, *arguments: object) -> None:
        leaves.append(Leave(clock.now(), speech=True))
        send(media, *arguments)

    def note_idle(media: Media, *arguments: object) -> None:
        leaves.append(Leave(clock.now(), speech=False))
        send_idle(media, *arguments)

    monkeypatch.setattr(Media, "send", note_speech)
    monkeypatch.setattr(Media, "send_idle", note_idle)
    return leaves


def play_chunked(
    clock: FakeClock,
    script: list[dict[str, object] | bytes | Step],
    sentences: int,
    *,
    max_lag_ms: int = ENOUGH_LAG_MS,
    worker: ClockedWorker | None = None,
) -> list[ServerEvent]:
    """The events of a session over the chunked provider, run on `clock`."""
    return played(
        script,
        sentences,
        provider=chunked(clock),
        pacing=clock.pacing,
        worker=worker or clock.worker(),
        max_lag_ms=max_lag_ms,
    )


def waiting(clock: FakeClock, seconds: float) -> Step:
    """A step of a script that lets `seconds` pass on `clock` before the next one."""

    async def wait(_socket: ScriptedSocket) -> None:
        await clock.sleep(seconds)

    return wait


def gaps(leaves: list[Leave], since: float) -> list[float]:
    """The time between each frame that left from `since` and the one before it."""
    times = [leave.at for leave in leaves if leave.at >= since]
    return [later - earlier for earlier, later in pairwise(times)]
