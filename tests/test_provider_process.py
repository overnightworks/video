"""The process adapter against a real child: the fake provider, and scripted ones."""

from __future__ import annotations

import logging
import os
import select
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from video_provider_contract import DecodedImage, ProviderFailedError
from video_provider_contract.protocol import MAX_PCM_BYTES

from support.scripted_session import played
from support.session_support import pcm, sentence_events
from support.worker_support import (
    CALL_SECONDS,
    SHORT_BOUNDS,
    MakeProvider,
    install_worker,
    is_gone,
)
from video_client.session_wire import ErrorEvent, OutputAudioBufferStopped
from video_client.wire import RefusalCode
from video_service.provider_process import (
    ProcessBounds,
    ProcessFailure,
    ProcessProvider,
    ProviderLaunch,
)
from video_service.runtime import REAL_TIME

if TYPE_CHECKING:
    from pathlib import Path

    from _pytest.mark.structures import ParameterSet

_RATE = 16_000
_WAIT_SECONDS = 20.0
_IMAGE = DecodedImage(2, 2, bytes(12))
_LOUD = b"\xff\x7f" * (_RATE // 25)
_QUIET = bytes(2 * (_RATE // 25))
_MOZART = "mozart"
# One binary message longer than a frame of the protocol holds.
_LONG_MESSAGE_MS = 2_500

# A service that starts the worker it is given, says so and waits to be killed.
_SERVICE = """
import sys, threading
from pathlib import Path
from video_service.provider_process import (
    ProcessBounds, ProcessProvider, ProviderLaunch,
)

provider = ProcessProvider(
    ProviderLaunch(Path(sys.argv[1]), 1), ProcessBounds(), 5.0
)
provider.start()
print("started", flush=True)
threading.Event().wait()
"""


def _wait_for_worker(tmp_path: Path) -> None:
    """Block until the scripted worker calls `tell()`."""
    with (tmp_path / "worker.fifo").open("rb") as fifo:
        fifo.read(1)


def test_the_fake_child_serves_a_session_from_start_to_close(
    make_provider: MakeProvider,
) -> None:
    provider = make_provider(bounds=ProcessBounds())

    info = provider.start()
    provider.prepare(_MOZART, _IMAGE)
    session = provider.open_session(_MOZART)
    at_rest = session.idle_frame()
    played = [*session.feed(_LOUD, _RATE), *session.finish_sentence()]
    session.clear()
    session.close()

    assert info.video.frame_rate > 0
    assert len(at_rest.rgb) == info.video.width * info.video.height * 3
    assert played
    assert all(frame.rgb != at_rest.rgb for frame in played)
    assert not provider.failed


def test_two_sessions_share_one_child_without_mixing_their_frames(
    make_provider: MakeProvider,
) -> None:
    provider = make_provider(bounds=ProcessBounds())
    provider.start()
    provider.prepare(_MOZART, _IMAGE)
    loud = provider.open_session(_MOZART)
    quiet = provider.open_session(_MOZART)
    at_rest = loud.idle_frame()

    with ThreadPoolExecutor(max_workers=2) as pool:
        loud_frames = pool.submit(lambda: list(loud.feed(_LOUD, _RATE)))
        quiet_frames = pool.submit(lambda: list(quiet.feed(_QUIET, _RATE)))

    assert [frame.rgb != at_rest.rgb for frame in loud_frames.result()] == [True]
    assert [frame.rgb == at_rest.rgb for frame in quiet_frames.result()] == [True]


def test_a_failed_request_fails_that_call_and_the_provider_serves_on(
    make_provider: MakeProvider,
) -> None:
    provider = make_provider(
        """
        ready()
        request()
        send(encode_failed(FailureCode.REQUEST))
        serve()
        """
    )
    provider.start()

    with pytest.raises(ProviderFailedError):
        provider.prepare(_MOZART, _IMAGE)
    provider.prepare(_MOZART, _IMAGE)

    assert not provider.failed


def test_a_clear_fences_out_the_frames_of_a_request_sent_before_it(
    make_provider: MakeProvider, tmp_path: Path
) -> None:
    provider = make_provider(
        """
        ready()
        serve(2)
        stale = request()
        tell()
        cleared = request()
        send(ProtocolFrame(FrameKind.FRAMES, stale.payload[:4] + PICTURE))
        answer(cleared)
        serve()
        """
    )
    provider.start()
    provider.prepare(_MOZART, _IMAGE)
    session = provider.open_session(_MOZART)

    with ThreadPoolExecutor(max_workers=1) as pool:
        in_flight = pool.submit(lambda: list(session.feed(_LOUD, _RATE)))
        _wait_for_worker(tmp_path)
        session.clear()

    assert in_flight.result() == []
    assert session.idle_frame()
    assert not provider.failed


def _exercise(provider: ProcessProvider) -> None:
    provider.start()
    provider.prepare(_MOZART, _IMAGE)
    session = provider.open_session(_MOZART)
    session.feed(_LOUD, _RATE)
    session.idle_frame()


def _failure(
    body: str, reason: ProcessFailure, bounds: ProcessBounds | None = None
) -> ParameterSet:
    if bounds is None:
        bounds = SHORT_BOUNDS if reason is ProcessFailure.TIMEOUT else ProcessBounds()
    return pytest.param(body, reason, bounds, id=body)


@pytest.mark.parametrize(
    ("body", "reason", "bounds"),
    [
        _failure("raise SystemExit(3)", ProcessFailure.EXITED),
        _failure("hang()", ProcessFailure.TIMEOUT),
        _failure("send(encode_failed(FailureCode.PROVIDER))", ProcessFailure.REPORTED),
        _failure(
            "sys.stderr.write('x' * (2 << 20)); sys.stderr.flush(); hang()",
            ProcessFailure.STDERR_OVERSIZED,
            ProcessBounds(start_seconds=_WAIT_SECONDS),
        ),
        _failure("ready(); serve(1); request(); os._exit(1)", ProcessFailure.EXITED),
        _failure("ready(); request(); hang()", ProcessFailure.TIMEOUT),
        _failure("ready(); serve(1); request(); hang()", ProcessFailure.TIMEOUT),
        _failure("ready(); serve(2); request(); hang()", ProcessFailure.TIMEOUT),
        _failure(
            "ready(); serve(2); asked = request(); "
            "send(encode_failed(FailureCode.PROVIDER, session_of(asked.payload)))",
            ProcessFailure.REPORTED,
        ),
        _failure(
            "ready(); raw(b'\\x63\\x00\\x00\\x00\\x00'); hang()",
            ProcessFailure.PROTOCOL,
        ),
        _failure(
            "ready(); raw(struct.pack('!BI', FrameKind.FRAMES, 1 << 30)); hang()",
            ProcessFailure.PROTOCOL,
        ),
        _failure(
            "ready(); raw(struct.pack('!BI', FrameKind.FRAMES, 16) + b'abc'); "
            "os._exit(0)",
            ProcessFailure.PROTOCOL,
        ),
        _failure(
            "ready(); request(); send(encode_session(FrameKind.OPENED, 1)); hang()",
            ProcessFailure.PROTOCOL,
        ),
        _failure(
            "ready(); serve(3); send(encode_session(FrameKind.CLEARED, 1)); hang()",
            ProcessFailure.PROTOCOL,
        ),
        _failure(
            "ready(); serve(2); asked = request(); "
            "send(encode_session(FrameKind.CLEARED, session_of(asked.payload)))",
            ProcessFailure.PROTOCOL,
        ),
        _failure("os.close(0); ready(); hang()", ProcessFailure.EXITED),
        _failure(
            "ready(); request(); send(ProtocolFrame(FrameKind.PREPARED, b'x')); hang()",
            ProcessFailure.PROTOCOL,
        ),
        _failure(
            "ready(); serve(2); asked = request(); "
            "send(ProtocolFrame(FrameKind.FRAMES, asked.payload[:4] + b'xx'))",
            ProcessFailure.PROTOCOL,
        ),
        _failure(
            "ready(); serve(3); asked = request(); "
            "send(ProtocolFrame(FrameKind.FRAMES, asked.payload[:4] + PICTURE * 2))",
            ProcessFailure.PROTOCOL,
        ),
    ],
)
def test_a_child_that_fails_ends_in_the_failed_path_without_a_hang(
    body: str,
    reason: ProcessFailure,
    bounds: ProcessBounds,
    make_provider: MakeProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = make_provider(body, bounds)

    with caplog.at_level(logging.WARNING), pytest.raises(ProviderFailedError):
        _exercise(provider)

    assert provider.failed
    logged = [r.reason for r in caplog.records if r.msg == "provider.failed"]
    assert logged == [reason.value]
    with pytest.raises(ProviderFailedError):
        provider.open_session(_MOZART)


@pytest.mark.parametrize("executable", ["missing", "not-executable"])
def test_a_worker_that_cannot_run_fails_the_start(
    executable: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / executable
    if executable == "not-executable":
        path.write_text("#!/bin/sh\n", encoding="utf-8")
    provider = ProcessProvider(
        ProviderLaunch(executable=path, max_image_pixels=1),
        ProcessBounds(),
        CALL_SECONDS,
    )

    with caplog.at_level(logging.WARNING), pytest.raises(ProviderFailedError):
        provider.start()

    assert provider.failed
    assert [r.reason for r in caplog.records] == [ProcessFailure.UNAVAILABLE.value]


def test_a_stop_during_the_load_ends_the_child_without_waiting_for_it(
    make_provider: MakeProvider, tmp_path: Path
) -> None:
    provider = make_provider("tell(); hang()", ProcessBounds())
    outcome: list[BaseException | None] = []

    def start() -> None:
        try:
            provider.start()
        except ProviderFailedError as error:
            outcome.append(error)

    loading = threading.Thread(target=start)
    loading.start()
    _wait_for_worker(tmp_path)

    provider.close()
    loading.join(timeout=_WAIT_SECONDS)

    assert not loading.is_alive()
    assert len(outcome) == 1


@pytest.mark.usefixtures("stub_environment")
def test_a_killed_service_leaves_no_child(tmp_path: Path) -> None:
    worker = install_worker(
        tmp_path / "root",
        """
        from video_provider_contract.worker import run_worker
        from video_provider_fake import FakeProvider

        hold()
        run_worker(FakeProvider())
        """,
    )
    service = subprocess.Popen(  # noqa: S603 - this interpreter, a fixed program
        [sys.executable, "-c", _SERVICE, str(worker)],
        stdout=subprocess.PIPE,
    )
    with service:
        try:
            assert service.stdout is not None
            service.stdout.readline()
            held = os.open(tmp_path / "worker.fifo", os.O_RDONLY | os.O_NONBLOCK)
            service.kill()
            service.wait()
            try:
                exited, _, _ = select.select([held], [], [], _WAIT_SECONDS)
            finally:
                os.close(held)
        finally:
            service.kill()

    assert exited


def test_a_session_ends_unavailable_when_its_child_dies(
    make_provider: MakeProvider,
) -> None:
    provider = make_provider("ready(); serve(2); request(); os._exit(1)")
    provider.start()
    provider.prepare(_MOZART, _IMAGE)

    events = played([], 0, provider=provider, pacing=REAL_TIME, max_lag_ms=5_000)

    failure = events[-1]
    assert isinstance(failure, ErrorEvent)
    assert failure.error.code is RefusalCode.UNAVAILABLE


@pytest.mark.parametrize(
    ("payload", "rate"),
    [(_LOUD, 0), (bytes(MAX_PCM_BYTES + 2), _RATE)],
    ids=["no-rate", "more-than-one-request-holds"],
)
def test_a_request_the_protocol_cannot_hold_fails_that_call_not_the_provider(
    make_provider: MakeProvider, payload: bytes, rate: int
) -> None:
    provider = make_provider(bounds=ProcessBounds())
    provider.start()
    session = provider.open_session(_MOZART)

    with pytest.raises(ProviderFailedError):
        session.feed(payload, rate)

    assert session.idle_frame()
    assert not provider.failed


def test_a_sentence_in_one_binary_message_of_any_length_plays_to_its_end(
    make_provider: MakeProvider,
) -> None:
    provider = make_provider(bounds=ProcessBounds())
    provider.start()
    provider.prepare(_MOZART, _IMAGE)
    start, end = sentence_events("s1")

    events = played(
        [start, pcm(_LONG_MESSAGE_MS), end],
        1,
        provider=provider,
        pacing=REAL_TIME,
        max_lag_ms=5_000,
    )

    stopped = events[-1]
    assert isinstance(stopped, OutputAudioBufferStopped)
    assert stopped.audio_end_ms == _LONG_MESSAGE_MS


def test_a_closed_session_serves_no_frame(make_provider: MakeProvider) -> None:
    provider = make_provider(bounds=ProcessBounds())
    provider.start()
    session = provider.open_session(_MOZART)

    session.close()

    with pytest.raises(ProviderFailedError):
        session.idle_frame()


def test_a_closed_provider_that_never_started_does_not_start(
    make_provider: MakeProvider,
) -> None:
    provider = make_provider(bounds=ProcessBounds())

    provider.close()

    with pytest.raises(ProviderFailedError):
        provider.start()


def test_a_closed_provider_that_started_does_not_start_again(
    make_provider: MakeProvider,
) -> None:
    provider = make_provider(bounds=ProcessBounds())
    provider.start()

    provider.close()

    with pytest.raises(ProviderFailedError):
        provider.start()


def test_a_second_close_kills_no_process_group(
    make_provider: MakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = make_provider(bounds=ProcessBounds())
    provider.start()
    provider.close()
    killed: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pid, _signal: killed.append(pid))

    provider.close()

    assert killed == []


def test_a_worker_that_exits_takes_its_descendants_with_it(
    make_provider: MakeProvider, tmp_path: Path
) -> None:
    pidfile = tmp_path / "descendant.pid"
    provider = make_provider(
        "import subprocess; "
        "helper = subprocess.Popen(['sleep', '60'], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"open({str(pidfile)!r}, 'w').write(str(helper.pid)); os._exit(1)"
    )

    with pytest.raises(ProviderFailedError):
        provider.start()

    descendant = int(pidfile.read_text(encoding="utf-8"))
    deadline = time.monotonic() + _WAIT_SECONDS
    while not is_gone(descendant) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert is_gone(descendant)
