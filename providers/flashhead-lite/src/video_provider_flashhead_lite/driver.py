"""The FlashHead lite provider: the port's calls turned into slice renders.

Audio is resampled to the renderer's rate and handed over a slice at a time; a slice
the audio does not fill is padded with silence at a sentence's end, and the face at
rest is a slice of silence. Which model renders is the renderer's business: the
driver reads every number from its shape.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from typing import TYPE_CHECKING

import numpy as np
import soxr
from video_provider_contract import (
    DecodedImage,
    Frame,
    ProviderFailedError,
    ProviderInfo,
    ProviderSession,
    ProviderVerb,
    ProviderVideoFormat,
)

from video_provider_flashhead_lite.renderer import RenderError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from numpy.typing import NDArray

    from video_provider_flashhead_lite.renderer import (
        Renderer,
        RenderSession,
        RenderShape,
    )

_BYTES_PER_PIXEL = 3
_PCM16_FULL_SCALE = 32768


@contextmanager
def _failing_as_provider_failure() -> Iterator[None]:
    try:
        yield
    except RenderError as error:
        raise ProviderFailedError(str(error)) from error


class FlashHeadLiteSession:
    """One look: its sentence audio is resampled and rendered a slice at a time."""

    def __init__(self, render_session: RenderSession, shape: RenderShape) -> None:
        """Draw through `render_session` in slices of `shape`."""
        self._render_session = render_session
        self._shape = shape
        self._slice_samples = (
            shape.frames_per_slice * shape.sample_rate // shape.frame_rate
        )
        self._frame_bytes = shape.width * shape.height * _BYTES_PER_PIXEL
        self._idle: deque[Frame] = deque()
        self._reset_sentence()

    def idle_frame(self) -> Frame:
        """Draw the next frame of the face at rest, a slice of silence at a time."""
        if not self._idle:
            self._idle.extend(self._render(np.zeros(self._slice_samples, np.float32)))
        return self._idle.popleft()

    def feed(self, pcm: bytes, rate: int) -> Iterator[Frame]:
        """Resample the audio and render every slice it completes."""
        if self._stream is None:
            self._stream = soxr.ResampleStream(
                rate, self._shape.sample_rate, num_channels=1, dtype="float32"
            )
            self._wire_rate = rate
        samples = np.frombuffer(pcm, dtype="<i2")
        self._wire_samples += len(samples)
        self._buffer = np.concatenate(
            [
                self._buffer,
                self._stream.resample_chunk(
                    samples.astype(np.float32) / _PCM16_FULL_SCALE
                ),
            ]
        )
        frames: list[Frame] = []
        while len(self._buffer) >= self._slice_samples:
            frames += self._render_speech(self._take_slice())
        return iter(frames)

    def finish_sentence(self) -> Iterator[Frame]:
        """Flush the resampler and render until the sentence's frames are drawn."""
        if self._stream is None:
            return iter(())
        total = -(-self._wire_samples * self._shape.frame_rate // self._wire_rate)
        already_returned = self._drawn
        self._buffer = np.concatenate(
            [
                self._buffer,
                self._stream.resample_chunk(np.zeros(0, np.float32), last=True),
            ]
        )
        frames: list[Frame] = []
        while self._drawn < total:
            padding = self._slice_samples - min(len(self._buffer), self._slice_samples)
            frames += self._render_speech(np.pad(self._take_slice(), (0, padding)))
        self._reset_sentence()
        return iter(frames[: total - already_returned])

    def clear(self) -> None:
        """Drop the open sentence's audio; what the renderer heard stays."""
        self._reset_sentence()

    def close(self) -> None:
        """Release the render session."""
        with _failing_as_provider_failure():
            self._render_session.close()

    def _take_slice(self) -> NDArray[np.float32]:
        taken = self._buffer[: self._slice_samples]
        self._buffer = self._buffer[self._slice_samples :]
        return taken

    def _render_speech(self, audio: NDArray[np.float32]) -> list[Frame]:
        self._idle.clear()
        self._drawn += self._shape.frames_per_slice
        return self._render(audio)

    def _render(self, audio: NDArray[np.float32]) -> list[Frame]:
        with _failing_as_provider_failure():
            rgb = self._render_session.render(audio)
        return [
            Frame(rgb[start : start + self._frame_bytes])
            for start in range(0, len(rgb), self._frame_bytes)
        ]

    def _reset_sentence(self) -> None:
        self._stream: soxr.ResampleStream | None = None
        self._wire_rate = 1
        self._wire_samples = 0
        self._buffer: NDArray[np.float32] = np.zeros(0, np.float32)
        self._drawn = 0


class FlashHeadLiteProvider:
    """A provider that draws through a renderer."""

    def __init__(self, renderer: Renderer) -> None:
        """Draw through `renderer`."""
        self._renderer = renderer
        self._shape: RenderShape | None = None

    def start(self) -> ProviderInfo:
        """Start the renderer and report what it plays and draws."""
        with _failing_as_provider_failure():
            self._shape = self._renderer.start()
        return ProviderInfo(
            plays=(ProviderVerb.SPEAK, ProviderVerb.CLEAR),
            video=ProviderVideoFormat(
                width=self._shape.width,
                height=self._shape.height,
                frame_rate=self._shape.frame_rate,
            ),
        )

    def prepare(self, look: str, image: DecodedImage) -> None:
        """Pass the image on to the renderer."""
        with _failing_as_provider_failure():
            self._renderer.prepare(look, image)

    def open_session(self, look: str) -> ProviderSession:
        """Open a render session on `look`."""
        if self._shape is None:
            message = "the provider is not started"
            raise ProviderFailedError(message)
        with _failing_as_provider_failure():
            return FlashHeadLiteSession(self._renderer.open_session(look), self._shape)

    def close(self) -> None:
        """Release the renderer."""
        with _failing_as_provider_failure():
            self._renderer.close()
