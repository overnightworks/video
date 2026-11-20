"""A viewer of the proof: a receive-only aiortc peer that counts what reaches it.

Its figures come from what the peer receives, never from the service's own lines. One
viewer can also record its audio and video into a file and keep frames for contact
sheets, which a delegated judge reads for the picture.
"""

from __future__ import annotations

import array
import asyncio
import itertools
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRecorder
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from PIL import Image

from card_drive import Window, post_viewer_offer
from card_record import (
    CLEAR_TO_SILENCE_LINE,
    FRAMES_PER_SECOND_LINE,
    Figure,
    judged,
    value_of,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from av import AudioFrame, VideoFrame

    from card_drive import Service
    from card_identity import Identity
    from video_client.session_wire import SessionCreated

FIRST_FRAME_WITHIN_SECONDS: Final = 15.0
CLOSE_WITHIN_SECONDS: Final = 1.0
VIDEO_CLOCK_HZ: Final = 90_000
LOUD_PEAK: Final = 2_000
SAMPLE_BYTES: Final = 2
SHEET_INTERVAL_SECONDS: Final = 0.5
SHEET_CELL_PIXELS: Final = 160
SHEET_COLUMNS: Final = 4
SHEET_ROWS: Final = 3
# A video timestamp step beyond one and a half frame periods is a gap.
GAP_FRAME_PERIODS: Final = 1.5


@dataclass(frozen=True)
class Recording:
    """Where one viewer's video and contact sheets are written."""

    video: Path
    sheet_stem: Path

    def sheet(self, number: int) -> Path:
        """The path of contact sheet `number`."""
        return self.sheet_stem.with_name(f"{self.sheet_stem.name}-{number}.png")


@dataclass
class Received:
    """What reached the peer: arrival times, video timestamps and loud audio."""

    video_times: list[float] = field(default_factory=list)
    video_ticks: list[int] = field(default_factory=list)
    loud_times: list[float] = field(default_factory=list)
    frame_size: tuple[int, int] = (0, 0)


class _Counted(MediaStreamTrack):
    """A track that hands each frame to a counter before its consumer sees it."""

    def __init__(
        self,
        source: MediaStreamTrack,
        video: Callable[[VideoFrame, float], None],
        audio: Callable[[AudioFrame, float], None],
    ) -> None:
        super().__init__()
        self.kind = source.kind
        self._source = source
        self._count = video if source.kind == "video" else audio

    async def recv(self) -> VideoFrame | AudioFrame:
        frame = await self._source.recv()
        self._count(frame, time.monotonic())
        return frame


async def _drain(track: MediaStreamTrack) -> None:
    """Receive until the track ends."""
    with suppress(MediaStreamError):
        while True:
            await track.recv()


class Viewer:
    """An aiortc peer offering one audio and one video section, receive only."""

    def __init__(self, recording: Recording | None) -> None:
        """Offer the sections; a `recording` also writes the video and the sheets."""
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.received = Received()
        self.recording = recording
        self.sheets: list[Path] = []
        self._thumbnails: list[Image.Image] = []
        self._last_sampled = float("-inf")
        self._recording_started = False
        self._recorder = MediaRecorder(str(recording.video)) if recording else None
        self._tasks: list[asyncio.Task[None]] = []
        self._first_frame = asyncio.Event()
        for kind in ("audio", "video"):
            self.peer.addTransceiver(kind, direction="recvonly")
        self.peer.on("track", self._collect)

    def _collect(self, track: MediaStreamTrack) -> None:
        counted = _Counted(track, self._note_video, self._note_audio)
        if self._recorder is not None:
            self._recorder.addTrack(counted)
        else:
            self._tasks.append(asyncio.ensure_future(_drain(counted)))

    def _note_video(self, frame: VideoFrame, arrived: float) -> None:
        received = self.received
        received.video_times.append(arrived)
        received.video_ticks.append(frame.pts)
        received.frame_size = (frame.width, frame.height)
        self._first_frame.set()
        if self.recording and arrived - self._last_sampled >= SHEET_INTERVAL_SECONDS:
            self._last_sampled = arrived
            thumbnail = frame.to_image()
            thumbnail.thumbnail((SHEET_CELL_PIXELS, SHEET_CELL_PIXELS))
            self._thumbnails.append(thumbnail)

    def _note_audio(self, frame: AudioFrame, arrived: float) -> None:
        size = frame.samples * len(frame.layout.channels) * SAMPLE_BYTES
        samples = array.array("h", bytes(frame.planes[0])[:size])
        if max(map(abs, samples), default=0) > LOUD_PEAK:
            self.received.loud_times.append(arrived)

    async def start(
        self, service: Service, identity: Identity, session_id: str
    ) -> None:
        """Offer, take the service's answer and wait for the first picture."""
        await self.peer.setLocalDescription(await self.peer.createOffer())
        answer = await post_viewer_offer(
            service, identity, session_id, self.peer.localDescription.sdp
        )
        await self.peer.setRemoteDescription(RTCSessionDescription(answer, "answer"))
        if self._recorder is not None:
            await self._recorder.start()
            self._recording_started = True
        async with asyncio.timeout(FIRST_FRAME_WITHIN_SECONDS):
            await self._first_frame.wait()

    async def close(self) -> None:
        """End the peer and finish the recording and the sheets."""
        # An interrupt cancels the peer's own close task, and a second close then
        # waits on it for good.
        with suppress(TimeoutError):
            await asyncio.wait_for(self.peer.close(), CLOSE_WITHIN_SECONDS)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._recorder is not None and self._recording_started:
            await self._recorder.stop()
            self.sheets = _write_sheets(self.recording, self._thumbnails)

    def video_frames_in(self, window: Window) -> int:
        """How many video frames arrived within `window`."""
        times = self.received.video_times
        return sum(window.start <= arrived <= window.end for arrived in times)

    def loud_frames_in(self, window: Window) -> int:
        """How many loud audio frames arrived within `window`."""
        return sum(window.start <= at <= window.end for at in self.received.loud_times)

    def last_loud_ms_after(self, moment: float) -> float:
        """Milliseconds from `moment` to the last loud audio frame; 0 if none came."""
        later = [at for at in self.received.loud_times if at >= moment]
        return (max(later) - moment) * 1000 if later else 0.0

    def timestamp_steps(self) -> list[int]:
        """The steps between consecutive video timestamps, in clock ticks."""
        ticks = self.received.video_ticks
        return [later - earlier for earlier, later in itertools.pairwise(ticks)]


def _write_sheets(recording: Recording, thumbnails: list[Image.Image]) -> list[Path]:
    """The thumbnails laid out in grids, one image per `SHEET_COLUMNS * SHEET_ROWS`."""
    per_sheet = SHEET_COLUMNS * SHEET_ROWS
    written = []
    for number, first in enumerate(range(0, len(thumbnails), per_sheet), start=1):
        sheet = Image.new(
            "RGB",
            (SHEET_COLUMNS * SHEET_CELL_PIXELS, SHEET_ROWS * SHEET_CELL_PIXELS),
        )
        for index, thumbnail in enumerate(thumbnails[first : first + per_sheet]):
            row, column = divmod(index, SHEET_COLUMNS)
            sheet.paste(
                thumbnail, (column * SHEET_CELL_PIXELS, row * SHEET_CELL_PIXELS)
            )
        path = recording.sheet(number)
        sheet.save(path)
        written.append(path)
    return written


@asynccontextmanager
async def attached(
    service: Service,
    identity: Identity,
    created: SessionCreated,
    recording: Recording | None = None,
) -> AsyncIterator[Viewer]:
    """A viewer of the session; its recording is finished at the block's end."""
    viewer = Viewer(recording)
    try:
        await viewer.start(service, identity, created.session.id)
        yield viewer
    finally:
        await viewer.close()


def clear_figure(viewer: Viewer, cleared_at: float, *, dry_run: bool) -> Figure:
    """The time from a clear to the last loud audio frame the viewer received."""
    return judged(
        viewer.last_loud_ms_after(cleared_at), CLEAR_TO_SILENCE_LINE, dry_run=dry_run
    )


def figures(
    viewer: Viewer, *, sentence: Window, idle: Window, frame_rate: int, dry_run: bool
) -> dict[str, object]:
    """One viewer's frames per second in a sentence and between, silence and gaps."""
    gap_ticks = GAP_FRAME_PERIODS * VIDEO_CLOCK_HZ / frame_rate
    steps = viewer.timestamp_steps()
    width, height = viewer.received.frame_size
    return {
        "frames_per_second_in_sentence": judged(
            viewer.video_frames_in(sentence) / sentence.seconds,
            FRAMES_PER_SECOND_LINE,
            dry_run=dry_run,
        ),
        "frames_per_second_between_sentences": judged(
            viewer.video_frames_in(idle) / idle.seconds,
            FRAMES_PER_SECOND_LINE,
            dry_run=dry_run,
        ),
        "loud_audio_frames_between_sentences": value_of(viewer.loud_frames_in(idle)),
        "video_timestamp_gaps": value_of(sum(step > gap_ticks for step in steps)),
        "longest_video_timestamp_gap_ms": value_of(
            round(max(steps, default=0) * 1000 / VIDEO_CLOCK_HZ)
        ),
        "frame_size": value_of({"width": width, "height": height}),
    }
