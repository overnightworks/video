"""The closed catalogue of providers, and the one `VIDEO_PROVIDER` names."""

from __future__ import annotations

import sys
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from video_service.provider_process import (
    ProcessBounds,
    ProcessProvider,
    ProviderLaunch,
)

if TYPE_CHECKING:
    from video_service.config import Settings


class ProviderName(StrEnum):
    """The providers this service can run."""

    FAKE = "fake"


def worker_executable(name: ProviderName, provider_root: Path | None) -> Path:
    """The worker `name` runs as, and no other location.

    With a `provider_root` it is the provider's own environment under it; without,
    it is beside the service's interpreter, the environment `fake` is installed in.
    """
    worker = f"video-{name.value}-worker"
    if provider_root is None:
        return Path(sys.executable).parent / worker
    return provider_root / name.value / ".venv" / "bin" / worker


def provider_for(settings: Settings) -> ProcessProvider:
    """The provider `settings` names, built and not yet started."""
    return ProcessProvider(
        ProviderLaunch(
            executable=worker_executable(settings.provider, settings.provider_root),
            max_image_pixels=settings.look_max_pixels,
        ),
        ProcessBounds(),
        call_seconds=settings.max_lag_ms / 1000,
    )
