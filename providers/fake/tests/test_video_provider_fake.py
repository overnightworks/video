"""The fake provider keeps only the audio its frames have yet to draw."""

from __future__ import annotations

import tracemalloc

import video_provider_fake
from video_provider_fake import FakeProvider

_RATE = 16_000
_CHUNK_SECONDS = 0.1
_SENTENCE_SECONDS = 20


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
    session = FakeProvider().open_session("mozart")
    chunk = bytes(2 * int(_RATE * _CHUNK_SECONDS))
    traced_before = tracemalloc.is_tracing()
    if not traced_before:
        tracemalloc.start()
    try:
        held_before = _held_by_the_fake()
        for _ in range(int(_SENTENCE_SECONDS / _CHUNK_SECONDS)):
            list(session.feed(chunk, _RATE))
        played = _held_by_the_fake() - held_before
        session.feed(chunk, _RATE)
        session.close()
        closed = _held_by_the_fake() - held_before
    finally:
        if not traced_before:
            tracemalloc.stop()

    assert played < len(chunk)
    assert closed < len(chunk)
