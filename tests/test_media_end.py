"""A viewer ends on its own close or with its session, and the session goes on."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest

from support.viewer_support import Service, offer_of
from video_service.media import ViewerEnd

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from support.viewer_support import OpenSession

_FRAMES_AFTER = 5


def test_a_viewer_whose_peer_closes_ends_and_the_session_keeps_serving_the_others(
    live: Callable[..., None],
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        leaving = await service.watch(session)
        staying = await service.watch(session)
        await staying.received.until(lambda got: len(got.video_pts) >= 1)

        await leaving.close()
        await service.logged.wait_for("viewer.ended")
        seen = len(staying.received.video_pts)
        await staying.received.until(
            lambda got: len(got.video_pts) >= seen + _FRAMES_AFTER
        )
        replacement = await service.post(session, await offer_of())

        assert service.viewer_ends() == [ViewerEnd.PEER_CLOSED]
        assert replacement.status_code == HTTPStatus.CREATED

    live(scenario, max_viewers=2)


async def _disconnects(session: OpenSession, service: Service) -> None:
    del service
    await session.socket.close()


async def _fails(session: OpenSession, service: Service) -> None:
    del service
    await session.socket.send(json.dumps({"type": "no.such.event"}))


async def _drains(session: OpenSession, service: Service) -> None:
    del session
    service.runtime.begin_drain()


@pytest.mark.parametrize(
    "ending",
    [_disconnects, _fails, _drains],
    ids=["the client's disconnect", "a failure", "the drain's 1012"],
)
def test_every_viewer_ends_with_its_session(
    live: Callable[..., None],
    ending: Callable[[OpenSession, Service], Awaitable[None]],
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        first = await service.watch(session)
        second = await service.watch(session)
        await first.received.until(lambda got: len(got.video_pts) >= 1)
        await second.received.until(lambda got: len(got.video_pts) >= 1)

        await ending(session, service)

        await service.logged.wait_for("viewer.ended", 2)
        assert service.viewer_ends() == [ViewerEnd.SESSION_ENDED] * 2

    live(scenario)
