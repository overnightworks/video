"""A viewer's POST is refused in the wire's order, and the first fault wins."""

from __future__ import annotations

import asyncio
import re
import tracemalloc
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from aiortc import RTCPeerConnection

from support.identity_support import (
    OTHER_APP,
    OTHER_CLIENT,
    callers_over,
    headers_of,
    valid_headers,
)
from support.viewer_support import Service, beep, offer_of
from video_client.session_wire import OutputAudioBufferStarted
from video_client.wire import (
    BUSY_RETRY_AFTER_SECONDS,
    MAX_OFFER_BYTES,
    RETRY_AFTER_HEADER,
    SDP_CONTENT_TYPE,
    Refusal,
    RefusalCode,
    viewer_path,
    viewers_path,
)
from video_service.app import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import httpx2
    from starlette.types import Message

_TOO_LARGE = b"v=0\r\n" * (MAX_OFFER_BYTES // 3)
_NOT_SDP = "text/plain"
_UNKNOWN_SESSION = "0" * 32
_SPEAKING_MS = 1000
_CHUNK = b"v=0\r\n" * 256


def _refused(response: httpx2.Response) -> tuple[int, RefusalCode]:
    return response.status_code, Refusal.model_validate_json(
        response.content
    ).error.code


def test_bad_tokens_are_refused_before_the_offer_is_looked_at(
    live: Callable[..., None],
) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()

        response = await service.post(
            session, _TOO_LARGE, headers={}, content_type=_NOT_SDP
        )

        assert _refused(response) == (HTTPStatus.UNAUTHORIZED, RefusalCode.UNAUTHORIZED)

    live(scenario)


def test_an_unknown_and_an_ended_session_are_not_found_for_any_caller(
    live: Callable[..., None],
) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        stranger = headers_of(app=OTHER_APP, client=OTHER_CLIENT)
        ended = await service.open_session()
        await ended.socket.close()
        await service.logged.wait_for("session.ended")

        unknown = await service.post(
            _UNKNOWN_SESSION, _TOO_LARGE, headers=stranger, content_type=_NOT_SDP
        )
        gone = await service.post(
            ended, _TOO_LARGE, headers=stranger, content_type=_NOT_SDP
        )

        assert _refused(unknown) == (HTTPStatus.NOT_FOUND, RefusalCode.NOT_FOUND)
        assert _refused(gone) == (HTTPStatus.NOT_FOUND, RefusalCode.NOT_FOUND)

    live(scenario)


@pytest.mark.parametrize(
    "stranger",
    [
        headers_of(subject="another-player"),
        headers_of(app=OTHER_APP, client=OTHER_CLIENT),
    ],
    ids=["another subject", "another registered app"],
)
def test_a_caller_who_did_not_open_the_session_is_forbidden_whatever_else_is_wrong(
    live: Callable[..., None], stranger: dict[str, str]
) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()

        response = await service.post(
            session, _TOO_LARGE, headers=stranger, content_type=_NOT_SDP
        )

        assert _refused(response) == (HTTPStatus.FORBIDDEN, RefusalCode.FORBIDDEN)

    live(scenario)


def test_a_session_without_its_ice_configuration_is_unavailable(
    live: Callable[..., None],
) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        session = await service.open_session(update=False)

        response = await service.post(session, _TOO_LARGE, content_type=_NOT_SDP)

        assert _refused(response) == (
            HTTPStatus.SERVICE_UNAVAILABLE,
            RefusalCode.UNAVAILABLE,
        )

    live(scenario)


def test_a_draining_service_is_unavailable_before_the_content_type_is_looked_at(
    live: Callable[..., None],
) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()
        await session.speak("playing", beep(_SPEAKING_MS))
        assert isinstance(await session.next_event(), OutputAudioBufferStarted)
        service.runtime.begin_drain()

        response = await service.post(session, _TOO_LARGE, content_type=_NOT_SDP)

        assert _refused(response) == (
            HTTPStatus.SERVICE_UNAVAILABLE,
            RefusalCode.UNAVAILABLE,
        )

    live(scenario)


def test_another_content_type_is_invalid_before_the_size_is_looked_at(
    live: Callable[..., None],
) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()

        response = await service.post(session, _TOO_LARGE, content_type=_NOT_SDP)

        assert _refused(response) == (
            HTTPStatus.UNPROCESSABLE_ENTITY,
            RefusalCode.INVALID_REQUEST,
        )

    live(scenario)


def test_a_body_over_the_cap_is_too_large_even_though_it_does_not_parse(
    live: Callable[..., None],
) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()

        response = await service.post(session, _TOO_LARGE)

        assert _refused(response) == (
            HTTPStatus.CONTENT_TOO_LARGE,
            RefusalCode.TOO_LARGE,
        )

    live(scenario)


@pytest.mark.parametrize(
    "kind",
    [
        "garbage",
        "unparsable",
        "sendrecv",
        "two video",
        "no vp8",
        "no opus",
        "no mid",
        "one mid",
        "shared mid",
    ],
)
def test_an_offer_that_is_not_acceptable_is_invalid_even_at_the_viewer_limit(
    live: Callable[..., None], kind: str
) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()
        first = await service.post(session, await offer_of())
        assert first.status_code == HTTPStatus.CREATED
        offers = {
            "garbage": "not an offer",
            "unparsable": "v=0\r\nm=audio\r\n",
            "sendrecv": (await offer_of()).replace("a=recvonly", "a=sendrecv"),
            "two video": await offer_of(video=2),
            "no vp8": await offer_of(video_codecs=["video/H264"]),
            "no opus": await offer_of(audio_codecs=["audio/PCMU"]),
            "no mid": re.sub(r"a=mid:.*\r\n", "", await offer_of()),
            "one mid": re.sub(r"a=mid:.*\r\n", "", await offer_of(), count=1),
            "shared mid": re.sub(r"a=mid:.*\r\n", "a=mid:0\r\n", await offer_of()),
        }

        response = await service.post(session, offers[kind])

        assert _refused(response) == (
            HTTPStatus.UNPROCESSABLE_ENTITY,
            RefusalCode.INVALID_REQUEST,
        )

    live(scenario, max_viewers=1)


def test_the_viewer_above_the_limit_is_busy_and_told_when_to_retry(
    live: Callable[..., None],
) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()
        first = await service.post(session, await offer_of())
        assert first.status_code == HTTPStatus.CREATED

        response = await service.post(session, await offer_of())

        assert _refused(response) == (HTTPStatus.TOO_MANY_REQUESTS, RefusalCode.BUSY)
        assert response.headers[RETRY_AFTER_HEADER] == str(BUSY_RETRY_AFTER_SECONDS)

    live(scenario, max_viewers=1)


def test_a_viewer_is_ended_by_its_opener_once_and_other_methods_are_not_found(
    live: Callable[..., None],
) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()
        created = await service.post(session, await offer_of())
        location = created.headers["Location"]
        assert location == viewer_path(session.session_id, location.rsplit("/", 1)[1])

        by_stranger = await service.http.delete(
            location, headers=headers_of(subject="another-player")
        )
        deleted = await service.http.delete(location, headers=valid_headers())
        again = await service.http.delete(location, headers=valid_headers())
        patched = await service.http.patch(location, headers=valid_headers())
        listed = await service.http.get(
            viewers_path(session.session_id), headers=valid_headers()
        )

        assert _refused(by_stranger) == (HTTPStatus.FORBIDDEN, RefusalCode.FORBIDDEN)
        assert deleted.status_code == HTTPStatus.OK
        assert _refused(again) == (HTTPStatus.NOT_FOUND, RefusalCode.NOT_FOUND)
        assert _refused(patched) == (HTTPStatus.NOT_FOUND, RefusalCode.NOT_FOUND)
        assert _refused(listed) == (HTTPStatus.NOT_FOUND, RefusalCode.NOT_FOUND)

    live(scenario)


def test_an_ice_server_the_peer_cannot_parse_is_unavailable_before_the_content_type(
    live: Callable[..., None],
) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        session = await service.open_session(update=False)
        await session.update([{"urls": "not-an-ice-server"}], "all")

        response = await service.post(session, await offer_of(), content_type=_NOT_SDP)

        assert _refused(response) == (
            HTTPStatus.SERVICE_UNAVAILABLE,
            RefusalCode.UNAVAILABLE,
        )

    live(scenario)


@pytest.mark.parametrize("missing", ["a=ice-ufrag:.*\r\n", "a=setup:.*\r\n"])
def test_an_offer_the_peer_cannot_take_is_invalid_at_the_viewer_limit_and_admits_none(
    live: Callable[..., None], missing: str
) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()
        first = await service.post(session, await offer_of())
        assert first.status_code == HTTPStatus.CREATED
        offer = re.sub(missing, "", await offer_of())

        response = await service.post(session, offer)

        assert _refused(response) == (
            HTTPStatus.UNPROCESSABLE_ENTITY,
            RefusalCode.INVALID_REQUEST,
        )
        assert service.runtime.session(session.session_id).media.admitted == 1

    live(scenario, max_viewers=1)


def test_a_body_sent_in_chunks_is_cut_off_at_the_cap(live: Callable[..., None]) -> None:
    """WIRE-9."""

    async def scenario(service: Service) -> None:
        session = await service.open_session()

        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(MAX_OFFER_BYTES // len(_CHUNK) + 1):
                yield _CHUNK

        response = await service.post(session, chunks())

        assert _refused(response) == (
            HTTPStatus.CONTENT_TOO_LARGE,
            RefusalCode.TOO_LARGE,
        )

    live(scenario)


def test_a_chunk_past_the_cap_is_refused_before_it_is_copied_into_the_offer(
    live: Callable[..., None],
) -> None:
    """WIRE-9."""
    oversized = b"v" * (64 * MAX_OFFER_BYTES)
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": oversized, "more_body": True}

    async def send(message: Message) -> None:
        sent.append(message)

    async def scenario(service: Service) -> None:
        session = await service.open_session()
        app = create_app(service.runtime, callers_over())
        headers = valid_headers() | {"content-type": SDP_CONTENT_TYPE}
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": viewers_path(session.session_id),
            "raw_path": viewers_path(session.session_id).encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in headers.items()
            ],
            "client": ("127.0.0.1", 0),
            "server": ("127.0.0.1", 80),
        }
        traced_before = tracemalloc.is_tracing()
        if not traced_before:
            tracemalloc.start()
        tracemalloc.reset_peak()
        held_before = tracemalloc.get_traced_memory()[0]
        try:
            await app(scope, receive, send)
            peak_bytes = tracemalloc.get_traced_memory()[1] - held_before
        finally:
            if not traced_before:
                tracemalloc.stop()

        (start, *_) = sent
        assert start["status"] == HTTPStatus.CONTENT_TOO_LARGE
        assert peak_bytes < len(oversized)

    live(scenario)


def test_a_viewer_whose_session_ends_while_its_offer_is_negotiated_is_not_found(
    live: Callable[..., None],
) -> None:
    """WIRE-9."""
    reached = asyncio.Event()
    release = asyncio.Event()

    class Negotiating(RTCPeerConnection):
        async def setRemoteDescription(  # noqa: N802 - aiortc's name
            self,
            sessionDescription: object,  # noqa: N803
        ) -> None:
            reached.set()
            await release.wait()
            await super().setRemoteDescription(sessionDescription)

    async def scenario(service: Service) -> None:
        session = await service.open_session()
        posting = asyncio.create_task(service.post(session, await offer_of()))
        await reached.wait()

        await session.socket.close()
        await service.logged.wait_for("session.ended")
        release.set()

        assert _refused(await posting) == (HTTPStatus.NOT_FOUND, RefusalCode.NOT_FOUND)

    live(scenario, peers=Negotiating)
