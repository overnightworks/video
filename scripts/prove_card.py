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
import signal
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

from pydantic import ValidationError
from video_provider_fake import worker as fake_worker

from card_cells import (
    CELLS,
    DEFAULT_CELLS,
    GROUPS,
    HOLD_SECONDS,
    Drive,
    Hold,
    beside,
)
from card_contention import cpu_set
from card_drive import (
    ENVIRONMENT_PREFIX,
    FAKE_PROVIDER,
    KERNEL,
    PROVIDER_VARIABLE,
    STOP_SIGNALS,
    Kill,
    ProcessControl,
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
    lag_cell,
    not_measured,
    service_figures,
    value_of,
)
from card_supervision import (
    CHILD_MARKER,
    Probed,
    Stops,
    Supervision,
    become_subreaper,
    begun_child,
    ended_below,
    service_pid_file,
    started_under_probe_run,
)
from video_service.card import read_card_memory
from video_service.config import Settings
from video_service.looks import LookFailure, LookRefusedError, decode_image
from video_service.providers import ProviderName

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from pydantic_settings import PydanticBaseSettingsSource

    from card_drive import Audio, Service
    from card_identity import Identity


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
# The `fake` provider at the card's cost: its picture, frames and seconds a render.
_CARD_COST: Final = {
    fake_worker.WIDTH: "512",
    fake_worker.HEIGHT: "512",
    fake_worker.CHUNK_FRAMES: "24",
    fake_worker.RENDER_SECONDS: "0.6",
    fake_worker.IDLE_RENDER_SECONDS: "0.6",
}
# The lag the service runs at when the command names none.
_DEFAULT_LAG_MS: Final = Settings.model_fields["max_lag_ms"].default


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


def _positive(text: str) -> float:
    """A number of seconds above 0, as an argument."""
    try:
        number = float(text)
    except ValueError:
        message = f"{text!r} is not a number"
        raise argparse.ArgumentTypeError(message) from None
    if number <= 0:
        message = f"{number} is not above 0"
        raise argparse.ArgumentTypeError(message)
    return number


