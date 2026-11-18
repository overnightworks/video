"""The fake provider keeps only the audio its frames have yet to draw."""

from __future__ import annotations

import tracemalloc

from video_provider_fake import FakeProvider

_RATE = 16_000
_CHUNK_SECONDS = 0.1
_SENTENCE_SECONDS = 20


def test_played_and_closed_audio_is_released() -> None:
    session = FakeProvider().open_session("mozart")
    chunk = bytes(2 * int(_RATE * _CHUNK_SECONDS))
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for _ in range(int(_SENTENCE_SECONDS / _CHUNK_SECONDS)):
            list(session.feed(chunk, _RATE))
        played = tracemalloc.get_traced_memory()[0] - before
        session.close()
        closed = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()

    assert played < len(chunk)
    assert closed < len(chunk)
