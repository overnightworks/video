"""The service as a process of its own, and a session driven through the wire.

The service starts from this checkout in a process group of its own, under a
parent-death signal; every exit stops it first and kills only a service that
outlives its stop, its workers first. Its log lines are the figures' source; the
session's events are the rest. The command that drives it is a child subreaper and
ends, from outside, every process still below it once a group has ended.
"""

from __future__ import annotations

import asyncio
import ctypes
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
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import httpx2
from pydantic import ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from card_identity import LOOK_NAMES
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
from video_client.wire import (
    LOOK_PARAM,
    SDP_CONTENT_TYPE,
    SESSIONS_PATH,
    RefusalCode,
    viewers_path,
)
from video_service.config import Settings
from video_service.provider_process import ProcessBounds
from video_service.providers import ProviderName

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping

    from websockets.asyncio.client import ClientConnection

    from card_identity import Identity

# On top of the service's own bounds for the provider's start and each preparation.
READY_SLACK_SECONDS: Final = 30.0
STOP_WITHIN_SECONDS: Final = 20.0
# What the unwinding of the drive may take after the kill of a service that stayed.
KILL_MARGIN_SECONDS: Final = 10.0
# How long the kill below the caller passes until nothing below it is alive.
KILL_WITHIN_SECONDS: Final = 2.0
EVENT_WITHIN_SECONDS: Final = 10.0
LINE_WITHIN_SECONDS: Final = 10.0
DRAIN_SECONDS: Final = 5
POLL_SECONDS: Final = 0.02
CHUNK_BYTES: Final = 8192
MINIMUM_AUDIO_SECONDS: Final = 1
STOP_SIGNALS: Final = (signal.SIGINT, signal.SIGTERM)
_SIGNAL_EXIT_BASE: Final = 128
_HOST: Final = "127.0.0.1"
_PARENT_DEATH_COMMAND: Final = ("setpriv", "--pdeathsig", "KILL")
# The service's own settings name the variables that configure it.
ENVIRONMENT_PREFIX: Final[str] = Settings.model_config["env_prefix"]
PROVIDER_VARIABLE: Final = f"{ENVIRONMENT_PREFIX}PROVIDER"
FAKE_PROVIDER: Final = MappingProxyType({PROVIDER_VARIABLE: ProviderName.FAKE.value})
_SET_CHILD_SUBREAPER: Final = 36
# The interpreter this repository runs on is a standalone build whose `os` has no
# pidfd calls, so they are the kernel's system calls, numbered alike on every
# architecture the proof runs on.
_SYSCALL_PIDFD_SEND_SIGNAL: Final = 424
_SYSCALL_PIDFD_OPEN: Final = 434
_ZOMBIE: Final = "Z"
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
    """The PCM of a mono 16-bit WAV file of enough length at a rate the wire carries."""
    try:
        with wave.open(str(path)) as source:
            shape = (
                source.getnchannels(),
                source.getsampwidth(),
                source.getframerate(),
            )
            pcm = source.readframes(source.getnframes())
    except (OSError, EOFError, wave.Error) as error:
        message = f"{path}: the audio cannot be read ({error})"
        raise SystemExit(message) from None
    try:
        PcmFormat(type="audio/pcm", rate=shape[2])
    except ValidationError:
        shape = (0, 0, 0)
    audio = Audio(rate=shape[2], pcm=pcm)
    if shape[:2] != (1, BYTES_PER_SAMPLE) or (
        audio.milliseconds < MINIMUM_AUDIO_SECONDS * 1000
    ):
        message = (
            f"{path}: the audio must be a mono 16-bit WAV of at least "
            f"{MINIMUM_AUDIO_SECONDS} second at a rate the wire carries"
        )
        raise SystemExit(message)
    return audio


def _read_stat(pid: int) -> bytes:
    return Path(f"/proc/{pid}/stat").read_bytes()


def _stat_fields(stat: bytes) -> list[str]:
    """The fields of a `stat` after the command name: state first.

    The kernel cuts the name at a byte, which may split a character, so only the
    fields after its closing bracket are decoded.
    """
    return stat.rsplit(b")", 1)[1].decode("ascii").split()


def _process_fields(pid: int) -> list[str]:
    return _stat_fields(_read_stat(pid))


_PARENT_FIELD: Final = 1
_GROUP_FIELD: Final = 2
_START_FIELD: Final = 19
_USER_TICKS_FIELD: Final = 11
_SYSTEM_TICKS_FIELD: Final = 12


