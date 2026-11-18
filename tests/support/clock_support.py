"""A clock that moves only when a test or the earliest of the waits on it moves it.

A wait is a pacer's `sleep` or the render time of a provider call. Time advances
to the earliest wait only once every other task of the loop has run as far as it
can, so renders and waits that overlap resolve in the order of their deadlines,
without a real sleep and without a thread's timing.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from concurrent.futures import Future
from functools import partial
from typing import TYPE_CHECKING, Any

from video_service.runtime import Pacing, ProviderThread

if TYPE_CHECKING:
    from collections.abc import Callable


class FakeClock:
    """Seconds that pass only as they are advanced or waited for."""

    def __init__(self) -> None:
        """Start at zero."""
        self._now = 0.0
        self._waits: list[tuple[float, int, Callable[[], None]]] = []
        self._order = itertools.count()
        self._driver: asyncio.Task[None] | None = None
        self._rendered = 0.0

    def now(self) -> float:
        """The seconds that have passed."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Let `seconds` pass."""
        self._now += seconds

    async def sleep(self, seconds: float) -> None:
        """Wait until `seconds` have passed, which they do once nothing else can run."""
        waiting = asyncio.get_running_loop().create_future()
        self._wait_until(self._now + seconds, lambda: _resolve(waiting))
        await waiting

    def render(self, seconds: float) -> None:
        """Take `seconds` for the provider call that draws; the provider's sleep."""
        self._rendered += seconds

    @property
    def pacing(self) -> Pacing:
        """The pacing a runtime composed over this clock takes."""
        return Pacing(now=self.now, sleep=self.sleep)

    def worker(self) -> ClockedWorker:
        """A provider thread whose calls take the time the provider renders."""
        return ClockedWorker(self)

    def take_rendered(self) -> float:
        """The seconds the call that just ran rendered, which restart at zero."""
        rendered, self._rendered = self._rendered, 0.0
        return rendered

    def finish_at(self, at: float, finish: Callable[[], None]) -> None:
        """Run `finish` once the clock reaches `at`, after the waits before it."""
        self._wait_until(at, finish)

    def _wait_until(self, at: float, release: Callable[[], None]) -> None:
        heapq.heappush(self._waits, (at, next(self._order), release))
        if self._driver is None:
            self._driver = asyncio.get_running_loop().create_task(self._drive())

    async def _drive(self) -> None:
        loop = asyncio.get_running_loop()
        while self._waits:
            # The loop's ready queue is the only sign that another task can still run.
            while loop._ready:  # noqa: ASYNC110, SLF001 - yielding is the wait; no public way to see a loop idle
                await asyncio.sleep(0)
            at, _, release = heapq.heappop(self._waits)
            self._now = max(self._now, at)
            release()
        self._driver = None


class ClockedWorker(ProviderThread):
    """The provider's calls, which run when made and end when their render has passed.

    The calls follow one another as on the provider's thread: one that is made while
    another renders starts when that one ends.
    """

    def __init__(self, clock: FakeClock) -> None:
        """Serve the calls on `clock`; no thread starts."""
        self._clock = clock
        self._free_at = 0.0
        self.longest_call = 0.0

    def submit[Result](self, work: Callable[[], Result]) -> Future[Result]:
        """Run `work` now and complete it once the time it rendered has passed."""
        outcome: Future[Result] = Future()
        self._clock.take_rendered()
        complete: Callable[[], None]
        try:
            complete = partial(_succeed, outcome, work())
        except Exception as error:  # noqa: BLE001 - handed to the awaiting session
            complete = partial(_fail, outcome, error)
        rendered = self._clock.take_rendered()
        self.longest_call = max(self.longest_call, rendered)
        self._free_at = max(self._clock.now(), self._free_at) + rendered
        self._clock.finish_at(self._free_at, complete)
        return outcome

    def stop(self) -> None:
        """Nothing runs, so nothing stops."""


def _resolve(waiting: asyncio.Future[None]) -> None:
    if not waiting.done():
        waiting.set_result(None)


def _succeed[Result](outcome: Future[Result], result: Result) -> None:
    if outcome.set_running_or_notify_cancel():
        outcome.set_result(result)


def _fail(outcome: Future[Any], error: Exception) -> None:
    if outcome.set_running_or_notify_cancel():
        outcome.set_exception(error)
