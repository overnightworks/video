"""The port between the video service and a provider.

A provider is a pure model: a decoded image in at preparation, and what it plays
and draws out at start. What it prepares from an image stays inside it. The
service owns the fetching, the checking and the decoding of every image, so a
provider never touches the network or an untrusted file.

A session is one open look: sentence audio goes in and frames come out on the
audio's timeline, at the frame rate the provider reported. The service calls a
session from one thread at a time, and paces the frames it gets; the provider
never sleeps to keep time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator

    from video_client.wire import Verb, VideoFormat


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """What a started provider plays and draws."""

    plays: tuple[Verb, ...]
    video: VideoFormat


@dataclass(frozen=True, slots=True)
class DecodedImage:
    """A look's image: `width` times `height` pixels, three bytes each, row by row."""

    width: int
    height: int
    rgb: bytes


@dataclass(frozen=True, slots=True)
class Frame:
    """One picture of the provider's video format: three bytes a pixel, row by row."""

    rgb: bytes


class ProviderFailedError(Exception):
    """The provider could not start, prepare a look, open a session or draw."""


class ProviderSession(Protocol):
    """One open look: it draws the face idle and plays one sentence at a time."""

    def idle_frame(self) -> Frame:
        """Draw the next frame of the face at rest."""

    def feed(self, pcm: bytes, rate: int) -> Iterator[Frame]:
        """Take PCM16 little-endian mono audio of the open sentence at `rate` Hz.

        The frames of the audio fed so far that are not yet drawn come out in order.
        """

    def finish_sentence(self) -> Iterator[Frame]:
        """End the open sentence; the frames of its audio not yet drawn come out."""

    def clear(self) -> None:
        """Drop the open sentence; the face returns to idle."""

    def close(self) -> None:
        """Release the session."""


class Provider(Protocol):
    """The one provider this instance runs."""

    def start(self) -> ProviderInfo:
        """Load the model and report what it plays and draws, or raise."""

    def prepare(self, look: str, image: DecodedImage) -> None:
        """Prepare `look` from `image`, or raise `ProviderFailedError`."""

    def open_session(self, look: str) -> ProviderSession:
        """Open a session on a prepared `look`, or raise `ProviderFailedError`."""

    def close(self) -> None:
        """Release what the provider holds."""
