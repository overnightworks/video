"""The real worker executable, driven in a process of its own without a card."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest
from video_provider_contract.protocol import (
    ANSWER_KINDS,
    NO_SESSION,
    Failure,
    FailureCode,
    FrameKind,
    FrameLimits,
    decode_failed,
    read_frame,
)
from video_provider_contract.worker import CARD_MEMORY_MB_ENV, MAX_IMAGE_PIXELS_ENV
from video_provider_flashhead_lite import worker
from video_provider_flashhead_lite.renderer import NoCardError

if TYPE_CHECKING:
    from collections.abc import Callable

_WORKER_EXECUTABLE = Path(sys.executable).parent / "video-flashhead-lite-worker"
_EXIT_WITHIN_SECONDS = 120
_AN_IMAGE_CAP = "1000"
_A_CARD_CAP = "1024"


def _run_worker(
    environment: dict[str, str],
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - the project's own installed executable
        [str(_WORKER_EXECUTABLE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )


@pytest.fixture
def worker_process(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., subprocess.Popen[bytes]]:
    """Start the worker with the settings given; at the end every worker is gone."""
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    started: list[subprocess.Popen[bytes]] = []

    def start(**settings: str | None) -> subprocess.Popen[bytes]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {MAX_IMAGE_PIXELS_ENV, CARD_MEMORY_MB_ENV}
        }
        environment[MAX_IMAGE_PIXELS_ENV] = _AN_IMAGE_CAP
        environment[CARD_MEMORY_MB_ENV] = _A_CARD_CAP
        for name, value in settings.items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
        process = _run_worker(environment)
        started.append(process)
        return process

    yield start
    for process in started:
        process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_a_worker_on_a_host_without_a_card_answers_failed_for_the_provider_and_exits(
    worker_process: Callable[..., subprocess.Popen[bytes]],
) -> None:
    process = worker_process()

    assert process.stdout is not None
    answer = read_frame(process.stdout, FrameLimits(), ANSWER_KINDS)
    code = process.wait(timeout=_EXIT_WITHIN_SECONDS)

    assert answer.kind is FrameKind.FAILED
    assert decode_failed(answer.payload) == Failure(
        FailureCode.PROVIDER, NO_SESSION, NoCardError.__name__
    )
    assert code != 0
    assert process.stdout.read() == b""


@pytest.mark.parametrize(
    "value",
    [None, "", "twelve", "1.5", "0", "-3"],
    ids=["unset", "empty", "not-a-number", "fractional", "zero", "negative"],
)
def test_a_worker_with_a_bad_card_memory_cap_ends_before_any_frame_naming_the_variable(
    worker_process: Callable[..., subprocess.Popen[bytes]], value: str | None
) -> None:
    process = worker_process(**{CARD_MEMORY_MB_ENV: value})

    code = process.wait(timeout=_EXIT_WITHIN_SECONDS)

    assert process.stdout is not None
    assert process.stderr is not None
    stderr = process.stderr.read()
    assert code != 0
    assert process.stdout.read() == b""
    assert CARD_MEMORY_MB_ENV.encode() in stderr
    assert not value or value.encode() not in stderr


def test_a_worker_goes_offline_before_it_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv(CARD_MEMORY_MB_ENV, _A_CARD_CAP)
    seen: list[str | None] = []

    def serve(_provider: object) -> NoReturn:
        seen.append(os.environ.get("HF_HUB_OFFLINE"))
        raise SystemExit

    with pytest.raises(SystemExit):
        worker.main(serve)

    assert seen == ["1"]
