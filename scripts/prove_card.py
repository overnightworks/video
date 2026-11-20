"""The card proof: drive the service as an app does and write a closed record.

`--dry-run` drives the `fake` provider, so every verdict that needs a card stays
`not_measured`. Without it the command drives `flashhead-lite` with the operator's
`VIDEO_PROVIDER_ROOT` and `VIDEO_CARD_MEMORY_MB`, refuses a group the card has no room
for and ends a group whose memory falls below the floor. Each group of cells runs as
one service under `probe-run`, which this command calls itself. Each group's process
writes a fragment after every cell and when the group ends, and the record is written
from the fragments however a group ends, so a failed group leaves its cells' figures
and a reason for the rest; the recording and its contact sheets are written beside it,
and each service's log is written there from the service's start.
"""

# ruff: noqa: T201 - the command reports its result on the command line

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import traceback
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from card_drive import (
    ENVIRONMENT_PREFIX,
    FAKE_PROVIDER,
    KERNEL,
    PROVIDER_VARIABLE,
    STOP_SIGNALS,
    Audio,
    Kill,
    ProcessControl,
    SessionEndedError,
    Window,
    end_processes_below,
    exit_on_stop,
    hold_workers,
    opened_session,
    read_audio,
    running_service,
    timed,
)
from card_identity import LOOK_NAMES, throwaway_identity
from card_memory import GroupWatch
from card_record import (
    ENCODER_CORES_LINE,
    FIRST_OUTPUT_LINE,
    FREE_MEMORY_LINE,
    KILL_FIGURES,
    MEMORY_FIGURES,
    START_TO_READY_LINE,
    Reason,
    closed_record,
    completed_cells,
    completed_service,
    judged,
    not_measured,
    value_of,
)
from card_supervision import (
    CHILD_MARKER,
    Stops,
    Supervision,
    ending,
    pid_file,
    run_child,
)
from card_viewer import Recording, attached, clear_figure, figures
from video_client.session_wire import (
    OutputAudioBufferCleared,
    OutputAudioBufferStarted,
    OutputAudioBufferStopped,
)
from video_client.wire import RefusalCode
from video_service.card import read_card_memory
from video_service.config import Settings
from video_service.providers import ProviderName

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager, AbstractContextManager

    from pydantic_settings import PydanticBaseSettingsSource

    from card_drive import Service, Session
    from card_identity import Identity

IDLE_SECONDS: Final = 0.5
# After a sentence ends its last frames still reach the peer.
SETTLE_SECONDS: Final = 0.5
STEADY_SECONDS: Final = 1.5
FIVE_VIEWERS: Final = 5
TOO_SMALL_LAG_MS: Final = 1
DEFAULT_LAG_MS: Final = 500
PROBE_RUN: Final = "probe-run"
KILL_GRACE_SECONDS: Final = 30
CAP_SECONDS: Final = 1800
# A clear with nothing queued gets no confirmation, so the wait for the viewer's first
# loud frame ends well inside the sentence's playing time.
CLEAR_WAIT_SHARE: Final = 0.5
_SENTENCE_COLUMNS: Final = ("audio_ms", "frame_count", "late_frame_count", "outcome")


class GroupOutcome(StrEnum):
    """How far a group's process has come, as its fragment says."""

    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class Drive:
    """What a cell drives: the service, its identity and the audio to speak."""

    service: Service
    identity: Identity
    audio: Audio
    dry_run: bool
    record: Path
    written: list[Path] = field(default_factory=list)

    def beside(self, suffix: str) -> Path:
        """A path beside the record, named after it."""
        return _beside(self.record, suffix)

    def session(self, look: str) -> AbstractAsyncContextManager[Session]:
        """An opened session on `look`."""
        return opened_session(self.service, self.identity, look)


def _beside(record: Path, suffix: str) -> Path:
    return record.with_name(f"{record.stem}.{suffix}")


def _group_log(record: Path, group: str) -> Path:
    """Where the service of `group` writes its log."""
    return _beside(record, f"{group}.log")


def step_figures(line: dict[str, object]) -> dict[str, object]:
    """A load or preparation as the service logged it: outcome, reason, duration."""
    return {
        name: value_of(line[name])
        for name in ("outcome", "reason", "duration_ms")
        if name in line
    }


