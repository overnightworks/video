"""`POST /v1/sessions/{id}/viewers` and `DELETE` on a viewer: refusals, then media.

Both calls prove the tokens and that the caller is the session's opener first. A
viewer's `viewer.ended` line is written when the viewer ends, in the trace of the
POST that admitted it, or at once when the POST is refused or fails.
"""

from __future__ import annotations

import logging
from functools import partial
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

from logline import StepTimer, set_route
from starlette.responses import Response

from video_client.wire import (
    LOCATION_HEADER,
    MAX_OFFER_BYTES,
    SDP_CONTENT_TYPE,
    SESSION_ID_PARAM,
    VIEWER_ID_PARAM,
    VIEWER_ROUTE,
    VIEWERS_ROUTE,
    RefusalCode,
    viewer_path,
)
from video_service.media import parse_offer
from video_service.refusals import refusal_response

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from starlette.datastructures import Headers
    from starlette.requests import Request

    from video_service.callers import VideoCallers
    from video_service.media import Admission, Offer, ViewerEnd
    from video_service.runtime import Runtime
    from video_service.session import Session

logger = logging.getLogger(__name__)

_CONTENT_TYPE: Final = "content-type"
_CONTENT_LENGTH: Final = "content-length"
_ENDED_BY: Final = "ended_by"


class _ViewerRoute:
    def __init__(self, runtime: Runtime, callers: VideoCallers) -> None:
        self._runtime = runtime
        self._callers = callers

    async def post(self, request: Request) -> Response:
        """Admit the viewer the offer asks for, or refuse it in the wire's order."""
        set_route(request.scope, VIEWERS_ROUTE)
        session_id = request.path_params[SESSION_ID_PARAM]
        step = StepTimer("viewer.ended", logger)

        def viewer_ended(reason: ViewerEnd) -> None:
            step.add(**{_ENDED_BY: reason.value})
            step.end()

        try:
            admitted = await self._admit(request, session_id, viewer_ended)
        except BaseException as error:
            # The step's own exit names the outcome: failed, or cancelled.
            step.__exit__(type(error), error, error.__traceback__)
            raise
        if isinstance(admitted, RefusalCode):
            step.refuse(admitted.value)
            step.end()
            return refusal_response(admitted)
        return Response(
            admitted.answer,
            status_code=HTTPStatus.CREATED,
            media_type=SDP_CONTENT_TYPE,
            headers={LOCATION_HEADER: viewer_path(session_id, admitted.viewer_id)},
        )

    async def delete(self, request: Request) -> Response:
        """End the viewer, or refuse as a viewer's POST would."""
        set_route(request.scope, VIEWER_ROUTE)
        session = await self._opened_by_caller(
            request.headers, request.path_params[SESSION_ID_PARAM]
        )
        if isinstance(session, RefusalCode):
            return refusal_response(session)
        if not session.media.remove(request.path_params[VIEWER_ID_PARAM]):
            return refusal_response(RefusalCode.NOT_FOUND)
        return Response(status_code=HTTPStatus.OK)

    async def _admit(
        self,
        request: Request,
        session_id: str,
        on_end: Callable[[ViewerEnd], None],
    ) -> Admission | RefusalCode:
        session = await self._opened_by_caller(request.headers, session_id)
        if isinstance(session, RefusalCode):
            return session
        if not (self._runtime.accepts_viewers and session.media.ready):
            return RefusalCode.UNAVAILABLE
        return await session.media.admit(partial(_offer_of, request), on_end)

    async def _opened_by_caller(
        self, headers: Headers, session_id: str
    ) -> Session | RefusalCode:
        """The open session `session_id`, if the tokens are valid and its opener's."""
        opener = await self._callers.admit(headers)
        if isinstance(opener, RefusalCode):
            return opener
        session = self._runtime.session(session_id)
        if session is None or session.media.closed:
            return RefusalCode.NOT_FOUND
        if session.opener != opener:
            return RefusalCode.FORBIDDEN
        return session


async def _offer_of(request: Request) -> Offer | RefusalCode:
    """The request's offer, or the refusal of its type, size or shape."""
    if _media_type(request.headers) != SDP_CONTENT_TYPE:
        return RefusalCode.INVALID_REQUEST
    body = await _bounded_body(request)
    if body is None:
        return RefusalCode.TOO_LARGE
    offer = parse_offer(body)
    return RefusalCode.INVALID_REQUEST if offer is None else offer


def _media_type(headers: Headers) -> str:
    return headers.get(_CONTENT_TYPE, "").partition(";")[0].strip().lower()


async def _bounded_body(request: Request) -> bytes | None:
    """The body, or `None` once it is over the offer cap; the rest is never read."""
    declared = request.headers.get(_CONTENT_LENGTH, "")
    if declared.isdigit() and int(declared) > MAX_OFFER_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_OFFER_BYTES:
            return None
        body += chunk
    return bytes(body)


def mount_viewer_route(app: FastAPI, runtime: Runtime, callers: VideoCallers) -> None:
    """Mount the viewer paths; any other method on them is the wire's 404."""
    route = _ViewerRoute(runtime, callers)
    app.router.add_route(VIEWERS_ROUTE, route.post, methods=["POST"])
    app.router.add_route(VIEWER_ROUTE, route.delete, methods=["DELETE"])
