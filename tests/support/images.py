"""Images the tests serve as a look's picture."""

from __future__ import annotations

import hashlib
import io
import random
from dataclasses import dataclass

from PIL import Image


@dataclass(frozen=True, slots=True)
class Picture:
    """An encoded image and the sha256 a look file names for it."""

    data: bytes
    sha256: str


def noisy_picture(width: int, height: int) -> Picture:
    """A PNG of random pixels, which no compression makes small."""
    noise = random.Random(0).randbytes(width * height * 3)  # noqa: S311 - test data
    buffer = io.BytesIO()
    Image.frombytes("RGB", (width, height), noise).save(buffer, "PNG")
    data = buffer.getvalue()
    return Picture(data=data, sha256=hashlib.sha256(data).hexdigest())


def picture(width: int = 8, height: int = 6, *, fmt: str = "PNG") -> Picture:
    """A solid picture of `width` times `height` pixels in `fmt`."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 90)).save(buffer, fmt)
    data = buffer.getvalue()
    return Picture(data=data, sha256=hashlib.sha256(data).hexdigest())


def webp_claiming_a_canvas(width: int, height: int, *, extended: bool) -> bytes:
    """A small WebP whose header claims a `width` by `height` canvas.

    `extended` writes the VP8X layout, which an ICC profile makes; otherwise the
    lossless layout, whose size sits in its bitstream header.
    """
    encoded = io.BytesIO()
    image = Image.new("RGB", (8, 6))
    if extended:
        image.save(encoded, "WEBP", icc_profile=b"profile")
    else:
        image.save(encoded, "WEBP", lossless=True)
    data = bytearray(encoded.getvalue())
    if extended:
        data[24:27] = (width - 1).to_bytes(3, "little")
        data[27:30] = (height - 1).to_bytes(3, "little")
    else:
        bits = int.from_bytes(data[21:25], "little") & ~0xFFFFFFF
        data[21:25] = (bits | (width - 1) | ((height - 1) << 14)).to_bytes(4, "little")
    return bytes(data)