def worker_pids(service_pid: int) -> list[int]:
    """The children of the service that lead a process group of their own."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        with suppress(OSError):
            fields = _process_fields(int(entry.name))
            if int(fields[_PARENT_FIELD]) == service_pid and int(
                fields[_GROUP_FIELD]
            ) == int(entry.name):
                found.append(int(entry.name))
    return found


def _checked(result: int) -> int:
    """`result` of a libc call, or the `OSError` its `errno` names."""
    if result < 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))
    return result


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def become_subreaper() -> None:
    """Make this process the parent of every orphan below it."""
    _checked(_libc().prctl(_SET_CHILD_SUBREAPER, 1, 0, 0, 0))


def _pidfd_open(pid: int) -> int:
    return _checked(_libc().syscall(_SYSCALL_PIDFD_OPEN, pid, 0))


def _pidfd_send_signal(pidfd: int, signum: int) -> None:
    _checked(_libc().syscall(_SYSCALL_PIDFD_SEND_SIGNAL, pidfd, signum, None, 0))


@dataclass(frozen=True)
class ProcessControl:
    """What the kill and a forwarded stop ask of the kernel; a test swaps it."""

    become_subreaper: Callable[[], None]
    list_processes: Callable[[], list[int]]
    open: Callable[[int], int]
    send_signal: Callable[[int, int], None]
    read_stat: Callable[[int], bytes]


def _list_processes() -> list[int]:
    return [int(e.name) for e in Path("/proc").iterdir() if e.name.isdigit()]


KERNEL: Final = ProcessControl(
    become_subreaper=become_subreaper,
    list_processes=_list_processes,
    open=_pidfd_open,
    send_signal=_pidfd_send_signal,
    read_stat=_read_stat,
)


@dataclass(frozen=True)
class Process:
    """A process as `/proc` showed it; its parent and start time tell a reused id."""

    pid: int
    parent: int
    started: int
    state: str

    @property
    def alive(self) -> bool:
        """Whether it still runs; a zombie only waits to be reaped."""
        return self.state != _ZOMBIE


# A process first, then each parent of it up to the child of the top.
Lineage = tuple[Process, ...]


def read_process(pid: int, control: ProcessControl = KERNEL) -> Process | None:
    """The process `pid` as it is now, or none once it is gone; other errors raise."""
    try:
        fields = _stat_fields(control.read_stat(pid))
    except FileNotFoundError, ProcessLookupError:
        return None
    return Process(
        pid, int(fields[_PARENT_FIELD]), int(fields[_START_FIELD]), fields[0]
    )


def _all_processes(control: ProcessControl) -> tuple[dict[int, Process], bool]:
    """Every process `/proc` shows, and whether one of them could not be read."""
    found: dict[int, Process] = {}
    unreadable = False
    for pid in control.list_processes():
        try:
            process = read_process(pid, control)
        except OSError:
            unreadable = True
            continue
        if process is not None:
            found[process.pid] = process
    return found, unreadable


def _lineage(
    process: Process, processes: dict[int, Process], top: int
) -> Lineage | None:
    """`process` and its parents up to the child of `top`, or none if cut short."""
    chain = [process]
    while chain[-1].parent != top:
        found = processes.get(chain[-1].parent)
        if found is None or len(chain) > len(processes):
            return None
        chain.append(found)
    return tuple(chain)


def has_child(parent: int, control: ProcessControl = KERNEL) -> bool:
    """Whether any process shows `parent` as its parent now."""
    processes, _ = _all_processes(control)
    return any(process.parent == parent for process in processes.values())


@dataclass(frozen=True)
class Below:
    """The lineages of the processes below the top, deepest first, from one read."""

    lineages: list[Lineage]
    unreadable: bool


def _below(top: int, control: ProcessControl) -> Below:
    processes, unreadable = _all_processes(control)
    lineages = [
        lineage
        for found in processes.values()
        if (lineage := _lineage(found, processes, top)) is not None
    ]
    return Below(sorted(lineages, key=len, reverse=True), unreadable)


def _unchanged(link: Process, control: ProcessControl) -> bool:
    """Whether `link` is still the process it was, under the parent it had."""
    now = read_process(link.pid, control)
    return now is not None and (now.parent, now.started) == (link.parent, link.started)


def signal_if_unchanged(
    lineage: Lineage, signum: int, control: ProcessControl = KERNEL
) -> bool:
    """Send `signum` to the first process of `lineage` through a pidfd unless it moved.

    The pidfd is opened first and the process and each parent of it are read after, so
    an id freed and reused in between, the process's or a parent's, is not signalled:
    one read of every process is not atomic, and a parent that was reaped and replaced
    would else lead a stranger into the top's tree. Whether it went without an error: a
    process already gone is no error.
    """
    try:
        pidfd = control.open(lineage[0].pid)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    try:
        if all(_unchanged(link, control) for link in lineage):
            control.send_signal(pidfd, signum)
    except ProcessLookupError:
        pass
    except OSError:
        return False
    finally:
        os.close(pidfd)
    return True


@dataclass(frozen=True)
class Kill:
    """What the kill below its caller found and left."""

    processes_left: int
    processes_alive: int
    failed: bool


def _reap_children(zombies: list[Process]) -> None:
    for zombie in zombies:
        with suppress(ChildProcessError):
            os.waitpid(zombie.pid, os.WNOHANG)


def end_processes_below(
    control: ProcessControl = KERNEL, *, spare: int | None = None
) -> Kill:
    """End every process below this one by pidfd-checked SIGKILL passes, stopping none.

    A pass signals every live descendant, the deepest first, and reaps the zombies
    that are this process's children by their ids, all but `spare`, whose status stays
    for the `Popen` that holds it. It runs until none is alive or
    `KILL_WITHIN_SECONDS` has passed. It must run in a child subreaper, so an orphan
    of a descendant stays below. A process that cannot be read or signalled, and a pass
    that cannot list or reap, fail the kill, which goes on with the others and tries
    again until the bound, since an unread process is not known to be gone.
    """
    top = os.getpid()
    deadline = time.monotonic() + KILL_WITHIN_SECONDS
    first: int | None = None
    failed = False
    while True:
        below = Below([], unreadable=True)
        try:
            below = _below(top, control)
            _reap_children(
                [
                    lineage[0]
                    for lineage in below.lineages
                    if lineage[0].parent == top
                    and not lineage[0].alive
                    and lineage[0].pid != spare
                ]
            )
        except OSError:
            below = Below(below.lineages, unreadable=True)
        failed |= below.unreadable
        alive = [lineage for lineage in below.lineages if lineage[0].alive]
        first = len(alive) if first is None else first
        if not (alive or below.unreadable) or time.monotonic() >= deadline:
            return Kill(first, len(alive), failed)
        for lineage in alive:
            failed |= not signal_if_unchanged(lineage, signal.SIGKILL, control)
        time.sleep(POLL_SECONDS)


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
        # None while the stop has not timed an exit: a service gone before its stop.
        self.stop_to_exit_ms: int | None = None

    def lines(self, event: str) -> list[dict[str, object]]:
        """The JSON lines of `event` written so far."""
        text = self.log.read_text(encoding="utf-8")
        records = (
            json.loads(line) for line in text.splitlines() if line.startswith("{")
        )
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

    def cpu_seconds(self) -> float:
        """The CPU time the service's own process has used, by the kernel's count."""
        fields = _process_fields(self.process.pid)
        ticks = int(fields[_USER_TICKS_FIELD]) + int(fields[_SYSTEM_TICKS_FIELD])
        return ticks / os.sysconf("SC_CLK_TCK")

    def url(self, look: str) -> str:
        """The session upgrade of `look`."""
        # The service speaks plain HTTP on the loopback address and has no TLS.
        origin = f"ws://{_HOST}:{self.port}"  # NOSONAR(S5332)
        return f"{origin}{SESSIONS_PATH}?{LOOK_PARAM}={look}"


def ready_within_seconds(look_count: int) -> float:
    """How long the service may take to be ready, by its own bounds and `look_count`."""
    bounds = ProcessBounds()
    return (
        bounds.start_seconds + look_count * bounds.prepare_seconds + READY_SLACK_SECONDS
    )


def _wait_until_ready(service: Service, started: float) -> None:
    ready_within = ready_within_seconds(len(LOOK_NAMES))
    while time.monotonic() - started < ready_within:
        if service.process.poll() is not None:
            message = "the service exited before it was ready"
            raise DriveError(message)
        if service.lines(_READY_EVENT):
            service.start_to_ready_ms = round((time.monotonic() - started) * 1000)
            return
        time.sleep(POLL_SECONDS)
    message = "the service was not ready in time"
    raise DriveError(message)


def hold_workers(service_pid: int) -> None:
    """Stop the service's workers where they are, so no call to them is answered."""
    for worker in worker_pids(service_pid):
        with suppress(ProcessLookupError):
            os.killpg(worker, signal.SIGSTOP)


