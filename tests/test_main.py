"""The process: health is answered while the start runs, and the stop is clean."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

import pytest
from video_provider_fake import FakeProvider
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from support.identity_support import callers_over, valid_headers, write_caller_config
from support.images import picture
from support.session_support import (
    pcm,
    sentence_events,
    session_url,
    update_event,
)
from video_client.health import fetch_health
from video_client.session_wire import (
    OutputAudioBufferStarted,
    OutputAudioBufferStopped,
    ServerEvent,
    parse_server_event,
)
from video_client.wire import DRAIN_CLOSE_CODE, Health
from video_service.__main__ import main, serve
from video_service.config import Settings
from video_service.providers import ProviderName

if TYPE_CHECKING:
    from pathlib import Path

    from video_provider_contract import DecodedImage, ProviderInfo
    from websockets.asyncio.client import ClientConnection

    from support.look_server import LookServer
    from support.runtime_support import BuildRuntime
    from video_service.runtime import Runtime

READY_WITHIN_SECONDS = 20.0


class _ExplodingProvider:
    def start(self) -> ProviderInfo:
        message = "a defect in the provider"
        raise RuntimeError(message)

    def prepare(self, look: str, image: DecodedImage) -> None:
        raise NotImplementedError

    def close(self) -> None:
        return None


def _free_port() -> int:
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        return bound.getsockname()[1]


class _ReadyAnnouncement(logging.Handler):
    """Sets an event when the service logs `process.ready`, which follows the load."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.announced = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "process.ready":
            self.announced.set()


def _health(port: int) -> Health | None:
    outcome = fetch_health(
        f"http://127.0.0.1:{port}", deadline_seconds=2, cap_bytes=65536
    )
    return outcome if isinstance(outcome, Health) else None


def test_the_service_answers_health_ready_and_stops_clean_on_request(
    source: LookServer, build_runtime: BuildRuntime
) -> None:
    provider = FakeProvider()
    runtime = build_runtime(
        {"mozart": source.host("mozart.png", picture().data)}, provider=provider
    )
    port = _free_port()
    settings = Settings(provider=ProviderName.FAKE, port=port)

    ready = _ReadyAnnouncement()

    async def drive() -> tuple[int, Health | None, bool]:
        stop = asyncio.Event()
        serving = asyncio.create_task(serve(settings, runtime, callers_over(), stop))
        announced = await asyncio.to_thread(ready.announced.wait, READY_WITHIN_SECONDS)
        health = await asyncio.to_thread(_health, port)
        stop.set()
        return await serving, health, announced

    logger = logging.getLogger("video_service.__main__")
    logger.addHandler(ready)
    logger.setLevel(logging.INFO)
    try:
        result, health, announced = asyncio.run(drive())
    finally:
        logger.removeHandler(ready)
        logger.setLevel(logging.NOTSET)

    assert announced

    assert result == 0
    assert health is not None
    assert health.looks["mozart"].ready
    assert provider.closed


def test_a_load_that_fails_ends_the_process_nonzero(
    build_runtime: BuildRuntime,
) -> None:
    runtime = build_runtime({}, provider=_ExplodingProvider())
    settings = Settings(provider=ProviderName.FAKE, port=_free_port())

    result = asyncio.run(serve(settings, runtime, callers_over(), asyncio.Event()))

    assert result == 1


def test_a_port_that_is_taken_ends_the_process_nonzero(
    build_runtime: BuildRuntime,
) -> None:
    runtime = build_runtime({})
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        settings = Settings(provider=ProviderName.FAKE, port=taken.getsockname()[1])

        result = asyncio.run(serve(settings, runtime, callers_over(), asyncio.Event()))

    assert result == 1


