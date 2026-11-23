"""The session's lines follow logline's standard, each in its client event's trace."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING

from logline import Outcome
from logline.testing import configured
from starlette.websockets import WebSocketDisconnect
from video_provider_fake import FakeProvider, FakeSession

from support.chunk_support import waiting
from support.clock_support import FakeClock
from support.identity_support import valid_headers
from support.scripted_session import played
from support.session_support import (
    ICE_SERVERS,
    close_code,
    connect,
    denial,
    failure_of,
    opened,
    pcm,
    receive,
    sentence_events,
    session_url,
    update_event,
    written,
)
from video_client.session_wire import OutputAudioBufferStarted, SessionCreated
from video_client.wire import DRAIN_CLOSE_CODE

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from fastapi.testclient import TestClient
    from video_provider_contract import Frame, ProviderSession

    from video_service.runtime import Runtime

_SENTENCE_MS = 80
_IDLE_WAIT_SECONDS = 0.2
_IDLE_RENDER_SECONDS = 0.03
_FEED_RENDER_SECONDS = 0.2
_MAX_LAG_MS = 1000
_SESSION_TRACE = "0af7651916cd43dd8448eb211c80319c"
_SENTENCE_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def _traceparent(trace_id: str) -> str:
    return f"00-{trace_id}-b7ad6b7169203331-01"


def _written(capsys: pytest.CaptureFixture[str], event: str) -> list[dict]:
    """The JSON lines of `event` written so far."""
    return written(capsys.readouterr().out, event)


def _play_one_sentence_then_drain(client: TestClient, runtime: Runtime) -> None:
    with connect(client) as session:
        receive(session)
        session.send_json(update_event(traceparent=_traceparent(_SESSION_TRACE)))
        receive(session)
        start, end = sentence_events("s1")
        session.send_json(start | {"traceparent": _traceparent(_SENTENCE_TRACE)})
        session.send_bytes(pcm(_SENTENCE_MS))
        session.send_json(end)
        receive(session)
        receive(session)
        # The session ends by the service's own close, which the line follows.
        client.portal.call(runtime.begin_drain)
        close_code(session)


def test_the_session_line_names_look_and_provider_in_the_update_s_trace(
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = started()
    client = wire(runtime)

    with configured("video"):
        _play_one_sentence_then_drain(client, runtime)

    (line,) = _written(capsys, "session.ended")
    assert line["trace_id"] == _SESSION_TRACE
    assert (line["look"], line["provider"], line["sentence_count"]) == (
        "mozart",
        "fake",
        1,
    )
    assert line["outcome"] == Outcome.OK
    assert line["close_code"] == DRAIN_CLOSE_CODE


def test_the_sentence_line_counts_its_audio_and_frames_in_the_sentence_s_trace(
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = started()
    client = wire(runtime)

    with configured("video"):
        _play_one_sentence_then_drain(client, runtime)

    (line,) = _written(capsys, "sentence.ended")
    assert line["trace_id"] == _SENTENCE_TRACE
    assert line["outcome"] == Outcome.OK
    assert line["audio_ms"] == _SENTENCE_MS
    frame_rate = client.get("/health").json()["provider"]["video"]["frame_rate"]
    assert line["frame_count"] == _SENTENCE_MS * frame_rate // 1000
    assert line["first_output_ms"] <= line["duration_ms"]


def test_a_cleared_sentence_ends_its_line_cancelled(
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = wire(started())
    with configured("video"), opened(client) as session:
        start, end = sentence_events("s1")
        session.send_json(start)
        session.send_bytes(pcm(10_000))
        session.send_json(end)
        assert isinstance(receive(session), OutputAudioBufferStarted)
        session.send_json({"type": "output_audio_buffer.clear"})
        receive(session)

    (line,) = _written(capsys, "sentence.ended")
    assert line["outcome"] == Outcome.CANCELLED


def test_a_refused_upgrade_writes_a_refused_session_line_with_its_code(
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = wire(started())

    with configured("video"):
        denial(client, session_url("no-such-look"), valid_headers())

    (line,) = _written(capsys, "session.ended")
    assert (line["outcome"], line["reason"]) == (Outcome.REFUSED, "invalid_request")


def test_a_client_leaving_before_its_update_still_writes_one_session_line(
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = wire(started())

    with configured("video"), connect(client) as session:
        receive(session)
        session.close()

    (line,) = _written(capsys, "session.ended")
    assert line["trace_id"] is not None


def test_a_session_ended_by_the_client_s_fault_is_refused_and_by_the_provider_failed(
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = wire(started())
    failing = wire(started(FakeProvider(fails_to_draw=True)))
    with configured("video"):
        with opened(client) as session:
            session.send_json({"type": "no.such.event"})
            failure_of(receive(session))
        with opened(failing) as session:
            start, end = sentence_events("s1")
            session.send_json(start)
            session.send_bytes(pcm(_SENTENCE_MS))
            session.send_json(end)
            failure_of(receive(session))

    ended = [
        (line["outcome"], line["reason"]) for line in _written(capsys, "session.ended")
    ]
    assert ended == [
        (Outcome.REFUSED, "invalid_request"),
        (Outcome.FAILED, "unavailable"),
    ]


def test_a_session_not_ended_unavailable_names_no_path_and_none_behind_before_its_clock(
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = started()
    draining, refusing = wire(runtime), wire(started())

    with configured("video"):
        _play_one_sentence_then_drain(draining, runtime)
        denial(refusing, session_url("no-such-look"), valid_headers())
        with opened(refusing) as session:
            session.send_json({"type": "no.such.event"})
            failure_of(receive(session))

    ended, refused_upgrade, refused_event = _written(capsys, "session.ended")
    assert "ended_by" not in ended
    assert "ended_by" not in refused_upgrade
    assert "ended_by" not in refused_event
    assert refused_upgrade["max_behind_ms"] == 0
    assert refused_event["max_behind_ms"] == 0


def test_the_longest_idle_call_is_logged_and_a_longer_feed_call_is_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = FakeClock()
    start, end = sentence_events("s1")

    with configured("video"):
        played(
            [waiting(clock, _IDLE_WAIT_SECONDS), start, pcm(_SENTENCE_MS), end],
            sentences=1,
            provider=FakeProvider(
                render_seconds=_FEED_RENDER_SECONDS,
                idle_render_seconds=_IDLE_RENDER_SECONDS,
                sleep=clock.render,
            ),
            pacing=clock.pacing,
            worker=clock.worker(),
            max_lag_ms=_MAX_LAG_MS,
        )

    (line,) = _written(capsys, "session.ended")
    assert line["max_idle_call_ms"] == round(_IDLE_RENDER_SECONDS * 1000)


def test_no_credential_reaches_a_line_even_when_the_update_is_refused(
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = wire(started())
    bad = update_event(
        session={"ice_servers": ICE_SERVERS, "ice_transport_policy": "x"}
    )
    with configured("video"), connect(client) as session:
        assert isinstance(receive(session), SessionCreated)
        session.send_json(bad)
        failure_of(receive(session))

    assert "SENTINEL-CRED" not in capsys.readouterr().out


class _DefectiveSession(FakeSession):
    def idle_frame(self) -> Frame:
        raise RuntimeError


class _ProviderDefectOnIdleFrame(FakeProvider):
    def open_session(self, look: str) -> ProviderSession:
        del look
        return _DefectiveSession(
            25, 0.0, fails_to_draw=False, finish_gate=None, sleep=time.sleep
        )


def test_an_error_escaping_the_session_still_writes_one_failed_session_line(
    started: Callable[..., Runtime],
    wire: Callable[[Runtime], TestClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = wire(started(_ProviderDefectOnIdleFrame()))

    with (
        configured("video"),
        suppress(RuntimeError, WebSocketDisconnect),
        connect(client) as session,
    ):
        receive(session)
        session.send_json(update_event(traceparent=_traceparent(_SESSION_TRACE)))
        receive(session)

    (line,) = _written(capsys, "session.ended")
    assert (line["trace_id"], line["outcome"]) == (_SESSION_TRACE, Outcome.FAILED)
