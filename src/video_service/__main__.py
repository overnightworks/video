"""Start the video service on its TCP listener."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
import time
from typing import TYPE_CHECKING, Final

import uvicorn
from logline import configure
from webauth.https_client import fetch_key_set_over_https

from video_service.app import create_app
from video_service.callers import VideoCallers
from video_service.config import Settings, load_caller_config, load_settings
from video_service.runtime import Runtime, in_daemon_thread

if TYPE_CHECKING:
    from collections.abc import Iterator

_SERVICE_NAME: Final = "video"
# The most of the drain kept for closing the sessions left when it runs out.
_CUT_SECONDS: Final = 1.0
_CONFIG_FILE_REQUIRED: Final = "VIDEO_CONFIG_FILE is required: it names the callers"
_LOGGER = logging.getLogger(__name__)


def main() -> None:
    """Read one configuration, serve until a signal, and exit nonzero when it fails."""
    try:
        settings = load_settings()
        runtime = Runtime.from_settings(settings)
        callers = _callers(settings)
    except ValueError as fault:
        raise SystemExit(str(fault)) from None
    configure(_SERVICE_NAME)
    _LOGGER.info("process.started")
    stop = asyncio.Event()
    raise SystemExit(
        asyncio.run(_serve_until_signalled(settings, runtime, callers, stop))
    )


def _callers(settings: Settings) -> VideoCallers:
    if settings.config_file is None:
        raise ValueError(_CONFIG_FILE_REQUIRED)
    return VideoCallers.from_config(
        load_caller_config(settings.config_file),
        fetch_key_set=fetch_key_set_over_https,
        wall_clock=time.time,
        monotonic_clock=time.monotonic,
    )


async def _serve_until_signalled(
    settings: Settings, runtime: Runtime, callers: VideoCallers, stop: asyncio.Event
) -> int:
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, stop.set)
    return await serve(settings, runtime, callers, stop)


class _ServiceServer(uvicorn.Server):
    """uvicorn's server without its own signal handling: the service owns the stop."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def _load(runtime: Runtime, fatal: asyncio.Event) -> None:
    try:
        await asyncio.wrap_future(in_daemon_thread(runtime.load, "load"))
    except Exception:
        _LOGGER.exception("load.failed")
        fatal.set()
    else:
        _LOGGER.info("process.ready")


def _cut_seconds(drain_seconds: float) -> float:
    """The time of the drain kept for the cut of the sessions left."""
    return min(_CUT_SECONDS, drain_seconds / 2)


async def _within(deadline: float, server_task: asyncio.Task[None]) -> None:
    """Wait for the server's shutdown until `deadline`, then cancel it."""
    left = max(deadline - asyncio.get_running_loop().time(), 0.0)
    _done, pending = await asyncio.wait({server_task}, timeout=left)
    for task in pending:
        task.cancel()
    await asyncio.gather(server_task, return_exceptions=True)


async def serve(
    settings: Settings, runtime: Runtime, callers: VideoCallers, stop: asyncio.Event
) -> int:
    """Serve health at once, load in the background, and drain when `stop` is set.

    A stop drains first: the service reports not ready, refuses new sessions and
    waits for the open ones to end, then cuts those left, a session stuck in a provider
    call included, and exits, the whole within `VIDEO_DRAIN_SECONDS` of the stop.
    Returns 0 after a stop, and 1 when the listener, the server or the load fails.
    """
    try:
        listener = socket.create_server((settings.host, settings.port))
    except OSError:
        _LOGGER.exception("listener.failed", extra={"port": settings.port})
        return 1
    server = _ServiceServer(
        uvicorn.Config(create_app(runtime, callers), log_config=None)
    )
    fatal = asyncio.Event()
    with listener:
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        waiting = [
            asyncio.create_task(_load(runtime, fatal)),
            asyncio.create_task(stop.wait()),
            asyncio.create_task(fatal.wait()),
        ]
        done, _pending = await asyncio.wait(
            {server_task, *waiting[1:]}, return_when=asyncio.FIRST_COMPLETED
        )
        result = 0 if waiting[1] in done else 1
        deadline = asyncio.get_running_loop().time() + settings.drain_seconds
        if result == 0:
            runtime.begin_drain()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout_at(
                    deadline - _cut_seconds(settings.drain_seconds)
                ):
                    await runtime.wait_idle()
        for task in waiting:
            task.cancel()
        server.should_exit = True
        await _within(deadline, server_task)
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout_at(deadline):
            await asyncio.wrap_future(in_daemon_thread(runtime.close, "close"))
    _LOGGER.info("process.stopped", extra={"exit_code": result})
    return result


if __name__ == "__main__":
    main()
