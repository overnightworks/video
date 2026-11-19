"""The seam between the driver and a model that renders slices of audio into frames.

A renderer loads the model and reports its shape, prepares a look from a decoded
image and opens one render session per look. A render session owns what the model
heard before (its audio window and motion state): the driver's contract with it is
the slices it hands over, in order, silence included.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray
    from video_provider_contract import DecodedImage


class RenderError(Exception):
    """The renderer could not start, prepare a look, open a session or render."""


@dataclass(frozen=True, slots=True)
class RenderShape:
    """What a started renderer hears and draws: `frames_per_slice` frames a slice."""

    sample_rate: int
    frame_rate: int
    frames_per_slice: int
    width: int
    height: int


class RenderSession(Protocol):
    """One look's render state."""

    def render(self, audio: NDArray[np.float32]) -> bytes:
        """Hear one slice of mono audio at the renderer's rate.

        Returns exactly that slice's frames, each `width` times `height` pixels of
        three bytes, row by row. Raises `RenderError` when it cannot draw.
        """

    def close(self) -> None:
        """Release the session."""


class Renderer(Protocol):
    """The model behind the driver."""

    def start(self) -> RenderShape:
        """Load and warm up the model and report its shape, or raise `RenderError`."""

    def prepare(self, look: str, image: DecodedImage) -> None:
        """Prepare `look` from `image`, or raise `RenderError`."""

    def open_session(self, look: str) -> RenderSession:
        """Open a render session on a prepared `look`, or raise `RenderError`."""

    def close(self) -> None:
        """Release the model."""
