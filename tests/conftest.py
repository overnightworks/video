"""The look source and the service the tests share."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from video_provider_fake import FakeProvider

from support.look_server import LookServer, look_server
from support.runtime_support import (
    CARD_MEMORY_MB,
    MAX_BYTES,
    MAX_PIXELS,
    MAX_SESSIONS,
    BuildRuntime,
)
from video_service.app import create_app
from video_service.looks import LookLimits, LookSource
from video_service.providers import ProviderName
from video_service.runtime import Runtime

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

    def build(
        looks: Mapping[str, LookSource],
        *,
        provider: Provider | None = None,
        sources: tuple[str, ...] | None = None,
        max_pixels: int = MAX_PIXELS,
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
            max_sessions=MAX_SESSIONS,
            memory_probe=lambda: CARD_MEMORY_MB,
        )

    return build


@pytest.fixture
def wire() -> Callable[[Runtime], TestClient]:
    """The HTTP boundary of a runtime."""
    return lambda runtime: TestClient(create_app(runtime))
