"""The bounded transport of the health fetch: one origin, one deadline, one body cap.

Every connect, send and read is bounded by one deadline. At connect any `OSError`,
a timeout included, is `Unreachable`; once connected a timeout is `PastDeadline`,
then `OSError` and `IncompleteRead` are `Cut`, then `HTTPException` is `Malformed`.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, IncompleteRead
from typing import Final
from urllib.parse import urlsplit

from video_client.outcomes import Cut, Malformed, OverCap, PastDeadline, Unreachable

_READ_BYTES: Final = 64 * 1024
_LEAST_TIMEOUT_SECONDS: Final = 0.001


@dataclass(frozen=True, slots=True)
class Origin:
    """Where the service listens."""

    host: str
    port: int


@dataclass(frozen=True, slots=True)
class Answer:
    """A whole response: its status and its body within the cap."""

    status: int
    body: bytes


type Result = Answer | Cut | PastDeadline | OverCap | Malformed | Unreachable


def checked_origin(base_url: str, deadline_seconds: float, cap_bytes: int) -> Origin:
    """The origin of `base_url`; a defect of the caller raises `ValueError`."""
    address = urlsplit(base_url)
    # An empty query or fragment parses to nothing, so the marks are looked for.
    if (
        address.scheme != "http"
        or not address.hostname
        or address.path
        or "?" in base_url
        or "#" in base_url
    ):
        message = "base_url must be http://host[:port] with no path, query or fragment"
        raise ValueError(message)
    if deadline_seconds <= 0 or cap_bytes <= 0:
        message = "deadline_seconds and cap_bytes must be above 0"
        raise ValueError(message)
    return Origin(address.hostname, address.port or 80)


class _DeadlineSocket(socket.socket):
    """A socket whose connect, sends and reads are each bounded by one deadline."""

    def __init__(
        self,
        family: int,
        kind: int,
        protocol: int,
        deadline: float,
    ) -> None:
        super().__init__(family, kind, protocol)
        self._deadline = deadline

    def arm(self) -> None:
        # settimeout(0) would make the socket non-blocking and a negative value raises.
        self.settimeout(max(self._deadline - time.monotonic(), _LEAST_TIMEOUT_SECONDS))

    def sendall(self, data: bytes, flags: int = 0) -> None:
        self.arm()
        super().sendall(data, flags)

    def recv_into(self, buffer: bytearray, nbytes: int = 0, flags: int = 0) -> int:
        self.arm()
        return super().recv_into(buffer, nbytes, flags)


def _connect(origin: Origin, deadline: float) -> socket.socket:
    failure = OSError("the name resolved to no address")
    for family, kind, protocol, _, address in socket.getaddrinfo(
        origin.host, origin.port, type=socket.SOCK_STREAM
    ):
        candidate = _DeadlineSocket(family, kind, protocol, deadline)
        try:
            candidate.arm()
            candidate.connect(address)
        except OSError as error:
            candidate.close()
            failure = error
        else:
            return candidate
    raise failure


def _read_within(connection: HTTPConnection, path: str, cap_bytes: int) -> Result:
    connection.request("GET", path)
    response = connection.getresponse()
    body = bytearray()
    while piece := response.read1(_READ_BYTES):
        body += piece
        if len(body) > cap_bytes:
            return OverCap()
    if response.length:
        raise IncompleteRead(bytes(body), response.length)
    return Answer(response.status, bytes(body))


def get(
    origin: Origin,
    path: str,
    *,
    deadline_seconds: float,
    cap_bytes: int,
) -> Result:
    """`GET path` on one connection to `origin`, or the outcome that stopped it."""
    try:
        connected = _connect(origin, time.monotonic() + deadline_seconds)
    except OSError:
        return Unreachable()
    connection = HTTPConnection(origin.host, origin.port)
    connection.sock = connected
    try:
        return _read_within(connection, path, cap_bytes)
    except TimeoutError:
        return PastDeadline()
    except OSError, IncompleteRead:
        return Cut()
    except HTTPException:
        return Malformed()
    finally:
        connection.close()
