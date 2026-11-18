"""The limits tests compose a runtime with, and what they read of its health."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from video_client.wire import Health
    from video_service.runtime import Runtime

MAX_BYTES = 4 * 1024
MAX_PIXELS = 10_000
MAX_SESSIONS = 2
MAX_LAG_MS = 5_000
MAX_QUEUED_AUDIO_SECONDS = 30.0
CARD_MEMORY_MB = 4096

type BuildRuntime = Callable[..., Runtime]


def ready_looks(health: Health) -> dict[str, bool]:
    """Whether each look of `health` is ready, by name."""
    return {name: look.ready for name, look in health.looks.items()}
