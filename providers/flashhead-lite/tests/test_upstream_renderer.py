"""The renderer over upstream's own pipeline, against tiny CPU stand-in networks."""

from __future__ import annotations

import errno
import fcntl
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest
import torch
from stand_in_upstream import StandInCard, StandInModelError, StandInPipeline
from video_provider_contract import DecodedImage
from video_provider_flashhead_lite.renderer import RenderError, RenderShape
from video_provider_flashhead_lite.upstream import locate_weights
from video_provider_flashhead_lite.upstream_renderer import (
    CardMemory,
    UpstreamRenderer,
    allocator_fraction,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_MEBIBYTE = 1 << 20
_SMALL_PICTURE = {
    "height": 64,
    "width": 64,
    "frame_num": 17,
    "cached_audio_duration": 1,
}
_OTHER_PICTURE = {
    "height": 96,
    "width": 64,
    "frame_num": 25,
    "cached_audio_duration": 2,
}
_PIXEL_TOLERANCE = 1
_WARM_UP_SLICES = 2


@pytest.fixture(autouse=True, scope="module")
def _no_card() -> Iterator[None]:
    # CUDA initialises lazily, after this fixture and before any test reaches a card.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CUDA_VISIBLE_DEVICES", "")
        yield


class _Rig:
    """A renderer over a stand-in card, and the audio and images to drive it."""

    def __init__(self, card: StandInCard, renderer: UpstreamRenderer) -> None:
        self.card = card
        self.renderer = renderer

    @property
    def pipeline(self) -> StandInPipeline:
        assert self.card.loaded is not None
        assert isinstance(self.card.loaded.pipeline, StandInPipeline)
        return self.card.loaded.pipeline

    def start(self) -> RenderShape:
        self.shape = self.renderer.start()
        return self.shape

    def slice_of(self, loudness: float) -> np.ndarray:
        samples = self.shape.frames_per_slice * self.shape.sample_rate
        samples //= self.shape.frame_rate
        wave = np.sin(np.arange(samples) * 0.05)
        return (wave * loudness).astype(np.float32)


@pytest.fixture
def make_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Rig]:
    def make(
        overrides: dict[str, int] | None = None,
        *,
        cap_mebibytes: int = 1024,
        with_weights: bool = True,
        **card_options: object,
    ) -> _Rig:
        weights = locate_weights(tmp_path / ".venv")
        if with_weights:
            weights.root.mkdir(exist_ok=True)
        card = StandInCard(
            monkeypatch,
            overrides=_SMALL_PICTURE if overrides is None else overrides,
            **card_options,
        )
        return _Rig(
            card,
            UpstreamRenderer(
                cap_mebibytes=cap_mebibytes, weights=weights, card=card.calls()
            ),
        )

    return make


def _look(red: int, green: int, blue: int, side: int = 8) -> DecodedImage:
    """An image of a colour gradient, so colour matching has a picture to keep."""
    pixels = (
        bytes((base + 17 * column + 29 * row) % 256 for base in (red, green, blue))
        for row in range(side)
        for column in range(side)
    )
    return DecodedImage(side, side, b"".join(pixels))


def _open_descriptors() -> int:
    return len(list(Path("/proc/self/fd").iterdir()))


def _frames(rendered: bytes) -> np.ndarray:
    return np.frombuffer(rendered, dtype=np.uint8).astype(np.int16)


def _same_picture(first: bytes, second: bytes) -> bool:
    return (
        len(first) == len(second)
        and np.abs(_frames(first) - _frames(second)).max() <= _PIXEL_TOLERANCE
    )


@pytest.mark.parametrize(
    ("cap_mebibytes", "total_bytes"),
    [
        (1, 3 << 30),
        (7, 24 << 30),
        (24_000, 24 << 30),
        (24_564, 25_757_220_864),
        (1_539, 25_757_220_864),  # unrounded cap / total times total exceeds the cap
    ],
)
def test_the_allocator_fraction_never_lets_the_cap_be_exceeded(
    cap_mebibytes: int, total_bytes: int
) -> None:
    memory = CardMemory(free=total_bytes, total=total_bytes)

    fraction = allocator_fraction(cap_mebibytes, memory)

    assert 0 < fraction <= 1
    assert fraction * total_bytes <= cap_mebibytes * _MEBIBYTE


@pytest.mark.parametrize(
    ("cap_mebibytes", "free_mebibytes", "total_mebibytes"),
    [(9, 20, 8), (9, 8, 20)],
    ids=["above the total", "above the free memory"],
)
def test_a_cap_the_card_cannot_hold_is_refused(
    cap_mebibytes: int, free_mebibytes: int, total_mebibytes: int
) -> None:
    memory = CardMemory(
        free=free_mebibytes * _MEBIBYTE, total=total_mebibytes * _MEBIBYTE
    )

    with pytest.raises(RenderError):
        allocator_fraction(cap_mebibytes, memory)


def test_a_cap_equal_to_the_free_memory_is_served() -> None:
    memory = CardMemory(free=8 * _MEBIBYTE, total=16 * _MEBIBYTE)

    assert allocator_fraction(8, memory) == pytest.approx(0.5)


