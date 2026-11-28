"""The worker loop, driven in a process of its own over real pipes."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest
from video_provider_contract import DecodedImage, ProviderFailedError, ProviderVerb
from video_provider_contract.protocol import (
    ANSWER_KINDS,
    NO_SESSION,
    Failure,
    FailureCode,
    FrameKind,
    FrameLimits,
    ProtocolError,
    ProtocolFrame,
    decode_failed,
    decode_frames,
    decode_ready,
    decode_session,
    encode_open,
    encode_pcm,
    encode_prepare,
    encode_session,
    read_frame,
    write_frame,
)
from video_provider_contract.worker import CARD_MEMORY_MB_ENV, MAX_IMAGE_PIXELS_ENV

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_PICTURE_SIDE = 2
_PICTURE_BYTES = _PICTURE_SIDE * _PICTURE_SIDE * 3
_SESSION = 3
_EXIT_WITHIN_SECONDS = 20
_WORKER = textwrap.dedent(
    """
    import os, sys, threading
    from video_provider_contract import (
        Frame, ProviderFailedError, ProviderInfo, ProviderVerb, ProviderVideoFormat,
    )
    from video_provider_contract.worker import run_worker

    MODE = os.environ["WORKER_MODE"]
    SIDE = 2

    class Session:
        def idle_frame(self):
            if MODE == "computes":
                threading.Event().wait()
            if MODE == "writes-stderr":
                sys.stderr.write("after")
                sys.stderr.flush()
            return Frame(bytes(SIDE * SIDE * 3))

        def feed(self, pcm, rate):
            if MODE == "breaks":
                raise RuntimeError("secret words")
            if MODE == "draws-wrong-size":
                return iter([Frame(b"short")])
            return iter([Frame(bytes(SIDE * SIDE * 3))])

        def finish_sentence(self):
            return iter([])

        def clear(self):
            pass

        def close(self):
            pass

    class Provider:
        def start(self):
            if MODE == "prints":
                print("noise on stdout")
            if MODE == "writes-stderr":
                sys.stderr.write("before")
                sys.stderr.flush()
            if MODE == "fails-to-start":
                raise ProviderFailedError
            if MODE == "fails-to-start-on-a-cause":
                try:
                    raise OSError("a secret path")
                except OSError as error:
                    raise ProviderFailedError from error
            return ProviderInfo(
                (ProviderVerb.SPEAK,), ProviderVideoFormat(SIDE, SIDE, 25)
            )

        def prepare(self, look, image):
            if MODE == "refuses":
                raise ProviderFailedError

        def open_session(self, look):
            return Session()

        def close(self):
            pass

    run_worker(Provider())
    """
)


class _Worker:
    """A worker process, and the frames to and from it."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.limits = FrameLimits(picture_bytes=_PICTURE_BYTES)

    def send(self, frame: ProtocolFrame) -> None:
        assert self.process.stdin is not None
        write_frame(self.process.stdin, frame)

    def answer(self) -> ProtocolFrame:
        assert self.process.stdout is not None
        return read_frame(self.process.stdout, self.limits, ANSWER_KINDS)

    def finish(self) -> int:
        """Close its input and wait for its exit."""
        assert self.process.stdin is not None
        self.process.stdin.close()
        return self.process.wait(timeout=_EXIT_WITHIN_SECONDS)


