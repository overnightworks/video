"""The start checks of a look's image: its source, size, digest, format and pixels."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from support.images import noisy_picture, picture
from support.look_server import hosted_picture
from support.runtime_support import MAX_BYTES, MAX_PIXELS, ready_looks
from video_service.looks import LookFailure, LookSource

if TYPE_CHECKING:
    from collections.abc import Callable

    from support.look_server import LookServer
    from support.runtime_support import BuildRuntime


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_each_image_format_a_look_may_have_is_ready(
    fmt: str,
    source: LookServer,
    build_runtime: BuildRuntime,
) -> None:
    runtime = build_runtime({"mozart": source.host("face", picture(fmt=fmt).data)})

    runtime.load()

    assert ready_looks(runtime.health()) == {"mozart": True}


def _truncated() -> bytes:
    data = picture().data
    return data[: len(data) // 2]


def _missing(source: LookServer) -> LookSource:
    return LookSource(url=source.url("missing.png"), sha256=picture().sha256)


def _moved(source: LookServer) -> LookSource:
    target = source.host("target.png", picture().data)
    source.redirects["moved.png"] = target.url
    return LookSource(url=source.url("moved.png"), sha256=target.sha256)


def _unreachable(_source: LookServer) -> LookSource:
    return LookSource(url=f"{_NOBODY}a.png", sha256=picture().sha256)


def _over_the_byte_cap(source: LookServer) -> LookSource:
    """A whole, valid picture within the pixel cap that is too many bytes."""
    data = noisy_picture(40, 40).data
    assert len(data) > MAX_BYTES
    return source.host("huge", data)


_NOBODY = "http://127.0.0.1:1/looks/"
_REFUSED_BY_A_CHECK: dict[
    str, tuple[Callable[[LookServer], LookSource], LookFailure]
] = {
    "missing": (_missing, LookFailure.FETCH_FAILED),
    "redirected": (_moved, LookFailure.FETCH_FAILED),
    "unreachable": (_unreachable, LookFailure.FETCH_FAILED),
    "over-the-byte-cap": (_over_the_byte_cap, LookFailure.TOO_LARGE),
    "another-digest": (
        lambda source: source.host("other", picture().data, sha256="0" * 64),
        LookFailure.DIGEST_MISMATCH,
    ),
    "not-an-image": (
        lambda source: source.host("text", b"hello"),
        LookFailure.NOT_AN_IMAGE,
    ),
    "truncated": (
        lambda source: source.host("cut", _truncated()),
        LookFailure.NOT_AN_IMAGE,
    ),
    "a-format-no-look-may-have": (
        lambda source: source.host("anim", picture(fmt="GIF").data),
        LookFailure.NOT_AN_IMAGE,
    ),
    "over-the-pixel-cap": (
        lambda source: source.host("big", picture(MAX_PIXELS + 1, 1).data),
        LookFailure.TOO_MANY_PIXELS,
    ),
}


@pytest.mark.parametrize(
    ("make", "failure"), _REFUSED_BY_A_CHECK.values(), ids=_REFUSED_BY_A_CHECK
)
def test_a_look_that_fails_a_check_is_not_ready_logs_why_and_the_others_serve(
    make: Callable[[LookServer], LookSource],
    failure: LookFailure,
    source: LookServer,
    build_runtime: BuildRuntime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = build_runtime(
        {"refused": make(source), "mozart": hosted_picture(source)},
        sources=(source.base_url, _NOBODY),
    )

    with caplog.at_level(logging.INFO):
        runtime.load()
    health = runtime.health()

    assert health.ready
    assert ready_looks(health) == {"refused": False, "mozart": True}
    (failed,) = [
        line for line in caplog.records if getattr(line, "outcome", None) == "failed"
    ]
    assert (failed.look, failed.reason) == ("refused", failure.value)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.png",
        "a/../../outside.png",
        "./a.png",
        "a//b.png",
        "a.png?x=1",
        "a.png#x",
        "%2e%2e/a.png",
    ],
)
def test_a_look_url_that_leaves_the_allowed_source_is_never_fetched(
    path: str, source: LookServer, build_runtime: BuildRuntime
) -> None:
    shown = picture()
    runtime = build_runtime(
        {"mozart": LookSource(url=source.url(path), sha256=shown.sha256)}
    )

    runtime.load()

    assert source.requested == []
    assert not runtime.health().looks["mozart"].ready


@pytest.mark.parametrize(
    "allowed", [(), ("http://127.0.0.1:1/looks/",)], ids=["no-source", "other-source"]
)
def test_a_look_from_a_source_that_is_not_allowed_is_never_fetched(
    allowed: tuple[str, ...], source: LookServer, build_runtime: BuildRuntime
) -> None:
    runtime = build_runtime({"mozart": hosted_picture(source)}, sources=allowed)

    runtime.load()

    assert source.requested == []
    assert not runtime.health().looks["mozart"].ready