def test_the_weights_lie_beside_the_environment(tmp_path: Path) -> None:
    weights = locate_weights(tmp_path / "provider" / ".venv")

    assert weights.root == tmp_path / "provider" / "weights"
    assert weights.checkpoint.parent == weights.root
    assert weights.wav2vec.parent == weights.root
    assert weights.checkpoint != weights.wav2vec


def test_start_caps_the_allocator_before_it_loads_anything(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()

    rig.start()

    assert rig.card.events.index("limit") < rig.card.events.index("load")
    assert rig.card.fraction is not None


def test_start_loads_from_the_weights_the_provider_owns(
    make_rig: Callable[..., _Rig], tmp_path: Path
) -> None:
    rig = make_rig()

    rig.start()

    assert rig.card.weights == locate_weights(tmp_path / ".venv")


@pytest.mark.parametrize("overrides", [_SMALL_PICTURE, _OTHER_PICTURE])
def test_the_shape_follows_upstreams_parameters(
    make_rig: Callable[..., _Rig], overrides: dict[str, int]
) -> None:
    rig = make_rig(overrides)

    shape = rig.start()
    parameters = rig.card.loaded.inference.get_infer_params()

    assert shape.sample_rate == parameters["sample_rate"]
    assert shape.frame_rate == parameters["tgt_fps"]
    assert shape.width == overrides["width"]
    assert shape.height == overrides["height"]
    assert (
        shape.frames_per_slice
        == parameters["frame_num"] - parameters["motion_frames_num"]
    )


def test_start_warms_up_with_two_slices_before_it_reports_the_shape(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()

    rig.start()

    assert len(rig.pipeline.heard) == _WARM_UP_SLICES


def test_the_warm_up_look_can_never_be_opened(make_rig: Callable[..., _Rig]) -> None:
    rig = make_rig()
    rig.start()

    prepared_by_warm_up = rig.pipeline.person_name
    with pytest.raises(RenderError):
        rig.renderer.open_session(prepared_by_warm_up)


@pytest.mark.parametrize(
    "card_options",
    [
        {"visible": False},
        {"free_bytes": 1 * _MEBIBYTE},
        {"total_bytes": 1 * _MEBIBYTE, "free_bytes": 1 * _MEBIBYTE},
    ],
    ids=["no card", "above the free memory", "above the total"],
)
def test_start_refuses_before_it_caps_or_loads_when_the_card_cannot_serve(
    make_rig: Callable[..., _Rig], card_options: dict[str, object]
) -> None:
    rig = make_rig(**card_options)

    with pytest.raises(RenderError):
        rig.start()

    assert "limit" not in rig.card.events
    assert "load" not in rig.card.events


def test_start_refuses_a_missing_weights_directory_before_it_loads(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig(with_weights=False)

    with pytest.raises(RenderError):
        rig.start()

    assert "load" not in rig.card.events


def test_a_renderer_that_is_not_started_refuses_every_look(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()

    with pytest.raises(RenderError):
        rig.renderer.prepare("mozart", _look(10, 20, 30))
    with pytest.raises(RenderError):
        rig.renderer.open_session("mozart")


def test_an_unknown_look_is_refused(make_rig: Callable[..., _Rig]) -> None:
    rig = make_rig()
    rig.start()

    with pytest.raises(RenderError):
        rig.renderer.open_session("mozart")


def test_a_closed_renderer_serves_no_look(make_rig: Callable[..., _Rig]) -> None:
    rig = make_rig()
    rig.start()
    rig.renderer.prepare("mozart", _look(10, 20, 30))

    rig.renderer.close()

    with pytest.raises(RenderError):
        rig.renderer.open_session("mozart")


def test_upstreams_own_reader_sees_the_image_exactly(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    rig.start()
    image = DecodedImage(3, 2, bytes(range(18)))

    rig.renderer.prepare("mozart", image)

    (seen,) = rig.pipeline.cond_image_dict.values()
    assert seen.size == (image.width, image.height)
    assert seen.mode == "RGB"
    assert seen.tobytes() == image.rgb


def test_preparing_leaves_no_descriptor_open(make_rig: Callable[..., _Rig]) -> None:
    rig = make_rig()
    rig.start()
    before = _open_descriptors()

    rig.renderer.prepare("mozart", _look(10, 20, 30))

    assert _open_descriptors() == before


def test_a_failed_prepare_leaves_no_descriptor_open_and_passes_the_error_on(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    rig.start()
    rig.pipeline.vae.fails = True
    before = _open_descriptors()

    with pytest.raises(StandInModelError):
        rig.renderer.prepare("mozart", _look(10, 20, 30))

    assert _open_descriptors() == before


def test_the_look_is_read_from_a_close_on_exec_descriptor(
    make_rig: Callable[..., _Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    rig.start()
    pipeline = rig.pipeline
    upstream_prepare = pipeline.prepare_params
    flags: list[int] = []

    def reading(**options: object) -> None:
        descriptor = int(Path(str(options["cond_image_path_or_dir"])).name)
        flags.append(fcntl.fcntl(descriptor, fcntl.F_GETFD))
        upstream_prepare(**options)

    monkeypatch.setattr(pipeline, "prepare_params", reading)

    rig.renderer.prepare("mozart", _look(10, 20, 30))

    (descriptor_flags,) = flags
    assert descriptor_flags & fcntl.FD_CLOEXEC


def test_a_memory_file_the_system_refuses_is_raised_with_its_errno(
    make_rig: Callable[..., _Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    rig.start()

    class _RefusingLibrary:
        @staticmethod
        def memfd_create(_name: bytes, _flags: int) -> int:
            return -1

    monkeypatch.setattr("ctypes.CDLL", lambda *_a, **_k: _RefusingLibrary())
    monkeypatch.setattr("ctypes.get_errno", lambda: errno.EMFILE)

    with pytest.raises(OSError, match="Too many open files") as raised:
        rig.renderer.prepare("mozart", _look(10, 20, 30))

    assert raised.value.errno == errno.EMFILE


def test_a_slice_is_the_renders_frames_as_uint8_rgb_without_the_motion_frames(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    shape = rig.start()
    rig.renderer.prepare("mozart", _look(200, 100, 50))
    session = rig.renderer.open_session("mozart")

    rendered = session.render(rig.slice_of(0.5))

    assert isinstance(rendered, bytes)
    assert len(rendered) == shape.frames_per_slice * shape.width * shape.height * 3


def test_two_looks_each_draw_from_their_own_look(
    make_rig: Callable[..., _Rig],
) -> None:
    mozart_look, bach_look = _look(200, 40, 40), _look(30, 30, 220)
    together = make_rig()
    together.start()
    together.renderer.prepare("mozart", mozart_look)
    together.renderer.prepare("bach", bach_look)
    mozart = together.renderer.open_session("mozart")
    bach = together.renderer.open_session("bach")
    audio = together.slice_of(0.5)
    first = {"mozart": mozart.render(audio), "bach": bach.render(audio)}
    second = {"mozart": mozart.render(audio), "bach": bach.render(audio)}

    for name, look in (("mozart", mozart_look), ("bach", bach_look)):
        alone = make_rig()
        alone.start()
        alone.renderer.prepare(name, look)
        lone = alone.renderer.open_session(name)
        assert _same_picture(first[name], lone.render(audio))
        assert _same_picture(second[name], lone.render(audio))
    assert not _same_picture(first["mozart"], first["bach"])


def test_two_sessions_on_one_look_keep_their_own_motion_and_window(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    rig.start()
    rig.renderer.prepare("mozart", _look(200, 40, 40))
    busy = rig.renderer.open_session("mozart")
    quiet = rig.renderer.open_session("mozart")
    for _ in range(3):
        busy.render(rig.slice_of(0.9))
    silence = rig.slice_of(0.0)
    quiet_render = quiet.render(silence)

    alone = make_rig()
    alone.start()
    alone.renderer.prepare("mozart", _look(200, 40, 40))
    lone_render = alone.renderer.open_session("mozart").render(silence)

    assert _same_picture(quiet_render, lone_render)


def test_a_look_prepared_again_changes_new_sessions_and_leaves_open_ones(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    rig.start()
    rig.renderer.prepare("mozart", _look(200, 40, 40))
    open_before = rig.renderer.open_session("mozart")
    audio = rig.slice_of(0.5)
    first = open_before.render(audio)
    rig.renderer.prepare("mozart", _look(30, 30, 220))
    open_after = rig.renderer.open_session("mozart")
    second = open_before.render(audio)
    new_first = open_after.render(audio)

    alone = make_rig()
    alone.start()
    alone.renderer.prepare("mozart", _look(200, 40, 40))
    lone = alone.renderer.open_session("mozart")
    lone.render(audio)
    lone_second = lone.render(audio)

    assert _same_picture(second, lone_second)
    assert not _same_picture(first, new_first)


def test_the_audio_window_drops_what_is_older_than_its_length(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    rig.start()
    rig.renderer.prepare("mozart", _look(200, 40, 40))
    heard = rig.pipeline.heard
    loud_then_silent = rig.renderer.open_session("mozart")
    always_silent = rig.renderer.open_session("mozart")
    silence = rig.slice_of(0.0)
    loud_then_silent.render(rig.slice_of(0.9))
    start = len(heard)
    for _ in range(4):
        loud_then_silent.render(silence)
        always_silent.render(silence)

    assert torch.equal(heard[start + 6], heard[start + 7])
    assert not torch.equal(heard[start], heard[start + 1])


@pytest.mark.parametrize("failing_call", ["prepare", "render"])
def test_an_upstream_exception_is_not_converted(
    make_rig: Callable[..., _Rig], failing_call: str
) -> None:
    rig = make_rig()
    rig.start()
    rig.renderer.prepare("mozart", _look(200, 40, 40))
    session = rig.renderer.open_session("mozart")
    rig.pipeline.model.fails = True
    rig.pipeline.vae.fails = True
    calls = {
        "prepare": lambda: rig.renderer.prepare("bach", _look(1, 2, 3)),
        "render": lambda: session.render(rig.slice_of(0.5)),
    }

    with pytest.raises(StandInModelError):
        calls[failing_call]()
