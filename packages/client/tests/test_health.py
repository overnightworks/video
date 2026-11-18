"""The health fetch against a raw listener: each outcome the wire allows (WIRE-12)."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest

from video_client.health import UnexpectedStatus, fetch_health
from video_client.outcomes import Cut, Malformed, OverCap, PastDeadline, Unreachable
from video_client.wire import HEALTH_PATH, Health

if TYPE_CHECKING:
    import socket
    import threading

    from support.listener_support import RawListener
    from video_client.health import HealthOutcome

DEADLINE_SECONDS = 0.5
MARGIN_SECONDS = 3.0
GENEROUS_DEADLINE_SECONDS = 30.0
CAP_BYTES = 64 * 1024

_HEALTH = {
    "ready": True,
    "provider": {
        "name": "a-provider-this-client-does-not-know",
        "ready": True,
        "plays": ["speak", "clear"],
        "video": {"width": 512, "height": 512, "frame_rate": 25},
    },
    "looks": {"mozart": {"ready": True}, "bach": {"ready": False}},
    "sessions": {"open": 0, "limit": 1},
    "card_memory_mb": 4096,
}


def _fetch(
    base_url: str,
    *,
    deadline_seconds: float = GENEROUS_DEADLINE_SECONDS,
    cap_bytes: int = CAP_BYTES,
) -> HealthOutcome:
    return fetch_health(
        base_url, deadline_seconds=deadline_seconds, cap_bytes=cap_bytes
    )


def _response(status: str, body: bytes) -> bytes:
    head = f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n\r\n"
    return head.encode() + body


def _ok(document: object) -> bytes:
    return _response("200 OK", json.dumps(document).encode())


def _stalls(connection: socket.socket, stopped: threading.Event) -> None:
    del connection
    stopped.wait()


def _floods_trailers(connection: socket.socket, stopped: threading.Event) -> None:
    """Answer chunked, then send trailer lines without a pause past the deadline."""
    connection.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n")
    ends = time.monotonic() + DEADLINE_SECONDS + MARGIN_SECONDS
    while time.monotonic() < ends and not stopped.is_set():
        connection.sendall(b"X-Trailer: 1\r\n" * 100_000)


def test_a_200_health_is_the_health(raw_listener: RawListener) -> None:
    with raw_listener(_ok(_HEALTH)) as (base_url, _):
        outcome = _fetch(base_url)

    assert outcome == Health.model_validate(_HEALTH)


def test_unknown_keys_of_a_200_are_ignored(raw_listener: RawListener) -> None:
    extended = {
        **_HEALTH,
        "uptime": 7,
        "provider": {**_HEALTH["provider"], "gpu": "none"},
    }

    with raw_listener(_ok(extended)) as (base_url, _):
        outcome = _fetch(base_url)

    assert outcome == Health.model_validate(_HEALTH)


def test_a_provider_that_is_not_ready_has_no_plays_and_no_video(
    raw_listener: RawListener,
) -> None:
    provider = {"name": "p", "ready": False, "plays": [], "video": None}

    with raw_listener(_ok({**_HEALTH, "provider": provider})) as (base_url, _):
        outcome = _fetch(base_url)

    assert isinstance(outcome, Health)
    assert outcome.provider.video is None


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"ready": true}', b""],
    ids=["text", "array", "missing-fields", "empty"],
)
def test_a_200_that_is_not_a_health_is_malformed(
    body: bytes, raw_listener: RawListener
) -> None:
    with raw_listener(_response("200 OK", body)) as (base_url, _):
        outcome = _fetch(base_url)

    assert outcome == Malformed()


@pytest.mark.parametrize("status", [503, 404])
def test_any_other_status_is_unexpected(status: int, raw_listener: RawListener) -> None:
    with raw_listener(_response(f"{status} Anything", b"{}")) as (base_url, _):
        outcome = _fetch(base_url)

    assert outcome == UnexpectedStatus(status)


def test_a_port_nothing_listens_on_is_unreachable(refused_url: str) -> None:
    assert _fetch(refused_url) == Unreachable()


def test_a_body_that_ends_before_its_length_is_cut(raw_listener: RawListener) -> None:
    raw = b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{"

    with raw_listener(raw) as (base_url, _):
        outcome = _fetch(base_url)

    assert outcome == Cut()


def test_an_answer_that_is_not_http_is_malformed(raw_listener: RawListener) -> None:
    with raw_listener(b"not an http answer\r\n\r\n") as (base_url, _):
        outcome = _fetch(base_url)

    assert outcome == Malformed()


def test_a_server_that_stalls_is_past_the_deadline(raw_listener: RawListener) -> None:
    with raw_listener(_stalls) as (base_url, _):
        began = time.monotonic()
        outcome = _fetch(base_url, deadline_seconds=DEADLINE_SECONDS)

    assert outcome == PastDeadline()
    assert time.monotonic() - began < DEADLINE_SECONDS + MARGIN_SECONDS


def test_a_response_that_never_stops_arriving_is_past_the_deadline(
    raw_listener: RawListener,
) -> None:
    with raw_listener(_floods_trailers) as (base_url, _):
        began = time.monotonic()
        outcome = _fetch(base_url, deadline_seconds=DEADLINE_SECONDS)

    assert outcome == PastDeadline()
    assert time.monotonic() - began < DEADLINE_SECONDS + MARGIN_SECONDS


def test_a_body_above_the_cap_is_over_the_cap(raw_listener: RawListener) -> None:
    with raw_listener(_ok(_HEALTH)) as (base_url, _):
        outcome = _fetch(base_url, cap_bytes=len(json.dumps(_HEALTH)) - 1)

    assert outcome == OverCap()


@pytest.mark.parametrize(
    "base_url",
    ["https://127.0.0.1:1", "http://:1", "http://127.0.0.1:1/health", "127.0.0.1:1"],
)
def test_an_origin_that_is_not_plain_http_without_a_path_raises_before_connecting(
    base_url: str,
) -> None:
    with pytest.raises(ValueError, match=r"base_url"):
        _fetch(base_url)


@pytest.mark.parametrize(
    "options",
    [
        {"deadline_seconds": 0},
        {"deadline_seconds": -1},
        {"cap_bytes": 0},
        {"cap_bytes": -1},
    ],
)
def test_a_deadline_or_cap_of_zero_or_below_raises(options: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"above 0"):
        _fetch("http://127.0.0.1:1", **options)


def test_the_request_is_a_get_of_the_health_path_without_a_token(
    raw_listener: RawListener,
) -> None:
    with raw_listener(_ok(_HEALTH)) as (base_url, requests):
        _fetch(base_url)

    (request,) = requests
    lines = request.partition(b"\r\n\r\n")[0].decode().split("\r\n")
    assert lines[0] == f"GET {HEALTH_PATH} HTTP/1.1"
    assert not any(line.lower().startswith("authorization") for line in lines)
