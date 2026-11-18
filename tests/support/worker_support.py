"""Worker executables that answer, or fail to, the way a test scripts them."""

from __future__ import annotations

import os
import stat
import sys
import textwrap
from typing import TYPE_CHECKING

from video_service.provider_process import ProcessBounds
from video_service.providers import ProviderName, worker_executable

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from video_service.provider_process import ProcessProvider

type MakeProvider = Callable[..., ProcessProvider]

CALL_SECONDS = 5.0
SHORT_BOUNDS = ProcessBounds(start_seconds=0.4, prepare_seconds=0.4, open_seconds=0.4)

# What a scripted worker may use: the contract's protocol on its own pipes, and
# `serve` to answer requests the way a healthy provider would.
_PRELUDE = """\
import os, struct, sys, time
from video_provider_contract import ProviderInfo, ProviderVerb, ProviderVideoFormat
from video_provider_contract.protocol import (
    REQUEST_KINDS, FailureCode, FrameKind, FrameLimits, ProtocolFrame, encode_failed,
    encode_ready, encode_session, read_frame, session_of, write_frame,
)

INFO = ProviderInfo(
    (ProviderVerb.SPEAK, ProviderVerb.CLEAR), ProviderVideoFormat(2, 2, 25)
)
PICTURE = bytes(2 * 2 * 3)
out = os.fdopen(os.dup(1), "wb")
inp = os.fdopen(0, "rb")
with open(os.environ["STUB_PIDFILE"], "w") as pidfile:
    pidfile.write(str(os.getpid()))

def send(frame): write_frame(out, frame)
def ready(): send(encode_ready(INFO))
def raw(data): out.write(data); out.flush()
def request(): return read_frame(inp, FrameLimits(image_bytes=1 << 20), REQUEST_KINDS)
def hang(): time.sleep(60)
def tell():
    descriptor = os.open(os.environ["STUB_FIFO"], os.O_WRONLY)
    os.write(descriptor, b"x")
    os.close(descriptor)

def hold():
    return os.open(os.environ["STUB_FIFO"], os.O_RDWR)

ANSWERS = {
    FrameKind.OPEN: FrameKind.OPENED,
    FrameKind.CLEAR: FrameKind.CLEARED,
}

def answer(asked):
    if asked.kind is FrameKind.PREPARE:
        send(ProtocolFrame(FrameKind.PREPARED))
    elif asked.kind in ANSWERS:
        send(encode_session(ANSWERS[asked.kind], session_of(asked.payload)))
    elif asked.kind is not FrameKind.CLOSE:
        send(ProtocolFrame(FrameKind.FRAMES, asked.payload[:4] + PICTURE))

def serve(count=None):
    while count != 0:
        answer(request())
        count = None if count is None else count - 1
"""


def install_worker(root: Path, body: str) -> Path:
    """Write a worker whose `body` follows the prelude, where `root` makes it launch."""
    executable = worker_executable(ProviderName.FAKE, root)
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!{sys.executable}\n{_PRELUDE}\n{textwrap.dedent(body)}\n", encoding="utf-8"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def is_gone(pid: int) -> bool:
    """Whether no process has the id `pid`."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False
