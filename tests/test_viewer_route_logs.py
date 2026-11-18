"""The viewer lines follow logline's standard; no line holds the media's secrets."""

from __future__ import annotations

import logging
import re
from http import HTTPStatus
from typing import TYPE_CHECKING

import httpx2
import pytest
from logline import Outcome
from logline.testing import configured

from support.identity_support import valid_headers
from support.session_support import ICE_SERVERS, written
from support.viewer_support import Service, Watcher, offer_of
from video_client.wire import RefusalCode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_DELETED_TRACE = "0af7651916cd43dd8448eb211c80319c"
_ENDED_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
_REFUSED_TRACE = "5bf92f3577b34da6a3ce929d0e0e4737"
_ABORTED_TRACE = "6bf92f3577b34da6a3ce929d0e0e4738"


def _traced(trace_id: str) -> dict[str, str]:
    return valid_headers() | {"traceparent": f"00-{trace_id}-b7ad6b7169203331-01"}


def test_each_post_writes_one_viewer_line_in_its_own_trace_and_the_session_counts_them(
    live: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        deleted = await service.post(
            session, await offer_of(), headers=_traced(_DELETED_TRACE)
        )
        await service.post(session, await offer_of(), headers=_traced(_ENDED_TRACE))
        refused = await service.post(
            session, "x", headers=_traced(_REFUSED_TRACE), content_type="text/plain"
        )
        assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        await service.http.delete(deleted.headers["Location"], headers=valid_headers())
        await session.socket.close()
        await service.logged.wait_for("viewer.ended", 3)
        await service.logged.wait_for("session.ended")

    with configured("video"):
        live(scenario)

    out = capsys.readouterr().out
    viewers = {line["trace_id"]: line for line in written(out, "viewer.ended")}
    assert set(viewers) == {_DELETED_TRACE, _ENDED_TRACE, _REFUSED_TRACE}
    assert viewers[_DELETED_TRACE]["outcome"] == Outcome.OK
    assert viewers[_DELETED_TRACE]["ended_by"] == "deleted"
    assert viewers[_ENDED_TRACE]["outcome"] == Outcome.OK
    assert viewers[_ENDED_TRACE]["ended_by"] == "session_ended"
    assert viewers[_REFUSED_TRACE]["outcome"] == Outcome.REFUSED
    assert viewers[_REFUSED_TRACE]["reason"] == RefusalCode.INVALID_REQUEST
    (session_line,) = written(out, "session.ended")
    assert session_line["viewer_count"] == 2  # noqa: PLR2004 - the two admitted POSTs


class _ClientAbortedError(Exception):
    """The client gives up on its body after the first chunk."""


def test_a_post_whose_client_drops_its_body_still_writes_its_viewer_line(
    live: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()

        async def first_chunk_only() -> AsyncIterator[bytes]:
            yield b"v=0\r\n"
            raise _ClientAbortedError

        body = first_chunk_only()
        headers = _traced(_ABORTED_TRACE)
        with pytest.raises((_ClientAbortedError, httpx2.TransportError)):
            await service.post(session, body, headers=headers)
        await service.logged.wait_for("viewer.ended")

    with configured("video"):
        live(scenario)

    (line,) = written(capsys.readouterr().out, "viewer.ended")
    assert line["trace_id"] == _ABORTED_TRACE
    assert line["outcome"] == Outcome.FAILED


def _candidate_addresses(sdp: str) -> set[str]:
    return set(re.findall(r"^a=candidate:\S+ \d+ \S+ \d+ (\S+) ", sdp, re.MULTILINE))


def _credentials(sdp: str) -> set[str]:
    kept = ("a=ice-ufrag:", "a=ice-pwd:", "a=fingerprint:")
    return {
        line.partition(":")[2].strip()
        for line in sdp.splitlines()
        if line.startswith(kept)
    }


def test_no_line_holds_an_offer_answer_candidate_credential_or_token_at_debug(
    live: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets: set[str] = {"SENTINEL-CRED"}

    async def scenario(service: Service) -> None:
        session = await service.open_session(update=False)
        await session.update(ICE_SERVERS, "all")
        watcher = Watcher()
        offer = await watcher.offer()
        response = await service.post(session, offer)
        await watcher.accept(response.text)
        await watcher.received.until(lambda got: len(got.video_pts) >= 1)
        for sdp in (offer, response.text):
            secrets.update(_candidate_addresses(sdp) | _credentials(sdp))
        location = response.headers["Location"]
        await service.http.delete(location, headers=valid_headers())
        await watcher.close()
        await service.logged.wait_for("viewer.ended")

    monkeypatch.setenv("LOG_LEVEL", "debug")
    # The test's own WebSocket client writes its frames, credentials included.
    monkeypatch.setattr(
        logging.getLogger("websockets.client"), "level", logging.WARNING
    )
    with configured("video"):
        live(scenario)

    out = capsys.readouterr().out
    secrets.update(valid_headers().values())
    secrets.update(
        token for header in valid_headers().values() for token in header.split()
    )
    assert len(secrets) > len(valid_headers()) + 1
    assert not [secret for secret in secrets if secret and secret in out]
