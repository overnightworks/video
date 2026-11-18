"""Who may open a session (WIRE-2) and the order the upgrade is refused in (WIRE-3)."""

from __future__ import annotations

import time
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from video_provider_fake import FakeProvider

from support.identity_support import (
    SERVICE_GROUP,
    person_token,
    valid_headers,
    workload_token,
)
from support.session_support import connect, denial, opened, receive, session_url
from video_client.session_wire import SessionCreated
from video_client.wire import (
    AUTHORIZATION_HEADER,
    BUSY_RETRY_AFTER_SECONDS,
    SESSIONS_PATH,
    WORKLOAD_TOKEN_HEADER,
    RefusalCode,
    caller_headers,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

    from support.identity_support import KeySet
    from video_service.runtime import Runtime

_UNAUTHORIZED = (HTTPStatus.UNAUTHORIZED, RefusalCode.UNAUTHORIZED)
_FORBIDDEN = (HTTPStatus.FORBIDDEN, RefusalCode.FORBIDDEN)
_UNAVAILABLE = (HTTPStatus.SERVICE_UNAVAILABLE, RefusalCode.UNAVAILABLE)
_PAST_SECONDS = 600


def _without(name: str) -> dict[str, str]:
    return {k: v for k, v in valid_headers().items() if k != name}


def _headers(workload: str | None = None, person: str | None = None) -> dict[str, str]:
    return caller_headers(
        workload_token=workload or workload_token(),
        person_token=person or person_token(),
    )


def _expired() -> float:
    return time.time() - _PAST_SECONDS


@pytest.fixture
def client(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> TestClient:
    return wire(started())


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (_without(WORKLOAD_TOKEN_HEADER), _UNAUTHORIZED),
        (_without(AUTHORIZATION_HEADER), _UNAUTHORIZED),
        (
            {**_without(AUTHORIZATION_HEADER), AUTHORIZATION_HEADER: "Basic abc"},
            _UNAUTHORIZED,
        ),
        (_headers(workload=workload_token(aud="speech-service")), _UNAUTHORIZED),
        (_headers(person=person_token(aud="speech-service")), _UNAUTHORIZED),
        (_headers(workload=workload_token(_expired)), _UNAUTHORIZED),
        (_headers(person=person_token(_expired)), _UNAUTHORIZED),
        (_headers(workload=workload_token(realm_access={"roles": []})), _FORBIDDEN),
        (_headers(person=person_token(groups=("/apps/quiz",))), _FORBIDDEN),
        (
            _headers(
                person=person_token(groups=("/apps/quiz", f"{SERVICE_GROUP}/sub"))
            ),
            _FORBIDDEN,
        ),
        (
            _headers(
                workload=workload_token(azp="other-client", client_id="other-client"),
                person=person_token(azp="other-client"),
            ),
            _FORBIDDEN,
        ),
        (_headers(person=person_token(sub="quiz-service-account")), _FORBIDDEN),
    ],
    ids=[
        "no-workload-token",
        "no-person-token",
        "not-a-bearer",
        "workload-for-another-service",
        "person-for-another-service",
        "expired-workload",
        "expired-person",
        "not-a-workload",
        "not-in-the-service-group",
        "only-in-a-subgroup",
        "unregistered-app",
        "the-person-is-the-workload",
    ],
)
def test_an_upgrade_without_two_valid_tokens_of_a_member_is_refused(
    client: TestClient, headers: dict[str, str], expected: tuple[int, RefusalCode]
) -> None:
    denied = denial(client, session_url(), headers)

    assert (denied.status, denied.code) == expected


def test_an_upgrade_is_refused_unavailable_while_the_issuer_keys_are_down(
    client: TestClient, key_set: KeySet
) -> None:
    key_set.down = True

    denied = denial(client, session_url(), valid_headers())

    assert (denied.status, denied.code) == _UNAVAILABLE


def test_an_unauthorized_refusal_challenges_for_a_bearer_token(
    client: TestClient,
) -> None:
    denied = denial(client, session_url(), {})

    assert denied.headers["www-authenticate"] == "Bearer"


def test_the_tokens_are_checked_before_the_look(client: TestClient) -> None:
    denied = denial(client, session_url("no-such-look"), {})

    assert (denied.status, denied.code) == _UNAUTHORIZED


@pytest.mark.parametrize("look", ["no-such-look", "bad%20name", "x" * 65, ""])
def test_a_look_the_look_file_does_not_hold_is_refused_naming_the_look(
    client: TestClient, look: str
) -> None:
    denied = denial(client, session_url(look), valid_headers())

    assert denied.status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert denied.code is RefusalCode.INVALID_REQUEST
    assert denied.param == "look"


def test_an_upgrade_without_a_look_is_refused_naming_the_look(
    client: TestClient,
) -> None:
    denied = denial(client, SESSIONS_PATH, valid_headers())

    assert (denied.status, denied.param) == (HTTPStatus.UNPROCESSABLE_ENTITY, "look")


def test_a_look_that_is_not_ready_is_refused_unavailable_before_the_limit_applies(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    runtime = started(FakeProvider(refuses=["mozart"]), max_sessions=1)

    denied = denial(wire(runtime), session_url(), valid_headers())

    assert (denied.status, denied.code) == _UNAVAILABLE


def test_every_look_is_refused_unavailable_while_the_provider_is_not_ready(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    runtime = started(FakeProvider(fails_to_start=True))

    denied = denial(wire(runtime), session_url(), valid_headers())

    assert (denied.status, denied.code) == _UNAVAILABLE


def test_a_provider_that_fails_to_open_the_session_refuses_it_unavailable(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    client = wire(started(FakeProvider(fails_to_open=True)))

    denied = denial(client, session_url(), valid_headers())

    assert (denied.status, denied.code) == _UNAVAILABLE


def test_a_session_past_the_limit_is_refused_busy_with_a_retry_hint(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    client = wire(started(max_sessions=1))

    with opened(client):
        denied = denial(client, session_url(), valid_headers())

    assert (denied.status, denied.code) == (
        HTTPStatus.TOO_MANY_REQUESTS,
        RefusalCode.BUSY,
    )
    assert denied.headers["retry-after"] == str(BUSY_RETRY_AFTER_SECONDS)


def test_a_place_frees_when_its_session_ends(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    client = wire(started(max_sessions=1))
    with opened(client):
        pass

    with connect(client) as again:
        created = receive(again)

    assert isinstance(created, SessionCreated)


def test_the_limit_is_checked_after_the_look_and_its_readiness(
    started: Callable[..., Runtime], wire: Callable[[Runtime], TestClient]
) -> None:
    client = wire(started(max_sessions=1))

    with opened(client):
        unknown = denial(client, session_url("no-such-look"), valid_headers())

    assert unknown.code is RefusalCode.INVALID_REQUEST
