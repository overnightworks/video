"""The version and the refusals (WIRE-1); what the service does not offer (WIRE-13)."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest

from video_client.wire import API_VERSION, Refusal, RefusalCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

    from support.runtime_support import BuildRuntime
    from video_service.runtime import Runtime


@pytest.fixture
def client(
    build_runtime: BuildRuntime, wire: Callable[[Runtime], TestClient]
) -> TestClient:
    return wire(build_runtime({}))


def _refusal_of(client: TestClient, method: str, path: str) -> tuple[int, Refusal]:
    answer = client.request(method, path)
    assert answer.headers["content-type"] == "application/json"
    return answer.status_code, Refusal.model_validate_json(answer.content)


@pytest.mark.parametrize(
    "path",
    ["/v0", "/v2", "/v2/", "/v2/health", "/v2/sessions", "/v10/sessions", "/v01/x"],
)
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_a_version_other_than_the_supported_is_refused_whatever_the_method(
    client: TestClient, method: str, path: str
) -> None:
    status, refusal = _refusal_of(client, method, path)

    assert status == HTTPStatus.BAD_REQUEST
    assert refusal.error.code is RefusalCode.UNSUPPORTED_VERSION
    assert refusal.error.type == "invalid_request_error"


@pytest.mark.parametrize(
    "path",
    [
        f"/v{API_VERSION}",
        f"/v{API_VERSION}/",
        f"/v{API_VERSION}/health",
        f"/v{API_VERSION}/sessions",
        f"/v{API_VERSION}x/sessions",
        "/",
        "/sessions",
        "/health/",
        "/health/x",
        "/vx/health",
    ],
)
def test_any_other_path_is_not_found(client: TestClient, path: str) -> None:
    status, refusal = _refusal_of(client, "GET", path)

    assert status == HTTPStatus.NOT_FOUND
    assert refusal.error.code is RefusalCode.NOT_FOUND


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_method_that_health_does_not_offer_is_not_found(
    client: TestClient, method: str
) -> None:
    status, refusal = _refusal_of(client, method, "/health")

    assert status == HTTPStatus.NOT_FOUND
    assert refusal.error.code is RefusalCode.NOT_FOUND


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/docs"),
        ("GET", "/redoc"),
        ("GET", "/openapi.json"),
        ("GET", f"/v{API_VERSION}/docs"),
        ("GET", f"/v{API_VERSION}/openapi.json"),
        ("GET", f"/v{API_VERSION}/looks"),
        ("POST", f"/v{API_VERSION}/looks"),
        ("PUT", f"/v{API_VERSION}/looks/mozart"),
        ("DELETE", f"/v{API_VERSION}/looks/mozart"),
        ("POST", f"/v{API_VERSION}/looks/mozart/image"),
        ("GET", f"/v{API_VERSION}/recordings"),
        ("POST", f"/v{API_VERSION}/recordings"),
    ],
)
def test_no_schema_docs_look_administration_or_recording_is_offered(
    client: TestClient, method: str, path: str
) -> None:
    status, refusal = _refusal_of(client, method, path)

    assert status == HTTPStatus.NOT_FOUND
    assert refusal.error.code is RefusalCode.NOT_FOUND


def test_a_refusal_never_echoes_the_request(client: TestClient) -> None:
    answer = client.get("/v1/an-unlikely-path-name?secret=hunter2")

    assert "unlikely" not in answer.text
    assert "hunter2" not in answer.text
