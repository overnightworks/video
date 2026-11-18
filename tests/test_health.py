"""Health and the looks behind it (WIRE-12): what is ready, and why a look is not."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from video_provider_fake import FakeProvider

from conftest import CARD_MEMORY_MB, MAX_BYTES, MAX_PIXELS, MAX_SESSIONS
from support.images import noisy_picture, picture
from video_client.wire import Health
from video_service.looks import LookFailure, LookSource

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

    from conftest import BuildRuntime
    from support.look_server import LookServer
    from video_service.runtime import Runtime


def _health(client: TestClient) -> Health:
    answer = client.get("/health")
    assert answer.status_code == HTTPStatus.OK
    return Health.model_validate_json(answer.content)


def _ready(health: Health) -> dict[str, bool]:
    return {name: look.ready for name, look in health.looks.items()}


def _mozart(source: LookServer, name: str = "mozart.png") -> LookSource:
    return source.host(name, picture().data)


def test_nothing_is_ready_before_the_start_ends(
    source: LookServer,
    build_runtime: BuildRuntime,
    wire: Callable[[Runtime], TestClient],
) -> None:
    runtime = build_runtime({"mozart": _mozart(source)})

    health = _health(wire(runtime))

    assert not health.ready
    assert not health.provider.ready
    assert health.provider.plays == []
    assert health.provider.video is None
    assert _ready(health) == {"mozart": False}


def test_a_started_service_offers_its_provider_and_its_ready_looks(
    source: LookServer,
    build_runtime: BuildRuntime,
    wire: Callable[[Runtime], TestClient],
) -> None:
    runtime = build_runtime({"mozart": _mozart(source), "bach": _mozart(source, "b")})

    runtime.load()
    health = _health(wire(runtime))

    assert health.ready
    assert health.provider.name == "fake"
    assert health.provider.ready
    assert health.provider.plays == ["speak", "clear"]
    assert health.provider.video is not None
    assert _ready(health) == {"mozart": True, "bach": True}
    assert health.sessions.open == 0
    assert health.sessions.limit == MAX_SESSIONS
    assert health.card_memory_mb == CARD_MEMORY_MB


def test_a_service_with_no_looks_is_ready_and_offers_none(
    build_runtime: BuildRuntime, wire: Callable[[Runtime], TestClient]
) -> None:
    runtime = build_runtime({})

    runtime.load()
    health = _health(wire(runtime))

    assert health.ready
    assert health.looks == {}


def test_a_look_that_reaches_the_provider_arrives_decoded(
    source: LookServer, build_runtime: BuildRuntime
) -> None:
    provider = FakeProvider()
    runtime = build_runtime({"mozart": _mozart(source)}, provider=provider)

    runtime.load()

    image = provider.prepared["mozart"]
    assert (image.width, image.height) == (8, 6)
    assert len(image.rgb) == 8 * 6 * 3


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_each_image_format_a_look_may_have_is_ready(
    fmt: str,
    source: LookServer,
    build_runtime: BuildRuntime,
    wire: Callable[[Runtime], TestClient],
) -> None:
    runtime = build_runtime({"mozart": source.host("face", picture(fmt=fmt).data)})

    runtime.load()

    assert _health(wire(runtime)).looks["mozart"].ready


def test_a_look_is_fetched_once_at_start_and_never_during_a_session(
    source: LookServer,
    build_runtime: BuildRuntime,
    wire: Callable[[Runtime], TestClient],
) -> None:
    runtime = build_runtime({"mozart": _mozart(source)})
    client = wire(runtime)

    runtime.load()
    for _ in range(3):
        _health(client)

    assert source.requested == ["mozart.png"]


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
        {"refused": make(source), "mozart": _mozart(source)},
        sources=(source.base_url, _NOBODY),
    )

    with caplog.at_level(logging.INFO):
        runtime.load()
    health = runtime.health()

    assert health.ready
    assert _ready(health) == {"refused": False, "mozart": True}
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
    runtime = build_runtime({"mozart": _mozart(source)}, sources=allowed)

    runtime.load()

    assert source.requested == []
    assert not runtime.health().looks["mozart"].ready


def test_a_look_the_provider_refuses_is_not_ready_and_the_others_serve(
    source: LookServer, build_runtime: BuildRuntime
) -> None:
    runtime = build_runtime(
        {"mozart": _mozart(source), "bach": _mozart(source, "b.png")},
        provider=FakeProvider(refuses={"bach"}),
    )

    runtime.load()

    assert _ready(runtime.health()) == {"mozart": True, "bach": False}


def test_a_provider_that_fails_to_start_leaves_every_look_unready_and_fetches_none(
    source: LookServer, build_runtime: BuildRuntime
) -> None:
    runtime = build_runtime(
        {"mozart": _mozart(source)}, provider=FakeProvider(fails_to_start=True)
    )

    runtime.load()
    health = runtime.health()

    assert not health.ready
    assert not health.provider.ready
    assert _ready(health) == {"mozart": False}
    assert source.requested == []
