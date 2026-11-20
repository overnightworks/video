"""A session that has ended frees its place for the next upgrade (WIRE-3, WIRE-12)."""

from __future__ import annotations

import asyncio
import json
import threading
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from video_provider_fake import FakeProvider
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from support.identity_support import valid_headers
from support.images import picture
from support.service_support import READY_WITHIN_SECONDS, free_port, start_service
from support.session_support import pcm, sentence_events, update_event
from video_client.health import fetch_health
from video_client.wire import Health
from video_service.config import Settings
from video_service.providers import ProviderName

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from websockets.asyncio.client import ClientConnection

    from support.look_server import LookServer
    from support.runtime_support import BuildRuntime

_STILL_WAITING_SECONDS = 0.3


class _Service:
    """The started service, whose provider holds a sentence's last frames at `gate`."""

    def __init__(
        self, url: str, port: int, provider: FakeProvider, gate: threading.Event
    ) -> None:
        self.url = url
        self.provider = provider
        self.gate = gate
        self._port = port

    async def open_holding_a_call(self) -> ClientConnection:
        """Open a session and speak a sentence whose last frames wait for the gate."""
        session = await connect(self.url, additional_headers=valid_headers())
        await session.recv()
        await session.send(json.dumps(update_event()))
        await session.recv()
        start, end = sentence_events("held")
        await session.send(json.dumps(start))
        await session.send(pcm(100))
        await session.send(json.dumps(end))
        held = self.provider.sessions[-1].finishing
        assert await asyncio.to_thread(held.wait, READY_WITHIN_SECONDS)
        return session

    async def leave_holding_a_call(self) -> Health:
        """Open a session as above, close it, and read health once it counts zero."""
        leaving = await self.open_holding_a_call()
        await leaving.close()
        async with asyncio.timeout(READY_WITHIN_SECONDS):
            while True:
                health = await asyncio.to_thread(self._health)
                if health is not None and health.sessions.open == 0:
                    return health
                await asyncio.sleep(0.01)

    def upgrade(self) -> asyncio.Task[ClientConnection]:
        """Start an upgrade, which the caller awaits."""
        return asyncio.create_task(self._connect())

    async def _connect(self) -> ClientConnection:
        return await connect(self.url, additional_headers=valid_headers())

    def _health(self) -> Health | None:
        outcome = fetch_health(
            f"http://127.0.0.1:{self._port}", deadline_seconds=2, cap_bytes=65536
        )
        return outcome if isinstance(outcome, Health) else None


type Scenario = Callable[[_Service], Awaitable[None]]


@pytest.fixture
def run_on_service(
    source: LookServer, build_runtime: BuildRuntime
) -> Callable[[Scenario], None]:
    """Run a scenario on the service over a real socket, with one place."""

    async def serve(scenario: Scenario) -> None:
        gate = threading.Event()
        provider = FakeProvider(finish_gate=gate)
        runtime = build_runtime(
            {"mozart": source.host("mozart.png", picture().data)},
            provider=provider,
            max_sessions=1,
            max_lag_ms=60_000,
        )
        settings = Settings(provider=ProviderName.FAKE, port=free_port())
        stop = asyncio.Event()
        serving, url = await start_service(runtime, settings, stop)
        try:
            await scenario(_Service(url, settings.port, provider, gate))
        finally:
            gate.set()
            stop.set()
            await serving

    return lambda scenario: asyncio.run(serve(scenario))


async def _still_waiting(upgrade: asyncio.Task[ClientConnection]) -> bool:
    done, _ = await asyncio.wait({upgrade}, timeout=_STILL_WAITING_SECONDS)
    return not done


def test_an_upgrade_after_a_session_ended_waits_for_its_place(
    run_on_service: Callable[[Scenario], None],
) -> None:
    outcome: list[object] = []

    async def scenario(service: _Service) -> None:
        await service.leave_holding_a_call()
        upgrade = service.upgrade()
        outcome.append(await _still_waiting(upgrade))
        service.gate.set()
        async with asyncio.timeout(READY_WITHIN_SECONDS):
            admitted = await upgrade
        outcome.append(json.loads(await admitted.recv())["type"])
        await admitted.close()

    run_on_service(scenario)

    assert outcome == [True, "session.created"]


def test_health_counts_no_open_session_while_an_ended_one_holds_its_place(
    run_on_service: Callable[[Scenario], None],
) -> None:
    outcome: list[object] = []

    async def scenario(service: _Service) -> None:
        health = await service.leave_holding_a_call()
        outcome.extend([health.sessions.open, service.provider.sessions[0].closed])

    run_on_service(scenario)

    assert outcome == [0, False]


def test_only_one_of_two_waiting_upgrades_takes_the_freed_place(
    run_on_service: Callable[[Scenario], None],
) -> None:
    outcome: list[object] = []

    async def scenario(service: _Service) -> None:
        await service.leave_holding_a_call()
        upgrades = [service.upgrade(), service.upgrade()]
        service.gate.set()
        async with asyncio.timeout(READY_WITHIN_SECONDS):
            results = await asyncio.gather(*upgrades, return_exceptions=True)
        admitted = [result for result in results if not isinstance(result, Exception)]
        refused = [result for result in results if isinstance(result, InvalidStatus)]
        outcome.extend([len(admitted), [late.response.status_code for late in refused]])
        for session in admitted:
            await session.close()

    run_on_service(scenario)

    assert outcome == [1, [HTTPStatus.TOO_MANY_REQUESTS]]


def test_an_upgrade_while_a_live_session_holds_the_place_is_busy(
    run_on_service: Callable[[Scenario], None],
) -> None:
    outcome: list[int] = []

    async def scenario(service: _Service) -> None:
        live = await connect(service.url, additional_headers=valid_headers())
        await live.recv()
        with pytest.raises(InvalidStatus) as refused:
            await service.upgrade()
        outcome.append(refused.value.response.status_code)
        await live.close()

    run_on_service(scenario)

    assert outcome == [HTTPStatus.TOO_MANY_REQUESTS]