def test_the_process_serves_the_configured_looks_and_stops_on_a_signal(
    source: LookServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = source.host("mozart.png", picture().data)
    looks_file = tmp_path / "looks.toml"
    looks_file.write_text(
        f'[mozart]\nurl = "{shown.url}"\nsha256 = "{shown.sha256}"\n', encoding="utf-8"
    )
    port = _free_port()
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")
    monkeypatch.setenv("VIDEO_CONFIG_FILE", str(write_caller_config(tmp_path)))
    monkeypatch.setenv("VIDEO_PORT", str(port))
    monkeypatch.setenv("VIDEO_LOOKS_FILE", str(looks_file))
    monkeypatch.setenv("VIDEO_LOOK_SOURCES", json.dumps([source.base_url]))

    with subprocess.Popen(
        [sys.executable, "-m", "video_service"],
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        watchdog = threading.Timer(READY_WITHIN_SECONDS, process.kill)
        watchdog.start()
        try:
            announced = []
            for line in process.stdout:
                announced.append(line)
                if (
                    line.startswith("{")
                    and json.loads(line)["event"] == "process.ready"
                ):
                    break
            health = _health(port)
            process.send_signal(signal.SIGTERM)
            rest, _ = process.communicate(timeout=READY_WITHIN_SECONDS)
            output = "".join(announced) + rest
        finally:
            watchdog.cancel()
            process.kill()

    assert health is not None
    assert health.looks["mozart"].ready
    assert process.returncode == 0
    events = {
        json.loads(line)["event"]
        for line in output.splitlines()
        if line.startswith("{")
    }
    assert {"process.started", "process.ready", "look_preparation.ended"} <= events


@pytest.mark.parametrize(
    ("environment", "named"),
    [
        ({}, "VIDEO_PROVIDER"),
        ({"VIDEO_PROVIDER": "fake"}, "VIDEO_CONFIG_FILE"),
        (
            {"VIDEO_PROVIDER": "fake", "VIDEO_CONFIG_FILE": "/no/such/config.toml"},
            "VIDEO_CONFIG_FILE",
        ),
        (
            {"VIDEO_PROVIDER": "fake", "VIDEO_LOOKS_FILE": "/no/such/looks.toml"},
            "VIDEO_LOOKS_FILE",
        ),
    ],
    ids=[
        "no-provider",
        "no-config-file",
        "unreadable-config-file",
        "unreadable-look-file",
    ],
)
def test_main_refuses_to_start_on_a_bad_setting_and_names_it(
    environment: dict[str, str],
    named: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in [name for name in os.environ if name.startswith("VIDEO_")]:
        monkeypatch.delenv(name)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit) as exited:
        main()

    assert named in str(exited.value.code)


def test_a_runtime_closes_its_provider(build_runtime: BuildRuntime) -> None:
    provider = FakeProvider()

    build_runtime({}, provider=provider).close()

    assert provider.closed


async def _serving(
    runtime: Runtime, settings: Settings, stop: asyncio.Event
) -> tuple[asyncio.Task[int], str]:
    """Start `serve` on a free port and return its task and its session URL."""
    ready = _ReadyAnnouncement()
    logger = logging.getLogger("video_service.__main__")
    logger.addHandler(ready)
    logger.setLevel(logging.INFO)
    task = asyncio.create_task(serve(settings, runtime, callers_over(), stop))
    try:
        assert await asyncio.to_thread(ready.announced.wait, READY_WITHIN_SECONDS)
    finally:
        logger.removeHandler(ready)
        logger.setLevel(logging.NOTSET)
    return task, f"ws://127.0.0.1:{settings.port}{session_url()}"


async def _open_playing(url: str, milliseconds: int) -> ClientConnection:
    """Open a session at `url` and speak one sentence; return once it plays."""
    session = await connect(url, additional_headers=valid_headers())
    await session.recv()
    await session.send(json.dumps(update_event()))
    await session.recv()
    start, end = sentence_events("playing")
    await session.send(json.dumps(start))
    await session.send(pcm(milliseconds))
    await session.send(json.dumps(end))
    assert isinstance(
        parse_server_event(str(await session.recv())), OutputAudioBufferStarted
    )
    return session


def test_a_stop_lets_the_playing_sentence_finish_then_closes_the_session(
    source: LookServer, build_runtime: BuildRuntime
) -> None:
    runtime = build_runtime({"mozart": source.host("mozart.png", picture().data)})
    settings = Settings(provider=ProviderName.FAKE, port=_free_port())

    async def drive() -> tuple[int, ServerEvent, int | None]:
        stop = asyncio.Event()
        serving, url = await _serving(runtime, settings, stop)
        session = await _open_playing(url, 200)
        stop.set()
        stopped = parse_server_event(str(await session.recv()))
        with pytest.raises(ConnectionClosed):
            await session.recv()
        return await serving, stopped, session.close_code

    result, stopped, close_code = asyncio.run(drive())

    assert result == 0
    assert isinstance(stopped, OutputAudioBufferStopped)
    assert close_code == DRAIN_CLOSE_CODE


def test_a_session_that_outlasts_the_drain_bound_is_cut_when_the_service_exits(
    source: LookServer, build_runtime: BuildRuntime
) -> None:
    runtime = build_runtime({"mozart": source.host("mozart.png", picture().data)})
    settings = Settings(
        provider=ProviderName.FAKE, port=_free_port(), drain_seconds=0.2
    )

    async def drive() -> tuple[int, int | None]:
        stop = asyncio.Event()
        serving, url = await _serving(runtime, settings, stop)
        session = await _open_playing(url, 20_000)
        stop.set()
        async with asyncio.timeout(READY_WITHIN_SECONDS):
            with pytest.raises(ConnectionClosed):
                await session.recv()
            return await serving, session.close_code

    result, close_code = asyncio.run(drive())

    assert result == 0
    assert close_code == DRAIN_CLOSE_CODE


def test_a_stop_cuts_a_session_whose_provider_never_answers(
    source: LookServer, build_runtime: BuildRuntime
) -> None:
    held = threading.Event()
    runtime = build_runtime(
        {"mozart": source.host("mozart.png", picture().data)},
        provider=FakeProvider(finish_gate=held),
        max_lag_ms=60_000,
    )
    settings = Settings(
        provider=ProviderName.FAKE, port=_free_port(), drain_seconds=0.2
    )

    async def drive() -> tuple[int, int | None]:
        stop = asyncio.Event()
        serving, url = await _serving(runtime, settings, stop)
        session = await _open_playing(url, 200)
        stop.set()
        async with asyncio.timeout(READY_WITHIN_SECONDS):
            with pytest.raises(ConnectionClosed):
                await session.recv()
            return await serving, session.close_code

    try:
        result, close_code = asyncio.run(drive())
    finally:
        held.set()

    assert result == 0
    assert close_code == DRAIN_CLOSE_CODE


class _SessionLines(logging.Handler):
    """Collects the `session.ended` records of the session logger."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "session.ended":
            self.records.append(record)


def test_a_client_that_leaves_ends_its_session_ok_and_frees_its_place(
    source: LookServer, build_runtime: BuildRuntime
) -> None:
    runtime = build_runtime(
        {"mozart": source.host("mozart.png", picture().data)}, max_sessions=1
    )
    settings = Settings(provider=ProviderName.FAKE, port=_free_port())
    lines = _SessionLines()
    logger = logging.getLogger("video_service.session")
    logger.addHandler(lines)
    logger.setLevel(logging.INFO)

    async def drive() -> int:
        stop = asyncio.Event()
        serving, url = await _serving(runtime, settings, stop)
        session = await _open_playing(url, 20_000)
        await session.close()
        stop.set()
        return await serving

    try:
        result = asyncio.run(drive())
    finally:
        logger.removeHandler(lines)
        logger.setLevel(logging.NOTSET)

    assert result == 0
    (line,) = lines.records
    assert line.outcome == "ok"
    assert not hasattr(line, "close_code")