def _kill(service_pid: int) -> None:
    """Kill the groups of the service's worker children now, then the service's."""
    for worker in worker_pids(service_pid):
        with suppress(ProcessLookupError):
            os.killpg(worker, signal.SIGKILL)
    with suppress(ProcessLookupError):
        os.killpg(service_pid, signal.SIGKILL)


def _stop(service: Service) -> bool:
    """Stop the service and time its exit; whether it exited within the bound.

    The stop comes first and reaps the worker. A service that stays is killed
    together with the worker groups that are its children at that moment. One that
    had exited before the stop has no exit to time.
    """
    process = service.process
    if process.poll() is not None:
        return True
    stopping = time.monotonic()
    process.send_signal(signal.SIGTERM)
    in_time = True
    try:
        process.wait(STOP_WITHIN_SECONDS)
    except subprocess.TimeoutExpired:
        in_time = False
    elapsed_seconds = time.monotonic() - stopping
    service.stop_to_exit_ms = round(elapsed_seconds * 1000)
    if not in_time:
        _kill(process.pid)
        process.wait()
    return in_time


def exit_on_stop(signum: int, _frame: object) -> None:
    """Turn the first stop into an exit that runs the cleanups; ignore a second.

    What the cleanups of cancelled peers print is no use to the operator, so the
    run ends with the one line its command writes.
    """
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, signal.SIG_IGN)
    with Path(os.devnull).open("w", encoding="utf-8") as silence:
        os.dup2(silence.fileno(), sys.stderr.fileno())
    sys.exit(_SIGNAL_EXIT_BASE + signum)


