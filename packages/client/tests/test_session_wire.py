"""The session events: what the client sends parses, and anything else names a field."""

from __future__ import annotations

import json

import pytest

from video_client.session_wire import (
    ClientEventError,
    OutputAudioBufferCleared,
    ServerEventError,
    SessionUpdate,
    client_event_id,
    error_event,
    is_session_update,
    parse_client_event,
    parse_server_event,
)
from video_client.wire import RefusalCode, caller_headers


def _update(**session: object) -> str:
    base = {"ice_servers": [], "ice_transport_policy": "relay"}
    return json.dumps({"type": "session.update", "session": base | session})


def test_a_single_url_is_the_same_ice_server_as_a_list_of_one() -> None:
    single = parse_client_event(_update(ice_servers=[{"urls": "stun:a.test"}]))
    listed = parse_client_event(_update(ice_servers=[{"urls": ["stun:a.test"]}]))

    assert isinstance(single, SessionUpdate)
    assert single == listed


def test_a_credential_is_not_in_the_representation_of_the_event() -> None:
    event = parse_client_event(
        _update(ice_servers=[{"urls": "turn:a.test", "credential": "SENTINEL"}])
    )

    assert "SENTINEL" not in repr(event)


@pytest.mark.parametrize(
    ("text", "param", "event_id"),
    [
        ("not json", None, None),
        ('{"type": "session.update", "event_id": "e1"}', "session", "e1"),
        ('{"event_id": "e2"}', "type", "e2"),
        ('{"type": "output_audio_buffer.clear", "extra": 1}', "extra", None),
    ],
)
def test_a_message_that_is_no_event_names_the_field_and_the_client_event(
    text: str, param: str | None, event_id: str | None
) -> None:
    with pytest.raises(ClientEventError) as refused:
        parse_client_event(text)

    assert (refused.value.param, refused.value.event_id) == (param, event_id)


@pytest.mark.parametrize(
    ("text", "names_update", "event_id"),
    [
        ('{"type": "session.update", "event_id": "e1", "session": 1}', True, "e1"),
        ('{"type": "sentence.start", "event_id": 7}', False, None),
        ('["session.update"]', False, None),
        ("not json", False, None),
    ],
)
def test_the_type_and_the_id_of_a_message_are_read_without_validating_the_rest(
    text: str, *, names_update: bool, event_id: str | None
) -> None:
    assert is_session_update(text) is names_update
    assert client_event_id(text) == event_id


def test_an_error_event_carries_the_code_in_the_form_of_the_refusal_list() -> None:
    event = error_event(RefusalCode.TOO_LARGE, "audio", "e1")

    assert event.error.code is RefusalCode.TOO_LARGE
    assert event.error.type == "invalid_request_error"
    assert (event.error.param, event.error.event_id) == ("audio", "e1")


def test_a_server_event_reads_back_as_the_event_that_was_sent() -> None:
    sent = OutputAudioBufferCleared(response_id="s1", audio_end_ms=120)

    assert parse_server_event(sent.model_dump_json()) == sent


@pytest.mark.parametrize("text", ["not json", '{"type": "no.such.event"}'])
def test_a_message_that_is_no_server_event_is_refused_without_its_content(
    text: str,
) -> None:
    with pytest.raises(ServerEventError) as refused:
        parse_server_event(text)

    assert text not in str(refused.value)


def test_the_caller_headers_carry_the_workload_token_and_a_bearer_person_token() -> (
    None
):
    headers = caller_headers(workload_token="w", person_token="p")  # noqa: S106 - placeholders

    assert headers == {"Video-Workload-Token": "w", "Authorization": "Bearer p"}
