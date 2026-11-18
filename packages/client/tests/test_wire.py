"""The refusal form: OpenAI's error form with a fixed message per code (WIRE-1)."""

from __future__ import annotations

import pytest

from video_client.wire import (
    REFUSAL_STATUS,
    RefusalCode,
    refusal,
    viewer_path,
    viewers_path,
)


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (RefusalCode.NOT_FOUND, "invalid_request_error"),
        (RefusalCode.BUSY, "invalid_request_error"),
        (RefusalCode.UNAVAILABLE, "server_error"),
    ],
)
def test_a_refusal_type_follows_its_status_class(code: RefusalCode, kind: str) -> None:
    assert refusal(code).error.type == kind


def test_a_refusal_names_the_field_at_fault() -> None:
    body = refusal(RefusalCode.INVALID_REQUEST, "look").model_dump(mode="json")

    assert body["error"]["param"] == "look"
    assert body["error"]["code"] == "invalid_request"


def test_every_code_has_one_status_and_one_message() -> None:
    messages = {refusal(code).error.message for code in RefusalCode}

    assert set(REFUSAL_STATUS) == set(RefusalCode)
    assert len(messages) == len(RefusalCode)


def test_a_viewer_path_extends_the_viewers_path_of_its_session() -> None:
    assert viewer_path("s1", "v1") == f"{viewers_path('s1')}/v1"
