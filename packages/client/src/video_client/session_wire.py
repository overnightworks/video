"""The events of a session: what a client and the service send over `WS /v1/sessions`.

A client event is one JSON text message, or a binary message of PCM16 little-endian
mono audio for the open sentence. `parse_client_event` and `parse_server_event` are
the only readers of the text messages; a message that is not one of the events is
refused naming a field, never echoing its content, which can carry credentials.
"""

from __future__ import annotations

import json
from typing import Annotated, Final, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    TypeAdapter,
    ValidationError,
)

from video_client.wire import LookName, RefusalCode, VideoFormat, refusal

BYTES_PER_SAMPLE: Final = 2
MAX_EVENT_BYTES: Final = 64 * 1024
_MAX_ICE_SERVERS: Final = 16
_MAX_ICE_URLS: Final = 8
_MAX_TEXT_CHARACTERS: Final = 1024


_Text = Annotated[str, Field(max_length=_MAX_TEXT_CHARACTERS)]


class _ClientModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )


def _as_list(urls: object) -> object:
    return [urls] if isinstance(urls, str) else urls


class IceServer(_ClientModel):
    """One entry of W3C `RTCConfiguration.iceServers`; the credential is never shown."""

    urls: Annotated[
        list[_Text],
        BeforeValidator(_as_list),
        Field(min_length=1, max_length=_MAX_ICE_URLS),
    ]
    username: _Text | None = None
    credential: _Text | None = Field(default=None, repr=False)


class IceConfiguration(_ClientModel):
    """The W3C `RTCConfiguration` members a session needs."""

    ice_servers: Annotated[list[IceServer], Field(max_length=_MAX_ICE_SERVERS)]
    ice_transport_policy: Literal["relay", "all"]


class PcmFormat(_ClientModel):
    """The audio of a sentence: PCM16 little-endian mono at a rate from a closed set."""

    type: Literal["audio/pcm"]
    rate: Literal[16000, 22050, 24000, 48000]


SentenceId = Annotated[str, Field(min_length=1, max_length=128)]


class _ClientEvent(_ClientModel):
    event_id: _Text | None = None


class SessionUpdate(_ClientEvent):
    """`session.update`: the client's first event, once."""

    type: Literal["session.update"]
    session: IceConfiguration
    traceparent: _Text | None = None


class SentenceStart(_ClientEvent):
    """`sentence.start`: opens a sentence; binary audio frames follow."""

    type: Literal["sentence.start"]
    sentence: SentenceId
    format: PcmFormat
    traceparent: _Text | None = None


class SentenceEnd(_ClientEvent):
    """`sentence.end`: the open sentence has all its audio."""

    type: Literal["sentence.end"]
    sentence: SentenceId


class OutputAudioBufferClear(_ClientEvent):
    """`output_audio_buffer.clear`: drop the playing and every queued sentence."""

    type: Literal["output_audio_buffer.clear"]


ClientEvent = SessionUpdate | SentenceStart | SentenceEnd | OutputAudioBufferClear
_SESSION_UPDATE: Final = "session.update"
_CLIENT_EVENT: Final = TypeAdapter(Annotated[ClientEvent, Field(discriminator="type")])


class ClientEventError(ValueError):
    """A client message that is not one of the events this service accepts."""

    def __init__(self, param: str | None, event_id: str | None) -> None:
        """Name the field at fault and the client event's own id; never its content."""
        super().__init__("the client event is not valid")
        self.param = param
        self.event_id = event_id


def parse_client_event(text: str) -> ClientEvent:
    """The client event in `text`; raises `ClientEventError` for anything else."""
    try:
        return _CLIENT_EVENT.validate_json(text)
    except ValidationError as error:
        first = error.errors(
            include_url=False, include_context=False, include_input=False
        )[0]
        # A field error leads with the event's type, which the param leaves out.
        if first["type"] in {"union_tag_invalid", "union_tag_not_found"}:
            param = "type"
        else:
            param = ".".join(map(str, first["loc"][1:])) or None
        raise ClientEventError(param, client_event_id(text)) from None


def is_session_update(text: str) -> bool:
    """Whether `text` names itself a `session.update`, whatever else it holds."""
    event = _members(text)
    return event.get("type") == _SESSION_UPDATE


def client_event_id(text: str) -> str | None:
    """The `event_id` the client's message carries, valid or not."""
    event_id = _members(text).get("event_id")
    return event_id if isinstance(event_id, str) else None


def _members(text: str) -> dict[str, object]:
    try:
        event = json.loads(text)
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}


def new_event_id() -> str:
    """A fresh id for a server event."""
    return f"event_{uuid4().hex}"


class _ServerEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=new_event_id)


class SessionInfo(BaseModel):
    """The session as created: its id, its look, its picture and its audio limit."""

    id: str
    look: LookName
    video: VideoFormat
    max_queued_audio_seconds: float


class SessionCreated(_ServerEvent):
    """`session.created`: first on every session."""

    type: Literal["session.created"] = "session.created"
    session: SessionInfo


class SessionUpdated(_ServerEvent):
    """`session.updated`: the provider drew the look's first idle frame."""

    type: Literal["session.updated"] = "session.updated"


class OutputAudioBufferStarted(_ServerEvent):
    """A sentence's first frame left for the viewers."""

    type: Literal["output_audio_buffer.started"] = "output_audio_buffer.started"
    response_id: str


class OutputAudioBufferStopped(_ServerEvent):
    """A sentence's last frame left; `audio_end_ms` is the whole sentence."""

    type: Literal["output_audio_buffer.stopped"] = "output_audio_buffer.stopped"
    response_id: str
    audio_end_ms: StrictInt = Field(ge=0)


class OutputAudioBufferCleared(_ServerEvent):
    """A sentence dropped by a clear; `audio_end_ms` is the part that had played."""

    type: Literal["output_audio_buffer.cleared"] = "output_audio_buffer.cleared"
    response_id: str
    audio_end_ms: StrictInt = Field(ge=0)


class SessionFailure(BaseModel):
    """The `error` object of an `error` event."""

    message: str
    type: str
    code: RefusalCode
    param: str | None
    event_id: str | None = None


class ErrorEvent(_ServerEvent):
    """`error`: the session ends after it."""

    type: Literal["error"] = "error"
    error: SessionFailure


ServerEvent = (
    SessionCreated
    | SessionUpdated
    | OutputAudioBufferStarted
    | OutputAudioBufferStopped
    | OutputAudioBufferCleared
    | ErrorEvent
)
_SERVER_EVENT: Final = TypeAdapter(Annotated[ServerEvent, Field(discriminator="type")])


class ServerEventError(ValueError):
    """A server message that is not one of the events the wire names."""

    def __init__(self) -> None:
        """Carry no field and no content."""
        super().__init__("the server event is not valid")


def parse_server_event(text: str) -> ServerEvent:
    """The server event in `text`; raises `ServerEventError` for anything else."""
    try:
        return _SERVER_EVENT.validate_json(text)
    except ValidationError:
        raise ServerEventError from None


def error_event(
    code: RefusalCode, param: str | None = None, event_id: str | None = None
) -> ErrorEvent:
    """The `error` event for `code`, naming the field and the client event at fault."""
    detail = refusal(code, param).error
    return ErrorEvent(
        error=SessionFailure(
            message=detail.message,
            type=detail.type,
            code=code,
            param=param,
            event_id=event_id,
        )
    )
