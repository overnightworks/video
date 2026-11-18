"""A live service, its sessions over WebSocket and its viewers as aiortc peers."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import httpx2
from aiortc import (
    RTCConfiguration,
    RTCPeerConnection,
    RTCRtpReceiver,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError
from websockets.asyncio.client import connect

from support.identity_support import valid_headers
from support.service_support import READY_WITHIN_SECONDS, free_port, start_service
from support.session_support import sentence_events
from video_client.session_wire import SessionCreated, parse_server_event
from video_client.wire import SDP_CONTENT_TYPE, viewers_path
from video_service.config import Settings
from video_service.providers import ProviderName

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiortc.mediastreams import MediaStreamTrack
    from websockets.asyncio.client import ClientConnection

    from video_service.runtime import Runtime

WITHIN_SECONDS: Final = 15.0
FRAME_RATE: Final = 25
VIDEO_TICKS_PER_FRAME: Final = 90_000 // FRAME_RATE
BEEP_PEAK: Final = 20_000
_LOUD_AUDIO: Final = 2_000
_BRIGHT_LUMA: Final = 128
_LUMA_SAMPLE: Final = 64
_SAMPLE_BYTES: Final = 2


def beep(milliseconds: int, rate: int = 16_000) -> bytes:
    """Loud PCM16 mono audio of `milliseconds`: a square wave."""
    samples = rate * milliseconds // 1000
    return struct.pack(f"<{samples}h", *([BEEP_PEAK, -BEEP_PEAK] * samples)[:samples])


class Logged(logging.Handler):
    """Notes the lines written, and lets a test wait for one."""

    def __init__(self) -> None:
        """Listen from the start."""
        super().__init__(logging.INFO)
        self.records: list[logging.LogRecord] = []
        self._written = asyncio.Event()

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the line and wake the waiters."""
        self.records.append(record)
        self._written.set()

    def lines(self, event: str) -> list[logging.LogRecord]:
        """The lines of `event` written so far."""
        return [record for record in self.records if record.getMessage() == event]

    async def wait_for(self, event: str, count: int = 1) -> list[logging.LogRecord]:
        """The lines of `event` once `count` of them are written."""
        async with asyncio.timeout(WITHIN_SECONDS):
            while len(self.lines(event)) < count:
                self._written.clear()
                await self._written.wait()
        return self.lines(event)


@dataclass
class Received:
    """What a viewer decoded: video timestamps and brightness, audio loudness."""

    video_pts: list[int] = field(default_factory=list)
    bright: list[bool] = field(default_factory=list)
    loud: list[bool] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    async def until(self, condition: Callable[[Received], bool]) -> None:
        """Wait until `condition` holds of what has arrived."""
        async with asyncio.timeout(WITHIN_SECONDS):
            while not condition(self):
                self.changed.clear()
                await self.changed.wait()


class Watcher:
    """A viewer: an aiortc peer that only receives."""

    def __init__(self, *, audio: int = 1, video: int = 1) -> None:
        """Offer `audio` audio and `video` video sections, all recvonly."""
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.received = Received()
        self.location = ""
        self._tasks: list[asyncio.Task[None]] = []
        self.transceivers = [
            self.peer.addTransceiver(kind, direction="recvonly")
            for kind, count in (("audio", audio), ("video", video))
            for _ in range(count)
        ]
        self.peer.on("track", self._collect)

    def prefer(self, kind: str, mime_types: list[str]) -> None:
        """Offer only the codecs of `kind` named by `mime_types`, in that order."""
        codecs = RTCRtpReceiver.getCapabilities(kind).codecs
        ordered = [
            codec
            for mime in mime_types
            for codec in codecs
            if codec.mimeType.lower() == mime.lower()
        ]
        for transceiver in self.transceivers:
            if transceiver.kind == kind:
                transceiver.setCodecPreferences(ordered)

    async def offer(self) -> str:
        """The SDP offer, with this peer's candidates."""
        await self.peer.setLocalDescription(await self.peer.createOffer())
        return self.peer.localDescription.sdp

    async def accept(self, answer: str) -> None:
        """Take the service's answer; frames start to arrive."""
        await self.peer.setRemoteDescription(RTCSessionDescription(answer, "answer"))

    def _collect(self, track: MediaStreamTrack) -> None:
        self._tasks.append(asyncio.ensure_future(self._receive(track)))

    async def _receive(self, track: MediaStreamTrack) -> None:
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            received = self.received
            if track.kind == "video":
                luma = bytes(frame.planes[0])[:_LUMA_SAMPLE]
                received.video_pts.append(frame.pts)
                received.bright.append(sum(luma) / len(luma) > _BRIGHT_LUMA)
            else:
                size = frame.samples * len(frame.layout.channels) * _SAMPLE_BYTES
                samples = struct.unpack(
                    f"<{size // _SAMPLE_BYTES}h", bytes(frame.planes[0])[:size]
                )
                received.loud.append(max(map(abs, samples)) > _LOUD_AUDIO)
            received.changed.set()

    async def close(self) -> None:
        """Close the peer, which tells the service."""
        await self.peer.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


