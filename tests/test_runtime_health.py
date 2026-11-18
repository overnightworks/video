"""Health and what is ready behind it (WIRE-12): the start, provider and looks."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from video_provider_fake import FakeProvider

from support.look_server import hosted_picture
from support.runtime_support import CARD_MEMORY_MB, MAX_SESSIONS, ready_looks
from video_client.wire import Health

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

    from support.look_server import LookServer
    from support.runtime_support import BuildRuntime
    from video_service.runtime import Runtime


def _health(client: TestClient) -> Health:
    answer = client.get("/health")
    assert answer.status_code == HTTPStatus.OK
    return Health.model_validate_json(answer.content)


def test_nothing_is_ready_before_the_start_ends(
    source: LookServer,
    build_runtime: BuildRuntime,
    wire: Callable[[Runtime], TestClient],
) -> None:
    runtime = build_runtime({"mozart": hosted_picture(source)})

    health = _health(wire(runtime))

    assert not health.ready
    assert not health.provider.ready
    assert health.provider.plays == []
    assert health.provider.video is None
    assert ready_looks(health) == {"mozart": False}


def test_a_started_service_offers_its_provider_and_its_ready_looks(
    source: LookServer,
    build_runtime: BuildRuntime,
    wire: Callable[[Runtime], TestClient],
) -> None:
    runtime = build_runtime(
        {"mozart": hosted_picture(source), "bach": hosted_picture(source, "b")}
    )

    runtime.load()
    health = _health(wire(runtime))

    assert health.ready
    assert health.provider.name == "fake"
    assert health.provider.ready
    assert health.provider.plays == ["speak", "clear"]
    assert health.provider.video is not None
    assert ready_looks(health) == {"mozart": True, "bach": True}
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
    runtime = build_runtime({"mozart": hosted_picture(source)}, provider=provider)

    runtime.load()

    image = provider.prepared["mozart"]
    assert (image.width, image.height) == (8, 6)
    assert len(image.rgb) == 8 * 6 * 3


def test_a_look_is_fetched_once_at_start_and_never_during_a_session(
    source: LookServer,
    build_runtime: BuildRuntime,
    wire: Callable[[Runtime], TestClient],
) -> None:
    runtime = build_runtime({"mozart": hosted_picture(source)})
    client = wire(runtime)

    runtime.load()
    for _ in range(3):
        _health(client)

    assert source.requested == ["mozart.png"]


def test_a_look_the_provider_refuses_is_not_ready_and_the_others_serve(
    source: LookServer, build_runtime: BuildRuntime
) -> None:
    runtime = build_runtime(
        {"mozart": hosted_picture(source), "bach": hosted_picture(source, "b.png")},
        provider=FakeProvider(refuses={"bach"}),
    )

    runtime.load()

    assert ready_looks(runtime.health()) == {"mozart": True, "bach": False}


def test_a_provider_that_fails_to_start_leaves_every_look_unready_and_fetches_none(
    source: LookServer, build_runtime: BuildRuntime
) -> None:
    runtime = build_runtime(
        {"mozart": hosted_picture(source)}, provider=FakeProvider(fails_to_start=True)
    )

    runtime.load()
    health = runtime.health()

    assert not health.ready
    assert not health.provider.ready
    assert ready_looks(health) == {"mozart": False}
    assert source.requested == []
