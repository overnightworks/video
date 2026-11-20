"""The service as a process of its own, and a session driven through the wire.

The service starts from this checkout in a process group of its own, which every exit
ends. Its log lines are the figures' source; the session's events are the rest.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
import wave
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from video_client.session_wire import (
    BYTES_PER_SAMPLE,
    ClientEvent,
    ErrorEvent,
    IceConfiguration,
    OutputAudioBufferClear,
    PcmFormat,
    SentenceEnd,
    SentenceStart,
    ServerEvent,
    SessionCreated,
    SessionUpdate,
    SessionUpdated,
    parse_server_event,
)
from video_client.wire import LOOK_PARAM, SESSIONS_PATH, RefusalCode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from websockets.asyncio.client import ClientConnection

    from card_identity import Identity

READY_WITHIN_SECONDS: Final = 120.0
STOP_WITHIN_SECONDS: Final = 20.0
EVENT_WITHIN_SECONDS: Final = 10.0
LINE_WITHIN_SECONDS: Final = 10.0
DRAIN_SECONDS: Final = 5
POLL_SECONDS: Final = 0.02
CHUNK_BYTES: Final = 8192
_HOST: Final = "127.0.0.1"
_ENVIRONMENT_PREFIX: Final = "VIDEO_"
_READY_EVENT: Final = "process.ready"
_NO_ICE: Final = IceConfiguration(ice_servers=[], ice_transport_policy="all")


class DriveError(Exception):
    """The drive saw something the wire does not promise."""


class SessionEndedError(DriveError):
    """The service ended the session with an `error` event."""

    def __init__(self, code: RefusalCode) -> None:
        """Carry the refusal code the event named."""
        super().__init__(f"the session ended with {code}")
        self.code = code


@dataclass(frozen=True)
class Audio:
    """PCM16 mono audio and its rate."""

    rate: int
    pcm: bytes

    @property
    def milliseconds(self) -> int:
        """The audio's length."""
        return len(self.pcm) * 1000 // (BYTES_PER_SAMPLE * self.rate)


def read_audio(path: Path) -> Audio:
    """The PCM of a mono 16-bit WAV file at a rate the wire carries."""
    with wave.open(str(path)) as source:
        shape = (source.getnchannels(), source.getsampwidth(), source.getframerate())
        pcm = source.readframes(source.getnframes())
    try:
        PcmFormat(type="audio/pcm", rate=shape[2])
    except ValidationError:
        shape = (0, 0, 0)
    if shape[:2] != (1, BYTES_PER_SAMPLE):
        message = (
            f"{path}: the audio must be a mono 16-bit WAV at a rate the wire carries"
        )
        raise SystemExit(message)
    return Audio(rate=shape[2], pcm=pcm)


def _free_port() -> int:
    with socket.socket() as bound:
        bound.bind((_HOST, 0))
        return bound.getsockname()[1]


class Service:
    """A running service: its port, its log and the time its start and stop took."""

    def __init__(self, process: subprocess.Popen[bytes], port: int, log: Path) -> None:
        """Hold the process whose standard output is `log`."""
        self.process = process
        self.port = port
        self.log = log
        self.start_to_ready_ms = 0
        self.stop_to_exit_ms = 0

    def lines(self, event: str) -> list[dict[str, object]]:
        """The JSON lines of `event` written so far."""
        text = self.log.read_text(encoding="utf-8")
        records = (json.loads(line) for line in text.splitlines() if line[:1] == "{")
        return [record for record in records if record["event"] == event]

    async def line(self, event: str, **fields: object) -> dict[str, object]:
        """The line of `event` holding `fields`, once it is written."""
        deadline = time.monotonic() + LINE_WITHIN_SECONDS
        while time.monotonic() < deadline:
            for record in self.lines(event):
                if all(record.get(key) == value for key, value in fields.items()):
                    return record
            await asyncio.sleep(POLL_SECONDS)
        message = f"the service wrote no {event} line"
        raise DriveError(message)

    def url(self, look: str) -> str:
        """The session upgrade of `look`."""
        return f"ws://{_HOST}:{self.port}{SESSIONS_PATH}?{LOOK_PARAM}={look}"