def _sentence_figures(
    line: dict[str, object], frame_rate: int, *, dry_run: bool
) -> dict[str, object]:
    """What a sentence's own log line says, against its audio's length."""
    audio_ms = int(line["audio_ms"])
    return (
        {name: value_of(line[name]) for name in _SENTENCE_COLUMNS}
        | {"frames_the_audio_needs": value_of(audio_ms * frame_rate // 1000)}
        | {
            "first_output_ms": judged(
                float(line["first_output_ms"]), FIRST_OUTPUT_LINE, dry_run=dry_run
            )
        }
    )


async def _spoken(drive: Drive, session: Session, name: str) -> dict[str, object]:
    trace_id = await session.speak(name, drive.audio)
    await session.until(OutputAudioBufferStopped)
    line = await drive.service.line("sentence.ended", trace_id=trace_id)
    return {"sentence": name} | _sentence_figures(
        line, session.created.session.video.frame_rate, dry_run=drive.dry_run
    )


async def _sentences(drive: Drive) -> object:
    """Two sentences of one session on a look, with idle between them."""
    async with drive.session(LOOK_NAMES[0]) as session:
        first = await _spoken(drive, session, "first")
        await asyncio.sleep(IDLE_SECONDS)
        second = await _spoken(drive, session, "second")
        return {
            "look": LOOK_NAMES[0],
            "opening_ms": value_of(session.opening_ms),
            "sentences": [first, second],
        }


async def _clear(drive: Drive) -> object:
    """A clear inside a sentence, watched by a viewer: confirmation and silence.

    The clear waits for the viewer's first loud audio frame, since the viewer's audio
    arrives after the sentence has started, for at most `CLEAR_WAIT_SHARE` of the
    audio's length.
    """
    async with (
        drive.session(LOOK_NAMES[0]) as session,
        attached(drive.service, drive.identity, session.created) as viewer,
    ):
        trace_id = await session.speak("cleared", drive.audio)
        await session.until(OutputAudioBufferStarted)
        await viewer.heard_loud_audio(
            within_seconds=drive.audio.milliseconds * CLEAR_WAIT_SHARE / 1000
        )
        started = time.monotonic()
        await session.clear()
        cleared = await session.until(OutputAudioBufferCleared)
        confirmed_ms = (time.monotonic() - started) * 1000
        line = await drive.service.line("sentence.ended", trace_id=trace_id)
        await asyncio.sleep(SETTLE_SECONDS)
        return {
            "clear_to_confirmation_ms": value_of(confirmed_ms),
            "clear_to_last_loud_frame_ms": clear_figure(
                viewer, started, dry_run=drive.dry_run
            ),
            "audio_played_ms": value_of(cleared.audio_end_ms),
            "outcome": value_of(line["outcome"]),
        }


async def _steady(drive: Drive) -> Window:
    """The window of idle picture after a sentence, once its last frames are in."""
    await asyncio.sleep(SETTLE_SECONDS)
    window, _ = await timed(drive.service, asyncio.sleep(STEADY_SECONDS))
    return window


async def _viewer(drive: Drive) -> object:
    """One recording viewer through a sentence, the idle after it and another."""
    recording = Recording(drive.beside("viewer.mp4"), drive.beside("viewer-sheet"))
    async with drive.session(LOOK_NAMES[0]) as session:
        async with attached(
            drive.service, drive.identity, session.created, recording
        ) as viewer:
            sentence, _ = await timed(drive.service, _spoken(drive, session, "first"))
            idle = await _steady(drive)
            await _spoken(drive, session, "second")
        frame_rate = session.created.session.video.frame_rate
    drive.written.extend([recording.video, *viewer.sheets])
    return {
        "look": LOOK_NAMES[0],
        "viewer": figures(
            viewer,
            sentence=sentence,
            idle=idle,
            frame_rate=frame_rate,
            dry_run=drive.dry_run,
        ),
        "service_cpu_cores": {"one_viewer": value_of(idle.cores)},
        "files": {
            "video": value_of(recording.video.name),
            "sheets": value_of([sheet.name for sheet in viewer.sheets]),
        },
    }


async def _viewers(drive: Drive) -> object:
    """Five viewers of one session: each one's figures, the CPU against none."""
    async with drive.session(LOOK_NAMES[0]) as session:
        frame_rate = session.created.session.video.frame_rate
        none = await _steady(drive)
        async with AsyncExitStack() as stack:
            watchers = [
                await stack.enter_async_context(
                    attached(drive.service, drive.identity, session.created)
                )
                for _ in range(FIVE_VIEWERS)
            ]
            sentence, _ = await timed(drive.service, _spoken(drive, session, "first"))
            idle = await _steady(drive)
    return {
        "viewers": [
            figures(
                watcher,
                sentence=sentence,
                idle=idle,
                frame_rate=frame_rate,
                dry_run=drive.dry_run,
            )
            for watcher in watchers
        ],
        "service_cpu_cores": {
            "no_viewers": value_of(none.cores),
            "five_viewers": value_of(idle.cores),
        },
        "encoders_cores": judged(
            idle.cores - none.cores, ENCODER_CORES_LINE, dry_run=drive.dry_run
        ),
    }


async def _looks(drive: Drive) -> object:
    """One session on each look in sequence, each recorded under its look."""
    sessions = []
    for look in LOOK_NAMES:
        async with drive.session(look) as session:
            sessions.append(
                {"look": look, "opening_ms": value_of(session.opening_ms)}
                | await _spoken(drive, session, look)
            )
    return sessions


async def _lag(drive: Drive) -> object:
    """A provider that cannot answer within a lag far too small ends the session.

    The worker is held once the session is open, so the outcome does not wait for the
    provider to be slower than the lag by chance.
    """
    code = None
    try:
        async with drive.session(LOOK_NAMES[0]) as session:
            hold_workers(drive.service.process.pid)
            await session.until(OutputAudioBufferStarted)
    except SessionEndedError as ended:
        code = ended.code
    return {
        "max_lag_ms": value_of(TOO_SMALL_LAG_MS),
        "ended_unavailable": value_of(code == RefusalCode.UNAVAILABLE),
    }


# A group is one service; the lag cell needs a service of its own.
GROUPS: Final = {
    "wire": {
        "sentences": _sentences,
        "clear": _clear,
        "looks": _looks,
        "viewer": _viewer,
        "viewers": _viewers,
    },
    "lag": {"lag": _lag},
}
LAGS: Final = {"wire": DEFAULT_LAG_MS, "lag": TOO_SMALL_LAG_MS}
CELLS: Final = tuple(cell for group in GROUPS.values() for cell in group)
_PROVIDER_FIELDS: Final = ("provider_root", "card_memory_mb")


def _non_negative(text: str) -> int:
    """A whole number of at least 0, as an argument."""
    try:
        number = int(text)
    except ValueError:
        message = f"{text!r} is not a whole number"
        raise argparse.ArgumentTypeError(message) from None
    if number < 0:
        message = f"{number} is below 0"
        raise argparse.ArgumentTypeError(message)
    return number


def _arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True, help="the record's path")
    parser.add_argument("--audio", type=Path, required=True, help="a mono 16-bit WAV")
    parser.add_argument(
        "--dry-run", action="store_true", help="drive the fake provider"
    )
    parser.add_argument("--cells", nargs="+", choices=CELLS, default=list(CELLS))
    parser.add_argument(
        "--memory-floor-mb",
        type=_non_negative,
        default=int(FREE_MEMORY_LINE.limit),
        help="free card memory in MiB the run keeps beside the provider's cap",
    )
    parser.add_argument("--fragment", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _checked_record(path: Path) -> Path:
    """The record's absolute path, once it names a file in a writable directory."""
    record = path.resolve()
    directory = record.parent
    if record.is_dir() or not (
        directory.is_dir() and os.access(directory, os.W_OK | os.X_OK)
    ):
        message = (
            f"--record {path}: it must name a file in an existing writable directory"
        )
        raise SystemExit(message)
    return record


class _GivenSettings(Settings):
    """`Settings` judged from the values given alone, never from the environment."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[Settings],  # noqa: ARG003 - the signature pydantic-settings calls
        init_settings: PydanticBaseSettingsSource,
        **_other_sources: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


@dataclass(frozen=True)
class CardProvider:
    """The operator's provider settings, as the service gets them, and the cap."""

    environment: dict[str, str]
    card_memory_mb: int


def _card_provider(environ: Mapping[str, str]) -> CardProvider:
    """The operator's two provider settings, once the service's own fields accept them.

    The refusal names the setting alone: the validation error carries the value.
    """
    variables = {
        name: f"{ENVIRONMENT_PREFIX}{name.upper()}" for name in _PROVIDER_FIELDS
    }
    given = {
        name: environ[variable]
        for name, variable in variables.items()
        if variable in environ
    }
    if "provider_root" not in given:
        message = f"{variables['provider_root']} is required without --dry-run"
        raise SystemExit(message)
    try:
        settings = _GivenSettings(provider=ProviderName.FLASHHEAD_LITE, **given)
    except ValidationError as error:
        field_name = str(error.errors()[0]["loc"][0])
        message = f"{ENVIRONMENT_PREFIX}{field_name.upper()} is missing or invalid"
        raise SystemExit(message) from None
    environment = {PROVIDER_VARIABLE: ProviderName.FLASHHEAD_LITE.value} | {
        variables[name]: value for name, value in given.items()
    }
    return CardProvider(environment, settings.card_memory_mb)


def _provider_of(arguments: argparse.Namespace) -> Mapping[str, str]:
    """The `VIDEO_` variables that choose the provider the service runs."""
    if arguments.dry_run:
        return FAKE_PROVIDER
    return _card_provider(os.environ).environment


def _service_figures(
    service: Service, log: Path, *, dry_run: bool
) -> dict[str, object]:
    """The service's start, load and preparations; its stop is added once it ends."""
    return {
        "start_to_ready_ms": judged(
            service.start_to_ready_ms, START_TO_READY_LINE, dry_run=dry_run
        ),
        "provider_load": step_figures(service.lines("provider_load.ended")[0]),
        "look_preparations": [
            {"look": value_of(line["look"])} | step_figures(line)
            for line in service.lines("look_preparation.ended")
        ],
        "log": value_of(log.name),
    }


def _stop_figure(service: Service) -> dict[str, object]:
    """How long the service took to exit after its stop, if it was still running."""
    if service.stop_to_exit_ms is None:
        return not_measured(Reason.EXITED_BEFORE_STOP)
    return value_of(service.stop_to_exit_ms)


def _chosen(group: str, arguments: argparse.Namespace) -> list[str]:
    """The cells of `group` the command was asked for, in the group's order."""
    return [name for name in GROUPS[group] if name in arguments.cells]


def _write_fragment(path: Path, fragment: dict[str, object]) -> None:
    """Write `fragment` whole: to a temporary file, renamed into place."""
    temporary = path.with_suffix(".tmp")
    # The parent names the fragment's path in its own scratch directory.
    temporary.write_text(json.dumps(fragment))  # NOSONAR(S8707)
    temporary.replace(path)


def _run_group(
    group: str,
    arguments: argparse.Namespace,
    scratch: Path,
    start_service: Callable[..., AbstractContextManager[Service]] = running_service,
) -> GroupOutcome:
    """Drive the cells of `group` through one service, keeping its fragment current.

    The fragment is rewritten after each cell and once more when the group ends. A cell
    that raises an `Exception` ends the group: it is `cell_failed`, the cells after it
    `group_ended`, and the fragment holds both before the service stops. Anything
    else, an interrupt's `SystemExit` included, is not caught.
    """
    audio = read_audio(arguments.audio)
    names = _chosen(group, arguments)
    log = _group_log(arguments.record, group)
    cells: dict[str, object] = {}
    with (
        throwaway_identity(scratch) as identity,
        start_service(
            identity, log, max_lag_ms=LAGS[group], provider=_provider_of(arguments)
        ) as service,
    ):
        drive = Drive(
            service, identity, audio, dry_run=arguments.dry_run, record=arguments.record
        )
        figures = _service_figures(service, log, dry_run=arguments.dry_run)

        def save(outcome: GroupOutcome) -> None:
            _write_fragment(
                arguments.fragment,
                {
                    "outcome": outcome,
                    "cells": cells,
                    "services": {group: figures},
                    "files": [path.name for path in drive.written],
                },
            )

        async def run_cells() -> GroupOutcome:
            for position, name in enumerate(names):
                try:
                    cells[name] = await GROUPS[group][name](drive)
                except Exception:  # noqa: BLE001 - the failure is the cell's record entry
                    traceback.print_exc()
                    cells[name] = not_measured(Reason.CELL_FAILED)
                    cells.update(
                        completed_cells(names[position + 1 :], {}, Reason.GROUP_ENDED)
                    )
                    save(GroupOutcome.RUNNING)
                    return GroupOutcome.FAILED
                save(GroupOutcome.RUNNING)
            return GroupOutcome.DONE

        outcome = asyncio.run(run_cells())
    figures["stop_to_exit_ms"] = _stop_figure(service)
    save(outcome)
    return outcome


def _child(group: str, arguments: argparse.Namespace) -> int:
    """Run one group where `probe-run` started it; non-zero when a cell failed."""
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, exit_on_stop)
    pid_file(arguments.fragment).write_text(str(os.getpid()))  # NOSONAR(S8707)
    # The identity's keys live in the top's temporary directory, which the top removes
    # also when it kills this process.
    identity_directory = arguments.fragment.with_suffix(".identity")
    identity_directory.mkdir()
    outcome = _run_group(group, arguments, identity_directory)
    return 0 if outcome is GroupOutcome.DONE else 1


def _under_probe_run(
    group: str,
    arguments: argparse.Namespace,
    fragment: Path,
    supervision: Supervision,
) -> int | None:
    """Run `group` as its own process under `probe-run`; its exit status.

    A card-mode group the card has no room for is refused before `probe-run` starts, and
    so is a group after a stop that arrived before it: nothing would forward that stop,
    and the group would run through. Neither has a status.
    """
    for tool in (PROBE_RUN, "setpriv"):
        if shutil.which(tool) is None:
            message = f"{tool} is not on PATH; the proof does not run without it"
            raise SystemExit(message)
    watch = supervision.watch
    if watch is not None and watch.refuse_first(
        ending(supervision.stops, supervision.control, None)
    ):
        return None
    if supervision.stops.recorded:
        return None
    command = [
        PROBE_RUN,
        "-k",
        str(KILL_GRACE_SECONDS),
        str(CAP_SECONDS),
        sys.executable,
        str(Path(__file__).resolve()),
        "--record",
        str(arguments.record),
        "--fragment",
        str(fragment),
        "--audio",
        str(arguments.audio),
        "--cells",
        *_chosen(group, arguments),
        *(["--dry-run"] if arguments.dry_run else []),
    ]
    try:
        return run_child(command, group, fragment, supervision)
    finally:
        if (log := _group_log(arguments.record, group)).exists():
            print(f"beside it: {log}")


def _run_group_process(
    group: str,
    arguments: argparse.Namespace,
    fragment: Path,
    control: ProcessControl,
    watch: GroupWatch | None,
) -> tuple[int | None, Kill]:
    """Run `group`, then end whatever is left below the top: its status and the kill.

    A stop that arrives while the group runs or the kill runs ends the command as an
    interrupt does once the kill has ended; otherwise the handlers the top had come
    back for the next group. After the top ended the group for its memory the stops are
    ignored: the command ends with that failure.
    """
    before = [signal.getsignal(stopping) for stopping in STOP_SIGNALS]
    stops = Stops()
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, stops.record)
    status = _under_probe_run(
        group, arguments, fragment, Supervision(stops, control, watch)
    )
    if watch is None:
        kill = end_processes_below(control)
    else:
        kill = watch.finish(lambda: end_processes_below(control))
    # A stop after the handlers are back reaches the top's own handler; one before is
    # in `stops`, which is read after.
    for stopping, handler in zip(STOP_SIGNALS, before, strict=True):
        signal.signal(stopping, signal.SIG_IGN if stops.ending else handler)
    if (interrupt := stops.to_act_on) is not None:
        _interrupted(interrupt, None)
    return status, kill


