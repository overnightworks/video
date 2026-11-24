"""The fake provider frees the audio it drew; its environment sets its renders."""

from __future__ import annotations

import tracemalloc

import video_provider_fake
from video_provider_fake import FakeProvider
from video_provider_fake.worker import settings_from

_RATE = 16_000
_CHUNK_SECONDS = 0.1
_SENTENCE_SECONDS = 20
_UNPLAYED_CHUNKS = 10


def _held_by_the_fake() -> int:
    """The bytes the fake's own lines allocated and still hold."""
    snapshot = tracemalloc.take_snapshot().filter_traces(
        [
            tracemalloc.Filter(
                inclusive=True, filename_pattern=video_provider_fake.__file__
            )
        ]
    )
    return sum(statistic.size for statistic in snapshot.statistics("filename"))


def test_played_and_closed_audio_is_released() -> None:
    chunk = bytes(2 * int(_RATE * _CHUNK_SECONDS))
    chunks = int(_SENTENCE_SECONDS / _CHUNK_SECONDS)
    session = FakeProvider().open_session("mozart")
    traced_before = tracemalloc.is_tracing()
    if not traced_before:
        tracemalloc.start()
    try:
        # What the interpreter caches while the fake plays is held from here on,
        # whatever ran before; the measure starts with the buffer empty, and only
        # what the audio adds after this point counts.
        for _ in range(chunks // 2):
            list(session.feed(chunk, _RATE))
        session.clear()
        held_before = _held_by_the_fake()
        for _ in range(chunks // 2):
            list(session.feed(chunk, _RATE))
        played = _held_by_the_fake() - held_before
        session.clear()
        for _ in range(_UNPLAYED_CHUNKS):
            session.feed(chunk, _RATE)
        session.close()
        closed = _held_by_the_fake() - held_before
    finally:
        if not traced_before:
            tracemalloc.stop()

    # A fake that kept audio would hold many chunks here, far above the bound.
    assert played < len(chunk)
    assert closed < len(chunk)


def test_the_worker_s_environment_sets_the_picture_and_the_cost_of_its_renders() -> (
    None
):
    slept: list[float] = []
    settings = settings_from(
        {
            "FAKE_WIDTH": "512",
            "FAKE_HEIGHT": "384",
            "FAKE_CHUNK_FRAMES": "2",
            "FAKE_RENDER_SECONDS": "0.6",
            "FAKE_IDLE_RENDER_SECONDS": "0.25",
        }
    )
    provider = FakeProvider(**settings, sleep=slept.append)

    video = provider.start().video
    session = provider.open_session("mozart")
    frame = session.idle_frame()

    assert (video.width, video.height) == (512, 384)
    assert len(frame.rgb) == 512 * 384 * 3
    assert slept == [0.25]
    list(session.feed(bytes(2 * _RATE // 25 * 2), _RATE))
    assert slept == [0.25, 0.6]


def test_an_environment_naming_no_setting_leaves_the_defaults() -> None:
    assert settings_from({}) == {}
