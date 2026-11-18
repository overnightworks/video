"""A clock that only the provider's drawing, the session's waits and a stall move."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from video_service.runtime import Pacing

if TYPE_CHECKING:
    from collections.abc import Awaitable


class FakeClock:
    """Time that passes as the provider draws, the session waits and the client stalls.

    A provider call is late when the provider drew for longer than it was given, so
    the client's stall never makes a call late, whenever it falls.
    """

    def __init__(self) -> None:
        """Start at zero."""
        self._lock = threading.Lock()
        self._now = 0.0
        self._drawn = 0.0

    def now(self) -> float:
        """The seconds that have passed."""
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        """Let the provider draw for `seconds`; callable from its thread."""
        with self._lock:
            self._now += seconds
            self._drawn += seconds

    def stall(self, seconds: float) -> None:
        """Let `seconds` pass with no call to the provider under way."""
        with self._lock:
            self._now += seconds

    async def sleep(self, seconds: float) -> None:
        """Let `seconds` pass at once, yielding to the other tasks."""
        self.stall(seconds)
        await asyncio.sleep(0)

    async def deadline[Result](
        self, awaited: Awaitable[Result], seconds: float
    ) -> Result:
        """The result of `awaited`; `TimeoutError` if the provider drew too long."""
        with self._lock:
            drawn_before = self._drawn
        result = await awaited
        with self._lock:
            drawn = self._drawn - drawn_before
        if drawn > seconds:
            raise TimeoutError
        return result

    @property
    def pacing(self) -> Pacing:
        """The pacing a runtime composed over this clock takes."""
        return Pacing(now=self.now, sleep=self.sleep, deadline=self.deadline)