@dataclass
class _Gathered:
    """What the groups' fragments add up to, and why no group follows the last."""

    cells: dict[str, object] = field(default_factory=dict)
    services: dict[str, object] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    failure: str | None = None
    stopped: bool = False
    later: Reason = Reason.EARLIER_GROUP_FAILED

    @property
    def ended(self) -> bool:
        """Whether a group failed or was stopped, so no later group starts."""
        return self.failure is not None or self.stopped


def _read_fragment(path: Path) -> dict[str, object]:
    """The fragment a group's process wrote, or none when it wrote none."""
    return json.loads(path.read_text()) if path.exists() else {}


def _kill_figures(kill: Kill) -> dict[str, object]:
    """The kill's two counts, or both not measured when a kill raised."""
    if kill.failed:
        return {name: not_measured(Reason.KILL_FAILED) for name in KILL_FIGURES}
    counts = (kill.processes_left, kill.processes_alive)
    return {
        name: value_of(count) for name, count in zip(KILL_FIGURES, counts, strict=True)
    }


@dataclass(frozen=True)
class CardLimits:
    """What card mode holds the card to: the provider's cap and the floor."""

    cap_mb: int
    floor_mb: int


def _gather(
    group: str,
    arguments: argparse.Namespace,
    scratch: Path,
    gathered: _Gathered,
    control: ProcessControl,
) -> None:
    """Run `group` unless a group failed or was stopped; add its fragment's figures.

    A group that ended without a fragment or one it did not end is failed: what its
    fragment lacks is `no_fragment`; a group never started is `earlier_group_failed`.
    A group the top ended for the card's memory is stopped: what it lacks is
    `memory_refused` or `watchdog_breach`, as is every later group.
    """
    parts: dict[str, object] = {}
    top_figures: dict[str, object] = {}
    reason = gathered.later
    unfinished: tuple[Reason, ...] = ()
    if not gathered.ended:
        fragment = scratch / f"{group}.json"
        limits = arguments.limits
        watch = (
            None
            if limits is None
            else GroupWatch(read_card_memory, limits.cap_mb, limits.floor_mb)
        )
        status, kill = _run_group_process(group, arguments, fragment, control, watch)
        parts = _read_fragment(fragment)
        reason = Reason.NO_FRAGMENT
        top_figures = _kill_figures(kill) | (
            {name: not_measured(Reason.NO_CARD) for name in MEMORY_FIGURES}
            if watch is None
            else watch.entry()
        )
        if watch is not None and watch.stop is not None:
            reason = gathered.later = watch.stop.cell_reason
            # A cell the stop failed may have left later cells `group_ended`.
            unfinished = (Reason.GROUP_ENDED,)
            gathered.stopped = True
        if kill.failed:
            gathered.failure = (
                f"the {group} group left a process below the top that was not ended"
            )
        elif not gathered.stopped and (
            status or parts.get("outcome") != GroupOutcome.DONE
        ):
            gathered.failure = f"the {group} cells ended with status {status}"
    gathered.cells |= completed_cells(
        _chosen(group, arguments), parts.get("cells", {}), reason, replacing=unfinished
    )
    gathered.services[group] = completed_service(
        parts.get("services", {}).get(group, {}) | top_figures, reason
    )
    gathered.files += parts.get("files", [])


