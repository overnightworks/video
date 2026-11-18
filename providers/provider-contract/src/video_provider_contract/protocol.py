"""The private frame protocol between the video service and a provider's process.

A frame is a kind (one byte), the length of its payload (four bytes) and the
payload, all numbers in network order. The service sends requests and the provider
answers each in order; every per-session frame carries the session's id, which the
service chooses, so one process serves every open session.

| Request | Answer |
|---|---|
| (the start) | READY `{plays, video}` |
| PREPARE `{look, image}` | PREPARED |
| OPEN `{session, look}` | OPENED |
| IDLE, PCM, SENTENCE_END `{session}` | FRAMES `{session, pictures}` |
| CLEAR `{session}` | CLEARED, after the answer to every request sent before it |
| CLOSE `{session}` | none |

Any request may be answered by FAILED `{code, session}` instead, with a closed code
and never a message, since a provider's text can hold what a person said. A frame
longer than its kind allows is refused from its header, before its payload is read.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, BinaryIO

from video_provider_contract import (
    DecodedImage,
    Frame,
    ProviderInfo,
    ProviderVerb,
    ProviderVideoFormat,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

BYTES_PER_PIXEL = 3
# The most any frame besides a picture, an image or audio holds.
MAX_CONTROL_BYTES = 4096
MAX_LOOK_BYTES = 256
# The wire bounds an event at 64 KiB, and the audio in it is smaller.
MAX_PCM_BYTES = 64 * 1024
MAX_PICTURE_BYTES = 64 * 1024 * 1024
MAX_PICTURES_PER_ANSWER = 128
NO_SESSION = 0

_HEADER = struct.Struct("!BI")
_SESSION = struct.Struct("!I")
_READY = struct.Struct("!III")
_IMAGE_SIZE = struct.Struct("!II")
_FAILED = struct.Struct("!BI")
_LOOK_LENGTH = struct.Struct("!H")
# What a PREPARE holds besides its image: the look's name and the image's size.
_PREPARE_OVERHEAD = _LOOK_LENGTH.size + MAX_LOOK_BYTES + _IMAGE_SIZE.size


class FrameKind(IntEnum):
    """The kinds of frame; a request goes to the provider, an answer comes back."""

    READY = 1
    PREPARE = 2
    PREPARED = 3
    OPEN = 4
    OPENED = 5
    IDLE = 6
    PCM = 7
    SENTENCE_END = 8
    FRAMES = 9
    CLEAR = 10
    CLEARED = 11
    CLOSE = 12
    FAILED = 13


REQUEST_KINDS = frozenset(
    {
        FrameKind.PREPARE,
        FrameKind.OPEN,
        FrameKind.IDLE,
        FrameKind.PCM,
        FrameKind.SENTENCE_END,
        FrameKind.CLEAR,
        FrameKind.CLOSE,
    }
)
ANSWER_KINDS = frozenset(set(FrameKind) - REQUEST_KINDS)
# The kinds that carry nothing but a session's id.
_SESSION_KINDS = frozenset(
    {
        FrameKind.OPENED,
        FrameKind.IDLE,
        FrameKind.SENTENCE_END,
        FrameKind.CLEAR,
        FrameKind.CLEARED,
        FrameKind.CLOSE,
    }
)


class FailureCode(IntEnum):
    """Why a provider answered FAILED."""

    REQUEST = 1
    """The request failed and the provider serves on."""
    PROVIDER = 2
    """The provider can serve nothing more."""


class ProtocolError(ValueError):
    """A frame the protocol does not allow."""


@dataclass(frozen=True, slots=True)
class ProtocolFrame:
    """One frame: its kind and payload."""

    kind: FrameKind
    payload: bytes = b""


@dataclass(frozen=True, slots=True)
class FrameLimits:
    """What the picture and the image sizes allow a frame to hold.

    `picture_bytes` is READY's `width * height * 3`, zero until the provider is
    ready; `image_bytes` is the most an image may hold.
    """

    picture_bytes: int = 0
    image_bytes: int = 0

    def payload_cap(self, kind: FrameKind) -> int:
        """The longest payload a frame of `kind` may have."""
        if kind is FrameKind.FRAMES:
            return _SESSION.size + MAX_PICTURES_PER_ANSWER * self.picture_bytes
        if kind is FrameKind.PREPARE:
            return _PREPARE_OVERHEAD + self.image_bytes
        if kind is FrameKind.PCM:
            return _SESSION.size + _SESSION.size + MAX_PCM_BYTES
        return MAX_CONTROL_BYTES


def write_frame(stream: BinaryIO, frame: ProtocolFrame) -> None:
    """Write `frame` and flush it."""
    stream.write(_HEADER.pack(frame.kind, len(frame.payload)))
    stream.write(frame.payload)
    stream.flush()


def read_frame(
    stream: BinaryIO, limits: FrameLimits, accepted: Collection[FrameKind]
) -> ProtocolFrame:
    """Read one frame whose kind is in `accepted`; EOFError when the stream ends.

    Raises ProtocolError for an unknown or unaccepted kind, a payload longer than
    `limits` allows, and a frame cut short.
    """
    header = stream.read(_HEADER.size)
    if not header:
        raise EOFError
    if len(header) < _HEADER.size:
        raise ProtocolError
    raw_kind, length = _HEADER.unpack(header)
    try:
        kind = FrameKind(raw_kind)
    except ValueError:
        raise ProtocolError from None
    if kind not in accepted or length > limits.payload_cap(kind):
        raise ProtocolError
    payload = stream.read(length)
    if len(payload) < length:
        raise ProtocolError
    return ProtocolFrame(kind, payload)


def picture_bytes(video: ProviderVideoFormat) -> int:
    """The bytes of one picture of `video`."""
    return video.width * video.height * BYTES_PER_PIXEL


def encode_ready(info: ProviderInfo) -> ProtocolFrame:
    """READY for a provider that plays `info.plays` and draws `info.video`."""
    verbs = ",".join(verb.value for verb in info.plays).encode("ascii")
    video = info.video
    payload = _READY.pack(video.width, video.height, video.frame_rate) + verbs
    return ProtocolFrame(FrameKind.READY, payload)


def decode_ready(payload: bytes) -> ProviderInfo:
    """What a READY payload reports, or ProtocolError."""
    if len(payload) < _READY.size:
        raise ProtocolError
    width, height, frame_rate = _READY.unpack_from(payload)
    video = ProviderVideoFormat(width, height, frame_rate)
    if min(width, height, frame_rate) <= 0 or picture_bytes(video) > MAX_PICTURE_BYTES:
        raise ProtocolError
    names = payload[_READY.size :].decode("ascii", errors="replace")
    try:
        plays = tuple(ProviderVerb(name) for name in names.split(",") if name)
    except ValueError:
        raise ProtocolError from None
    return ProviderInfo(plays=plays, video=video)


def encode_prepare(look: str, image: DecodedImage) -> ProtocolFrame:
    """PREPARE `look` from `image`."""
    name = _encode_look(look)
    size = _IMAGE_SIZE.pack(image.width, image.height)
    return ProtocolFrame(FrameKind.PREPARE, name + size + image.rgb)


def decode_prepare(payload: bytes, limits: FrameLimits) -> tuple[str, DecodedImage]:
    """The look and image a PREPARE payload holds, or ProtocolError."""
    look, rest = _split_look(payload)
    if len(rest) < _IMAGE_SIZE.size:
        raise ProtocolError
    width, height = _IMAGE_SIZE.unpack_from(rest)
    rgb = rest[_IMAGE_SIZE.size :]
    if (
        min(width, height) <= 0
        or len(rgb) != width * height * BYTES_PER_PIXEL
        or len(rgb) > limits.image_bytes
    ):
        raise ProtocolError
    return look, DecodedImage(width, height, rgb)


def encode_open(session: int, look: str) -> ProtocolFrame:
    """OPEN `session` on `look`."""
    return ProtocolFrame(FrameKind.OPEN, _SESSION.pack(session) + _look_bytes(look))


def decode_open(payload: bytes) -> tuple[int, str]:
    """The session and look an OPEN payload holds, or ProtocolError."""
    session, rest = _split_session(payload)
    return session, _decode_look(rest)


def encode_session(kind: FrameKind, session: int) -> ProtocolFrame:
    """A frame of `kind` that carries only the id of `session`."""
    if kind not in _SESSION_KINDS:
        raise ProtocolError
    return ProtocolFrame(kind, _SESSION.pack(session))


def decode_session(payload: bytes) -> int:
    """The session id of a frame that carries nothing else, or ProtocolError."""
    session, rest = _split_session(payload)
    if rest:
        raise ProtocolError
    return session


def encode_pcm(session: int, rate: int, pcm: bytes) -> ProtocolFrame:
    """PCM16 audio `pcm` at `rate` Hz for `session`."""
    if rate <= 0 or len(pcm) > MAX_PCM_BYTES:
        raise ProtocolError
    return ProtocolFrame(
        FrameKind.PCM, _SESSION.pack(session) + _SESSION.pack(rate) + pcm
    )


def decode_pcm(payload: bytes) -> tuple[int, int, bytes]:
    """The session, rate and audio of a PCM payload, or ProtocolError."""
    session, rest = _split_session(payload)
    rate, pcm = _split_session(rest)
    if rate <= 0 or len(pcm) > MAX_PCM_BYTES:
        raise ProtocolError
    return session, rate, pcm


def encode_frames(
    session: int, pictures: Sequence[Frame], video: ProviderVideoFormat
) -> ProtocolFrame:
    """FRAMES: the pictures `session` drew, each a whole picture of `video`."""
    size = picture_bytes(video)
    if len(pictures) > MAX_PICTURES_PER_ANSWER or any(
        len(picture.rgb) != size for picture in pictures
    ):
        raise ProtocolError
    body = b"".join(picture.rgb for picture in pictures)
    return ProtocolFrame(FrameKind.FRAMES, _SESSION.pack(session) + body)


def decode_frames(
    payload: bytes, video: ProviderVideoFormat
) -> tuple[int, list[Frame]]:
    """The session and whole pictures of a FRAMES payload, or ProtocolError."""
    session, body = _split_session(payload)
    size = picture_bytes(video)
    count, remainder = divmod(len(body), size)
    if remainder or count > MAX_PICTURES_PER_ANSWER:
        raise ProtocolError
    return session, [Frame(body[at : at + size]) for at in range(0, len(body), size)]


def encode_failed(code: FailureCode, session: int = NO_SESSION) -> ProtocolFrame:
    """FAILED with `code`, for `session` or, with none, the provider's own request."""
    return ProtocolFrame(FrameKind.FAILED, _FAILED.pack(code, session))


