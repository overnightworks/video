"""The card proof: drive the service as an app does and write a closed record.

`--dry-run` drives the `fake` provider, so every verdict that needs a card stays
`not_measured`. Each group of cells runs as one service under `probe-run`, which
this command calls itself; the record is written once every group has ended, and
the recording, its contact sheets and each service's log are written beside it.
"""

# ruff: noqa: T201 - the command reports its result on the command line

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from card_drive import (
    Audio,
    SessionEndedError,
    Window,
    opened_session,
    read_audio,
    running_service,
    timed,
)
from card_identity import LOOK_NAMES, throwaway_identity
from card_record import (
    ENCODER_CORES_LINE,
    FIRST_OUTPUT_LINE,
    START_TO_READY_LINE,
    closed_record,
    judged,
    value_of,
)
from card_viewer import Recording, attached, clear_figure, figures
from video_client.session_wire import (
    OutputAudioBufferCleared,
    OutputAudioBufferStarted,
    OutputAudioBufferStopped,
)
from video_client.wire import RefusalCode

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractAsyncContextManager

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
_CHILD_MARKER: Final = "PROVE_CARD_GROUP"
_SENTENCE_COLUMNS: Final = ("audio_ms", "frame_count", "late_frame_count", "outcome")
_STOPS: Final = (signal.SIGINT, signal.SIGTERM)
_SIGNAL_BASE: Final = 128


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
        return self.record.with_name(f"{self.record.stem}.{suffix}")

    def session(self, look: str) -> AbstractAsyncContextManager[Session]:
        """An opened session on `look`."""
        return opened_session(self.service, self.identity, look)


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
    """A clear inside a sentence, watched by a viewer: confirmation and silence."""
    async with (
        drive.session(LOOK_NAMES[0]) as session,
        attached(drive.service, drive.identity, session.created) as viewer,
    ):
        trace_id = await session.speak("cleared", drive.audio)
        await session.until(OutputAudioBufferStarted)
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
    """A lag far too small ends the session as unavailable."""
    code = None
    try:
        async with drive.session(LOOK_NAMES[0]) as session:
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
_REFUSAL: Final = (
    "without --dry-run the card's cells are needed, and none exists yet: card "
    "memory, the watchdog, the lag sweep and the voice's window come with the next "
    "slices (the viewers are driven in the dry run already); "
    "run with --dry-run to drive the fake provider"
)


def _arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True, help="the record's path")
    parser.add_argument("--audio", type=Path, required=True, help="a mono 16-bit WAV")
    parser.add_argument(
        "--dry-run", action="store_true", help="drive the fake provider"
    )
    parser.add_argument("--cells", nargs="+", choices=CELLS, default=list(CELLS))
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


def _run_group(
    group: str, arguments: argparse.Namespace, scratch: Path
) -> dict[str, object]:
    """Drive the cells of `group` through one service; its fragment of the record."""
    audio = read_audio(arguments.audio)
    names = [name for name in GROUPS[group] if name in arguments.cells]
    with (
        throwaway_identity(scratch) as identity,
        running_service(identity, scratch, max_lag_ms=LAGS[group]) as service,
    ):
        drive = Drive(
            service, identity, audio, dry_run=arguments.dry_run, record=arguments.record
        )

        async def run_cells() -> dict[str, object]:
            return {name: await GROUPS[group][name](drive) for name in names}

        cells = asyncio.run(run_cells())
    log = drive.beside(f"{group}.log")
    shutil.copyfile(service.log, log)
    return {
        "cells": cells,
        "services": {
            group: {
                "start_to_ready_ms": judged(
                    service.start_to_ready_ms,
                    START_TO_READY_LINE,
                    dry_run=arguments.dry_run,
                ),
                "stop_to_exit_ms": value_of(service.stop_to_exit_ms),
                "log": value_of(log.name),
            }
        },
        "files": [path.name for path in [*drive.written, log]],
    }


def _exit_once(signum: int, _frame: object) -> None:
    """Turn the first stop into an exit that runs the cleanups; ignore a second."""
    for stopping in _STOPS:
        signal.signal(stopping, signal.SIG_IGN)
    sys.exit(_SIGNAL_BASE + signum)


def _child(group: str, arguments: argparse.Namespace) -> int:
    """Run one group where `probe-run` started it, and write its fragment."""
    for stopping in _STOPS:
        signal.signal(stopping, _exit_once)
    with tempfile.TemporaryDirectory(prefix="prove-card-") as scratch:
        fragment = _run_group(group, arguments, Path(scratch))
    # The parent names the fragment's path in its own scratch directory.
    arguments.fragment.write_text(json.dumps(fragment))  # NOSONAR(S8707)
    return 0


def _under_probe_run(group: str, arguments: argparse.Namespace, fragment: Path) -> None:
    """Run `group` as its own process under `probe-run`; a failure ends the proof."""
    if shutil.which(PROBE_RUN) is None:
        message = f"{PROBE_RUN} is not on PATH; the proof does not run without it"
        raise SystemExit(message)
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
        *[name for name in GROUPS[group] if name in arguments.cells],
        *(["--dry-run"] if arguments.dry_run else []),
    ]
    interrupts: list[int] = []

    def forward(signum: int, _frame: object) -> None:
        interrupts.append(signum)
        child.send_signal(signum)

    # The operator names the audio path that the command passes on.
    with subprocess.Popen(  # NOSONAR(S8705)  # noqa: S603 - probe-run and this python
        command, env={**os.environ, _CHILD_MARKER: group}
    ) as child:
        # The group's service is cleaned up by the child, so a stop reaches it first.
        previous = [signal.signal(stopping, forward) for stopping in _STOPS]
        status = child.wait()
        for stopping, handler in zip(_STOPS, previous, strict=True):
            signal.signal(stopping, handler)
    if interrupts:
        message = f"interrupted by {signal.Signals(interrupts[0]).name}"
        raise SystemExit(message)
    if status:
        message = f"the {group} cells ended with status {status}"
        raise SystemExit(message)


def _interrupted(signum: int, _frame: object) -> None:
    """End the command with one line; a second stop is ignored."""
    for stopping in _STOPS:
        signal.signal(stopping, signal.SIG_IGN)
    message = f"interrupted by {signal.Signals(signum).name}"
    raise SystemExit(message)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the chosen cells and write the record; refuse without `--dry-run`."""
    arguments = _arguments(sys.argv[1:] if argv is None else argv)
    if group := os.environ.get(_CHILD_MARKER):
        return _child(group, arguments)
    arguments.record = _checked_record(arguments.record)
    read_audio(arguments.audio)
    if not arguments.dry_run:
        raise SystemExit(_REFUSAL)
    for stopping in _STOPS:
        signal.signal(stopping, _interrupted)
    cells: dict[str, object] = {}
    services: dict[str, object] = {}
    files: list[str] = []
    with tempfile.TemporaryDirectory(prefix="prove-card-record-") as scratch:
        for group, members in GROUPS.items():
            if not set(members) & set(arguments.cells):
                continue
            fragment = Path(scratch) / f"{group}.json"
            _under_probe_run(group, arguments, fragment)
            parts = json.loads(fragment.read_text())
            cells |= parts["cells"]
            services |= parts["services"]
            files += parts["files"]
    record = closed_record(dry_run=arguments.dry_run, cells=cells, services=services)
    # The operator names the record's path; writing it is this command's job.
    arguments.record.write_text(json.dumps(record, indent=2) + "\n")  # NOSONAR(S8707)
    print(f"record written to {arguments.record}")
    for name in files:
        print(f"beside it: {arguments.record.with_name(name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
