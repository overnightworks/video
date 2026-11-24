"""A viewer's offer gets one stream: Opus and VP8, idle between sentences."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING

from aiortc.sdp import SessionDescription

from support.session_support import ICE_SERVERS
from support.viewer_support import (
    VIDEO_TICKS_PER_FRAME,
    Service,
    Watcher,
    beep,
)
from video_client.wire import SDP_CONTENT_TYPE, viewers_path

if TYPE_CHECKING:
    from collections.abc import Callable

_FRAMES = 12
_SENTENCE_MS = 200
_LONG_MS = 20_000


def test_an_offer_from_the_opener_is_answered_with_every_candidate_and_a_location(
    live: Callable[..., None],
) -> None:
    """WIRE-10."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()
        watcher = Watcher()

        response = await service.post(session, await watcher.offer())

        assert response.status_code == HTTPStatus.CREATED
        assert response.headers["Content-Type"] == SDP_CONTENT_TYPE
        assert response.headers["Location"].startswith(
            f"{viewers_path(session.session_id)}/"
        )
        assert "a=candidate:" in response.text
        assert "a=end-of-candidates" in response.text
        await watcher.close()

    live(scenario)


def test_a_viewer_of_a_silent_session_gets_dark_frames_at_the_frame_rate_and_silence(
    live: Callable[..., None],
) -> None:
    """WIRE-10."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()

        watcher = await service.watch(session)
        await watcher.received.until(lambda got: len(got.video_pts) >= _FRAMES)
        await watcher.received.until(lambda got: len(got.loud) >= _FRAMES)

        received = watcher.received
        steps = {
            later - earlier
            for earlier, later in zip(
                received.video_pts, received.video_pts[1:], strict=False
            )
        }
        assert steps == {VIDEO_TICKS_PER_FRAME}
        assert not any(received.bright)
        assert not any(received.loud)

    live(scenario)


def test_a_beep_reaches_the_viewer_as_sound_and_a_bright_frame_then_idle_returns(
    live: Callable[..., None],
) -> None:
    """WIRE-10."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()
        watcher = await service.watch(session)
        got = watcher.received
        await got.until(lambda seen: len(seen.video_pts) >= _FRAMES)

        await session.speak("beep", beep(_SENTENCE_MS))
        await got.until(lambda seen: any(seen.bright) and any(seen.loud))
        await got.until(lambda seen: not any(seen.bright[-_FRAMES:]))
        await got.until(lambda seen: not any(seen.loud[-_FRAMES:]))

    live(scenario)


def test_the_idle_picture_and_silence_follow_a_clear(live: Callable[..., None]) -> None:
    """WIRE-10."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()
        watcher = await service.watch(session)
        got = watcher.received
        await session.speak("long", beep(_LONG_MS))
        await got.until(lambda seen: any(seen.bright) and any(seen.loud))

        await session.socket.send(json.dumps({"type": "output_audio_buffer.clear"}))
        await got.until(lambda seen: not any(seen.bright[-_FRAMES:]))
        await got.until(lambda seen: not any(seen.loud[-_FRAMES:]))

    live(scenario)


def _answer_of(response_text: str) -> SessionDescription:
    return SessionDescription.parse(response_text)


def test_an_offer_listing_h264_first_is_answered_with_opus_and_vp8_in_one_stream(
    live: Callable[..., None],
) -> None:
    """WIRE-10."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()
        watcher = Watcher()
        watcher.prefer("video", ["video/H264", "video/VP8"])

        response = await service.post(session, await watcher.offer())

        answer = _answer_of(response.text)
        codecs = {
            section.kind: {codec.mimeType.lower() for codec in section.rtp.codecs}
            for section in answer.media
        }
        assert codecs == {"audio": {"audio/opus"}, "video": {"video/vp8"}}
        assert len({section.msid.split()[0] for section in answer.media}) == 1
        assert (
            len({ssrc.cname for section in answer.media for ssrc in section.ssrc}) == 1
        )
        await watcher.close()

    live(scenario)


def test_a_relay_policy_gathers_no_host_or_server_reflexive_candidate(
    live: Callable[..., None],
) -> None:
    """WIRE-10."""

    async def scenario(service: Service) -> None:
        session = await service.open_session(update=False)
        await session.update(ICE_SERVERS, "relay")
        watcher = Watcher()

        response = await service.post(session, await watcher.offer())

        assert response.status_code == HTTPStatus.CREATED
        assert " typ host" not in response.text
        assert " typ srflx" not in response.text
        await watcher.close()

    live(scenario)
