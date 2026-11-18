"""A clock that moves only when a test, the provider's drawing or a wait moves it."""

from __future__ import annotations

import asyncio

from video_service.runtime import Pacing


class FakeClock:
    """Seconds that pass only as they are advanced."""

    def __init__(self) -> None:
        """Start at zero."""
        self._now = 0.0

    def now(self) -> float:
        """The seconds that have passed."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Let `seconds` pass."""
        self._now += seconds

    async def sleep(self, seconds: float) -> None:
        """Let `seconds` pass at once, yielding to the other tasks."""
        self.advance(seconds)
        await asyncio.sleep(0)

    @property
    def pacing(self) -> Pacing:
        """The pacing a runtime composed over this clock takes."""
        return Pacing(now=self.now, sleep=self.sleep)
