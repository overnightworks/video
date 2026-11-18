"""A viewer's peer uses exactly the ICE servers its session was given."""

from __future__ import annotations

import asyncio
import struct
from typing import TYPE_CHECKING

from aiortc import RTCPeerConnection

from support.session_support import ICE_SERVERS
from support.viewer_support import Service, offer_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiortc import RTCConfiguration

_STUN_MAGIC_COOKIE = 0x2112A442
_STUN_HEADER = struct.Struct("!HHI")
_STUN_CLASS_BITS = 14


class _RecordingPeers:
    """A peer factory that notes the configuration of every peer it builds."""

    def __init__(self) -> None:
        self.configurations: list[RTCConfiguration] = []

    def __call__(self, configuration: RTCConfiguration) -> RTCPeerConnection:
        self.configurations.append(configuration)
        return RTCPeerConnection(configuration)


class _StunEgress:
    """Where the STUN packets sent from the event loop's UDP sockets go.

    Every ICE agent of the process, the service's and the test viewer's, opens its
    sockets through the loop's datagram endpoints, so this sees each STUN packet as
    it leaves its socket, and the address each socket is bound to.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.destinations: list[str] = []
        self.own_addresses: set[str] = set()
        self._open_endpoint = loop.create_datagram_endpoint
        loop.create_datagram_endpoint = self._recording_endpoint

    async def _recording_endpoint(
        self, *args: object, **options: object
    ) -> tuple[asyncio.DatagramTransport, asyncio.DatagramProtocol]:
        transport, protocol = await self._open_endpoint(*args, **options)
        self.own_addresses.add(transport.get_extra_info("sockname")[0])
        send = transport.sendto

        def sendto(data: bytes, address: tuple[str, int] | None = None) -> None:
            if _is_stun(data) and address is not None:
                self.destinations.append(address[0])
            send(data, address)

        transport.sendto = sendto
        return transport, protocol


def _is_stun(data: bytes) -> bool:
    """Whether `data` is a STUN message (RFC 8489, section 5)."""
    if len(data) < _STUN_HEADER.size:
        return False
    kind, _length, cookie = _STUN_HEADER.unpack_from(data)
    return not kind >> _STUN_CLASS_BITS and cookie == _STUN_MAGIC_COOKIE


def test_an_empty_ice_server_list_stays_empty_and_no_stun_packet_leaves_the_host(
    live: Callable[..., None],
) -> None:
    peers = _RecordingPeers()

    async def scenario(service: Service) -> None:
        egress = _StunEgress(asyncio.get_running_loop())
        session = await service.open_session()

        watcher = await service.watch(session)
        await watcher.received.until(lambda received: bool(received.video_pts))

        (configuration,) = peers.configurations
        assert configuration.iceServers == []
        assert egress.destinations
        assert set(egress.destinations) <= egress.own_addresses

    live(scenario, peers=peers)


def test_the_session_s_ice_servers_and_policy_are_the_peer_s(
    live: Callable[..., None],
) -> None:
    peers = _RecordingPeers()

    async def scenario(service: Service) -> None:
        session = await service.open_session(update=False)
        await session.update(ICE_SERVERS, "relay")

        await service.post(session, await offer_of())

        (configuration,) = peers.configurations
        assert [
            (server.urls, server.username, server.credential)
            for server in configuration.iceServers
        ] == [
            (server["urls"], server["username"], server["credential"])
            for server in ICE_SERVERS
        ]
        assert configuration.iceTransportPolicy.value == "relay"

    live(scenario, peers=peers)
