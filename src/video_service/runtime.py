"""The runtime: the provider, the looks, and the one readiness they share.

`load` runs once at start, on a thread of its own, while the service already
answers health: it starts the provider, then fetches and prepares each look. Nothing
is ready until `load` ends, since no session could open before. A look that fails
stays not ready and the others serve; a provider that fails leaves every look not
ready.

A session takes a place while it runs: `open_session` checks the look and the
limit in the order the wire refuses them, and `begin_drain` stops new places and
tells every open session to finish.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final

from logline import StepTimer
from video_provider_contract import ProviderFailedError

from video_client.wire import (
    Health,
    LookHealth,
    ProviderHealth,
    RefusalCode,
    SessionsHealth,
    VideoFormat,
)
from video_service.card import card_memory_mb
from video_service.looks import (
    LookFailure,
    LookLimits,
    LookRefusedError,
    fetch_image,
    read_look_file,
)
from video_service.providers import provider_for

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from video_provider_contract import Provider, ProviderInfo, ProviderSession

    from video_service.config import Settings
    from video_service.looks import LookSource
    from video_service.providers import ProviderName

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionLimits:
    """The bounds every session keeps."""

    max_queued_audio_seconds: float
    max_lag_ms: int


@dataclass(frozen=True, slots=True)
class Pacing:
    """The session's clock: its time and its wait for a frame."""

    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


REAL_TIME: Final = Pacing()


def in_daemon_thread[Result](work: Callable[[], Result], name: str) -> Future[Result]:
    """Run `work` on a daemon thread, which never holds the process back at its exit.

    A call on the loop's default executor would: `asyncio.run` joins it at the end.
    """
    outcome: Future[Result] = Future()

    def run() -> None:
        if not outcome.set_running_or_notify_cancel():
            return
        try:
            outcome.set_result(work())
        except Exception as error:  # noqa: BLE001 - handed to the waiting coroutine
            outcome.set_exception(error)

    threading.Thread(target=run, daemon=True, name=name).start()
    return outcome


class ProviderThread:
    """The daemon thread that makes one session's provider calls one at a time.

    A daemon, so a call that never returns holds back neither the end of its session
    nor the exit of the process.
    """

    def __init__(self) -> None:
        """Start the thread, which waits for its first call."""
        self._jobs: queue.SimpleQueue[Callable[[], None] | None] = queue.SimpleQueue()
        threading.Thread(target=self._run, daemon=True, name="provider").start()

    def submit[Result](self, work: Callable[[], Result]) -> Future[Result]:
        """Queue `work`; cancelling its future before its turn drops it."""
        outcome: Future[Result] = Future()
        self._jobs.put(partial(self._call, work, outcome))
        return outcome

    def stop(self) -> None:
        """End the thread once the calls queued before this one are done."""
        self._jobs.put(None)

    def _run(self) -> None:
        while (job := self._jobs.get()) is not None:
            job()

    def _call[Result](
        self, work: Callable[[], Result], outcome: Future[Result]
    ) -> None:
        if not outcome.set_running_or_notify_cancel():
            return
        try:
            outcome.set_result(work())
        except Exception as error:  # noqa: BLE001 - handed to the awaiting session
            outcome.set_exception(error)


class Place:
    """A session's place: its provider session, until `release`."""

    def __init__(  # noqa: PLR0913 - the parts the runtime hands each session
        self,
        *,
        session: ProviderSession,
        worker: ProviderThread,
        video: VideoFormat,
        limits: SessionLimits,
        pacing: Pacing,
        draining: asyncio.Event,
        release: Callable[[], None],
    ) -> None:
        """Hold the open provider session, the thread that calls it, and its format."""
        self.session = session
        self.worker = worker
        self.video = video
        self.limits = limits
        self.pacing = pacing
        self.draining = draining
        self._release = release

    def release(self) -> None:
        """Give the place back."""
        self._release()


