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
import traceback
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

from pydantic import ValidationError

from card_cells import (
    CELLS,
    GROUPS,
    LAGS,
    Drive,
    beside,
)
from card_drive import (
    ENVIRONMENT_PREFIX,
    FAKE_PROVIDER,
    KERNEL,
    PROVIDER_VARIABLE,
    STOP_SIGNALS,
    Kill,
    ProcessControl,
    end_processes_below,
    exit_on_stop,
    read_audio,
    running_service,
)
from card_identity import throwaway_identity
from card_memory import GroupWatch, peaks_of_a_failed_group
from card_record import (
    FREE_MEMORY_LINE,
    KILL_FIGURES,
    MEMORY_FIGURES,
    CompilerHome,
    Reason,
    closed_record,
    completed_cells,
    completed_service,
    failed_load,
    not_measured,
    service_figures,
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
from video_service.card import read_card_memory
from video_service.config import Settings
from video_service.providers import ProviderName

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from pydantic_settings import PydanticBaseSettingsSource

    from card_drive import Service

PROBE_RUN: Final = "probe-run"
KILL_GRACE_SECONDS: Final = 30
CAP_SECONDS: Final = 1800


class GroupOutcome(StrEnum):
    """How far a group's process has come, as its fragment says."""

    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class _Group(NamedTuple):
    """One service of the run: its name, the cells it drives and its lag."""

    name: str
    kind: str
    lag_ms: int


def _group_log(record: Path, group: str) -> Path:
    """Where the service of `group` writes its log."""
    return beside(record, f"{group}.log")


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


def _stop_figure(service: Service) -> dict[str, object]:
    """How long the service took to exit after its stop, if it was still running."""
    if service.stop_to_exit_ms is None:
        return not_measured(Reason.EXITED_BEFORE_STOP)
    return value_of(service.stop_to_exit_ms)


def _groups(arguments: argparse.Namespace) -> list[_Group]:
    """The groups the command was asked for, in the order they run."""
    return [
        _Group(kind, kind, LAGS[kind])
        for kind, members in GROUPS.items()
        if set(members) & set(arguments.cells)
    ]


def _chosen(group: _Group, arguments: argparse.Namespace) -> list[str]:
    """The cells of `group` the command was asked for, in the group's order."""
    return [name for name in GROUPS[group.kind] if name in arguments.cells]


def _write_fragment(path: Path, fragment: dict[str, object]) -> None:
    """Write `fragment` whole: to a temporary file, renamed into place."""
    temporary = path.with_suffix(".tmp")
    # The parent names the fragment's path in its own scratch directory.
    temporary.write_text(json.dumps(fragment))  # NOSONAR(S8707)
    temporary.replace(path)


async def _drive_cells(
    group: _Group,
    names: list[str],
    drive: Drive,
    cells: dict[str, object],
    save: Callable[[GroupOutcome], None],
) -> GroupOutcome:
    """Drive `names` in turn, saving after each; a cell that raises ends the group."""
    for position, name in enumerate(names):
        try:
            cells[name] = await GROUPS[group.kind][name](drive)
        except Exception:  # noqa: BLE001 - the failure is the cell's record entry
            traceback.print_exc()
            cells[name] = not_measured(Reason.CELL_FAILED)
            cells.update(completed_cells(names[position + 1 :], {}, Reason.GROUP_ENDED))
            save(GroupOutcome.RUNNING)
            return GroupOutcome.FAILED
        save(GroupOutcome.RUNNING)
    return GroupOutcome.DONE


def _run_group(
    group: _Group,
    arguments: argparse.Namespace,
    scratch: Path,
    compiler_home: Path,
    start_service: Callable[..., AbstractContextManager[Service]] = running_service,
) -> GroupOutcome:
    """Drive the cells of `group` through one service, keeping its fragment current.

    The fragment is rewritten after each cell and once more when the group ends. A cell
    that raises an `Exception` ends the group: it is `cell_failed`, the cells after it
    `group_ended`, and the fragment holds both before the service stops. Anything
    else, an interrupt's `SystemExit` included, is not caught. A service whose provider
    did not load or whose look was not prepared runs no cell: every cell is not
    measured for that, and one line on standard error says which step failed.
    """
    audio = read_audio(arguments.audio)
    names = _chosen(group, arguments)
    log = _group_log(arguments.record, group.name)
    cells: dict[str, object] = {}
    with (
        throwaway_identity(scratch) as identity,
        start_service(
            identity,
            log,
            max_lag_ms=group.lag_ms,
            compiler_home=compiler_home,
            provider=_provider_of(arguments),
        ) as service,
    ):
        drive = Drive(
            service, identity, audio, dry_run=arguments.dry_run, record=arguments.record
        )
        figures = service_figures(service, log, dry_run=arguments.dry_run)

        def save(outcome: GroupOutcome) -> None:
            _write_fragment(
                arguments.fragment,
                {
                    "outcome": outcome,
                    "cells": cells,
                    "services": {group.name: figures},
                    "files": [path.name for path in drive.written],
                },
            )

        if (failure := failed_load(service)) is None:
            outcome = asyncio.run(_drive_cells(group, names, drive, cells, save))
        else:
            reason, message = failure
            cells.update(completed_cells(names, {}, reason))
            figures["start_to_ready_ms"] |= not_measured(reason)
            print(f"the {group.name} group: {message}", file=sys.stderr)
            save(GroupOutcome.RUNNING)
            outcome = GroupOutcome.FAILED
    figures["stop_to_exit_ms"] = _stop_figure(service)
    save(outcome)
    return outcome


def _compiler_home(run_directory: Path) -> Path:
    """The run's one compiler home, in the temporary directory every group shares."""
    return run_directory / "compiler-home"


def _compiler_home_at_start(run_directory: Path) -> CompilerHome:
    """Whether the compiler home holds an entry an earlier service wrote."""
    held = any(_compiler_home(run_directory).iterdir())
    return CompilerHome.FILLED if held else CompilerHome.EMPTY


def _child(name: str, arguments: argparse.Namespace) -> int:
    """Run one group where `probe-run` started it; non-zero when a cell failed."""
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, exit_on_stop)
    pid_file(arguments.fragment).write_text(str(os.getpid()))  # NOSONAR(S8707)
    # The identity's keys live in the top's temporary directory, which the top removes
    # also when it kills this process.
    identity_directory = arguments.fragment.with_suffix(".identity")
    identity_directory.mkdir()
    (group,) = (group for group in _groups(arguments) if group.name == name)
    outcome = _run_group(
        group, arguments, identity_directory, _compiler_home(arguments.fragment.parent)
    )
    return 0 if outcome is GroupOutcome.DONE else 1


