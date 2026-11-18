"""A browser page watches a session through its app's route, in headless Chromium."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from starlette.responses import Response

from support.browser_support import (
    REFUSES_AUTOPLAY,
    VIEWERS_PATH,
    Stage,
    stage,
)
from support.identity_support import valid_headers
from support.service_support import free_port
from support.viewer_support import Service, beep
from video_client.wire import LOCATION_HEADER, SDP_CONTENT_TYPE
from video_service.media import ViewerEnd

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import httpx2

pytestmark = pytest.mark.browser

_SENTENCE_MS = 400
_ADVANCED_FRAMES = 5
_NOT_AN_ANSWER = b"v=0\r\nthis is not an answer\r\n"


def test_show_reports_loading_then_drawn_while_frames_advance_and_sound_arrives(
    live_in_browser: Callable[..., None], tmp_path: Path
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        async with stage(service, session, tmp_path / "profile") as page:
            await page.begin()
            assert await page.states() == ["loading"]
            await page.until("seen.states.includes('drawn')")
            await page.page.evaluate("stage.measure()")
            await session.speak("beep", beep(_SENTENCE_MS))

            await page.until("seen.heard.length >= 1")
            first = (await page.read()).presented
            await page.until(f"seen.presented > {first} + {_ADVANCED_FRAMES}")
            assert await page.states() == ["loading", "drawn"]
            assert (await page.read()).thrown == []

    live_in_browser(scenario)


def test_a_refused_play_reports_ended_and_never_a_muted_picture(
    live_in_browser: Callable[..., None], tmp_path: Path
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        async with stage(
            service, session, tmp_path / "profile", autoplay_flag=REFUSES_AUTOPLAY
        ) as page:
            await page.begin_on_load()
            await page.until("seen.states.includes('ended')")

            seen = await page.read()
            assert seen.states == ["loading", "ended"]
            assert seen.thrown == []
            assert await page.page.locator("video").count() == 0

    live_in_browser(scenario)


def _refused_with(status: HTTPStatus) -> Callable[[Stage], str]:
    def arrange(page: Stage) -> str:
        page.app.refusal = status
        return VIEWERS_PATH

    return arrange


def _answered_without_a_location(page: Stage) -> str:
    def alter(relayed: httpx2.Response) -> Response:
        return Response(
            relayed.content, HTTPStatus.CREATED, media_type=SDP_CONTENT_TYPE
        )

    page.app.alter = alter
    return VIEWERS_PATH


def _answered_with_what_does_not_set(page: Stage) -> str:
    def alter(relayed: httpx2.Response) -> Response:
        return Response(
            _NOT_AN_ANSWER,
            HTTPStatus.CREATED,
            media_type=SDP_CONTENT_TYPE,
            headers={LOCATION_HEADER: relayed.headers[LOCATION_HEADER]},
        )

    page.app.alter = alter
    return VIEWERS_PATH


def _to_a_route_that_does_not_listen(_page: Stage) -> str:
    return f"http://127.0.0.1:{free_port()}{VIEWERS_PATH}"


@pytest.mark.parametrize(
    "arrange",
    [
        _refused_with(HTTPStatus.TOO_MANY_REQUESTS),
        _refused_with(HTTPStatus.SERVICE_UNAVAILABLE),
        _refused_with(HTTPStatus.FORBIDDEN),
        _to_a_route_that_does_not_listen,
        _answered_without_a_location,
        _answered_with_what_does_not_set,
    ],
    ids=["429", "503", "403", "no route", "no location", "answer that does not set"],
)
def test_a_route_that_does_not_give_a_viewer_reports_ended_and_throws_nothing(
    live_in_browser: Callable[..., None],
    tmp_path: Path,
    arrange: Callable[[Stage], str],
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        async with stage(service, session, tmp_path / "profile") as page:
            await page.begin(arrange(page))
            await page.until("seen.states.includes('ended')")

            seen = await page.read()
            assert seen.states == ["loading", "ended"]
            assert seen.thrown == []

    live_in_browser(scenario)


def test_stop_sends_the_delete_to_the_routes_location_and_the_service_ends_the_viewer(
    live_in_browser: Callable[..., None], tmp_path: Path
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        async with stage(service, session, tmp_path / "profile") as page:
            await page.begin()
            await page.until("seen.states.includes('drawn')")

            await page.page.evaluate("stage.stop()")
            await service.logged.wait_for("viewer.ended")

            assert await page.states() == ["loading", "drawn", "ended"]
            assert service.viewer_ends() == [ViewerEnd.DELETED]

    live_in_browser(scenario)


def test_a_stop_before_the_post_has_answered_ends_at_once_and_deletes_after_the_answer(
    live_in_browser: Callable[..., None], tmp_path: Path
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        async with stage(service, session, tmp_path / "profile") as page:
            page.app.answer_gate.clear()
            await page.begin()
            await page.app.posted.wait()

            await page.page.evaluate("stage.stop()")
            assert await page.states() == ["loading", "ended"]
            assert service.viewer_ends() == []
            page.app.answer_gate.set()
            await service.logged.wait_for("viewer.ended")

            assert service.viewer_ends() == [ViewerEnd.DELETED]
            assert (await page.read()).thrown == []
            replacement = await service.watch(session)
            assert replacement.location

    live_in_browser(scenario, max_viewers=1)


def test_the_end_of_the_session_ends_the_viewer(
    live_in_browser: Callable[..., None], tmp_path: Path
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        async with stage(service, session, tmp_path / "profile") as page:
            await page.begin()
            await page.until("seen.states.includes('drawn')")

            await session.socket.close()
            await page.until("seen.states.includes('ended')")

            assert await page.states() == ["loading", "drawn", "ended"]
            assert service.viewer_ends() == [ViewerEnd.SESSION_ENDED]

    live_in_browser(scenario)


def test_no_request_from_the_page_carries_a_token(
    live_in_browser: Callable[..., None], tmp_path: Path
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        async with stage(service, session, tmp_path / "profile") as page:
            await page.begin()
            await page.until("seen.states.includes('drawn')")
            await page.page.evaluate("stage.stop()")
            await service.logged.wait_for("viewer.ended")

            tokens = {name.lower(): value for name, value in valid_headers().items()}
            assert {url for url, _ in page.requests} >= {
                f"{page.address}{VIEWERS_PATH[1:]}"
            }
            for url, headers in page.requests:
                assert tokens.keys().isdisjoint(headers), url
                assert not any(
                    value in text
                    for value in tokens.values()
                    for text in (url, *headers.values())
                ), url

    live_in_browser(scenario)
