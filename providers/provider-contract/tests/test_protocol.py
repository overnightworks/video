"""The frame protocol: what is encoded decodes, and what is not allowed is refused."""

from __future__ import annotations

import io
import struct
from typing import TYPE_CHECKING

import pytest
from video_provider_contract import (
    DecodedImage,
    Frame,
    ProviderInfo,
    ProviderVerb,
    ProviderVideoFormat,
)
from video_provider_contract.protocol import (
    ANSWER_KINDS,
    MAX_CONTROL_BYTES,
    MAX_LOOK_BYTES,
    MAX_PCM_BYTES,
    MAX_PICTURE_BYTES,
    MAX_PICTURES_PER_ANSWER,
    REQUEST_KINDS,
    FailureCode,
    FrameKind,
    FrameLimits,
    ProtocolError,
    ProtocolFrame,
    decode_failed,
    decode_frames,
    decode_open,
    decode_pcm,
    decode_prepare,
    decode_ready,
    decode_session,
    encode_failed,
    encode_frames,
    encode_open,
    encode_pcm,
    encode_prepare,
    encode_ready,
    encode_session,
    picture_bytes,
    read_frame,
    write_frame,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_VIDEO = ProviderVideoFormat(width=4, height=2, frame_rate=25)
_PICTURE = picture_bytes(_VIDEO)
_SESSION = 7
_LIMITS = FrameLimits(picture_bytes=_PICTURE, image_bytes=1024)
_HEADER = struct.Struct("!BI")


def _round_trip(frame: ProtocolFrame, accepted: frozenset[FrameKind]) -> ProtocolFrame:
    stream = io.BytesIO()
    write_frame(stream, frame)
    stream.seek(0)
    return read_frame(stream, _LIMITS, accepted)


def test_ready_carries_what_a_provider_plays_and_draws() -> None:
    info = ProviderInfo(plays=(ProviderVerb.SPEAK, ProviderVerb.CLEAR), video=_VIDEO)

    frame = _round_trip(encode_ready(info), ANSWER_KINDS)

    assert decode_ready(frame.payload) == info


def test_prepare_carries_a_look_and_its_image() -> None:
    image = DecodedImage(width=2, height=1, rgb=bytes(range(6)))

    frame = _round_trip(encode_prepare("mozart", image), REQUEST_KINDS)

    assert decode_prepare(frame.payload, _LIMITS) == ("mozart", image)


def test_open_carries_a_session_and_its_look() -> None:
    frame = _round_trip(encode_open(_SESSION, "mozart"), REQUEST_KINDS)

    assert decode_open(frame.payload) == (_SESSION, "mozart")


@pytest.mark.parametrize(
    "kind",
    [
        FrameKind.IDLE,
        FrameKind.SENTENCE_END,
        FrameKind.CLEAR,
        FrameKind.CLOSE,
        FrameKind.OPENED,
        FrameKind.CLEARED,
    ],
)
def test_a_session_frame_carries_only_its_session(kind: FrameKind) -> None:
    frame = _round_trip(encode_session(kind, _SESSION), frozenset(FrameKind))

    assert (frame.kind, decode_session(frame.payload)) == (kind, _SESSION)


def test_pcm_carries_the_audio_and_its_rate() -> None:
    frame = _round_trip(
        encode_pcm(_SESSION, 16_000, b"\x01\x02\x03\x04"), REQUEST_KINDS
    )

    assert decode_pcm(frame.payload) == (_SESSION, 16_000, b"\x01\x02\x03\x04")


@pytest.mark.parametrize("count", [0, 1, MAX_PICTURES_PER_ANSWER])
def test_frames_carry_whole_pictures_in_order(count: int) -> None:
    pictures = [Frame(bytes([index]) * _PICTURE) for index in range(count)]

    frame = _round_trip(encode_frames(_SESSION, pictures, _VIDEO), ANSWER_KINDS)

    assert decode_frames(frame.payload, _VIDEO) == (_SESSION, pictures)


@pytest.mark.parametrize("code", list(FailureCode))
def test_failed_carries_a_closed_code_and_a_session(code: FailureCode) -> None:
    frame = _round_trip(encode_failed(code, _SESSION), ANSWER_KINDS)

    assert decode_failed(frame.payload) == (code, _SESSION)


@pytest.mark.parametrize(
    "payload_length",
    [
        pytest.param(MAX_CONTROL_BYTES + 1, id="a control frame"),
        pytest.param(-1, id="a frame of an unknown kind"),
    ],
)
def test_a_frame_longer_than_its_kind_allows_is_refused_from_its_header(
    payload_length: int,
) -> None:
    kind = FrameKind.OPENED if payload_length > 0 else 99
    header_only = io.BytesIO(_HEADER.pack(kind, abs(payload_length)))
    every_kind = frozenset(FrameKind)

    with pytest.raises(ProtocolError):
        read_frame(header_only, _LIMITS, every_kind)


@pytest.mark.parametrize(
    ("kind", "limits", "length"),
    [
        (FrameKind.FRAMES, _LIMITS, 4 + MAX_PICTURES_PER_ANSWER * _PICTURE + 1),
        (FrameKind.FRAMES, FrameLimits(), 5),
        (FrameKind.PREPARE, _LIMITS, 2 + MAX_LOOK_BYTES + 8 + _LIMITS.image_bytes + 1),
        (FrameKind.PCM, _LIMITS, 8 + MAX_PCM_BYTES + 1),
    ],
    ids=["too many pictures", "a picture before ready", "an image", "audio"],
)
def test_the_caps_follow_the_picture_the_image_and_the_audio(
    kind: FrameKind, limits: FrameLimits, length: int
) -> None:
    header_only = io.BytesIO(_HEADER.pack(kind, length))
    every_kind = frozenset(FrameKind)

    with pytest.raises(ProtocolError):
        read_frame(header_only, limits, every_kind)


def test_a_kind_the_reader_does_not_accept_is_refused() -> None:
    stream = io.BytesIO()
    write_frame(stream, encode_session(FrameKind.IDLE, _SESSION))
    stream.seek(0)

    with pytest.raises(ProtocolError):
        read_frame(stream, _LIMITS, ANSWER_KINDS)


def test_a_frame_cut_short_is_refused_and_an_empty_stream_ends() -> None:
    whole = io.BytesIO()
    write_frame(whole, encode_session(FrameKind.IDLE, _SESSION))

    missing_a_byte = io.BytesIO(whole.getvalue()[:-1])
    missing_the_length = io.BytesIO(whole.getvalue()[:2])
    empty = io.BytesIO()

    with pytest.raises(ProtocolError):
        read_frame(missing_a_byte, _LIMITS, REQUEST_KINDS)
    with pytest.raises(ProtocolError):
        read_frame(missing_the_length, _LIMITS, REQUEST_KINDS)
    with pytest.raises(EOFError):
        read_frame(empty, _LIMITS, REQUEST_KINDS)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        struct.pack("!III", 0, 2, 25),
        struct.pack("!III", 4, 2, 0),
        struct.pack("!III", 1 << 31, 1 << 31, 25),
        struct.pack("!III", 4, 2, 25) + b"speak,sing",
    ],
    ids=[
        "empty",
        "no width",
        "no frame rate",
        "a picture too large",
        "an unknown verb",
    ],
)
def test_a_ready_that_reports_nothing_usable_is_refused(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_ready(payload)


def test_the_largest_picture_is_a_whole_number_of_pixels_within_its_cap() -> None:
    video = ProviderVideoFormat(width=4096, height=4096, frame_rate=25)

    assert picture_bytes(video) <= MAX_PICTURE_BYTES


@pytest.mark.parametrize(
    "decode",
    [
        lambda: decode_prepare(
            b"\x00\x01a" + struct.pack("!II", 2, 2) + bytes(11), _LIMITS
        ),
        lambda: decode_prepare(
            b"\x00\x00" + struct.pack("!II", 1, 1) + bytes(3), _LIMITS
        ),
        lambda: decode_prepare(
            b"\x00\x01a" + struct.pack("!II", 20, 20) + bytes(1200), _LIMITS
        ),
        lambda: decode_prepare(b"\x00\x01a" + b"xx", _LIMITS),
        lambda: decode_session(b"ab"),
        lambda: decode_open(struct.pack("!I", 1) + b"\xff\xfe"),
        lambda: decode_open(struct.pack("!I", 1)),
        lambda: decode_pcm(struct.pack("!II", 1, 0) + b"ab"),
        lambda: decode_frames(struct.pack("!I", 1) + bytes(_PICTURE + 1), _VIDEO),
        lambda: decode_frames(
            struct.pack("!I", 1) + bytes(_PICTURE * (MAX_PICTURES_PER_ANSWER + 1)),
            _VIDEO,
        ),
        lambda: decode_failed(b"x"),
        lambda: decode_failed(struct.pack("!BI", 99, 0)),
        lambda: decode_session(struct.pack("!I", 1) + b"x"),
    ],
    ids=[
        "an image of the wrong length",
        "a look without a name",
        "an image above the cap",
        "an image without a size",
        "a session frame shorter than a session",
        "a look that is not text",
        "an open without a look",
        "audio without a rate",
        "a partial picture",
        "too many pictures",
        "a failure without a session",
        "an unknown failure code",
        "a session frame with more than a session",
    ],
)
def test_a_payload_the_protocol_does_not_allow_is_refused(
    decode: Callable[[], object],
) -> None:
    with pytest.raises(ProtocolError):
        decode()


@pytest.mark.parametrize(
    "encode",
    [
        lambda: encode_pcm(_SESSION, 16_000, bytes(MAX_PCM_BYTES + 1)),
        lambda: encode_open(_SESSION, ""),
        lambda: encode_frames(_SESSION, [Frame(b"short")], _VIDEO),
        lambda: encode_session(FrameKind.PCM, _SESSION),
    ],
    ids=[
        "audio too long",
        "a look without a name",
        "a short picture",
        "a kind with more",
    ],
)
def test_a_frame_the_protocol_does_not_allow_is_not_written(
    encode: Callable[[], object],
) -> None:
    with pytest.raises(ProtocolError):
        encode()
