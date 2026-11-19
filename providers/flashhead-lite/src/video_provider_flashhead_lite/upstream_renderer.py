"""The renderer that calls upstream SoulX-FlashHead unchanged.

Upstream's pipeline holds one set of per-look and per-session state, so the
renderer keeps each look's and each session's own and sets upstream's three
attributes before every render: the colour reference, the reference latent and the
motion latent. Every number of the picture and the audio comes from upstream's
own parameters. The calls that need a card (its memory, the allocator, the load of
upstream) are handed in as `CardCalls`, so nothing here touches the card. Upstream's
own exceptions are not converted: a card error can leave the card unusable, so they
end the provider.
"""

from __future__ import annotations

import contextlib
import ctypes
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np
from video_provider_contract import DecodedImage

from video_provider_flashhead_lite.renderer import RenderError, RenderShape

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    import torch
    from numpy.typing import NDArray

    from video_provider_flashhead_lite.renderer import RenderSession
    from video_provider_flashhead_lite.upstream import Weights

_MODEL_TYPE = "lite"
_WORLD_SIZE = 1
_USE_FACE_CROP = False
_SEED = 9999
_WARM_UP_SLICES = 2
_WARM_UP_GREY = 128
_BYTES_PER_MEBIBYTE = 1 << 20
_PPM_MAXIMUM_VALUE = 255
_MFD_CLOEXEC = 1


@dataclass(frozen=True, slots=True)
class CardMemory:
    """The card's free and total memory in bytes."""

    free: int
    total: int


class UpstreamPipeline(Protocol):
    """The per-look and per-session state of upstream's pipeline."""

    original_color_reference: torch.Tensor
    ref_img_latent: torch.Tensor
    latent_motion_frames: torch.Tensor


class UpstreamInference(Protocol):
    """The helpers of upstream's `flash_head.inference` the renderer calls."""

    get_infer_params: Callable[[], Mapping[str, int]]
    """A copy of upstream's parameters."""

    get_base_data: Callable[[UpstreamPipeline, str, int, bool], None]
    """Prepare the pipeline's per-look state: pipeline, image file, seed, face crop."""

    get_audio_embedding: Callable[
        [UpstreamPipeline, NDArray[np.float32], int, int], torch.Tensor
    ]
    """The embedding of an audio window between two frame indices."""

    run_pipeline: Callable[[UpstreamPipeline, torch.Tensor], torch.Tensor]
    """Render one slice: frames by height, width and RGB, values from 0 to 255."""


@dataclass(frozen=True, slots=True)
class LoadedUpstream:
    """Upstream's inference module and the pipeline `get_pipeline` built."""

    inference: UpstreamInference
    pipeline: UpstreamPipeline


@dataclass(frozen=True, slots=True)
class CardCalls:
    """The calls that need a card; `memory` is `None` when no card is visible."""

    memory: Callable[[], CardMemory | None]
    limit_allocator: Callable[[float], None]
    load: Callable[..., LoadedUpstream]


def allocator_fraction(cap_mebibytes: int, memory: CardMemory) -> float:
    """The share of the card's total memory the allocator may use under the cap.

    The product of the fraction and the total never exceeds the cap.

    Raises:
        RenderError: the cap exceeds the card's total or free memory.
    """
    cap = cap_mebibytes * _BYTES_PER_MEBIBYTE
    if cap > memory.total or cap > memory.free:
        message = "the card cannot hold the memory cap"
        raise RenderError(message)
    return math.nextafter(cap / memory.total, 0.0)


@dataclass(frozen=True, slots=True)
class _Look:
    color_reference: torch.Tensor
    reference_latent: torch.Tensor
    motion_latent: torch.Tensor


@dataclass(frozen=True, slots=True)
class _Started:
    upstream: LoadedUpstream
    shape: RenderShape
    motion_frames: int
    window_samples: int
    window_start: int
    window_end: int


class _UpstreamSession:
    """One look's render state: its audio window and its motion latent."""

    def __init__(self, started: _Started, look: _Look) -> None:
        self._started = started
        self._look = look
        self._motion = look.motion_latent
        self._window: NDArray[np.float32] = np.zeros(started.window_samples, np.float32)

    def render(self, audio: NDArray[np.float32]) -> bytes:
        """Hear one slice and draw its frames, without the motion frames."""
        upstream = self._started.upstream
        self._window = np.concatenate([self._window, audio])[
            -self._started.window_samples :
        ]
        embedding = upstream.inference.get_audio_embedding(
            upstream.pipeline,
            self._window,
            self._started.window_start,
            self._started.window_end,
        )
        upstream.pipeline.original_color_reference = self._look.color_reference
        upstream.pipeline.ref_img_latent = self._look.reference_latent
        upstream.pipeline.latent_motion_frames = self._motion
        frames = upstream.inference.run_pipeline(upstream.pipeline, embedding)
        self._motion = upstream.pipeline.latent_motion_frames
        drawn = frames[self._started.motion_frames :]
        return drawn.cpu().numpy().astype(np.uint8).tobytes()

    def close(self) -> None:
        """Nothing is held beyond the session's own state."""


