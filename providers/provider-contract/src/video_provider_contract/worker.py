"""The loop that serves a `Provider` over the frame protocol, in its own process.

A provider's worker script calls `run_worker` with the provider it builds. The
process speaks the protocol on the pipes the service gave it, moved off fd 1 so a
library's `print` cannot corrupt it, and it ends on the end of its input at once,
even while the model computes: a service that dies closes that pipe, and no
provider outlives it.
"""

from __future__ import annotations

import contextlib
import os
import queue
import sys
import threading
import traceback
from typing import TYPE_CHECKING, BinaryIO, NoReturn

from video_provider_contract import ProviderFailedError
from video_provider_contract.protocol import (
    BYTES_PER_PIXEL,
    NO_SESSION,
    REQUEST_KINDS,
    FailureCode,
    FrameKind,
    FrameLimits,
    ProtocolError,
    ProtocolFrame,
    decode_open,
    decode_pcm,
    decode_prepare,
    decode_session,
    encode_failed,
    encode_frames,
    encode_ready,
    encode_session,
    read_frame,
    session_of,
    write_frame,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from video_provider_contract import (
        Frame,
        Provider,
        ProviderInfo,
        ProviderSession,
    )

MAX_IMAGE_PIXELS_ENV = "VIDEO_PROVIDER_MAX_IMAGE_PIXELS"
_PROTOCOL_FAILED = 1
_STDIN = 0
_STDOUT = 1
_STDERR = 2


def run_worker(provider: Provider) -> NoReturn:
    """Serve `provider` on this process's stdin and stdout, then end the process.

    The service names the most pixels an image may have in
    `VIDEO_PROVIDER_MAX_IMAGE_PIXELS`.
    """
    limits = FrameLimits(image_bytes=_max_image_pixels() * BYTES_PER_PIXEL)
    output = os.fdopen(os.dup(_STDOUT), "wb")
    _silence(_STDOUT)
    requests: queue.SimpleQueue[ProtocolFrame | ProtocolError] = queue.SimpleQueue()
    threading.Thread(
        target=_read_requests,
        args=(os.fdopen(_STDIN, "rb", closefd=False), limits, requests),
        daemon=True,
        name="video-provider-input",
    ).start()
    os._exit(_serve(provider, limits, output, requests))


def _max_image_pixels() -> int:
    try:
        pixels = int(os.environ[MAX_IMAGE_PIXELS_ENV])
    except (KeyError, ValueError):
        pixels = 0
    if pixels <= 0:
        sys.stderr.write(f"{MAX_IMAGE_PIXELS_ENV} must be a positive integer\n")
        os._exit(_PROTOCOL_FAILED)
    return pixels


def _silence(descriptor: int) -> None:
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, descriptor)
    finally:
        os.close(null)


def _read_requests(
    stream: BinaryIO,
    limits: FrameLimits,
    requests: queue.SimpleQueue[ProtocolFrame | ProtocolError],
) -> None:
    try:
        while True:
            requests.put(read_frame(stream, limits, REQUEST_KINDS))
    except (EOFError, OSError):
        os._exit(0)
    except ProtocolError as error:
        requests.put(error)


def _serve(
    provider: Provider,
    limits: FrameLimits,
    output: BinaryIO,
    requests: queue.SimpleQueue[ProtocolFrame | ProtocolError],
) -> int:
    """Start the provider, answer READY, then serve; the exit status when it ends."""
    try:
        _answer_until_failure(provider, limits, output, requests)
    except Exception:  # noqa: BLE001 - the one way out: FAILED for the whole provider
        # Before READY the service reads stderr bounded and never logs it as text.
        traceback.print_exc()
        _write_failure(output)
    return _PROTOCOL_FAILED


def _answer_until_failure(
    provider: Provider,
    limits: FrameLimits,
    output: BinaryIO,
    requests: queue.SimpleQueue[ProtocolFrame | ProtocolError],
) -> NoReturn:
    info = provider.start()
    _silence(_STDERR)
    write_frame(output, encode_ready(info))
    worker = _Worker(provider, info, limits)
    while True:
        request = requests.get()
        if isinstance(request, ProtocolError):
            raise request
        answer = worker.handle(request)
        if answer is not None:
            write_frame(output, answer)


def _write_failure(output: BinaryIO) -> None:
    with contextlib.suppress(OSError):
        write_frame(output, encode_failed(FailureCode.PROVIDER))


class _Worker:
    """The sessions of one provider, and the answer to each request."""

    def __init__(
        self, provider: Provider, info: ProviderInfo, limits: FrameLimits
    ) -> None:
        self._provider = provider
        self._info = info
        self._limits = limits
        self._sessions: dict[int, ProviderSession] = {}

    def handle(self, request: ProtocolFrame) -> ProtocolFrame | None:
        """The answer to `request`, FAILED when the provider refuses it."""
        kind, payload = request.kind, request.payload
        session = NO_SESSION if kind is FrameKind.PREPARE else session_of(payload)
        try:
            if kind is FrameKind.PREPARE:
                return self._prepare(payload)
            if kind is FrameKind.OPEN:
                return self._open(payload)
            if kind is FrameKind.CLOSE:
                self._close(session)
                return None
            if kind is FrameKind.CLEAR:
                return self._clear(session)
            return self._draw(kind, payload)
        except ProviderFailedError:
            return encode_failed(FailureCode.REQUEST, session)

    def _prepare(self, payload: bytes) -> ProtocolFrame:
        look, image = decode_prepare(payload, self._limits)
        self._provider.prepare(look, image)
        return ProtocolFrame(FrameKind.PREPARED)

    def _open(self, payload: bytes) -> ProtocolFrame:
        session, look = decode_open(payload)
        if session == NO_SESSION or session in self._sessions:
            raise ProtocolError
        self._sessions[session] = self._provider.open_session(look)
        return encode_session(FrameKind.OPENED, session)

    def _close(self, session: int) -> None:
        self._known(session).close()
        del self._sessions[session]

    def _clear(self, session: int) -> ProtocolFrame:
        self._known(session).clear()
        return encode_session(FrameKind.CLEARED, session)

    def _draw(self, kind: FrameKind, payload: bytes) -> ProtocolFrame:
        if kind is FrameKind.PCM:
            session, rate, pcm = decode_pcm(payload)
            pictures: Iterable[Frame] = self._known(session).feed(pcm, rate)
        elif kind is FrameKind.IDLE:
            session = decode_session(payload)
            pictures = [self._known(session).idle_frame()]
        else:
            session = decode_session(payload)
            pictures = self._known(session).finish_sentence()
        return encode_frames(session, list(pictures), self._info.video)

    def _known(self, session: int) -> ProviderSession:
        try:
            return self._sessions[session]
        except KeyError:
            raise ProtocolError from None
