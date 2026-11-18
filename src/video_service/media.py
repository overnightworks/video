"""The media of one session: a peer with an Opus and a VP8 track for each viewer.

Every frame period of the session's frame clock hands one picture and its audio to
`Media`, which gives each connected viewer its own frames: each viewer's peer
encodes for itself, so a slow viewer never holds back another. A viewer's two
tracks leave in one stream with one CNAME, and both are stamped from the frame
count, so the browser keeps sound and picture together. The peers gather every
candidate before the answer is returned (no trickle) and use only the ICE servers
the session was given.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.rtcconfiguration import RTCIceTransportPolicy
from aiortc.sdp import SessionDescription
from av import AudioFrame, AudioResampler, VideoFrame

from video_client.wire import RefusalCode

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from av.frame import Frame as MediaFrame
    from video_provider_contract import Frame

    from video_client.session_wire import IceConfiguration
    from video_client.wire import VideoFormat

# aioice writes candidate pairs, addresses included, at INFO, and the tracebacks aiortc
# warns with can name peers; whatever LOG_LEVEL is, neither writes a line.
for _library in ("aioice", "aiortc"):
    logging.getLogger(_library).setLevel(logging.ERROR)

_AUDIO: Final = "audio"
_VIDEO: Final = "video"
_CODEC: Final = {_AUDIO: "audio/opus", _VIDEO: "video/vp8"}
_RECVONLY: Final = "recvonly"
_AUDIO_RATE: Final = 48_000
_VIDEO_CLOCK_RATE: Final = 90_000
_BYTES_PER_SAMPLE: Final = 2
_BYTES_PER_PIXEL: Final = 3

type PeerFactory = Callable[[RTCConfiguration], RTCPeerConnection]
"""Builds a viewer's peer from its ICE configuration."""


class ViewerEnd(StrEnum):
    """Why a viewer ended."""

    DELETED = "deleted"
    PEER_CLOSED = "peer_closed"
    SESSION_ENDED = "session_ended"


@dataclass(frozen=True, slots=True)
class Offer:
    """A viewer's SDP offer, which carries addresses and fingerprints."""

    sdp: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Admission:
    """An admitted viewer: its id and the answer, which holds every candidate."""

    viewer_id: str
    answer: str = field(repr=False)


def parse_offer(body: bytes) -> Offer | None:
    """The offer in `body`; `None` unless it is recvonly with one Opus and one VP8."""
    try:
        text = body.decode()
        description = SessionDescription.parse(text)
    except Exception:  # noqa: BLE001 - the parser fails with whatever a bad line trips
        return None
    sections = {section.kind: section for section in description.media}
    if len(description.media) != len(_CODEC) or sections.keys() != _CODEC.keys():
        return None
    for kind, section in sections.items():
        offered = {codec.mimeType.lower() for codec in section.rtp.codecs}
        if section.direction != _RECVONLY or _CODEC[kind] not in offered:
            return None
    return Offer(text)


class _FeedTrack(MediaStreamTrack):
    """A track that hands its peer's sender the frames pushed to it.

    The bound keeps a viewer whose encoder stalls from holding frames without end;
    the oldest frame goes first.
    """

    def __init__(self, kind: str, depth: int) -> None:
        super().__init__()
        self.kind = kind
        self._frames: deque[MediaFrame] = deque(maxlen=depth)
        self._arrived = asyncio.Event()

    def feed(self, frame: MediaFrame) -> None:
        self._frames.append(frame)
        self._arrived.set()

    async def recv(self) -> MediaFrame:
        if self.readyState != "live":
            raise MediaStreamError
        while not self._frames:
            self._arrived.clear()
            await self._arrived.wait()
        return self._frames.popleft()


