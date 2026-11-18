"""The wire between an app and the video service.

This module is the one owner of the paths, the caller headers, the `GET /health`
answer and the closed list of refusals with their statuses, messages and OpenAI-form
body. `video_client.session_wire` owns the session's events. The service, its
providers and its callers import these types and copy none of them.
"""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Final

from pydantic import BaseModel, Field, StrictBool, StrictInt, StringConstraints

HEALTH_PATH: Final = "/health"
API_VERSION: Final = 1
SESSIONS_PATH: Final = f"/v{API_VERSION}/sessions"
LOOK_PARAM: Final = "look"
RETRY_AFTER_HEADER: Final = "Retry-After"
BUSY_RETRY_AFTER_SECONDS: Final = 5
DRAIN_CLOSE_CODE: Final = 1012  # the stopping service's close, with no `error` sent
AUTHENTICATE_HEADER: Final = "WWW-Authenticate"
AUTHORIZATION_HEADER: Final = "Authorization"
WORKLOAD_TOKEN_HEADER: Final = "Video-Workload-Token"  # noqa: S105 - a header name

LOOK_NAME_PATTERN: Final = r"^[A-Za-z0-9._-]{1,64}$"
LookName = Annotated[str, StringConstraints(pattern=LOOK_NAME_PATTERN)]


def caller_headers(*, workload_token: str, person_token: str) -> dict[str, str]:
    """The headers of a session upgrade: the app's workload token and the person's."""
    return {
        WORKLOAD_TOKEN_HEADER: workload_token,
        AUTHORIZATION_HEADER: f"Bearer {person_token}",
    }


class Verb(StrEnum):
    """What a provider can play on a session."""

    SPEAK = "speak"
    CLEAR = "clear"


class VideoFormat(BaseModel):
    """The picture a provider draws."""

    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)
    frame_rate: StrictInt = Field(gt=0)


class ProviderHealth(BaseModel):
    """The provider this instance runs: what it plays and draws, once it is ready."""

    name: str
    ready: StrictBool
    plays: list[Verb]
    video: VideoFormat | None


class LookHealth(BaseModel):
    """One look; `ready` is true exactly when a session for it would open now."""

    ready: StrictBool


class SessionsHealth(BaseModel):
    """The open sessions and the most the service admits."""

    open: StrictInt = Field(ge=0)
    limit: StrictInt = Field(ge=1)


class Health(BaseModel):
    """The `GET /health` answer; a reader ignores the keys it does not know."""

    ready: StrictBool
    provider: ProviderHealth
    looks: dict[LookName, LookHealth]
    sessions: SessionsHealth
    card_memory_mb: StrictInt = Field(ge=0)


class RefusalCode(StrEnum):
    """Every refusal an HTTP request can get; an app reads `code`."""

    UNSUPPORTED_VERSION = "unsupported_version"
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    INVALID_REQUEST = "invalid_request"
    TOO_LARGE = "too_large"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"


REFUSAL_STATUS: Final = {
    RefusalCode.UNSUPPORTED_VERSION: HTTPStatus.BAD_REQUEST,
    RefusalCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    RefusalCode.UNAUTHORIZED: HTTPStatus.UNAUTHORIZED,
    RefusalCode.FORBIDDEN: HTTPStatus.FORBIDDEN,
    RefusalCode.INVALID_REQUEST: HTTPStatus.UNPROCESSABLE_ENTITY,
    RefusalCode.TOO_LARGE: HTTPStatus.CONTENT_TOO_LARGE,
    RefusalCode.BUSY: HTTPStatus.TOO_MANY_REQUESTS,
    RefusalCode.UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
}

_REFUSAL_MESSAGE: Final = {
    RefusalCode.UNSUPPORTED_VERSION: "This API version is not supported.",
    RefusalCode.NOT_FOUND: "No such path.",
    RefusalCode.UNAUTHORIZED: "The tokens are missing or not valid.",
    RefusalCode.FORBIDDEN: "The tokens do not permit this request.",
    RefusalCode.INVALID_REQUEST: "The request is not valid.",
    RefusalCode.TOO_LARGE: "The request is too large.",
    RefusalCode.BUSY: "The service is busy.",
    RefusalCode.UNAVAILABLE: "The service cannot serve this now.",
}


class RefusalDetail(BaseModel):
    """The `error` object of OpenAI's error form."""

    message: str
    type: str
    param: str | None
    code: RefusalCode


class Refusal(BaseModel):
    """The body of every refusal: OpenAI's error form."""

    error: RefusalDetail


def refusal(code: RefusalCode, param: str | None = None) -> Refusal:
    """The refusal for `code`, naming the request field at fault, if any."""
    kind = (
        "server_error"
        if REFUSAL_STATUS[code] >= HTTPStatus.INTERNAL_SERVER_ERROR
        else "invalid_request_error"
    )
    return Refusal(
        error=RefusalDetail(
            message=_REFUSAL_MESSAGE[code], type=kind, param=param, code=code
        )
    )