def decode_failed(payload: bytes) -> tuple[FailureCode, int]:
    """The code and session of a FAILED payload, or ProtocolError."""
    if len(payload) != _FAILED.size:
        raise ProtocolError
    raw_code, session = _FAILED.unpack(payload)
    try:
        return FailureCode(raw_code), session
    except ValueError:
        raise ProtocolError from None


def session_of(payload: bytes) -> int:
    """The id of the session a per-session frame's payload begins with."""
    return _split_session(payload)[0]


def _split_session(payload: bytes) -> tuple[int, bytes]:
    if len(payload) < _SESSION.size:
        raise ProtocolError
    (session,) = _SESSION.unpack_from(payload)
    return session, payload[_SESSION.size :]


def _look_bytes(look: str) -> bytes:
    name = look.encode("utf-8")
    if not 0 < len(name) <= MAX_LOOK_BYTES:
        raise ProtocolError
    return name


def _encode_look(look: str) -> bytes:
    name = _look_bytes(look)
    return _LOOK_LENGTH.pack(len(name)) + name


def _split_look(payload: bytes) -> tuple[str, bytes]:
    if len(payload) < _LOOK_LENGTH.size:
        raise ProtocolError
    (length,) = _LOOK_LENGTH.unpack_from(payload)
    end = _LOOK_LENGTH.size + length
    return _decode_look(payload[_LOOK_LENGTH.size : end]), payload[end:]


def _decode_look(name: bytes) -> str:
    if not 0 < len(name) <= MAX_LOOK_BYTES:
        raise ProtocolError
    try:
        return name.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError from None
