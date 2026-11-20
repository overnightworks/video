"""The card proof: drive the service as an app does and write a closed record.

`--dry-run` drives the `fake` provider, so every verdict that needs a card stays
`not_measured`. Each group of cells runs as one service under `probe-run`, which
this command calls itself; the record is written once every group has ended.
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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from card_drive import (
    Audio,
    SessionEndedError,
    opened_session,
    read_audio,
    running_service,
)
from card_identity import LOOK_NAMES, throwaway_identity
from card_record import (
    CLEAR_TO_SILENCE_LINE,
    FIRST_OUTPUT_LINE,
    START_TO_READY_LINE,
    closed_record,
    judged,
    value_of,
)
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
TOO_SMALL_LAG_MS: Final = 1
DEFAULT_LAG_MS: Final = 500
PROBE_RUN: Final = "probe-run"
KILL_GRACE_SECONDS: Final = 30
CAP_SECONDS: Final = 1800
_CHILD_MARKER: Final = "PROVE_CARD_GROUP"
_SENTENCE_COLUMNS: Final = ("audio_ms", "frame_count", "late_frame_count", "outcome")
_TERMINATED: Final = 128 + signal.SIGTERM


@dataclass(frozen=True)
class Drive:
    """What a cell drives: the service, its identity and the audio to speak."""

    service: Service
    identity: Identity
    audio: Audio
    dry_run: bool

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
    """A clear inside a sentence: the time to its confirmation."""
    async with drive.session(LOOK_NAMES[0]) as session:
        trace_id = await session.speak("cleared", drive.audio)
        await session.until(OutputAudioBufferStarted)
        started = time.monotonic()
        await session.clear()
        cleared = await session.until(OutputAudioBufferCleared)
        confirmed_ms = (time.monotonic() - started) * 1000
        line = await drive.service.line("sentence.ended", trace_id=trace_id)
        return {
            "clear_to_confirmation_ms": judged(
                confirmed_ms, CLEAR_TO_SILENCE_LINE, dry_run=drive.dry_run
            ),
            "audio_played_ms": value_of(cleared.audio_end_ms),
            "outcome": value_of(line["outcome"]),
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
    "wire": {"sentences": _sentences, "clear": _clear, "looks": _looks},
    "lag": {"lag": _lag},
}
LAGS: Final = {"wire": DEFAULT_LAG_MS, "lag": TOO_SMALL_LAG_MS}
CELLS: Final = tuple(cell for group in GROUPS.values() for cell in group)
_REFUSAL: Final = (
    "without --dry-run the card's cells are needed, and none exists yet: viewers, card "
    "memory, the lag sweep and the voice's window come with the next slices; "
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
    return parser.parse_args(argv)


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
        drive = Drive(service, identity, audio, dry_run=arguments.dry_run)

        async def run_cells() -> dict[str, object]:
            return {name: await GROUPS[group][name](drive) for name in names}

        cells = asyncio.run(run_cells())
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
            }
        },
    }


def _exit_once(*_: object) -> None:
    """Turn the first TERM into an exit that runs the cleanups; ignore a second."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.exit(_TERMINATED)


def _child(group: str, arguments: argparse.Namespace) -> int:
    """Run one group where `probe-run` started it, and write its fragment."""
    signal.signal(signal.SIGTERM, _exit_once)
    with tempfile.TemporaryDirectory(prefix="prove-card-") as scratch:
        fragment = _run_group(group, arguments, Path(scratch))
    arguments.record.write_text(json.dumps(fragment))
    return 0


def _under_probe_run(group: str, arguments: argparse.Namespace, fragment: Path) -> None:
    """Run `group` as its own process under `probe-run`; a failure ends the proof."""
    if shutil.which(PROBE_RUN) is None:
        message = f"{PROBE_RUN} is not on PATH; the proof does not run without it"
        raise SystemExit(message)
    command = [
        PROBE_RUN, "-k", str(KILL_GRACE_SECONDS), str(CAP_SECONDS),
        sys.executable, str(Path(__file__).resolve()),
        "--record", str(fragment), "--audio", str(arguments.audio),
        "--cells", *[name for name in GROUPS[group] if name in arguments.cells],
        *(["--dry-run"] if arguments.dry_run else []),
    ]  # fmt: skip
    with subprocess.Popen(  # noqa: S603 - the fleet's probe-run and this interpreter
        command, env={**os.environ, _CHILD_MARKER: group}
    ) as child:
        # The group's service is cleaned up by the child, so a stop reaches it first.
        stopping = signal.signal(
            signal.SIGTERM, lambda *_: child.send_signal(signal.SIGTERM)
        )
        status = child.wait()
        signal.signal(signal.SIGTERM, stopping)
    if status:
        message = f"the {group} cells ended with status {status}"
        raise SystemExit(message)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the chosen cells and write the record; refuse without `--dry-run`."""
    arguments = _arguments(sys.argv[1:] if argv is None else argv)
    if group := os.environ.get(_CHILD_MARKER):
        return _child(group, arguments)
    if not arguments.dry_run:
        raise SystemExit(_REFUSAL)
    signal.signal(signal.SIGTERM, _exit_once)
    cells: dict[str, object] = {}
    services: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="prove-card-record-") as scratch:
        for group, members in GROUPS.items():
            if not set(members) & set(arguments.cells):
                continue
            fragment = Path(scratch) / f"{group}.json"
            _under_probe_run(group, arguments, fragment)
            parts = json.loads(fragment.read_text())
            cells |= parts["cells"]
            services |= parts["services"]
    record = closed_record(dry_run=arguments.dry_run, cells=cells, services=services)
    arguments.record.write_text(json.dumps(record, indent=2) + "\n")
    print(f"record written to {arguments.record}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
