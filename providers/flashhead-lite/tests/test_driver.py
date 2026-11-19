"""The driver turns the port's calls into slice renders on the renderer's timeline."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest
from deterministic_renderer import (
    SHAPE,
    SLICE_SAMPLES,
    DeterministicRenderer,
    DeterministicRenderSession,
    frame_of,
)
from video_provider_contract import (
    DecodedImage,
    Frame,
    ProviderFailedError,
    ProviderSession,
    ProviderVerb,
    ProviderVideoFormat,
)
from video_provider_flashhead_lite.driver import FlashHeadLiteProvider

if TYPE_CHECKING:
    from collections.abc import Callable

_LOUD = 8192
_SLICE_FRAMES = SHAPE.frames_per_slice
_WIRE_RATES = (16_000, 22_050, 24_000, 48_000)


def _loud(samples: int) -> bytes:
    return np.full(samples, _LOUD, dtype="<i2").tobytes()


def _slice_of_wire_audio(rate: int, slices: float) -> int:
    return int(slices * SLICE_SAMPLES * rate / SHAPE.sample_rate)


def _open() -> tuple[ProviderSession, DeterministicRenderSession]:
    renderer = DeterministicRenderer()
    provider = FlashHeadLiteProvider(renderer)
    provider.start()
    session = provider.open_session("mozart")
    return session, renderer.sessions[0]


def _feed(session: ProviderSession, samples: int, rate: int) -> list[Frame]:
    return list(session.feed(_loud(samples), rate))


@pytest.mark.parametrize("rate", _WIRE_RATES)
def test_audio_at_each_wire_rate_reaches_the_renderer_as_float_slices(
    rate: int,
) -> None:
    session, rendered = _open()
    period = rate // SHAPE.frame_rate
    three_slices = 3 * _SLICE_FRAMES
    frames: list[Frame] = []
    for _ in range(three_slices):
        frames += session.feed(_loud(period), rate)
    frames += session.finish_sentence()

    assert len(frames) == three_slices
    assert {audio.shape for audio in rendered.slices} == {(SLICE_SAMPLES,)}
    assert {audio.dtype for audio in rendered.slices} == {np.dtype("float32")}
    heard = np.concatenate(rendered.slices)
    assert heard.max() == pytest.approx(_LOUD / 32768, abs=0.05)


def test_a_feed_smaller_than_a_slice_draws_nothing() -> None:
    session, rendered = _open()

    assert _feed(session, _slice_of_wire_audio(16_000, 0.5), 16_000) == []
    assert rendered.slices == []


def test_a_feed_spanning_slices_renders_each_full_slice_in_order() -> None:
    session, rendered = _open()

    frames = _feed(session, _slice_of_wire_audio(16_000, 2.5), 16_000)

    assert [frame.rgb for frame in frames] == [
        frame_of(render, place) for render in (1, 2) for place in range(_SLICE_FRAMES)
    ]
    assert len(rendered.slices) == len(frames) // _SLICE_FRAMES


def test_finish_pads_the_last_slice_with_silence_and_returns_the_rest() -> None:
    session, rendered = _open()
    fed = _feed(session, _slice_of_wire_audio(16_000, 1.5), 16_000)

    finished = list(session.finish_sentence())

    assert len(fed) == _SLICE_FRAMES
    assert [frame.rgb for frame in finished] == [
        frame_of(2, place) for place in range(_SLICE_FRAMES // 2)
    ]
    assert len(rendered.slices) == len(fed) // _SLICE_FRAMES + 1
    assert rendered.slices[1].any()
    assert not rendered.slices[1][-1]


@pytest.mark.parametrize(
    ("rate", "samples", "frames"),
    [
        (16_000, 1, 1),
        (16_000, 7_680, 12),
        (22_050, 22_050, 25),
        (24_000, 10_001, 11),
        (48_000, 47_999, 25),
        (48_000, 48_001, 26),
    ],
)
def test_a_sentence_yields_the_frames_of_its_wire_samples_rounded_up(
    rate: int, samples: int, frames: int
) -> None:
    session, _ = _open()
    fed = _feed(session, samples, rate)

    finished = list(session.finish_sentence())

    assert len(fed) + len(finished) == frames


def test_a_flushed_tail_spanning_two_slices_is_drawn_in_full() -> None:
    session, rendered = _open()
    fed = _feed(session, _slice_of_wire_audio(48_000, 1) + 1, 48_000)

    finished = list(session.finish_sentence())

    assert len(fed) + len(finished) == _SLICE_FRAMES + 1
    assert len(rendered.slices) == len(fed) // _SLICE_FRAMES + 2


def test_an_empty_sentence_yields_no_frame() -> None:
    session, rendered = _open()

    assert list(session.finish_sentence()) == []
    assert rendered.slices == []


def test_idle_renders_a_slice_of_silence_when_no_idle_frame_is_left() -> None:
    session, rendered = _open()

    frames = [session.idle_frame() for _ in range(5)]

    assert [frame.rgb for frame in frames] == [
        *(frame_of(1, place) for place in range(4)),
        frame_of(2, 0),
    ]
    assert len(rendered.slices) == len(frames) // _SLICE_FRAMES + 1
    assert not any(audio.any() for audio in rendered.slices)


def test_idle_while_a_sentence_is_open_takes_none_of_its_audio() -> None:
    session, rendered = _open()
    half = _slice_of_wire_audio(16_000, 0.5)
    _feed(session, half, 16_000)

    session.idle_frame()
    frames = _feed(session, half, 16_000)

    assert len(frames) == _SLICE_FRAMES
    assert len(rendered.slices) == len(frames) // _SLICE_FRAMES + 1
    assert rendered.slices[1].any()
    assert not rendered.slices[0].any()
    assert rendered.slices[1][: SLICE_SAMPLES // 4].any()


def test_no_idle_frame_drawn_before_speech_leaves_after_it() -> None:
    session, rendered = _open()
    session.idle_frame()
    _feed(session, _slice_of_wire_audio(16_000, 1), 16_000)

    session.idle_frame()

    assert [bool(audio.any()) for audio in rendered.slices] == [False, True, False]


def test_a_rate_change_between_sentences_keeps_each_sentence_at_its_own_rate() -> None:
    session, _ = _open()
    first = _feed(session, 48_000, 48_000) + list(session.finish_sentence())
    second = _feed(session, 8_000, 16_000) + list(session.finish_sentence())

    assert (len(first), len(second)) == (25, 13)


def test_clear_drops_the_buffered_audio_and_the_next_sentence_starts_fresh() -> None:
    session, rendered = _open()
    half = _slice_of_wire_audio(16_000, 0.5)
    _feed(session, half, 16_000)

    session.clear()

    assert _feed(session, half, 16_000) == []
    assert rendered.slices == []
    assert len(list(session.finish_sentence())) == _SLICE_FRAMES // 2


def test_the_render_session_sees_every_slice_and_a_clear_does_not_reset_it() -> None:
    session, rendered = _open()
    session.idle_frame()
    _feed(session, _slice_of_wire_audio(16_000, 1), 16_000)
    session.clear()
    session.idle_frame()
    for _ in range(3):
        session.idle_frame()
    session.idle_frame()

    assert [bool(audio.any()) for audio in rendered.slices] == [
        False,
        True,
        False,
        False,
    ]
    assert not rendered.closed


def test_start_reports_the_renderers_picture_and_the_verbs() -> None:
    renderer = DeterministicRenderer()
    provider = FlashHeadLiteProvider(renderer)

    info = provider.start()

    assert renderer.started
    assert info.plays == (ProviderVerb.SPEAK, ProviderVerb.CLEAR)
    assert info.video == ProviderVideoFormat(
        width=SHAPE.width, height=SHAPE.height, frame_rate=SHAPE.frame_rate
    )


def test_prepare_passes_the_image_on_untouched() -> None:
    renderer = DeterministicRenderer()
    provider = FlashHeadLiteProvider(renderer)
    image = DecodedImage(width=1, height=1, rgb=bytes(3))

    provider.prepare("mozart", image)

    assert renderer.prepared["mozart"] is image


def test_closing_the_session_and_the_provider_reaches_the_renderer() -> None:
    renderer = DeterministicRenderer()
    provider = FlashHeadLiteProvider(renderer)
    provider.start()
    session = provider.open_session("mozart")

    session.close()
    provider.close()

    assert renderer.sessions[0].closed
    assert renderer.closed


def test_a_session_cannot_open_before_the_provider_started() -> None:
    provider = FlashHeadLiteProvider(DeterministicRenderer())

    with pytest.raises(ProviderFailedError):
        provider.open_session("mozart")


def _start(provider: FlashHeadLiteProvider) -> None:
    provider.start()


def _prepare(provider: FlashHeadLiteProvider) -> None:
    provider.prepare("mozart", DecodedImage(width=1, height=1, rgb=bytes(3)))


def _open_session(provider: FlashHeadLiteProvider) -> None:
    provider.start()
    provider.open_session("mozart")


def _render(provider: FlashHeadLiteProvider) -> None:
    provider.start()
    provider.open_session("mozart").idle_frame()


@pytest.mark.parametrize(
    ("call", "drive"),
    [
        ("start", _start),
        ("prepare", _prepare),
        ("open_session", _open_session),
        ("render", _render),
    ],
)
def test_a_renderer_error_leaves_as_a_provider_failure(
    call: str, drive: Callable[[FlashHeadLiteProvider], None]
) -> None:
    provider = FlashHeadLiteProvider(DeterministicRenderer(fails_in=call))

    with pytest.raises(ProviderFailedError):
        drive(provider)