def _cpus(text: str) -> frozenset[int]:
    """The CPUs the hold's group is pinned to, as an argument."""
    try:
        return cpu_set(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True, help="the record's path")
    parser.add_argument("--audio", type=Path, required=True, help="a mono 16-bit WAV")
    parser.add_argument(
        "--dry-run", action="store_true", help="drive the fake provider"
    )
    parser.add_argument(
        "--cells", nargs="+", choices=CELLS, default=list(DEFAULT_CELLS)
    )
    parser.add_argument(
        "--memory-floor-mb",
        type=_non_negative,
        default=int(FREE_MEMORY_LINE.limit),
        help="free card memory in MiB the run keeps beside the provider's cap",
    )
    parser.add_argument(
        "--lag-ms",
        nargs="+",
        default=[str(_DEFAULT_LAG_MS)],
        metavar="MS",
        help="the lags in ms the lag cell sweeps; the wire group runs at the largest",
    )
    parser.add_argument(
        "--hold-seconds",
        type=_positive,
        default=HOLD_SECONDS,
        help="how long the hold cell's one session runs",
    )
    parser.add_argument(
        "--cpus",
        type=_cpus,
        help="the CPUs the hold's group is pinned to, such as 0,1 or 0-1",
    )
    parser.add_argument(
        "--busy-workers",
        type=_non_negative,
        default=0,
        help="the processes that spin on the hold's CPUs beside its group",
    )
    parser.add_argument(
        "--look-image",
        type=Path,
        help="a face image the service takes, served as the look alpha",
    )
    parser.add_argument("--fragment", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments.busy_workers and not arguments.cpus:
        parser.error("--busy-workers needs --cpus, or the workers spin on every CPU")
    arguments.look_image_bytes = None
    arguments.lag_ms = _lags(arguments.lag_ms)
    return arguments


def checked_record(path: Path, option: str = "--record") -> Path:
    """The file's absolute path, once it names a file in a writable directory."""
    record = path.resolve()
    directory = record.parent
    if record.is_dir() or not (
        directory.is_dir() and os.access(directory, os.W_OK | os.X_OK)
    ):
        message = (
            f"{option} {path}: it must name a file in an existing writable directory"
        )
        raise SystemExit(message)
    return record


def checked_look_image(path: Path) -> bytes:
    """The file's bytes once the service's own limits accept them as a look's image."""
    limits = _GivenSettings(provider=ProviderName.FAKE)
    try:
        data = path.read_bytes()
    except OSError as error:
        message = f"--look-image {path}: it cannot be read ({error.strerror})"
        raise SystemExit(message) from None
    reason = LookFailure.TOO_LARGE.value if len(data) > limits.look_max_bytes else None
    try:
        if reason is None:
            decode_image(data, limits.look_max_pixels)
    except LookRefusedError as refused:
        reason = refused.failure.value
    if reason is not None:
        message = f"--look-image {path}: the service refuses it ({reason})"
        raise SystemExit(message)
    return data


def _give_look_image(arguments: argparse.Namespace) -> None:
    """Keep the checked bytes of the named look image; none named, none kept."""
    if arguments.look_image is not None:
        arguments.look_image = arguments.look_image.resolve()
        arguments.look_image_bytes = checked_look_image(arguments.look_image)


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


def _lags(texts: Sequence[str]) -> list[int]:
    """The lags named, once the service accepts each and none is named twice.

    The refusal is one line that names the argument.
    """
    lags = []
    for text in texts:
        try:
            lag = int(text)
            _GivenSettings(provider=ProviderName.FAKE, max_lag_ms=lag)
        except ValueError, ValidationError:
            message = f"--lag-ms {text}: the service takes whole numbers of at least 1"
            raise SystemExit(message) from None
        lags.append(lag)
    if len(set(lags)) != len(lags):
        message = "--lag-ms names a value twice"
        raise SystemExit(message)
    return lags


@dataclass(frozen=True)
class CardProvider:
    """The operator's provider settings, as the service gets them, and the cap."""

    environment: dict[str, str]
    card_memory_mb: int


def card_provider(environ: Mapping[str, str]) -> CardProvider:
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


def _provider_of(arguments: argparse.Namespace, group: _Group) -> Mapping[str, str]:
    """The variables that choose the provider the service runs and set the fake.

    A dry run's hold drives the fake at the card's cost.
    """
    if arguments.dry_run:
        return FAKE_PROVIDER | (_CARD_COST if group.kind == "hold" else {})
    return card_provider(os.environ).environment


def _stop_figure(service: Service) -> dict[str, object]:
    """How long the service took to exit after its stop, if it was still running."""
    if service.stop_to_exit_ms is None:
        return not_measured(Reason.EXITED_BEFORE_STOP)
    return value_of(service.stop_to_exit_ms)


def _groups(arguments: argparse.Namespace) -> list[_Group]:
    """The groups the command was asked for, in the order they run.

    The lag cell runs once at each lag named, the smallest first; the wire group runs
    after them at the largest.
    """
    lags = sorted(arguments.lag_ms)
    groups = [
        _Group(f"lag-{lag}", "lag", lag) for lag in lags if "lag" in arguments.cells
    ]
    if set(GROUPS["wire"]) & set(arguments.cells):
        groups.append(_Group("wire", "wire", lags[-1]))
    if "hold" in arguments.cells:
        groups.append(_Group("hold", "hold", lags[-1]))
    return groups


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


def _drive_of(
    service: Service,
    identity: Identity,
    audio: Audio,
    arguments: argparse.Namespace,
) -> Drive:
    """What a group's cells drive: the service, the audio and the hold's settings."""
    return Drive(
        service,
        identity,
        audio,
        dry_run=arguments.dry_run,
        record=arguments.record,
        hold=Hold(arguments.hold_seconds, arguments.busy_workers),
    )


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
        throwaway_identity(scratch, arguments.look_image_bytes) as identity,
        start_service(
            identity,
            log,
            max_lag_ms=group.lag_ms,
            compiler_home=compiler_home,
            service_pid_file=service_pid_file(arguments.fragment),
            provider=_provider_of(arguments, group),
        ) as service,
    ):
        drive = _drive_of(service, identity, audio, arguments)
        figures = service_figures(service, log, group.lag_ms, dry_run=arguments.dry_run)

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
    identity_directory = begun_child(arguments.fragment)
    if arguments.cpus and name == "hold":
        # Inherited by the service, its provider and the busy workers started below.
        os.sched_setaffinity(0, arguments.cpus)
    (group,) = (group for group in _groups(arguments) if group.name == name)
    outcome = _run_group(
        group, arguments, identity_directory, _compiler_home(arguments.fragment.parent)
    )
    return 0 if outcome is GroupOutcome.DONE else 1


