"""A service started the way the process starts it: `serve` on a free loopback port."""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from typing import TYPE_CHECKING, Final

from support.identity_support import callers_over
from support.session_support import session_url
from video_service.__main__ import serve

if TYPE_CHECKING:
    from support.identity_support import KeySet
    from video_service.config import Settings
    from video_service.runtime import Runtime

READY_WITHIN_SECONDS: Final = 20.0


def free_port() -> int:
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        return bound.getsockname()[1]


class ReadyAnnouncement(logging.Handler):
    """Sets an event when the service logs `process.ready`, which follows the load."""

    def __init__(self) -> None:
        """Wait for the line."""
        super().__init__(logging.INFO)
        self.announced = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        """Note the line when it is the start's end."""
        if record.getMessage() == "process.ready":
            self.announced.set()


async def start_service(
    runtime: Runtime,
    settings: Settings,
    stop: asyncio.Event,
    key_set: KeySet | None = None,
) -> tuple[asyncio.Task[int], str]:
    """Start `serve` on a free port and return its task and its session URL."""
    ready = ReadyAnnouncement()
    logger = logging.getLogger("video_service.__main__")
    logger.addHandler(ready)
    logger.setLevel(logging.INFO)
    task = asyncio.create_task(serve(settings, runtime, callers_over(key_set), stop))
    try:
        assert await asyncio.to_thread(ready.announced.wait, READY_WITHIN_SECONDS)
    finally:
        logger.removeHandler(ready)
        logger.setLevel(logging.NOTSET)
    return task, f"ws://127.0.0.1:{settings.port}{session_url()}"
