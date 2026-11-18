"""The process: health is answered while the start runs, and the stop is clean."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest
from video_provider_fake import FakeProvider

from support.images import picture
from video_client.health import fetch_health
from video_client.wire import Health
from video_service.__main__ import main, serve
from video_service.config import Settings
from video_service.providers import ProviderName

if TYPE_CHECKING:
    from pathlib import Path

    from video_provider_contract import DecodedImage, ProviderInfo

    from support.look_server import LookServer
    from support.runtime_support import BuildRuntime

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


def _ready_health(port: int) -> Health | None:
    """The health once it says ready, or `None` when it stays unready past the bound."""
    ends = time.monotonic() + READY_WITHIN_SECONDS
    while time.monotonic() < ends:
        outcome = fetch_health(
            f"http://127.0.0.1:{port}", deadline_seconds=2, cap_bytes=65536
        )
        if isinstance(outcome, Health) and outcome.ready:
            return outcome
        time.sleep(0.05)
    return None


def test_the_service_answers_health_ready_and_stops_clean_on_request(
    source: LookServer, build_runtime: BuildRuntime
) -> None:
    provider = FakeProvider()
    runtime = build_runtime(
        {"mozart": source.host("mozart.png", picture().data)}, provider=provider
    )
    port = _free_port()
    settings = Settings(provider=ProviderName.FAKE, port=port)

    async def drive() -> tuple[int, Health | None]:
        stop = asyncio.Event()
        serving = asyncio.create_task(serve(settings, runtime, stop))
        health = await asyncio.to_thread(_ready_health, port)
        stop.set()
        return await serving, health

    result, health = asyncio.run(drive())

    assert result == 0
    assert health is not None
    assert health.looks["mozart"].ready
    assert provider.closed


def test_a_load_that_fails_ends_the_process_nonzero(
    build_runtime: BuildRuntime,
) -> None:
    runtime = build_runtime({}, provider=_ExplodingProvider())
    settings = Settings(provider=ProviderName.FAKE, port=_free_port())

    result = asyncio.run(serve(settings, runtime, asyncio.Event()))

    assert result == 1


def test_a_port_that_is_taken_ends_the_process_nonzero(
    build_runtime: BuildRuntime,
) -> None:
    runtime = build_runtime({})
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        settings = Settings(provider=ProviderName.FAKE, port=taken.getsockname()[1])

        result = asyncio.run(serve(settings, runtime, asyncio.Event()))

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
    monkeypatch.setenv("VIDEO_PORT", str(port))
    monkeypatch.setenv("VIDEO_LOOKS_FILE", str(looks_file))
    monkeypatch.setenv("VIDEO_LOOK_SOURCES", json.dumps([source.base_url]))

    with subprocess.Popen(
        [sys.executable, "-m", "video_service"],
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            health = _ready_health(port)
            process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=READY_WITHIN_SECONDS)
        finally:
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
        (
            {"VIDEO_PROVIDER": "fake", "VIDEO_LOOKS_FILE": "/no/such/looks.toml"},
            "VIDEO_LOOKS_FILE",
        ),
    ],
    ids=["no-provider", "unreadable-look-file"],
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