class UpstreamRenderer:
    """Renders slices with upstream's Lite model on the card."""

    def __init__(
        self, *, cap_mebibytes: int, weights: Weights, card: CardCalls
    ) -> None:
        """Hold the card to `cap_mebibytes` and load Lite from `weights`."""
        self._cap_mebibytes = cap_mebibytes
        self._weights = weights
        self._card = card
        self._started: _Started | None = None
        self._looks: dict[str, _Look] = {}

    def start(self) -> RenderShape:
        """Cap the allocator, load Lite, warm it up and report its shape.

        Raises:
            RenderError: no card is visible, the cap exceeds the card's total or
                free memory, or the weights directory is missing.
        """
        memory = self._card.memory()
        if memory is None:
            message = "no card is visible"
            raise RenderError(message)
        fraction = allocator_fraction(self._cap_mebibytes, memory)
        if not self._weights.root.is_dir():
            message = "the weights directory is missing"
            raise RenderError(message)
        self._card.limit_allocator(fraction)
        upstream = self._card.load(
            self._weights, model_type=_MODEL_TYPE, world_size=_WORLD_SIZE
        )
        started = _started_from(upstream)
        self._warm_up(started)
        self._started = started
        return started.shape

    def prepare(self, look: str, image: DecodedImage) -> None:
        """Prepare `look` from `image` through upstream's own prepare."""
        self._looks[look] = _prepare_look(self._require_started(), image)

    def open_session(self, look: str) -> RenderSession:
        """Open a render session on a prepared `look`."""
        started = self._require_started()
        try:
            prepared = self._looks[look]
        except KeyError:
            message = "the look is unknown"
            raise RenderError(message) from None
        return _UpstreamSession(started, prepared)

    def close(self) -> None:
        """Drop the model and every look."""
        self._started = None
        self._looks.clear()

    def _require_started(self) -> _Started:
        if self._started is None:
            message = "the renderer is not started"
            raise RenderError(message)
        return self._started

    @staticmethod
    def _warm_up(started: _Started) -> None:
        # The look lives only here, so no look name opens it.
        shape = started.shape
        grey = bytes([_WARM_UP_GREY]) * (shape.width * shape.height * 3)
        look = _prepare_look(started, DecodedImage(shape.width, shape.height, grey))
        session = _UpstreamSession(started, look)
        slice_samples = shape.frames_per_slice * shape.sample_rate // shape.frame_rate
        for _ in range(_WARM_UP_SLICES):
            session.render(np.zeros(slice_samples, np.float32))
        session.close()


def _started_from(upstream: LoadedUpstream) -> _Started:
    parameters = upstream.inference.get_infer_params()
    sample_rate = parameters["sample_rate"]
    frame_rate = parameters["tgt_fps"]
    frame_count = parameters["frame_num"]
    motion_frames = parameters["motion_frames_num"]
    window_end = parameters["cached_audio_duration"] * frame_rate
    return _Started(
        upstream=upstream,
        shape=RenderShape(
            sample_rate=sample_rate,
            frame_rate=frame_rate,
            frames_per_slice=frame_count - motion_frames,
            width=parameters["width"],
            height=parameters["height"],
        ),
        motion_frames=motion_frames,
        window_samples=parameters["cached_audio_duration"] * sample_rate,
        window_start=window_end - frame_count,
        window_end=window_end,
    )


def _prepare_look(started: _Started, image: DecodedImage) -> _Look:
    upstream = started.upstream
    with _memory_file(_binary_ppm(image)) as path:
        upstream.inference.get_base_data(
            upstream.pipeline, str(path), _SEED, _USE_FACE_CROP
        )
    pipeline = upstream.pipeline
    return _Look(
        color_reference=pipeline.original_color_reference,
        reference_latent=pipeline.ref_img_latent,
        motion_latent=pipeline.latent_motion_frames,
    )


def _binary_ppm(image: DecodedImage) -> bytes:
    header = f"P6\n{image.width} {image.height}\n{_PPM_MAXIMUM_VALUE}\n"
    return header.encode("ascii") + image.rgb


@contextlib.contextmanager
def _memory_file(content: bytes) -> Iterator[Path]:
    """An anonymous, close-on-exec memory file holding `content`, as a path.

    Python's `os.memfd_create` is missing on some builds, so glibc's is called.
    """
    library = ctypes.CDLL(None, use_errno=True)
    descriptor = library.memfd_create(b"look", _MFD_CLOEXEC)
    if descriptor < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    try:
        with open(descriptor, "wb", closefd=False) as file:
            file.write(content)
        yield Path(f"/proc/self/fd/{descriptor}")
    finally:
        os.close(descriptor)