class Runtime:
    """What the service offers now, and how far its start has come."""

    def __init__(  # noqa: PLR0913 - the parts the composition root hands in
        self,
        *,
        provider_name: ProviderName,
        provider: Provider,
        looks: Mapping[str, LookSource],
        limits: LookLimits,
        max_sessions: int,
        session_limits: SessionLimits,
        pacing: Pacing = REAL_TIME,
        memory_probe: Callable[[], int] = card_memory_mb,
    ) -> None:
        """Offer the keys of `looks`, none of them ready until `load` ends."""
        self._provider_name = provider_name
        self._provider = provider
        self._looks = dict(looks)
        self._limits = limits
        self._max_sessions = max_sessions
        self._session_limits = session_limits
        self._pacing = pacing
        self._memory_probe = memory_probe
        self._lock = threading.Lock()
        self._provider_info: ProviderInfo | None = None
        self._ready_looks: set[str] = set()
        self._open = 0
        self._draining = False
        self._drain_begun = asyncio.Event()
        self._idle = asyncio.Event()

    @classmethod
    def from_settings(cls, settings: Settings) -> Runtime:
        """Compose what `VIDEO_PROVIDER` and the look file name; a bad file raises."""
        looks = (
            {} if settings.looks_file is None else read_look_file(settings.looks_file)
        )
        return cls(
            provider_name=settings.provider,
            provider=provider_for(settings.provider),
            looks=looks,
            limits=LookLimits(
                sources=settings.look_sources,
                max_bytes=settings.look_max_bytes,
                max_pixels=settings.look_max_pixels,
            ),
            max_sessions=settings.max_sessions,
            session_limits=SessionLimits(
                max_queued_audio_seconds=settings.max_queued_audio_seconds,
                max_lag_ms=settings.max_lag_ms,
            ),
        )

    @property
    def provider_name(self) -> str:
        """The catalogue name of the provider this instance runs."""
        return self._provider_name.value

    def load(self) -> None:
        """Start the provider, then prepare each look once; return when that is over."""
        info = self._start_provider()
        if info is not None:
            for name, source in self._looks.items():
                self._prepare(name, source)
        with self._lock:
            self._provider_info = info

    def _start_provider(self) -> ProviderInfo | None:
        with StepTimer(
            "provider_load.ended", logger, provider=self._provider_name.value
        ) as step:
            try:
                return self._provider.start()
            except ProviderFailedError:
                step.fail("provider_failed")
                return None

    def _prepare(self, name: str, source: LookSource) -> None:
        with StepTimer("look_preparation.ended", logger, look=name) as step:
            step.handled()
            try:
                image = fetch_image(source, self._limits)
                self._provider.prepare(name, image)
            except LookRefusedError as refused:
                step.fail(refused.failure.value)
            except ProviderFailedError:
                step.fail(LookFailure.PROVIDER_FAILED.value)
            else:
                with self._lock:
                    self._ready_looks.add(name)

    def close(self) -> None:
        """Release the provider."""
        self._provider.close()

    async def open_session(self, look: str) -> Place | RefusalCode:
        """A place on `look`, or the refusal, checked in the order the wire names."""
        with self._lock:
            admitted = self._admit(look)
        if isinstance(admitted, RefusalCode):
            return admitted
        # The session's own daemon thread, since a call stuck on the loop's default
        # executor would hold back the exit of `asyncio.run`.
        worker = ProviderThread()
        try:
            session = await asyncio.wrap_future(
                worker.submit(partial(self._provider.open_session, look))
            )
        except ProviderFailedError:
            worker.stop()
            self._give_back()
            return RefusalCode.UNAVAILABLE
        return Place(
            session=session,
            worker=worker,
            video=admitted,
            limits=self._session_limits,
            pacing=self._pacing,
            draining=self._drain_begun,
            release=self._give_back,
        )

    def begin_drain(self) -> None:
        """Stop new sessions, report not ready, and tell the open sessions to finish."""
        with self._lock:
            self._draining = True
            self._drain_begun.set()
            if self._open == 0:
                self._idle.set()

    async def wait_idle(self) -> None:
        """Return once the drain has begun and no session is open."""
        await self._idle.wait()

    def _admit(self, look: str) -> VideoFormat | RefusalCode:
        """Take a place on `look` and return the picture it draws, or refuse."""
        info = self._provider_info
        if look not in self._looks:
            return RefusalCode.INVALID_REQUEST
        if self._draining or info is None or look not in self._ready_looks:
            return RefusalCode.UNAVAILABLE
        if self._open >= self._max_sessions:
            return RefusalCode.BUSY
        self._open += 1
        return info.video

    def _give_back(self) -> None:
        with self._lock:
            self._open -= 1
            if self._draining and self._open == 0:
                self._idle.set()

    def health(self) -> Health:
        """`GET /health` as the service answers it now."""
        with self._lock:
            info = self._provider_info
            ready_looks = frozenset(self._ready_looks)
            open_sessions = self._open
            draining = self._draining
        return Health(
            ready=info is not None and not draining,
            provider=ProviderHealth(
                name=self._provider_name.value,
                ready=info is not None,
                plays=[] if info is None else list(info.plays),
                video=None if info is None else info.video,
            ),
            looks={
                name: LookHealth(
                    ready=info is not None and not draining and name in ready_looks
                )
                for name in self._looks
            },
            sessions=SessionsHealth(open=open_sessions, limit=self._max_sessions),
            card_memory_mb=self._memory_probe(),
        )
