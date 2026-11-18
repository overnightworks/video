"""The HTTP app: health and the refusals of every other path."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from logline import RequestLineMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from video_client.wire import (
    API_VERSION,
    HEALTH_PATH,
    REFUSAL_STATUS,
    Health,
    RefusalCode,
    refusal,
)

if TYPE_CHECKING:
    from fastapi import Request

    from video_service.runtime import Runtime

_VERSION_PREFIX: Final = re.compile(r"/v([0-9]+)(?:/|$)")


def _refused(request: Request, _error: Exception) -> JSONResponse:
    """The refusal of a path or method the service does not offer."""
    found = _VERSION_PREFIX.match(request.url.path)
    code = (
        RefusalCode.UNSUPPORTED_VERSION
        if found and found.group(1) != str(API_VERSION)
        else RefusalCode.NOT_FOUND
    )
    return JSONResponse(
        refusal(code).model_dump(mode="json"), status_code=REFUSAL_STATUS[code]
    )


def create_app(runtime: Runtime) -> FastAPI:
    """Build the app: health, and no docs, schema or look administration."""
    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False
    )
    app.add_middleware(RequestLineMiddleware)
    app.add_exception_handler(StarletteHTTPException, _refused)

    @app.get(HEALTH_PATH)
    def health() -> Health:
        return runtime.health()

    return app