class _Viewer:
    """One viewer's peer, tracks and end."""

    def __init__(
        self,
        peer: RTCPeerConnection,
        depth: int,
        on_peer_closed: Callable[[], None],
    ) -> None:
        self._peer = peer
        self._audio = _FeedTrack(_AUDIO, depth)
        self._video = _FeedTrack(_VIDEO, depth)
        self.connected = False
        self.ended: asyncio.Future[ViewerEnd] = (
            asyncio.get_running_loop().create_future()
        )
        self._on_peer_closed = on_peer_closed
        # The codecs are chosen when the offer is set, so the peer takes them first.
        for track in (self._audio, self._video):
            transceiver = self._peer.addTransceiver(track, direction="sendonly")
            transceiver.setCodecPreferences(
                [
                    codec
                    for codec in RTCRtpSender.getCapabilities(track.kind).codecs
                    if codec.mimeType.lower() == _CODEC[track.kind]
                ]
            )
        self._peer.on("connectionstatechange", self._on_state)

    def _on_state(self) -> None:
        state = self._peer.connectionState
        self.connected = state == "connected"
        if state in {"failed", "closed"}:
            self._on_peer_closed()

    async def take(self, offer: Offer) -> None:
        """Take `offer`, or raise whatever aiortc raises for one it cannot take."""
        await self._peer.setRemoteDescription(RTCSessionDescription(offer.sdp, "offer"))

    async def answer(self) -> str:
        """The answer to the offer taken: Opus and VP8 only, with every candidate."""
        await self._peer.setLocalDescription(await self._peer.createAnswer())
        return self._peer.localDescription.sdp

    def feed(self, audio: MediaFrame, video: MediaFrame) -> None:
        self._audio.feed(audio)
        self._video.feed(video)

    def end(self, reason: ViewerEnd) -> None:
        """End the viewer for `reason`; its owner calls this once."""
        self.ended.set_result(reason)

    async def close(self) -> None:
        self._audio.stop()
        self._video.stop()
        await self._peer.close()


