"""The port between the video service and a provider.

A provider is a pure model: a decoded image in at preparation, and what it plays
and draws out at start. What it prepares from an image stays inside it. The
service owns the fetching, the checking and the decoding of every image, so a
provider never touches the network or an untrusted file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
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


class ProviderFailedError(Exception):
    """The provider could not start, or could not prepare the look it was given."""


class Provider(Protocol):
    """The one provider this instance runs."""

    def start(self) -> ProviderInfo:
        """Load the model and report what it plays and draws, or raise."""

    def prepare(self, look: str, image: DecodedImage) -> None:
        """Prepare `look` from `image`, or raise `ProviderFailedError`."""

    def close(self) -> None:
        """Release what the provider holds."""