@pytest.fixture
def start_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., _Worker]]:
    """Start a worker in a mode; at the end every worker is gone."""
    script = tmp_path / "worker.py"
    script.write_text(_WORKER, encoding="utf-8")
    started: list[subprocess.Popen[bytes]] = []

    def start(mode: str = "serves", max_pixels: str = "100") -> _Worker:
        monkeypatch.setenv("WORKER_MODE", mode)
        monkeypatch.setenv(MAX_IMAGE_PIXELS_ENV, max_pixels)
        process = subprocess.Popen(  # noqa: S603 - this interpreter and the test's script
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        started.append(process)
        return _Worker(process)

    yield start
    for process in started:
        process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _ready(worker: _Worker) -> None:
    assert worker.answer().kind is FrameKind.READY


def test_a_worker_serves_a_provider_over_the_protocol(
    start_worker: Callable[..., _Worker],
) -> None:
    worker = start_worker()

    ready = worker.answer()
    worker.send(encode_prepare("mozart", DecodedImage(2, 2, bytes(12))))
    prepared = worker.answer()
    worker.send(encode_open(_SESSION, "mozart"))
    opened = worker.answer()
    worker.send(encode_session(FrameKind.IDLE, _SESSION))
    idle = worker.answer()
    worker.send(encode_pcm(_SESSION, 16_000, bytes(4)))
    fed = worker.answer()
    worker.send(encode_session(FrameKind.SENTENCE_END, _SESSION))
    finished = worker.answer()
    worker.send(encode_session(FrameKind.CLEAR, _SESSION))
    cleared = worker.answer()
    worker.send(encode_session(FrameKind.CLOSE, _SESSION))
    status = worker.finish()

    info = decode_ready(ready.payload)
    assert info.plays == (ProviderVerb.SPEAK,)
    assert info.video.width == _PICTURE_SIDE
    assert prepared.kind is FrameKind.PREPARED
    assert decode_session(opened.payload) == _SESSION
    assert [
        len(decode_frames(f.payload, info.video)[1]) for f in (idle, fed, finished)
    ] == [1, 1, 0]
    assert decode_session(cleared.payload) == _SESSION
    assert status == 0


def test_a_session_closed_can_be_opened_again(
    start_worker: Callable[..., _Worker],
) -> None:
    worker = start_worker()
    _ready(worker)
    worker.send(encode_open(_SESSION, "mozart"))
    worker.answer()

    worker.send(encode_session(FrameKind.CLOSE, _SESSION))
    worker.send(encode_open(_SESSION, "mozart"))
    reopened = worker.answer()

    assert reopened.kind is FrameKind.OPENED
    assert worker.finish() == 0


def test_a_print_in_the_provider_does_not_corrupt_the_protocol(
    start_worker: Callable[..., _Worker],
) -> None:
    worker = start_worker("prints")

    _ready(worker)

    assert worker.finish() == 0


def test_the_end_of_input_ends_the_worker_while_the_model_computes(
    start_worker: Callable[..., _Worker],
) -> None:
    worker = start_worker("computes")
    _ready(worker)
    worker.send(encode_open(_SESSION, "mozart"))
    worker.answer()
    worker.send(encode_session(FrameKind.IDLE, _SESSION))

    assert worker.finish() == 0


def test_stderr_is_read_before_ready_and_dropped_after(
    start_worker: Callable[..., _Worker],
) -> None:
    worker = start_worker("writes-stderr")
    _ready(worker)
    worker.send(encode_open(_SESSION, "mozart"))
    worker.answer()
    worker.send(encode_session(FrameKind.IDLE, _SESSION))
    worker.answer()

    worker.finish()

    assert worker.process.stderr is not None
    assert worker.process.stderr.read() == b"before"


def test_a_request_the_provider_refuses_is_answered_failed_and_the_worker_serves_on(
    start_worker: Callable[..., _Worker],
) -> None:
    worker = start_worker("refuses")
    _ready(worker)
    image = DecodedImage(2, 2, bytes(12))

    worker.send(encode_prepare("mozart", image))
    refused = worker.answer()
    worker.send(encode_open(_SESSION, "mozart"))
    opened = worker.answer()

    assert decode_failed(refused.payload) == Failure(FailureCode.REQUEST, NO_SESSION)
    assert opened.kind is FrameKind.OPENED
    assert worker.finish() == 0


@pytest.mark.parametrize(
    ("mode", "ask", "answered", "error_type"),
    [
        ("fails-to-start", None, None, ProviderFailedError.__name__),
        ("fails-to-start-on-a-cause", None, None, OSError.__name__),
        (
            "breaks",
            encode_pcm(_SESSION, 16_000, bytes(4)),
            _SESSION,
            RuntimeError.__name__,
        ),
        (
            "draws-wrong-size",
            encode_pcm(_SESSION, 16_000, bytes(4)),
            _SESSION,
            ProtocolError.__name__,
        ),
        ("serves", encode_session(FrameKind.IDLE, 99), None, ProtocolError.__name__),
        ("serves", encode_open(NO_SESSION, "mozart"), None, ProtocolError.__name__),
        (
            "serves",
            encode_prepare("mozart", DecodedImage(11, 10, bytes(330))),
            None,
            ProtocolError.__name__,
        ),
        (
            "serves",
            ProtocolFrame(FrameKind.PREPARE, b"\x00"),
            None,
            ProtocolError.__name__,
        ),
        (
            "serves",
            ProtocolFrame(FrameKind.PREPARE, bytes(1_000)),
            None,
            ProtocolError.__name__,
        ),
    ],
    ids=[
        "a provider that fails to start",
        "a provider that fails to start on a cause",
        "a provider that breaks",
        "a provider that draws the wrong size",
        "a request for no session",
        "an open on no session",
        "an image above the pixel cap",
        "a payload the protocol does not allow",
        "a frame longer than its kind allows",
    ],
)
def test_a_worker_that_cannot_go_on_answers_failed_naming_the_root_and_ends(
    mode: str,
    ask: ProtocolFrame | None,
    answered: int | None,
    error_type: str,
    start_worker: Callable[..., _Worker],
) -> None:
    worker = start_worker(mode)
    if ask is not None:
        _ready(worker)
        if answered is not None:
            worker.send(encode_open(answered, "mozart"))
            worker.answer()
        worker.send(ask)

    failure = worker.answer()
    worker.finish()

    assert decode_failed(failure.payload) == Failure(
        FailureCode.PROVIDER, NO_SESSION, error_type
    )
    assert worker.process.stdout is not None
    assert worker.process.stdout.read() == b""


def test_a_worker_without_the_pixel_cap_ends_naming_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "worker.py"
    script.write_text(_WORKER, encoding="utf-8")
    monkeypatch.delenv(MAX_IMAGE_PIXELS_ENV, raising=False)
    monkeypatch.setenv("WORKER_MODE", "serves")

    done = subprocess.run(  # noqa: S603 - this interpreter and the test's script
        [sys.executable, str(script)],
        capture_output=True,
        check=False,
        timeout=_EXIT_WITHIN_SECONDS,
    )

    assert done.returncode != 0
    assert MAX_IMAGE_PIXELS_ENV.encode() in done.stderr


_READ_CARD_MEMORY = (
    "from video_provider_contract.worker import "
    "CARD_MEMORY_MB_ENV, read_required_positive_integer;"
    "print(read_required_positive_integer(CARD_MEMORY_MB_ENV))"
)


def _read_card_memory_setting() -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - this interpreter and a fixed program
        [sys.executable, "-c", _READ_CARD_MEMORY],
        capture_output=True,
        check=False,
        timeout=_EXIT_WITHIN_SECONDS,
    )


@pytest.mark.parametrize(
    "value",
    [None, "", "twelve", "1.5", "0", "-3"],
    ids=["unset", "empty", "not-a-number", "fractional", "zero", "negative"],
)
def test_a_required_setting_that_is_no_positive_integer_ends_naming_only_the_variable(
    value: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CARD_MEMORY_MB_ENV, raising=False)
    if value is not None:
        monkeypatch.setenv(CARD_MEMORY_MB_ENV, value)

    done = _read_card_memory_setting()

    assert done.returncode != 0
    assert done.stdout == b""
    assert CARD_MEMORY_MB_ENV.encode() in done.stderr
    assert not value or value.encode() not in done.stderr


def test_a_required_setting_that_is_a_positive_integer_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CARD_MEMORY_MB_ENV, "1234")

    done = _read_card_memory_setting()

    assert done.stdout.strip() == b"1234"
