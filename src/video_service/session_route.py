"""`WS /v1/sessions`: the refusals of the upgrade, then the session itself."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from logline import StepTimer, set_route

from video_client.wire import LOOK_PARAM, SESSIONS_PATH, RefusalCode
from video_service.refusals import refusal_response, unknown_path_response
from video_service.session import Session

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.websockets import WebSocket

    from video_service.callers import Opener, VideoCallers
    from video_service.runtime import Place, Runtime

logger = logging.getLogger(__name__)


class _SessionRoute:
    def __init__(self, runtime: Runtime, callers: VideoCallers) -> None:
        self._runtime = runtime
        self._callers = callers

    async def serve(self, websocket: WebSocket) -> None:
        """Refuse the upgrade, or accept it and serve the session."""
        set_route(websocket.scope, SESSIONS_PATH)
        refused = StepTimer(
            "session.ended", logger, provider=self._runtime.provider_name
        )
        look = websocket.query_params.get(LOOK_PARAM, "")
        opener = await self._callers.admit(websocket.headers)
        placed = (
            opener
            if isinstance(opener, RefusalCode)
            else await self._runtime.open_session(look)
        )
        if isinstance(placed, RefusalCode):
            with refused:
                refused.refuse(placed.value)
            param = LOOK_PARAM if placed is RefusalCode.INVALID_REQUEST else None
            await websocket.send_denial_response(refusal_response(placed, param))
            return
        await self._run(websocket, look, placed, opener)

    async def _run(
        self, websocket: WebSocket, look: str, place: Place, opener: Opener
    ) -> None:
        """Serve the session, findable by its id for as long as it runs."""
        session_id = uuid4().hex
        session = Session(
            session_id=session_id,
            look=look,
            provider=self._runtime.provider_name,
            socket=websocket,
            place=place,
            opener=opener,
        )
        withdraw = self._runtime.enrol(session_id, session)
        try:
            await session.run()
        finally:
            withdraw()


async def _deny_upgrade(websocket: WebSocket) -> None:
    """Refuse an upgrade at a path the service does not offer."""
    await websocket.send_denial_response(unknown_path_response(websocket.url.path))


def mount_session_route(app: FastAPI, runtime: Runtime, callers: VideoCallers) -> None:
    """Mount the session's WebSocket route, then the refusal of every other upgrade."""
    app.router.add_websocket_route(SESSIONS_PATH, _SessionRoute(runtime, callers).serve)
    app.router.add_websocket_route("/{rest:path}", _deny_upgrade)
