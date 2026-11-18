"""The runtime: the provider, the looks, and the one readiness they share.

`load` runs once at start, on a thread of its own, while the service already
answers health: it starts the provider, then fetches and prepares each look. Nothing
is ready until `load` ends, since no session could open before. A look that fails
stays not ready and the others serve; a provider that fails leaves every look not
ready.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from logline import StepTimer
from video_provider_contract import ProviderFailedError

from video_client.wire import (
    Health,
    LookHealth,
    ProviderHealth,
    SessionsHealth,
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
    from collections.abc import Callable, Mapping

    from video_provider_contract import Provider, ProviderInfo

    from video_service.config import Settings
    from video_service.looks import LookSource
    from video_service.providers import ProviderName

logger = logging.getLogger(__name__)


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
        memory_probe: Callable[[], int] = card_memory_mb,
    ) -> None:
        """Offer the keys of `looks`, none of them ready until `load` ends."""
        self._provider_name = provider_name
        self._provider = provider
        self._looks = dict(looks)
        self._limits = limits
        self._max_sessions = max_sessions
        self._memory_probe = memory_probe
        self._lock = threading.Lock()
        self._provider_info: ProviderInfo | None = None
        self._ready_looks: set[str] = set()

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
        )

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

    def health(self) -> Health:
        """`GET /health` as the service answers it now."""
        with self._lock:
            info = self._provider_info
            ready_looks = frozenset(self._ready_looks)
        return Health(
            ready=info is not None,
            provider=ProviderHealth(
                name=self._provider_name.value,
                ready=info is not None,
                plays=[] if info is None else list(info.plays),
                video=None if info is None else info.video,
            ),
            looks={
                name: LookHealth(ready=info is not None and name in ready_looks)
                for name in self._looks
            },
            sessions=SessionsHealth(open=0, limit=self._max_sessions),
            card_memory_mb=self._memory_probe(),
        )
