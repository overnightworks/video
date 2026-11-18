"""A provider that holds no model: it reports a fixed picture and keeps its images.

It serves the service's own tests and a run from source; no image installs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from video_provider_contract import (
    DecodedImage,
    ProviderFailedError,
    ProviderInfo,
)

from video_client.wire import Verb, VideoFormat

if TYPE_CHECKING:
    from collections.abc import Collection

INFO = ProviderInfo(
    plays=(Verb.SPEAK, Verb.CLEAR),
    video=VideoFormat(width=256, height=256, frame_rate=25),
)


class FakeProvider:
    """Starts at once and prepares every look it is not told to refuse."""

    def __init__(
        self,
        *,
        fails_to_start: bool = False,
        refuses: Collection[str] = (),
    ) -> None:
        """Fail `start` when told to, and `prepare` for each look in `refuses`."""
        self._fails_to_start = fails_to_start
        self._refuses = frozenset(refuses)
        self.prepared: dict[str, DecodedImage] = {}
        self.closed = False

    def start(self) -> ProviderInfo:
        """Report the fixed picture, or fail as told."""
        if self._fails_to_start:
            message = "the fake provider was told to fail"
            raise ProviderFailedError(message)
        return INFO

    def prepare(self, look: str, image: DecodedImage) -> None:
        """Keep `image` for `look`, or fail as told."""
        if look in self._refuses:
            message = "the fake provider was told to refuse the look"
            raise ProviderFailedError(message)
        self.prepared[look] = image

    def close(self) -> None:
        """Note that the service closed the provider."""
        self.closed = True