def _interrupted(signum: int, _frame: object) -> None:
    """End the command with one line; a second stop is ignored."""
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, signal.SIG_IGN)
    message = f"interrupted by {signal.Signals(signum).name}"
    raise SystemExit(message)


def main(argv: Sequence[str] | None = None, control: ProcessControl = KERNEL) -> int:
    """Run the chosen cells and write the record.

    Without `--dry-run` the operator's provider settings are checked first, and the
    card is held to the floor. `control` is what the end-of-group kill asks of the
    kernel.
    """
    arguments = _arguments(sys.argv[1:] if argv is None else argv)
    if group := os.environ.get(CHILD_MARKER):
        return _child(group, arguments)
    arguments.record = _checked_record(arguments.record)
    read_audio(arguments.audio)
    arguments.limits = (
        None
        if arguments.dry_run
        else CardLimits(
            _card_provider(os.environ).card_memory_mb, arguments.memory_floor_mb
        )
    )
    try:
        control.become_subreaper()
    except OSError as error:
        message = f"the command cannot become a child subreaper ({error.strerror})"
        raise SystemExit(message) from None
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, _interrupted)
    gathered = _Gathered()
    with tempfile.TemporaryDirectory(prefix="prove-card-record-") as scratch:
        for group, members in GROUPS.items():
            if set(members) & set(arguments.cells):
                _gather(group, arguments, Path(scratch), gathered, control)
    record = closed_record(
        dry_run=arguments.dry_run, cells=gathered.cells, services=gathered.services
    )
    # The operator names the record's path; writing it is this command's job.
    arguments.record.write_text(json.dumps(record, indent=2) + "\n")  # NOSONAR(S8707)
    print(f"record written to {arguments.record}")
    for name in gathered.files:
        print(f"beside it: {arguments.record.with_name(name)}")
    if gathered.failure:
        raise SystemExit(gathered.failure)
    return 1 if gathered.stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