def _under_probe_run(
    group: _Group,
    arguments: argparse.Namespace,
    fragment: Path,
    supervision: Supervision,
) -> Probed:
    """Run `group` as its own process under `probe-run`; its start and status."""
    tail = [
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
        "--lag-ms",
        str(group.lag_ms),
        "--hold-seconds",
        str(arguments.hold_seconds),
        "--busy-workers",
        str(arguments.busy_workers),
        *(
            ["--cpus", ",".join(map(str, sorted(arguments.cpus)))]
            if arguments.cpus
            else []
        ),
        *(["--dry-run"] if arguments.dry_run else []),
        *(["--look-image", str(arguments.look_image)] if arguments.look_image else []),
    ]
    try:
        return started_under_probe_run(tail, group.name, fragment, supervision)
    finally:
        if (log := _group_log(arguments.record, group.name)).exists():
            print(f"beside it: {log}")


def _run_group_process(
    group: _Group,
    arguments: argparse.Namespace,
    fragment: Path,
    control: ProcessControl,
    watch: GroupWatch | None,
) -> tuple[Probed, Kill]:
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
    kill = ended_below(control, watch)
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
    sweep: dict[int, object] = field(default_factory=dict)
    services: dict[str, object] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    failure: str | None = None
    stopped: bool = False
    later: Reason = Reason.EARLIER_GROUP_FAILED

    @property
    def recorded_cells(self) -> dict[str, object]:
        """Every cell run, the sweep's merged into the lag cell, in the cells' order."""
        cells = self.cells | ({"lag": lag_cell(self.sweep)} if self.sweep else {})
        return {name: cells[name] for name in CELLS if name in cells}

    def add_cells(self, group: _Group, cells: dict[str, object]) -> None:
        """Add `group`'s cells: a sweep group's lag cell is one value of the sweep."""
        if group.kind == "lag":
            self.sweep[group.lag_ms] = cells["lag"]
        else:
            self.cells |= cells

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


def _watch_of(limits: CardLimits | None) -> GroupWatch | None:
    """What samples the card for a group in card mode; none in a dry run."""
    if limits is None:
        return None
    return GroupWatch(read_card_memory, limits.cap_mb, limits.floor_mb)


def _failure(
    group: _Group,
    kill: Kill,
    status: int | None,
    parts: dict[str, object],
    *,
    stopped: bool,
) -> str | None:
    """Why no group follows `group`, when it left something below the top or failed."""
    if kill.failed:
        return f"the {group.name} group left a process below the top that was not ended"
    if not stopped and (status or parts.get("outcome") != GroupOutcome.DONE):
        return f"the {group.name} cells ended with status {status}"
    return None


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
        watch = _watch_of(arguments.limits)
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
        gathered.failure = _failure(
            group, kill, status, parts, stopped=gathered.stopped
        )
    gathered.add_cells(
        group,
        completed_cells(
            _chosen(group, arguments),
            parts.get("cells", {}),
            reason,
            replacing=unfinished,
        ),
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
        _give_look_image(arguments)
        return _child(group, arguments)
    arguments.record = checked_record(arguments.record)
    read_audio(arguments.audio)
    _give_look_image(arguments)
    arguments.limits = (
        None
        if arguments.dry_run
        else CardLimits(
            card_provider(os.environ).card_memory_mb, arguments.memory_floor_mb
        )
    )
    become_subreaper(control)
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, _interrupted)
    gathered = _Gathered()
    with tempfile.TemporaryDirectory(prefix="prove-card-record-") as scratch:
        _compiler_home(Path(scratch)).mkdir()
        for group in _groups(arguments):
            _gather(group, arguments, Path(scratch), gathered, control)
    record = closed_record(
        dry_run=arguments.dry_run,
        cells=gathered.recorded_cells,
        services=gathered.services,
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
