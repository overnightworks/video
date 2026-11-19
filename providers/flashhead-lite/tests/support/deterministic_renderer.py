"""A renderer without a model: each frame names its render and its place."""

from __future__ import annotations

from typing import TYPE_CHECKING

from video_provider_flashhead_lite.renderer import RenderError, RenderShape

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray
    from video_provider_contract import DecodedImage

SHAPE = RenderShape(
    sample_rate=16_000, frame_rate=25, frames_per_slice=4, width=2, height=2
)
SLICE_SAMPLES = 2560
FRAME_BYTES = SHAPE.width * SHAPE.height * 3


def frame_of(render: int, place: int) -> bytes:
    """The frame the `render`th render of a session draws at `place` in its slice."""
    return (bytes([render, place]) * FRAME_BYTES)[:FRAME_BYTES]


class DeterministicRenderSession:
    """Records every slice it hears and draws frames that name where they came from."""

    def __init__(self, shape: RenderShape, *, fails_in: str | None) -> None:
        """Draw `shape`'s slices, failing in the call `fails_in` names."""
        self._shape = shape
        self._fails_in = fails_in
        self.slices: list[NDArray[np.float32]] = []
        self.closed = False

    def render(self, audio: NDArray[np.float32]) -> bytes:
        """Hear `audio` and draw the slice's frames."""
        if self._fails_in == "render":
            message = "the renderer was told to fail to render"
            raise RenderError(message)
        self.slices.append(audio.copy())
        render = len(self.slices)
        return b"".join(
            frame_of(render, place) for place in range(self._shape.frames_per_slice)
        )

    def close(self) -> None:
        """Note the close."""
        self.closed = True


class DeterministicRenderer:
    """Reports a small fixed shape and opens recording render sessions."""

    def __init__(self, *, fails_in: str | None = None) -> None:
        """Fail in the call `fails_in` names: `start`, `prepare` or `open_session`."""
        self._fails_in = fails_in
        self.started = False
        self.closed = False
        self.prepared: dict[str, DecodedImage] = {}
        self.sessions: list[DeterministicRenderSession] = []

    def start(self) -> RenderShape:
        """Report the shape."""
        self._fail_if("start")
        self.started = True
        return SHAPE

    def prepare(self, look: str, image: DecodedImage) -> None:
        """Keep `image` for `look`."""
        self._fail_if("prepare")
        self.prepared[look] = image

    def open_session(self, look: str) -> DeterministicRenderSession:
        """Open a session; the look is not told apart."""
        del look
        self._fail_if("open_session")
        session = DeterministicRenderSession(SHAPE, fails_in=self._fails_in)
        self.sessions.append(session)
        return session

    def close(self) -> None:
        """Note the close."""
        self.closed = True

    def _fail_if(self, call: str) -> None:
        if self._fails_in == call:
            message = f"the renderer was told to fail in {call}"
            raise RenderError(message)
