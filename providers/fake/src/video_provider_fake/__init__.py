"""A provider that holds no model: it draws a blank picture on the audio's timeline.

It serves the service's own tests and a run from source; no image installs it.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from video_provider_contract import (
    DecodedImage,
    Frame,
    ProviderFailedError,
    ProviderInfo,
    ProviderSession,
)

from video_client.wire import Verb, VideoFormat

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator

_WIDTH = 256
_HEIGHT = 256
_BYTES_PER_PIXEL = 3
_BYTES_PER_SAMPLE = 2


class FakeSession:
    """Draws one blank frame per frame period of the audio it is fed."""

    def __init__(
        self, frame_rate: int, render_seconds: float, *, fails_to_draw: bool
    ) -> None:
        """Draw at `frame_rate`, taking `render_seconds` for each frame."""
        self._frame_rate = frame_rate
        self._render_seconds = render_seconds
        self._fails_to_draw = fails_to_draw
        self._frame = Frame(bytes(_WIDTH * _HEIGHT * _BYTES_PER_PIXEL))
        self._samples = 0
        self._rate = 1
        self._drawn = 0
        self.idle_frames = 0
        self.clears = 0
        self.closed = False

    def idle_frame(self) -> Frame:
        """Draw the face at rest."""
        self.idle_frames += 1
        return self._frame

    def feed(self, pcm: bytes, rate: int) -> Iterator[Frame]:
        """Count the audio; its whole frame periods come out."""
        if self._fails_to_draw:
            message = "the fake provider was told to fail to draw"
            raise ProviderFailedError(message)
        self._samples += len(pcm) // _BYTES_PER_SAMPLE
        self._rate = rate
        return self._until(self._samples * self._frame_rate // rate)

    def finish_sentence(self) -> Iterator[Frame]:
        """Draw the frame of a last partial period, then forget the sentence."""
        total = -(-self._samples * self._frame_rate // self._rate)
        yield from self._until(total)
        self._reset()

    def clear(self) -> None:
        """Forget the sentence."""
        self.clears += 1
        self._reset()

    def close(self) -> None:
        """Note that the service closed the session."""
        self.closed = True

    def _until(self, total: int) -> Iterator[Frame]:
        while self._drawn < total:
            time.sleep(self._render_seconds)
            self._drawn += 1
            yield self._frame

    def _reset(self) -> None:
        self._samples = 0
        self._drawn = 0


class FakeProvider:
    """Starts at once and prepares every look it is not told to refuse."""

    def __init__(  # noqa: PLR0913 - one switch for each way a test makes it fail or lag
        self,
        *,
        fails_to_start: bool = False,
        refuses: Collection[str] = (),
        frame_rate: int = 25,
        render_seconds: float = 0.0,
        fails_to_open: bool = False,
        fails_to_draw: bool = False,
    ) -> None:
        """Fail or lag as told; `refuses` names the looks `prepare` fails for."""
        self._fails_to_start = fails_to_start
        self._refuses = frozenset(refuses)
        self._frame_rate = frame_rate
        self._render_seconds = render_seconds
        self._fails_to_open = fails_to_open
        self._fails_to_draw = fails_to_draw
        self.prepared: dict[str, DecodedImage] = {}
        self.sessions: list[FakeSession] = []
        self.closed = False

    def start(self) -> ProviderInfo:
        """Report the fixed picture, or fail as told."""
        if self._fails_to_start:
            message = "the fake provider was told to fail"
            raise ProviderFailedError(message)
        return ProviderInfo(
            plays=(Verb.SPEAK, Verb.CLEAR),
            video=VideoFormat(
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
            self._frame_rate, self._render_seconds, fails_to_draw=self._fails_to_draw
        )
        self.sessions.append(session)
        return session

    def close(self) -> None:
        """Note that the service closed the provider."""
        self.closed = True
