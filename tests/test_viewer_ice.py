"""A viewer's peer uses exactly the ICE servers its session was given."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiortc import rtcicetransport

from support.session_support import ICE_SERVERS
from support.viewer_support import Service, offer_of
from video_service import media

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiortc import RTCConfiguration


@pytest.fixture
def peers(monkeypatch: pytest.MonkeyPatch) -> list[RTCConfiguration]:
    """The configurations the service builds its viewers' peers with."""
    configurations: list[RTCConfiguration] = []
    build_peer = media.RTCPeerConnection

    def recording(configuration: RTCConfiguration) -> media.RTCPeerConnection:
        configurations.append(configuration)
        return build_peer(configuration)

    monkeypatch.setattr(media, "RTCPeerConnection", recording)
    return configurations


@pytest.fixture
def stun_servers(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """The STUN server each ICE connection of the process is built with."""
    chosen: list[object] = []
    build_connection = rtcicetransport.Connection

    def recording(**options: Any) -> rtcicetransport.Connection:  # noqa: ANN401 - aioice's own keywords
        chosen.append(options.get("stun_server"))
        return build_connection(**options)

    monkeypatch.setattr(rtcicetransport, "Connection", recording)
    return chosen


def test_an_empty_ice_server_list_stays_empty_and_no_stun_server_is_used(
    live: Callable[..., None],
    peers: list[RTCConfiguration],
    stun_servers: list[object],
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()

        await service.post(session, await offer_of())

        (configuration,) = peers
        assert configuration.iceServers == []
        assert stun_servers
        assert not any(stun_servers)

    live(scenario)


def test_the_session_s_ice_servers_and_policy_are_the_peer_s(
    live: Callable[..., None], peers: list[RTCConfiguration]
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session(update=False)
        await session.update(ICE_SERVERS, "relay")

        await service.post(session, await offer_of())

        (configuration,) = peers
        assert [
            (server.urls, server.username, server.credential)
            for server in configuration.iceServers
        ] == [
            (server["urls"], server["username"], server["credential"])
            for server in ICE_SERVERS
        ]
        assert configuration.iceTransportPolicy.value == "relay"

    live(scenario)