class _Probed(NamedTuple):
    """Whether `probe-run` started for a group, and the status it left."""

    started: bool
    status: int | None


def _under_probe_run(
    group: _Group,
    arguments: argparse.Namespace,
    fragment: Path,
    supervision: Supervision,
) -> _Probed:
    """Run `group` as its own process under `probe-run`; whether it started, its status.

    A card-mode group the card has no room for is refused before `probe-run` starts, and
    so is a group after a stop that arrived before it: nothing would forward that stop,
    and the group would run through. Neither has started. A started group has no status
    when it was ended for its memory and `probe-run` outlived the kill.
    """
    for tool in (PROBE_RUN, "setpriv"):
        if shutil.which(tool) is None:
            message = f"{tool} is not on PATH; the proof does not run without it"
            raise SystemExit(message)
    watch = supervision.watch
    if watch is not None and watch.refuse_first(
        ending(supervision.stops, supervision.control, None)
    ):
        return _Probed(started=False, status=None)
    if supervision.stops.recorded:
        return _Probed(started=False, status=None)
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
        return _Probed(
            started=True, status=run_child(command, group.name, fragment, supervision)
        )
    finally:
        if (log := _group_log(arguments.record, group.name)).exists():
            print(f"beside it: {log}")


def _run_group_process(
    group: _Group,
    arguments: argparse.Namespace,
    fragment: Path,
    control: ProcessControl,
    watch: GroupWatch | None,
) -> tuple[_Probed, Kill]:
    """Run `group`, then end what is left below the top: its `probe-run` and the kill.

    A stop that arrives while the group runs or the kill runs ends the command as an
    interrupt does once the kill has ended; otherwise the handlers the top had come
    back for the next group. After the top ended the group for its memory the stops are
    ignored: the command ends with that failure.
    """
    before = [signal.getsignal(stopping) for stopping in STOP_SIGNALS]
    stops = Stops()
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, stops.record)
    probed = _under_probe_run(
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
    return probed, kill


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
    group: _Group,
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
        fragment = scratch / f"{group.name}.json"
        limits = arguments.limits
        watch = (
            None
            if limits is None
            else GroupWatch(read_card_memory, limits.cap_mb, limits.floor_mb)
        )
        compiler_home_at_start = _compiler_home_at_start(scratch)
        probed, kill = _run_group_process(group, arguments, fragment, control, watch)
        status = probed.status
        parts = _read_fragment(fragment)
        reason = Reason.NO_FRAGMENT
        top_figures = _kill_figures(kill) | (
            {name: not_measured(Reason.NO_CARD) for name in MEMORY_FIGURES}
            if watch is None
            else watch.entry()
        )
        if probed.started:
            top_figures["compiler_home_at_start"] = value_of(compiler_home_at_start)
        top_figures |= peaks_of_a_failed_group(parts.get("cells", {}), top_figures)
        if watch is not None and watch.stop is not None:
            reason = gathered.later = watch.stop.cell_reason
            # A cell the stop failed may have left later cells `group_ended`.
            unfinished = (Reason.GROUP_ENDED,)
            gathered.stopped = True
        if kill.failed:
            gathered.failure = (
                f"the {group.name} group left a process below the top that was"
                " not ended"
            )
        elif not gathered.stopped and (
            status or parts.get("outcome") != GroupOutcome.DONE
        ):
            gathered.failure = f"the {group.name} cells ended with status {status}"
    gathered.cells |= completed_cells(
        _chosen(group, arguments), parts.get("cells", {}), reason, replacing=unfinished
    )
    gathered.services[group.name] = completed_service(
        parts.get("services", {}).get(group.name, {}) | top_figures, reason
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
        _compiler_home(Path(scratch)).mkdir()
        for group in _groups(arguments):
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
