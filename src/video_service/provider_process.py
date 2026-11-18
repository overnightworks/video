"""The process adapter: a provider runs in a child process behind the frame protocol.

The adapter is the one `Provider` the service builds. It starts the provider's
worker executable, reads its answers on a thread of its own and matches each to
the request that waits for it: per session in request order, so one child serves
every open session. Every bound is a watchdog that ends the child, since an answer
that comes late may still be on the pipe. A child that dies, hangs past a bound or
breaks the protocol makes the provider failed until the service restarts: every
call after it raises `ProviderFailedError`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import threading
from collections import deque
from concurrent.futures import Future, InvalidStateError
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import IO, TYPE_CHECKING, Final, cast

from video_provider_contract import ProviderFailedError
from video_provider_contract.protocol import (
    ANSWER_KINDS,
    NO_SESSION,
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
    picture_bytes,
    read_frame,
    session_of,
    write_frame,
)
from video_provider_contract.worker import MAX_IMAGE_PIXELS_ENV

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from video_provider_contract import (
        DecodedImage,
        Frame,
        ProviderInfo,
        ProviderSession,
    )

logger = logging.getLogger(__name__)

_STDERR_CHUNK_BYTES: Final = 64 * 1024
# What a worker may write to stderr before it is ready.
_STDERR_LIMIT_BYTES: Final = 1024 * 1024
_FIRST_SESSION: Final = 1


class ProcessFailure(StrEnum):
    """Why a provider's process failed: the closed `reason` of its log line."""

    UNAVAILABLE = "unavailable"
    """The worker executable is missing, not executable or would not start."""
    EXITED = "exited"
    """The process ended, or its pipe broke."""
    TIMEOUT = "timeout"
    """A request was not answered within its bound."""
    PROTOCOL = "protocol"
    """The process broke the frame protocol."""
    REPORTED = "reported"
    """The process answered FAILED for the whole provider."""
    STDERR_OVERSIZED = "stderr_oversized"
    """The process wrote more to stderr before it was ready than the service reads."""


@dataclass(frozen=True, slots=True)
class ProcessBounds:
    """How long the provider's start, a look's preparation and an open may take."""

    start_seconds: float = 600.0
    prepare_seconds: float = 60.0
    open_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class ProviderLaunch:
    """The worker to run, and the most pixels an image it prepares may have."""

    executable: Path
    max_image_pixels: int


class _ReportedFailureError(Exception):
    """The process answered FAILED for the whole provider."""


class _Request:
    """One request that waits for its answer; `None` is an answer fenced out."""

    def __init__(self, answer: FrameKind) -> None:
        self.answer = answer
        self.outcome: Future[ProtocolFrame | None] = Future()
        self.fenced = False

    def resolve(self, frame: ProtocolFrame | None) -> None:
        with suppress(InvalidStateError):
            self.outcome.set_result(frame)

    def reject(self) -> None:
        with suppress(InvalidStateError):
            self.outcome.set_exception(ProviderFailedError())


