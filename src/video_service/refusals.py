"""The HTTP form of a refusal, for the answers of a path and of a refused upgrade."""

from __future__ import annotations

import re
from typing import Final

from fastapi.responses import JSONResponse

from video_client.wire import (
    API_VERSION,
    AUTHENTICATE_HEADER,
    BUSY_RETRY_AFTER_SECONDS,
    REFUSAL_STATUS,
    RETRY_AFTER_HEADER,
    RefusalCode,
    refusal,
)

_VERSION_PREFIX: Final = re.compile(r"/v(\d+)(?:/|$)", re.ASCII)
_BEARER_CHALLENGE: Final = "Bearer"


def refusal_response(code: RefusalCode, param: str | None = None) -> JSONResponse:
    """The answer for `code` in OpenAI's error form, with the header its code needs."""
    headers = {
        RefusalCode.UNAUTHORIZED: {AUTHENTICATE_HEADER: _BEARER_CHALLENGE},
        RefusalCode.BUSY: {RETRY_AFTER_HEADER: str(BUSY_RETRY_AFTER_SECONDS)},
    }.get(code)
    return JSONResponse(
        refusal(code, param).model_dump(mode="json"),
        status_code=int(REFUSAL_STATUS[code]),
        headers=headers,
    )


def unknown_path_response(path: str) -> JSONResponse:
    """The answer for a path or method the service does not offer."""
    found = _VERSION_PREFIX.match(path)
    code = (
        RefusalCode.UNSUPPORTED_VERSION
        if found and found.group(1) != str(API_VERSION)
        else RefusalCode.NOT_FOUND
    )
    return refusal_response(code)
