"""A clock that only the provider's drawing and the session's waits move."""

from __future__ import annotations

import asyncio
import threading

from video_service.runtime import Pacing


class FakeClock:
    """Time that passes only as the provider draws and the session waits."""

    def __init__(self) -> None:
        """Start at zero."""
        self._lock = threading.Lock()
        self._now = 0.0

    def now(self) -> float:
        """The seconds that have passed."""
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        """Let `seconds` pass; callable from the provider's thread."""
        with self._lock:
            self._now += seconds

    async def sleep(self, seconds: float) -> None:
        """Let `seconds` pass at once, yielding to the other tasks."""
        self.advance(seconds)
        await asyncio.sleep(0)

    @property
    def pacing(self) -> Pacing:
        """The pacing a runtime composed over this clock takes."""
        return Pacing(now=self.now, sleep=self.sleep)
