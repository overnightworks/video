"""The frame protocol: what is encoded decodes, and what is not allowed is refused."""

from __future__ import annotations

import dataclasses
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
    BYTES_PER_PIXEL,
    MAX_CONTROL_BYTES,
    MAX_ERROR_TYPE_CHARACTERS,
    MAX_PCM_BYTES,
    MAX_PICTURE_BYTES,
    MAX_PICTURES_PER_ANSWER,
    REQUEST_KINDS,
    UNNAMED,
    Failure,
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
    error_type_of,
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


def test_a_request_failure_carries_a_code_and_a_session_and_no_name() -> None:
    frame = _round_trip(encode_failed(FailureCode.REQUEST, _SESSION), ANSWER_KINDS)

    assert decode_failed(frame.payload) == Failure(FailureCode.REQUEST, _SESSION)


@pytest.mark.parametrize("name", ["InductorError", "_", "a1", "x" * 128, UNNAMED])
def test_a_provider_failure_carries_its_name_in_the_bytes_after_the_session(
    name: str,
) -> None:
    frame = _round_trip(
        encode_failed(FailureCode.PROVIDER, _SESSION, name), ANSWER_KINDS
    )

    assert frame.payload == struct.pack("!BI", FailureCode.PROVIDER, _SESSION) + (
        name.encode("ascii")
    )
    assert decode_failed(frame.payload) == Failure(FailureCode.PROVIDER, _SESSION, name)


def test_a_failure_is_immutable() -> None:
    failure = Failure(FailureCode.REQUEST, _SESSION)

    with pytest.raises(dataclasses.FrozenInstanceError):
        failure.session = 1  # type: ignore[misc]


def _chained(*names: str) -> BaseException:
    """An exception named `names[0]` whose `__cause__` chain names the rest."""
    error: BaseException | None = None
    for name in reversed(names):
        raised = type(name, (Exception,), {})()
        raised.__cause__ = error
        error = raised
    assert error is not None
    return error


def test_the_root_of_a_failure_is_named_by_its_class_alone() -> None:
    assert error_type_of(ValueError("a path /secret and words")) == ValueError.__name__


def test_a_failure_without_a_cause_is_its_own_root() -> None:
    assert error_type_of(_chained("Only")) == "Only"


def test_the_root_is_found_through_the_chain_of_causes() -> None:
    assert error_type_of(_chained("Wrapper", "Middle", "Root")) == "Root"


def _raise_handled_key_error_as_value_error(*, cut_cause: bool) -> None:
    try:
        {}["missing"]
    except KeyError:
        if cut_cause:
            raise ValueError from None
        raise ValueError  # noqa: B904 - the implicit context is the case


@pytest.mark.parametrize("cut_cause", [True, False], ids=["from none", "in a handler"])
def test_a_handled_exception_in_the_context_is_not_the_root(*, cut_cause: bool) -> None:
    with pytest.raises(ValueError) as raised:  # noqa: PT011 - the class is the point
        _raise_handled_key_error_as_value_error(cut_cause=cut_cause)

    assert raised.value.__context__ is not None
    assert error_type_of(raised.value) == ValueError.__name__


def test_a_class_defined_in_a_function_is_named_by_its_own_name() -> None:
    class Local(Exception):  # noqa: N818 - the name is the example
        pass

    assert error_type_of(Local()) == "Local"


def test_the_walk_stops_after_sixteen_causes() -> None:
    names = [f"Link{index}" for index in range(40)]

    assert error_type_of(_chained(*names)) == "Link16"


def test_a_cycle_of_causes_ends_the_walk() -> None:
    first = _chained("First", "Second")
    assert first.__cause__ is not None
    first.__cause__.__cause__ = first

    assert error_type_of(first) == "First"


@pytest.mark.parametrize(
    "name",
    ["Fehlér", "x" * (MAX_ERROR_TYPE_CHARACTERS + 1), "has space", "line\nbreak", ""],
    ids=["non-ascii", "too long", "a space", "a newline", "empty"],
)
def test_a_name_outside_the_form_is_reported_as_unnamed(name: str) -> None:
    assert error_type_of(_chained(name)) == UNNAMED


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
    ("kind", "limits"),
    [
        pytest.param(FrameKind.FRAMES, _LIMITS, id="too many pictures"),
        pytest.param(FrameKind.FRAMES, FrameLimits(), id="a picture before ready"),
        pytest.param(FrameKind.PREPARE, _LIMITS, id="an image"),
        pytest.param(FrameKind.PCM, _LIMITS, id="audio"),
    ],
)
def test_the_caps_follow_the_picture_the_image_and_the_audio(
    kind: FrameKind, limits: FrameLimits
) -> None:
    header_only = io.BytesIO(_HEADER.pack(kind, limits.payload_cap(kind) + 1))
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


def test_a_ready_reporting_a_picture_above_the_cap_is_refused() -> None:
    width = 4096
    too_tall = MAX_PICTURE_BYTES // (width * BYTES_PER_PIXEL) + 1

    assert decode_ready(struct.pack("!III", width, width, 25))
    with pytest.raises(ProtocolError):
        decode_ready(struct.pack("!III", width, too_tall, 25))


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
        lambda: decode_failed(struct.pack("!BI", FailureCode.REQUEST, 0) + b"Name"),
        lambda: decode_failed(struct.pack("!BI", FailureCode.PROVIDER, 0)),
        lambda: decode_failed(struct.pack("!BI", FailureCode.PROVIDER, 0) + b"a b"),
        lambda: decode_failed(struct.pack("!BI", FailureCode.PROVIDER, 0) + b"a\n"),
        lambda: decode_failed(
            struct.pack("!BI", FailureCode.PROVIDER, 0) + "é".encode()
        ),
        lambda: decode_failed(struct.pack("!BI", FailureCode.PROVIDER, 0) + b"x" * 129),
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
        "a request failure with a name",
        "a provider failure without a name",
        "a name with a space",
        "a name with a newline",
        "a name that is not ascii",
        "a name of 129 characters",
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
        lambda: encode_failed(FailureCode.REQUEST, _SESSION, "Name"),
        lambda: encode_failed(FailureCode.PROVIDER, _SESSION),
        lambda: encode_failed(FailureCode.PROVIDER, _SESSION, "not a name"),
    ],
    ids=[
        "audio too long",
        "a look without a name",
        "a short picture",
        "a kind with more",
        "a request failure with a name",
        "a provider failure without a name",
        "a provider failure with an invalid name",
    ],
)
def test_a_frame_the_protocol_does_not_allow_is_not_written(
    encode: Callable[[], object],
) -> None:
    with pytest.raises(ProtocolError):
        encode()