class Media:
    """The viewers of one session and the frames they are fed."""

    def __init__(
        self, video: VideoFormat, max_viewers: int, peers: PeerFactory
    ) -> None:
        """Serve viewers of a picture of `video`, at most `max_viewers` at a time.

        Each viewer's peer is built by `peers`.
        """
        self._video = video
        self._max_viewers = max_viewers
        self._peers = peers
        self._ice: IceConfiguration | None = None
        self._viewers: dict[str, _Viewer] = {}
        self._watchers: set[asyncio.Task[None]] = set()
        self._resamplers: dict[int, AudioResampler] = {}
        self._periods = 0
        self._closed = False
        self.admitted = 0

    def configure(self, ice: IceConfiguration) -> None:
        """Take the ICE servers and policy the session was given."""
        self._ice = ice

    @property
    def ready(self) -> bool:
        """Whether the session has the ICE configuration its viewers need."""
        return self._ice is not None

    @property
    def closed(self) -> bool:
        """Whether the session has ended, and with it every viewer."""
        return self._closed

    async def admit(
        self,
        read_offer: Callable[[], Awaitable[Offer | RefusalCode]],
        on_end: Callable[[ViewerEnd], None],
    ) -> Admission | RefusalCode:
        """Answer the offer `read_offer` reads with a new viewer, or refuse.

        The refusals keep the wire's order: ICE servers the peer cannot parse, then
        what `read_offer` refuses, an offer the peer cannot take, and the viewer
        limit last. `on_end` hears the admitted viewer end.
        """
        viewer_id = uuid4().hex
        try:
            viewer = _Viewer(
                self._peers(self._configuration()),
                self._video.frame_rate,
                lambda: self._end(viewer_id, ViewerEnd.PEER_CLOSED),
            )
        except ValueError:
            # aiortc rejects an ICE server it cannot parse when it builds the peer.
            return RefusalCode.UNAVAILABLE
        try:
            answer = await self._negotiate(viewer_id, viewer, read_offer)
        except BaseException:
            # A request that fails while its body is read leaves no peer behind.
            await self._discard(viewer_id, viewer)
            raise
        if isinstance(answer, RefusalCode):
            await self._discard(viewer_id, viewer)
            return answer
        self.admitted += 1
        watcher = asyncio.create_task(self._watch(viewer_id, viewer, on_end))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return Admission(viewer_id, answer)

    def _configuration(self) -> RTCConfiguration:
        """The session's ICE servers, as given: an empty list stays empty."""
        ice = self._ice
        assert ice is not None  # noqa: S101 - the route admits viewers only when ready
        return RTCConfiguration(
            iceServers=[
                RTCIceServer(server.urls, server.username, server.credential)
                for server in ice.ice_servers
            ],
            iceTransportPolicy=RTCIceTransportPolicy(ice.ice_transport_policy),
        )

    async def _negotiate(
        self,
        viewer_id: str,
        viewer: _Viewer,
        read_offer: Callable[[], Awaitable[Offer | RefusalCode]],
    ) -> str | RefusalCode:
        """The answer `viewer` gives the offer read, or the refusal."""
        refused = await self._take(viewer, read_offer)
        if refused is not None:
            return refused
        # The viewer counts toward the limit while its candidates are gathered.
        self._viewers[viewer_id] = viewer
        try:
            answer = await viewer.answer()
        except Exception:  # noqa: BLE001 - as for the offer, aiortc fails any way
            return RefusalCode.INVALID_REQUEST
        return RefusalCode.NOT_FOUND if self._closed else answer

    async def _take(
        self,
        viewer: _Viewer,
        read_offer: Callable[[], Awaitable[Offer | RefusalCode]],
    ) -> RefusalCode | None:
        """Why `viewer` may not answer the offer read, if it may not."""
        offer = await read_offer()
        if isinstance(offer, RefusalCode):
            return offer
        try:
            await viewer.take(offer)
        except Exception:  # noqa: BLE001 - aiortc fails an offer it cannot take any way
            return RefusalCode.INVALID_REQUEST
        if self._closed:
            return RefusalCode.NOT_FOUND
        if len(self._viewers) >= self._max_viewers:
            return RefusalCode.BUSY
        return None

    async def _discard(self, viewer_id: str, viewer: _Viewer) -> None:
        self._viewers.pop(viewer_id, None)
        await viewer.close()

    async def _watch(
        self, viewer_id: str, viewer: _Viewer, on_end: Callable[[ViewerEnd], None]
    ) -> None:
        reason = await viewer.ended
        self._viewers.pop(viewer_id, None)
        await viewer.close()
        on_end(reason)

    def _end(self, viewer_id: str, reason: ViewerEnd) -> bool:
        viewer = self._viewers.pop(viewer_id, None)
        if viewer is None:
            return False
        viewer.end(reason)
        return True

    def remove(self, viewer_id: str) -> bool:
        """End the viewer `viewer_id` as deleted; whether there was one."""
        return self._end(viewer_id, ViewerEnd.DELETED)

    async def close(self) -> None:
        """End every viewer with the session, once their peers have closed."""
        self._closed = True
        ending, self._viewers = self._viewers, {}
        for viewer in ending.values():
            viewer.end(ViewerEnd.SESSION_ENDED)
        await asyncio.gather(*self._watchers)

    def send_idle(self, frame: Frame) -> None:
        """Hand every viewer the face at rest and one frame period of silence."""
        self._resamplers.clear()
        self._send(frame, None)

    def send(self, frame: Frame, pcm: bytes, rate: int) -> None:
        """Hand every viewer `frame` and the PCM16 mono audio of its frame period."""
        self._send(frame, (pcm, rate))

    def _send(self, frame: Frame, speech: tuple[bytes, int] | None) -> None:
        period = self._periods
        self._periods += 1
        connected = [viewer for viewer in self._viewers.values() if viewer.connected]
        if not connected:
            return
        rate = self._video.frame_rate
        first = period * _AUDIO_RATE // rate
        samples = (period + 1) * _AUDIO_RATE // rate - first
        audio = self._audio_period(speech, samples)
        for viewer in connected:
            viewer.feed(
                _audio_frame(audio, first),
                _video_frame(frame, self._video, period * _VIDEO_CLOCK_RATE // rate),
            )

    def _audio_period(self, speech: tuple[bytes, int] | None, samples: int) -> bytes:
        """`samples` of 48 kHz audio: the resampled speech, cut or padded to fit."""
        wanted = samples * _BYTES_PER_SAMPLE
        if speech is None:
            return bytes(wanted)
        pcm, rate = speech
        resampler = self._resamplers.get(rate)
        if resampler is None:
            resampler = AudioResampler(format="s16", layout="mono", rate=_AUDIO_RATE)
            self._resamplers[rate] = resampler
        incoming = AudioFrame(
            format="s16", layout="mono", samples=len(pcm) // _BYTES_PER_SAMPLE
        )
        incoming.planes[0].update(pcm)
        incoming.sample_rate = rate
        incoming.pts = 0
        incoming.time_base = Fraction(1, rate)
        resampled = b"".join(
            bytes(frame.planes[0])[: frame.samples * _BYTES_PER_SAMPLE]
            for frame in resampler.resample(incoming)
        )
        return resampled[:wanted].ljust(wanted, b"\0")


def _audio_frame(pcm: bytes, pts: int) -> AudioFrame:
    frame = AudioFrame(
        format="s16", layout="mono", samples=len(pcm) // _BYTES_PER_SAMPLE
    )
    frame.planes[0].update(pcm)
    frame.sample_rate = _AUDIO_RATE
    frame.pts = pts
    frame.time_base = Fraction(1, _AUDIO_RATE)
    return frame


def _video_frame(frame: Frame, video: VideoFormat, pts: int) -> VideoFrame:
    picture = VideoFrame(video.width, video.height, "rgb24")
    plane = picture.planes[0]
    row = video.width * _BYTES_PER_PIXEL
    # A plane's rows may be padded to its line size.
    plane.update(
        b"".join(
            frame.rgb[start : start + row].ljust(plane.line_size, b"\0")
            for start in range(0, len(frame.rgb), row)
        )
    )
    picture.pts = pts
    picture.time_base = Fraction(1, _VIDEO_CLOCK_RATE)
    return picture
