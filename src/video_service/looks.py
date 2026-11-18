"""The looks the service offers: the look file and each look's image, fetched once.

An image is fetched from an allowed source, kept only when its sha256 matches, and
decoded under a pixel cap. Nothing here runs during a session: the service fetches
at start and never again.
"""

from __future__ import annotations

import hashlib
import io
import re
import tomllib
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from http.client import HTTPException
from typing import TYPE_CHECKING, Annotated, Final
from urllib.parse import urlsplit

from PIL import Image
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)
from video_provider_contract import DecodedImage

from video_client.wire import LookName

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_FETCH_SECONDS: Final = 30
_IMAGE_FORMATS: Final = ("PNG", "JPEG", "WEBP")
_WEBP_HEADER_BYTES: Final = 30
_VP8_SIGNATURE: Final = b"\x9d\x01\x2a"
_VP8L_SIGNATURE: Final = 0x2F
_SEGMENT: Final = r"[A-Za-z0-9._~-]+"
_PREFIX_PATH: Final = re.compile(rf"/({_SEGMENT}/)*")
_FILE_PATH: Final = re.compile(rf"({_SEGMENT}/)*{_SEGMENT}")
_SOURCE_ERROR = (
    "a look source must be an http or https URL with a lowercase host, no user, "
    "no query or fragment, and a path that starts and ends with / and holds only "
    "ASCII letters, digits and -._~/, with no empty, . or .. segment"
)
_LOOK_FILE_ERROR = "VIDEO_LOOKS_FILE must name a readable TOML file of looks"


def _has_dot_segment(path: str) -> bool:
    return any(segment in {".", ".."} for segment in path.split("/"))


def _source_prefix(prefix: str) -> str:
    address = urlsplit(prefix)
    if (
        address.scheme not in {"http", "https"}
        or not address.hostname
        or address.netloc != address.netloc.lower()
        or "@" in address.netloc
        or "?" in prefix
        or "#" in prefix
        or _PREFIX_PATH.fullmatch(address.path) is None
        or _has_dot_segment(address.path)
    ):
        raise ValueError(_SOURCE_ERROR)
    return prefix


SourcePrefix = Annotated[str, AfterValidator(_source_prefix)]


class LookSource(BaseModel):
    """Where a look's image is and the sha256 it must have."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    url: str
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


_LOOK_FILE = TypeAdapter(
    dict[LookName, LookSource], config=ConfigDict(hide_input_in_errors=True)
)


def read_look_file(path: Path) -> dict[str, LookSource]:
    """Each look the file names with its image source; a bad file echoes no value."""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
        raise ValueError(_LOOK_FILE_ERROR) from None
    try:
        return _LOOK_FILE.validate_python(document)
    except ValidationError as error:
        faults = "; ".join(
            f"{'.'.join(map(str, fault['loc']))}: {fault['msg']}"
            for fault in error.errors(include_input=False, include_url=False)
        )
        message = f"VIDEO_LOOKS_FILE breaks the look file form ({faults})"
        raise ValueError(message) from None


class LookFailure(StrEnum):
    """Why a look is not ready; logged, never answered."""

    SOURCE_NOT_ALLOWED = "source_not_allowed"
    FETCH_FAILED = "fetch_failed"
    TOO_LARGE = "too_large"
    DIGEST_MISMATCH = "digest_mismatch"
    NOT_AN_IMAGE = "not_an_image"
    TOO_MANY_PIXELS = "too_many_pixels"
    PROVIDER_FAILED = "provider_failed"


class LookRefusedError(Exception):
    """A look's image failed one of the start checks."""

    def __init__(self, failure: LookFailure) -> None:
        """Carry the closed reason."""
        super().__init__(failure.value)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class LookLimits:
    """The sources an image may come from and the bounds it must stay within."""

    sources: Sequence[str]
    max_bytes: int
    max_pixels: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect leaves the allowed source, so it is an error answer."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        return None


_OPENER: Final = urllib.request.build_opener(_NoRedirect)


def _under_a_source(url: str, sources: Sequence[str]) -> bool:
    return any(
        url.startswith(source)
        and _FILE_PATH.fullmatch(url[len(source) :]) is not None
        and not _has_dot_segment(url[len(source) :])
        for source in sources
    )


def _fetch(url: str, max_bytes: int) -> bytes:
    try:
        # The URL lies under an allowed source, and those are http or https.
        with _OPENER.open(url, timeout=_FETCH_SECONDS) as response:
            body = response.read(max_bytes + 1)
    except OSError, HTTPException:
        raise LookRefusedError(LookFailure.FETCH_FAILED) from None
    if len(body) > max_bytes:
        raise LookRefusedError(LookFailure.TOO_LARGE)
    return body


def _webp_canvas_pixels(data: bytes) -> int | None:
    """The canvas of a WebP read from its header, `None` when it is not a WebP."""
    header = data[:_WEBP_HEADER_BYTES].ljust(_WEBP_HEADER_BYTES, b"\0")
    if header[:4] != b"RIFF" or header[8:12] != b"WEBP":
        return None
    chunk = header[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(header[24:27], "little") + 1
        height = int.from_bytes(header[27:30], "little") + 1
    elif chunk == b"VP8L" and header[20] == _VP8L_SIGNATURE:
        bits = int.from_bytes(header[21:25], "little")
        width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    elif chunk == b"VP8 " and header[23:26] == _VP8_SIGNATURE:
        width = int.from_bytes(header[26:28], "little") & 0x3FFF
        height = int.from_bytes(header[28:30], "little") & 0x3FFF
    else:
        raise LookRefusedError(LookFailure.NOT_AN_IMAGE)
    return width * height


def _decode(data: bytes, max_pixels: int) -> DecodedImage:
    # Pillow's own cap would refuse above the operator's setting as not an image; the
    # setting is the only pixel cap, and it is checked before any pixel is decoded.
    Image.MAX_IMAGE_PIXELS = None
    # Pillow allocates a WebP's whole canvas when it opens the file, so the cap is
    # checked on the header first; PNG and JPEG open without allocating pixels.
    pixels = _webp_canvas_pixels(data)
    if pixels is not None and pixels > max_pixels:
        raise LookRefusedError(LookFailure.TOO_MANY_PIXELS)
    try:
        with Image.open(io.BytesIO(data), formats=_IMAGE_FORMATS) as image:
            if image.width * image.height > max_pixels:
                raise LookRefusedError(LookFailure.TOO_MANY_PIXELS)
            rgb = image.convert("RGB")
    except OSError, SyntaxError, ValueError, Image.DecompressionBombError:
        raise LookRefusedError(LookFailure.NOT_AN_IMAGE) from None
    return DecodedImage(width=rgb.width, height=rgb.height, rgb=rgb.tobytes())


def fetch_image(source: LookSource, limits: LookLimits) -> DecodedImage:
    """The decoded image of `source`, or `LookRefusedError` for the check it failed."""
    if not _under_a_source(source.url, limits.sources):
        raise LookRefusedError(LookFailure.SOURCE_NOT_ALLOWED)
    data = _fetch(source.url, limits.max_bytes)
    if hashlib.sha256(data).hexdigest() != source.sha256:
        raise LookRefusedError(LookFailure.DIGEST_MISMATCH)
    return _decode(data, limits.max_pixels)
