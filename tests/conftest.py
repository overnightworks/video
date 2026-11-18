"""The look source and the service the tests share."""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from video_provider_fake import FakeProvider

from support.identity_support import KeySet, callers_over
from support.look_server import LookServer, hosted_picture, look_server
from support.runtime_support import (
    CARD_MEMORY_MB,
    MAX_BYTES,
    MAX_LAG_MS,
    MAX_PIXELS,
    MAX_QUEUED_AUDIO_SECONDS,
    MAX_SESSIONS,
    BuildRuntime,
)
from video_service.app import create_app
from video_service.looks import LookLimits, LookSource
from video_service.providers import ProviderName
from video_service.runtime import Runtime, SessionLimits

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from video_provider_contract import Provider


@pytest.fixture
def source() -> Iterator[LookServer]:
    """A look source that serves what a test puts into it."""
    with look_server() as served:
        yield served


@pytest.fixture
def build_runtime(source: LookServer) -> BuildRuntime:
    """Compose a runtime over the look source, which it allows unless told otherwise."""

    def build(  # noqa: PLR0913 - one switch for each limit a scenario varies
        looks: Mapping[str, LookSource],
        *,
        provider: Provider | None = None,
        sources: tuple[str, ...] | None = None,
        max_pixels: int = MAX_PIXELS,
        max_sessions: int = MAX_SESSIONS,
        max_lag_ms: int = MAX_LAG_MS,
        max_queued_audio_seconds: float = MAX_QUEUED_AUDIO_SECONDS,
    ) -> Runtime:
        return Runtime(
            provider_name=ProviderName.FAKE,
            provider=provider or FakeProvider(),
            looks=looks,
            limits=LookLimits(
                sources=(source.base_url,) if sources is None else sources,
                max_bytes=MAX_BYTES,
                max_pixels=max_pixels,
            ),
            max_sessions=max_sessions,
            session_limits=SessionLimits(
                max_queued_audio_seconds=max_queued_audio_seconds,
                max_lag_ms=max_lag_ms,
            ),
            memory_probe=lambda: CARD_MEMORY_MB,
        )

    return build


@pytest.fixture
def key_set() -> KeySet:
    """The issuer's key-set endpoint, which a test can take down."""
    return KeySet()


@pytest.fixture
def wire(key_set: KeySet) -> Iterator[Callable[[Runtime], TestClient]]:
    """The HTTP and WebSocket boundary of a runtime, on one event loop per client."""
    with ExitStack() as clients:
        yield lambda runtime: clients.enter_context(
            TestClient(create_app(runtime, callers_over(key_set)))
        )


@pytest.fixture
def started(source: LookServer, build_runtime: BuildRuntime) -> Callable[..., Runtime]:
    """Compose a runtime offering the look `mozart` and run its start."""

    def start(provider: Provider | None = None, **limits: float) -> Runtime:
        runtime = build_runtime(
            {"mozart": hosted_picture(source)}, provider=provider, **limits
        )
        runtime.load()
        return runtime

    return start