def _stop_first(service: Service) -> Callable[[int, object], None]:
    """A signal handler that stops the service before the exit unwinds the drive.

    The drive's teardown waits on connections the service holds, so it hangs while
    the service lives.
    """

    def stop(signum: int, frame: object) -> None:
        for stopping in STOP_SIGNALS:
            signal.signal(stopping, signal.SIG_IGN)
        _stop(service)
        exit_on_stop(signum, frame)

    return stop


@contextmanager
def running_service(
    identity: Identity,
    log: Path,
    *,
    max_lag_ms: int,
    provider: Mapping[str, str] = FAKE_PROVIDER,
) -> Iterator[Service]:
    """Start the service, logging to `log`; the block's end stops it.

    `provider` holds the `VIDEO_` variables that choose the provider and its settings:
    the `fake` provider unless the caller gives the operator's.

    The parent-death signal follows the thread that starts the service, so the
    caller is the main thread, which lives until the process ends.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(ENVIRONMENT_PREFIX)
    } | {
        "PYTHONUNBUFFERED": "1",
        "SSL_CERT_FILE": str(identity.ca_file),
        "VIDEO_PORT": str(port := _free_port()),
        "VIDEO_CONFIG_FILE": str(identity.config_file),
        "VIDEO_LOOKS_FILE": str(identity.looks_file),
        "VIDEO_LOOK_SOURCES": identity.look_sources,
        "VIDEO_MAX_LAG_MS": str(max_lag_ms),
        "VIDEO_DRAIN_SECONDS": str(DRAIN_SECONDS),
        **provider,
    }
    with log.open("wb") as output:
        started = time.monotonic()
        process = subprocess.Popen(  # noqa: S603 - fixed tools and this interpreter
            [*_PARENT_DEATH_COMMAND, sys.executable, "-m", "video_service"],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        service = Service(process, port, log)
        previous = [
            signal.signal(stopping, _stop_first(service)) for stopping in STOP_SIGNALS
        ]
        try:
            _wait_until_ready(service, started)
            yield service
        finally:
            stopped_in_time = _stop(service)
            for stopping, handler in zip(STOP_SIGNALS, previous, strict=True):
                signal.signal(stopping, handler)
    if not stopped_in_time:
        message = "the service did not exit after its stop and was killed"
        raise DriveError(message)


@dataclass(frozen=True)
class Window:
    """A stretch of the run and the service's CPU use over it."""

    start: float
    end: float
    cores: float

    @property
    def seconds(self) -> float:
        """The window's length."""
        return self.end - self.start


async def timed[Result](
    service: Service, work: Awaitable[Result]
) -> tuple[Window, Result]:
    """Await `work`, with the window it took and the service's CPU over it."""
    cpu_before = service.cpu_seconds()
    start = time.monotonic()
    result = await work
    end = time.monotonic()
    cores = (service.cpu_seconds() - cpu_before) / (end - start)
    return Window(start, end, cores), result


async def post_viewer_offer(
    service: Service, identity: Identity, session_id: str, offer: str
) -> str:
    """The service's SDP answer to a viewer's `offer` on the session's tokens."""
    # The service speaks plain HTTP on the loopback address and has no TLS.
    base_url = f"http://{_HOST}:{service.port}"  # NOSONAR(S5332)
    async with httpx2.AsyncClient(
        base_url=base_url, timeout=EVENT_WITHIN_SECONDS
    ) as client:
        response = await client.post(
            viewers_path(session_id),
            content=offer,
            headers=identity.headers() | {"Content-Type": SDP_CONTENT_TYPE},
        )
    if response.status_code != httpx2.codes.CREATED:
        message = f"the viewer's offer was answered with {response.status_code}"
        raise DriveError(message)
    return response.text


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