@dataclass
class OpenSession:
    """A session opened over WebSocket and its id."""

    socket: ClientConnection
    session_id: str

    async def update(
        self, ice_servers: object = (), policy: str = "all", **fields: object
    ) -> None:
        """Send `session.update` and wait for `session.updated`."""
        await self.send_update(ice_servers, policy, **fields)
        await self.socket.recv()

    async def send_update(
        self, ice_servers: object = (), policy: str = "all", **fields: object
    ) -> None:
        """Send `session.update`."""
        await self.socket.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "ice_servers": ice_servers,
                        "ice_transport_policy": policy,
                    },
                }
                | fields
            )
        )

    async def speak(self, sentence: str, audio: bytes) -> None:
        """Queue one sentence of `audio` (16 kHz)."""
        start, end = sentence_events(sentence)
        await self.socket.send(json.dumps(start))
        await self.socket.send(audio)
        await self.socket.send(json.dumps(end))

    async def next_event(self) -> object:
        """The next server event."""
        return parse_server_event(str(await self.socket.recv()))


@dataclass
class Service:
    """A running service: its address, its sessions and the peers of the scenario."""

    runtime: Runtime
    stop: asyncio.Event
    base_url: str
    ws_url: str
    http: httpx2.AsyncClient
    logged: Logged
    stack: AsyncExitStack

    async def open_session(
        self, headers: dict[str, str] | None = None, *, update: bool = True
    ) -> OpenSession:
        """Open a session; `update` sends a `session.update` with no ICE servers."""
        socket = await connect(
            self.ws_url, additional_headers=headers or valid_headers()
        )
        self.stack.push_async_callback(socket.close)
        created = parse_server_event(str(await socket.recv()))
        assert isinstance(created, SessionCreated)
        session = OpenSession(socket, created.session.id)
        if update:
            await session.update()
        return session

    async def post(
        self,
        session: OpenSession | str,
        offer: str | bytes,
        *,
        headers: dict[str, str] | None = None,
        content_type: str = SDP_CONTENT_TYPE,
    ) -> httpx2.Response:
        """POST `offer` to the viewers of `session`."""
        session_id = session if isinstance(session, str) else session.session_id
        return await self.http.post(
            viewers_path(session_id),
            content=offer,
            headers=(valid_headers() if headers is None else headers)
            | {"Content-Type": content_type},
        )

    async def watch(self, session: OpenSession, **sections: int) -> Watcher:
        """A connected viewer of `session`."""
        watcher = Watcher(**sections)
        self.stack.push_async_callback(watcher.close)
        response = await self.post(session, await watcher.offer())
        assert response.status_code == httpx2.codes.CREATED, response.text
        await watcher.accept(response.text)
        watcher.location = response.headers["Location"]
        return watcher


async def run_live(
    runtime: Runtime,
    scenario: Callable[[Service], Awaitable[None]],
    *,
    drain_seconds: float = 5.0,
) -> None:
    """Run `scenario` against `runtime` served the way the process serves it."""
    settings = Settings(
        provider=ProviderName.FAKE, port=free_port(), drain_seconds=drain_seconds
    )
    stop = asyncio.Event()
    serving, ws_url = await start_service(runtime, settings, stop)
    logged = Logged()
    root = logging.getLogger()
    level = root.level
    root.addHandler(logged)
    root.setLevel(logging.INFO)
    try:
        async with (
            AsyncExitStack() as stack,
            httpx2.AsyncClient(
                base_url=f"http://127.0.0.1:{settings.port}",
                timeout=READY_WITHIN_SECONDS,
            ) as http,
        ):
            service = Service(
                runtime,
                stop,
                f"http://127.0.0.1:{settings.port}",
                ws_url,
                http,
                logged,
                stack,
            )
            await scenario(service)
    finally:
        root.removeHandler(logged)
        root.setLevel(level)
        stop.set()
        await serving


async def offer_of(
    *,
    audio: int = 1,
    video: int = 1,
    audio_codecs: list[str] | None = None,
    video_codecs: list[str] | None = None,
) -> str:
    """An offer with `audio` audio and `video` video sections, all recvonly."""
    watcher = Watcher(audio=audio, video=video)
    if audio_codecs is not None:
        watcher.prefer("audio", audio_codecs)
    if video_codecs is not None:
        watcher.prefer("video", video_codecs)
    try:
        return await watcher.offer()
    finally:
        await watcher.close()
