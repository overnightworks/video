"""A provider that holds no model: it draws a picture on the audio's timeline.

The face at rest is dark; a sentence's frame is bright when its frame period of
audio holds a loud sample, so a test sees the idle between sentences and a beep.
It serves the service's own tests and a run from source, in a process of its own
like every provider (`video-fake-worker`); no image installs it. A chunked fake
draws a chunk of frames at a time and takes one render for the chunk, as a model
that renders in chunks does.
"""

from __future__ import annotations

import struct
import threading
import time
from typing import TYPE_CHECKING

from video_provider_contract import (
    DecodedImage,
    Frame,
    ProviderFailedError,
    ProviderInfo,
    ProviderSession,
    ProviderVerb,
    ProviderVideoFormat,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator

_WIDTH = 256
_HEIGHT = 256
_BYTES_PER_PIXEL = 3
_BYTES_PER_SAMPLE = 2
_LOUD_SAMPLE = 8192


class FakeSession:
    """Draws one frame per frame period of the audio it is fed, a chunk at a time."""

    def __init__(  # noqa: PLR0913 - one switch for each way a test makes it fail or lag
        self,
        frame_rate: int,
        render_seconds: float,
        *,
        idle_render_seconds: float = 0.0,
        chunk_frames: int = 1,
        fails_to_draw: bool,
        finish_gate: threading.Event | None,
        sleep: Callable[[float], None],
    ) -> None:
        """Draw at `frame_rate`, taking `render_seconds` of `sleep` for each chunk.

        A chunk is `chunk_frames` frames of the audio fed, drawn once the audio of
        all of them is there, or at the end of the sentence for the frames left; a
        call draws every chunk it can and nothing between calls. The face at rest
        is drawn in chunks too, each taking `idle_render_seconds`. A `finish_gate`
        holds the last frames of a sentence until it is set; `finishing` is set once
        a sentence's last frames are asked for.
        """
        self._frame_rate = frame_rate
        self._render_seconds = render_seconds
        self._idle_render_seconds = idle_render_seconds
        self._chunk_frames = chunk_frames
        self._idle_left = 0
        self._fails_to_draw = fails_to_draw
        self._finish_gate = finish_gate
        self._sleep = sleep
        self._dark = Frame(bytes(_WIDTH * _HEIGHT * _BYTES_PER_PIXEL))
        self._bright = Frame(b"\xff" * (_WIDTH * _HEIGHT * _BYTES_PER_PIXEL))
        self._audio = bytearray()
        self._samples = 0
        self._taken = 0
        self._rate = 1
        self._drawn = 0
        self.finishing = threading.Event()
        self.idle_frames = 0
        self.clears = 0
        self.closed = False

    def idle_frame(self) -> Frame:
        """Draw the face at rest."""
        self.idle_frames += 1
        if not self._idle_left:
            if self._idle_render_seconds:
                self._sleep(self._idle_render_seconds)
            self._idle_left = self._chunk_frames
        self._idle_left -= 1
        return self._dark

    def feed(self, pcm: bytes, rate: int) -> Iterator[Frame]:
        """Count the audio; the frames of its whole chunks come out."""
        if self._fails_to_draw:
            message = "the fake provider was told to fail to draw"
            raise ProviderFailedError(message)
        self._audio += pcm
        self._samples += len(pcm) // _BYTES_PER_SAMPLE
        self._rate = rate
        return self._draw(self._samples * self._frame_rate // rate, flush=False)

    def finish_sentence(self) -> Iterator[Frame]:
        """Draw the frames left, the last of a partial period too, then forget."""
        self.finishing.set()
        if self._finish_gate is not None:
            self._finish_gate.wait()
        total = -(-self._samples * self._frame_rate // self._rate)
        yield from self._draw(total, flush=True)
        self._reset()

    def clear(self) -> None:
        """Forget the sentence."""
        self.clears += 1
        self._reset()

    def close(self) -> None:
        """Note that the service closed the session, and forget its audio."""
        self.closed = True
        self._reset()

    def _draw(self, total: int, *, flush: bool) -> Iterator[Frame]:
        """The frames up to `total`: a render per chunk, a short one if `flush`."""
        while (pending := total - self._drawn) >= (1 if flush else self._chunk_frames):
            self._sleep(self._render_seconds)
            for _ in range(min(pending, self._chunk_frames)):
                period = self._take_period()
                self._drawn += 1
                yield self._bright if self._is_loud(period) else self._dark

    def _take_period(self) -> bytes:
        """Take out the next period's audio, which may be short at a sentence's end."""
        last = (self._drawn + 1) * self._rate // self._frame_rate
        size = (last - self._taken) * _BYTES_PER_SAMPLE
        period = bytes(self._audio[:size])
        self._audio = self._audio[size:]
        self._taken += len(period) // _BYTES_PER_SAMPLE
        return period

    @staticmethod
    def _is_loud(period: bytes) -> bool:
        samples = struct.unpack(f"<{len(period) // _BYTES_PER_SAMPLE}h", period)
        return any(abs(sample) >= _LOUD_SAMPLE for sample in samples)

    def _reset(self) -> None:
        self._audio = bytearray()
        self._samples = 0
        self._taken = 0
        self._drawn = 0
        self._idle_left = 0


class FakeProvider:
    """Starts at once and prepares every look it is not told to refuse."""

    def __init__(  # noqa: PLR0913 - one switch for each way a test makes it fail or lag
        self,
        *,
        fails_to_start: bool = False,
        refuses: Collection[str] = (),
        frame_rate: int = 25,
        render_seconds: float = 0.0,
        idle_render_seconds: float = 0.0,
        chunk_frames: int = 1,
        fails_to_open: bool = False,
        fails_to_draw: bool = False,
        finish_gate: threading.Event | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Fail or lag as told; `refuses` names the looks `prepare` fails for.

        A chunk of `chunk_frames` frames takes `render_seconds` of `sleep`, a chunk of
        the face at rest `idle_render_seconds`; a test hands the sleep of its own clock.
        """
        self._fails_to_start = fails_to_start
        self._refuses = frozenset(refuses)
        self._frame_rate = frame_rate
        self._render_seconds = render_seconds
        self._idle_render_seconds = idle_render_seconds
        self._chunk_frames = chunk_frames
        self._fails_to_open = fails_to_open
        self._fails_to_draw = fails_to_draw
        self._finish_gate = finish_gate
        self._sleep = sleep
        self.prepared: dict[str, DecodedImage] = {}
        self.sessions: list[FakeSession] = []
        self.closed = False

    def start(self) -> ProviderInfo:
        """Report the fixed picture, or fail as told."""
        if self._fails_to_start:
            message = "the fake provider was told to fail"
            raise ProviderFailedError(message)
        return ProviderInfo(
            plays=(ProviderVerb.SPEAK, ProviderVerb.CLEAR),
            video=ProviderVideoFormat(
                width=_WIDTH, height=_HEIGHT, frame_rate=self._frame_rate
            ),
        )

    def prepare(self, look: str, image: DecodedImage) -> None:
        """Keep `image` for `look`, or fail as told."""
        if look in self._refuses:
            message = "the fake provider was told to refuse the look"
            raise ProviderFailedError(message)
        self.prepared[look] = image

    def open_session(self, look: str) -> ProviderSession:
        """Open a session on `look`, or fail as told."""
        del look
        if self._fails_to_open:
            message = "the fake provider was told to fail to open"
            raise ProviderFailedError(message)
        session = FakeSession(
            self._frame_rate,
            self._render_seconds,
            idle_render_seconds=self._idle_render_seconds,
            chunk_frames=self._chunk_frames,
            fails_to_draw=self._fails_to_draw,
            finish_gate=self._finish_gate,
            sleep=self._sleep,
        )
        self.sessions.append(session)
        return session

    def close(self) -> None:
        """Note that the service closed the provider."""
        self.closed = True