class ProcessProvider:
    """The `Provider` that runs in a child process, thread-safe over its sessions."""

    def __init__(
        self, launch: ProviderLaunch, bounds: ProcessBounds, call_seconds: float
    ) -> None:
        """Hold the launch rule; nothing starts before `start`.

        A session call not answered within `call_seconds` ends the process.
        """
        self._launch = launch
        self._bounds = bounds
        self._call_seconds = call_seconds
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._child: subprocess.Popen[bytes] | None = None
        self._info: ProviderInfo | None = None
        self._failure: ProcessFailure | None = None
        self._closing = False
        self._failing = False
        self._failed = threading.Event()
        self._queues: dict[int, deque[_Request]] = {NO_SESSION: deque()}
        self._next_session = _FIRST_SESSION

    @property
    def failed(self) -> bool:
        """Whether the process failed, which it does for good."""
        with self._lock:
            return self._failure is not None

    def start(self) -> ProviderInfo:
        """Start the worker and wait for READY, or raise `ProviderFailedError`."""
        ready = _Request(FrameKind.READY)
        child = self._spawn(ready)
        threading.Thread(
            target=self._read_answers, args=(child,), daemon=True, name="provider-in"
        ).start()
        threading.Thread(
            target=self._drain_stderr, args=(child,), daemon=True, name="provider-err"
        ).start()
        try:
            with self._within(self._bounds.start_seconds):
                ready.outcome.result()
        except ProviderFailedError:
            self._fail(ProcessFailure.REPORTED)
            raise
        return self._ready_info()

    def prepare(self, look: str, image: DecodedImage) -> None:
        """Prepare `look` from `image`, or raise `ProviderFailedError`."""
        self._ready_info()
        with _encoding():
            request = encode_prepare(look, image)
        self._exchange(
            NO_SESSION, request, FrameKind.PREPARED, self._bounds.prepare_seconds
        )

    def open_session(self, look: str) -> ProviderSession:
        """Open a session on `look`, or raise `ProviderFailedError`."""
        self._ready_info()
        with self._lock:
            session = self._next_session
            self._next_session += 1
            self._queues[session] = deque()
        try:
            with _encoding():
                request = encode_open(session, look)
            self._exchange(
                session, request, FrameKind.OPENED, self._bounds.open_seconds
            )
        except ProviderFailedError:
            with self._lock:
                self._queues.pop(session, None)
            raise
        return _ProcessSession(self, session)

    def close(self) -> None:
        """End the process and reap it, without waiting for a load or a call."""
        with self._lock:
            self._closing = True
            child = self._child
        if child is None:
            return
        self._reject_waiting()
        self._reap(child)

    def draw(
        self, session: int, request: ProtocolFrame, expected: int | None = None
    ) -> list[Frame]:
        """The pictures `session` drew for `request`, `expected` of them if told."""
        answer = self._exchange(session, request, FrameKind.FRAMES, self._call_seconds)
        if answer is None:
            return []
        try:
            _, pictures = decode_frames(answer.payload, self._ready_info().video)
        except ProtocolError:
            self._fail(ProcessFailure.PROTOCOL)
            raise ProviderFailedError from None
        if expected is not None and len(pictures) != expected:
            self._fail(ProcessFailure.PROTOCOL)
            raise ProviderFailedError
        return pictures

    def clear(self, session: int) -> None:
        """Drop `session`'s sentence; every request sent before is fenced out."""
        self._exchange(
            session,
            encode_session(FrameKind.CLEAR, session),
            FrameKind.CLEARED,
            self._call_seconds,
        )

    def close_session(self, session: int) -> None:
        """Tell the process `session` is closed, if it is still there to be told."""
        with self._write_lock:
            with self._lock:
                child = self._child
                known = self._queues.pop(session, None) is not None
                if not known or self._failure is not None or self._closing:
                    return
            with suppress(ProviderFailedError):
                self._write(child, encode_session(FrameKind.CLOSE, session))

    def _ready_info(self) -> ProviderInfo:
        with self._lock:
            if self._info is None or self._failure is not None or self._closing:
                raise ProviderFailedError
            return self._info

    def _spawn(self, ready: _Request) -> subprocess.Popen[bytes]:
        with self._lock:
            if self._child is not None or self._closing or self._failing:
                raise ProviderFailedError
            try:
                child = _launch(self._launch)
            except OSError:
                child = None
            else:
                self._child = child
                self._queues[NO_SESSION].append(ready)
        if child is None:
            self._fail(ProcessFailure.UNAVAILABLE)
            raise ProviderFailedError
        return child

    def _exchange(
        self, session: int, request: ProtocolFrame, answer: FrameKind, seconds: float
    ) -> ProtocolFrame | None:
        """Send `request` and wait for its answer, which must come within `seconds`."""
        waiting = _Request(answer)
        with self._within(seconds):
            with self._write_lock:
                with self._lock:
                    child = self._child
                    queue = self._queues.get(session)
                    if (
                        child is None
                        or queue is None
                        or self._failure is not None
                        or self._closing
                    ):
                        raise ProviderFailedError
                    if request.kind is FrameKind.CLEAR:
                        for earlier in queue:
                            earlier.fenced = True
                            earlier.resolve(None)
                    queue.append(waiting)
                self._write(child, request)
            return waiting.outcome.result()

    @contextlib.contextmanager
    def _within(self, seconds: float) -> Iterator[None]:
        """Fail the provider unless the block ends within `seconds`.

        The watchdog also ends a write that a hung process holds on a full pipe.
        """
        watchdog = threading.Timer(seconds, self._fail, (ProcessFailure.TIMEOUT,))
        watchdog.daemon = True
        watchdog.start()
        try:
            yield
        finally:
            watchdog.cancel()

    def _write(
        self, child: subprocess.Popen[bytes] | None, frame: ProtocolFrame
    ) -> None:
        try:
            write_frame(_stream(None if child is None else child.stdin), frame)
        except OSError, ValueError:
            self._fail(ProcessFailure.EXITED)
            raise ProviderFailedError from None

    def _read_answers(self, child: subprocess.Popen[bytes]) -> None:
        limits = FrameLimits()
        stdout = _stream(child.stdout)
        try:
            while True:
                limits = self._dispatch(
                    read_frame(stdout, limits, ANSWER_KINDS), limits
                )
        except _ReportedFailureError:
            self._fail(ProcessFailure.REPORTED)
        except ProtocolError:
            self._fail(ProcessFailure.PROTOCOL)
        except EOFError, OSError, ValueError:
            self._fail(ProcessFailure.EXITED)
        finally:
            stdout.close()

    def _dispatch(self, frame: ProtocolFrame, limits: FrameLimits) -> FrameLimits:
        """Hand `frame` to the request it answers; the limits for the next frame."""
        kind = frame.kind
        session = _addressee(frame)
        if kind is FrameKind.READY:
            info = decode_ready(frame.payload)
            limits = FrameLimits(picture_bytes=picture_bytes(info.video))
            with self._lock:
                self._info = info
        with self._lock:
            queue = self._queues.get(session)
            if not queue or (
                kind is not FrameKind.FAILED and queue[0].answer is not kind
            ):
                raise ProtocolError
            request = queue.popleft()
        if kind is FrameKind.FAILED:
            request.reject()
        elif not request.fenced:
            request.resolve(frame)
        return limits

    def _drain_stderr(self, child: subprocess.Popen[bytes]) -> None:
        """Read and drop the worker's stderr; before READY it may write only so much."""
        stderr = _stream(child.stderr)
        total = 0
        with suppress(OSError, ValueError), stderr:
            while chunk := stderr.read(_STDERR_CHUNK_BYTES):
                total += len(chunk)
                with self._lock:
                    ready = self._info is not None
                if total > _STDERR_LIMIT_BYTES and not ready:
                    self._fail(ProcessFailure.STDERR_OVERSIZED)
                    return

    def _fail(self, reason: ProcessFailure) -> None:
        """Fail the provider for good: kill the process and fail every request.

        Every caller returns once the failure is complete and logged, so one that
        sees it can rely on the line.
        """
        with self._lock:
            first = not self._failing
            self._failing = True
            child = self._child
        if not first:
            self._failed.wait()
            return
        if child is not None:
            self._reap(child)
        with self._lock:
            closing = self._closing
        if not closing:
            self._log_failure(reason, child)
        with self._lock:
            self._failure = reason
        self._reject_waiting()
        self._failed.set()

    def _reject_waiting(self) -> None:
        with self._lock:
            waiting = [request for queue in self._queues.values() for request in queue]
            for queue in self._queues.values():
                queue.clear()
        for request in waiting:
            request.reject()

    def _reap(self, child: subprocess.Popen[bytes]) -> None:
        """Kill the worker's process group, reap it and close its input.

        A child already reaped is not killed again: its pid may belong to another
        process by now. A child that exited but is not reaped is a zombie, so its
        pid is still its own and its group is killed. Its output pipes close in the
        threads that read them, which end at the end of the output.
        """
        if child.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        with suppress(OSError):
            _stream(child.stdin).close()

    def _log_failure(
        self, reason: ProcessFailure, child: subprocess.Popen[bytes] | None
    ) -> None:
        with self._lock:
            phase = "after_ready" if self._info is not None else "before_ready"
        logger.warning(
            "provider.failed",
            extra={
                "reason": reason.value,
                "phase": phase,
                "exit_status": None if child is None else child.returncode,
            },
        )


