"""The HTTP app: health, the session upgrade, and the refusals of every other path."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from logline import RequestLineMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from video_client.wire import HEALTH_PATH, Health
from video_service.refusals import unknown_path_response
from video_service.session_route import mount_session_route

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from video_service.callers import VideoCallers
    from video_service.runtime import Runtime


def _refused(request: Request, _error: Exception) -> JSONResponse:
    """The refusal of a path or method the service does not offer."""
    return unknown_path_response(request.url.path)


def create_app(runtime: Runtime, callers: VideoCallers) -> FastAPI:
    """Build the app: health and sessions, and no docs, schema or administration."""
    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False
    )
    app.add_middleware(RequestLineMiddleware)
    app.add_exception_handler(StarletteHTTPException, _refused)

    @app.get(HEALTH_PATH)
    def health() -> Health:
        return runtime.health()

    mount_session_route(app, runtime, callers)
    return app