def _wait_until_ready(service: Service, started: float) -> None:
    while time.monotonic() - started < READY_WITHIN_SECONDS:
        if service.process.poll() is not None:
            message = "the service exited before it was ready"
            raise DriveError(message)
        if service.lines(_READY_EVENT):
            service.start_to_ready_ms = round((time.monotonic() - started) * 1000)
            return
        time.sleep(POLL_SECONDS)
    message = "the service was not ready in time"
    raise DriveError(message)


def _stop(service: Service) -> None:
    """Ask the service to stop and time its exit; a service that stays is cut."""
    stopping = time.monotonic()
    service.process.send_signal(signal.SIGTERM)
    try:
        service.process.wait(STOP_WITHIN_SECONDS)
    except subprocess.TimeoutExpired:
        message = "the service did not exit after its stop"
        raise DriveError(message) from None
    service.stop_to_exit_ms = round((time.monotonic() - stopping) * 1000)


@contextmanager
def running_service(
    identity: Identity, directory: Path, *, max_lag_ms: int
) -> Iterator[Service]:
    """Start the fake-provider service; its process group ends with the block."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_ENVIRONMENT_PREFIX)
    } | {
        "PYTHONUNBUFFERED": "1",
        "SSL_CERT_FILE": str(identity.ca_file),
        "VIDEO_PROVIDER": "fake",
        "VIDEO_PORT": str(port := _free_port()),
        "VIDEO_CONFIG_FILE": str(identity.config_file),
        "VIDEO_LOOKS_FILE": str(identity.looks_file),
        "VIDEO_LOOK_SOURCES": identity.look_sources,
        "VIDEO_MAX_LAG_MS": str(max_lag_ms),
        "VIDEO_DRAIN_SECONDS": str(DRAIN_SECONDS),
    }
    log = directory / f"service-{port}.log"
    with log.open("wb") as output:
        started = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, "-m", "video_service"],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        service = Service(process, port, log)
        try:
            _wait_until_ready(service, started)
            yield service
            _stop(service)
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


class Session:
    """One open session: the events read from it and the audio sent to it."""

    def __init__(self, connection: ClientConnection, created: SessionCreated) -> None:
        """Wrap the connection that has read `session.created`."""
        self.socket = connection
        self.created = created
        self.opening_ms = 0

    async def next_event(self) -> ServerEvent:
        """The next server event, within a bound."""
        text = await asyncio.wait_for(self.socket.recv(), EVENT_WITHIN_SECONDS)
        return parse_server_event(text)

    async def until[Event: ServerEvent](self, kind: type[Event]) -> Event:
        """The next event of `kind`; an `error` event before it is a fault."""
        while True:
            event = await self.next_event()
            if isinstance(event, kind):
                return event
            if isinstance(event, ErrorEvent):
                raise SessionEndedError(event.error.code)

    async def speak(self, sentence: str, audio: Audio) -> str:
        """Send `audio` as one sentence; the trace id its line is written in."""
        trace_id = uuid.uuid4().hex
        start = SentenceStart(
            type="sentence.start",
            sentence=sentence,
            format=PcmFormat(type="audio/pcm", rate=audio.rate),
            traceparent=f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01",
        )
        await self.send(start)
        for offset in range(0, len(audio.pcm), CHUNK_BYTES):
            await self.socket.send(audio.pcm[offset : offset + CHUNK_BYTES])
        await self.send(SentenceEnd(type="sentence.end", sentence=sentence))
        return trace_id

    async def clear(self) -> None:
        """Send `output_audio_buffer.clear`."""
        await self.send(OutputAudioBufferClear(type="output_audio_buffer.clear"))

    async def send(self, event: ClientEvent) -> None:
        """Send one client event."""
        await self.socket.send(event.model_dump_json(exclude_none=True))


@asynccontextmanager
async def opened_session(
    service: Service, identity: Identity, look: str
) -> AsyncIterator[Session]:
    """A session past `session.updated`, with the time that took."""
    opening = time.monotonic()
    async with connect(
        service.url(look), additional_headers=identity.headers()
    ) as socket_:
        first = parse_server_event(
            await asyncio.wait_for(socket_.recv(), EVENT_WITHIN_SECONDS)
        )
        if not isinstance(first, SessionCreated):
            message = "the session did not start with session.created"
            raise DriveError(message)
        session = Session(socket_, first)
        update = SessionUpdate(type="session.update", session=_NO_ICE)
        with suppress(ConnectionClosed):
            await session.send(update)
        await session.until(SessionUpdated)
        session.opening_ms = round((time.monotonic() - opening) * 1000)
        yield session