class _ProcessSession:
    """One open look of the process: a request and its answer for each call."""

    def __init__(self, provider: ProcessProvider, session: int) -> None:
        self._provider = provider
        self._session = session

    def idle_frame(self) -> Frame:
        """Draw the next frame of the face at rest."""
        request = encode_session(FrameKind.IDLE, self._session)
        return self._provider.draw(self._session, request, expected=1)[0]

    def feed(self, pcm: bytes, rate: int) -> Iterator[Frame]:
        """Take PCM16 audio in one request; the frames the call draws come out.

        A payload over `MAX_PCM_BYTES` fails the call; the player never sends one,
        as it feeds at most one frame period of audio a call.
        """
        with _encoding():
            request = encode_pcm(self._session, rate, pcm)
        return iter(self._provider.draw(self._session, request))

    def finish_sentence(self) -> Iterator[Frame]:
        """End the open sentence; the frames not yet drawn come out."""
        request = encode_session(FrameKind.SENTENCE_END, self._session)
        return iter(self._provider.draw(self._session, request))

    def clear(self) -> None:
        """Drop the open sentence."""
        self._provider.clear(self._session)

    def close(self) -> None:
        """Release the session."""
        self._provider.close_session(self._session)


@contextlib.contextmanager
def _encoding() -> Iterator[None]:
    """A request the protocol cannot hold fails its call, not the provider."""
    try:
        yield
    except ProtocolError:
        raise ProviderFailedError from None


def _addressee(frame: ProtocolFrame) -> int:
    """The session an answer is for; `NO_SESSION` for the provider's own requests.

    A FAILED for the whole provider raises, since no request waits for it.
    """
    kind, payload = frame.kind, frame.payload
    if kind is FrameKind.FAILED:
        code, session = decode_failed(payload)
        if code is FailureCode.PROVIDER:
            raise _ReportedFailureError
        return session
    if kind is FrameKind.FRAMES:
        return session_of(payload)
    if kind is FrameKind.PREPARED and payload:
        raise ProtocolError
    if kind in {FrameKind.OPENED, FrameKind.CLEARED}:
        return decode_session(payload)
    return NO_SESSION


def _stream(pipe: IO[bytes] | None) -> IO[bytes]:
    """The pipe of a child started with all three standard streams piped."""
    return cast("IO[bytes]", pipe)


def _launch(launch: ProviderLaunch) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - the one worker executable without a shell or arguments
        [str(launch.executable)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, MAX_IMAGE_PIXELS_ENV: str(launch.max_image_pixels)},
        close_fds=True,
        start_new_session=True,
    )
