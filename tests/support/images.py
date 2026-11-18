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
