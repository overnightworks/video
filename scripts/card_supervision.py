"""The top command's side of one group: its process under `probe-run`, its stops.

The group's process runs under `probe-run`, which the top starts; while it runs, the
top records every stop and sends it on to the group, and in card mode it samples the
card and ends the group when the card's memory calls for it.
"""

# ruff: noqa: T201 - the stop's line goes to standard error where it happens

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, NamedTuple

from card_drive import (
    STOP_SIGNALS,
    end_processes_below,
    exit_on_stop,
    has_child,
    read_process,
    signal_if_unchanged,
)

if TYPE_CHECKING:
    from pathlib import Path

    from card_drive import Kill, Process, ProcessControl
    from card_memory import Ender, GroupWatch, Stop

CHILD_MARKER: Final = "PROVE_CARD_GROUP"
PROBE_RUN: Final = "probe-run"
KILL_GRACE_SECONDS: Final = 30
CAP_SECONDS: Final = 1800


def pid_file(fragment: Path) -> Path:
    """Where the group's process names itself to the command that started it."""
    return fragment.with_suffix(".pid")


def begun_child(fragment: Path) -> Path:
    """Set up the group's process: stop handlers, pid file, the identity directory.

    The identity's keys live in the top's temporary directory, which the top removes
    also when it kills this process.
    """
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, exit_on_stop)
    pid_file(fragment).write_text(str(os.getpid()))  # NOSONAR(S8707)
    directory = fragment.with_suffix(".identity")
    directory.mkdir()
    return directory


def become_subreaper(control: ProcessControl) -> None:
    """Make the command the parent of every orphan below it, or end it with one line."""
    try:
        control.become_subreaper()
    except OSError as error:
        message = f"the command cannot become a child subreaper ({error.strerror})"
        raise SystemExit(message) from None


@dataclass
class Stops:
    """The stops the command has recorded while a group runs.

    From the mark `begin_ending` sets, the group is ending: its stop handler only
    records, and a stop after the mark is not acted on.
    """

    recorded: list[int] = field(default_factory=list)
    before_ending: int | None = None

    def record(self, signum: int, _frame: object) -> None:
        """Remember the stop; it never raises."""
        self.recorded.append(signum)

    def begin_ending(self) -> None:
        """Mark the group as ending, with the stops recorded so far."""
        self.before_ending = len(self.recorded)

    @property
    def ending(self) -> bool:
        """Whether the command has begun to end the group for its memory."""
        return self.before_ending is not None

    @property
    def to_act_on(self) -> int | None:
        """The first stop that ends the command as an interrupt, if there is one."""
        acted = self.recorded[: self.before_ending]
        return acted[0] if acted else None


@dataclass(frozen=True)
class Supervision:
    """How the top supervises one group: its stops, the kernel and the card watch."""

    stops: Stops
    control: ProcessControl
    watch: GroupWatch | None


def ending(stops: Stops, control: ProcessControl, spare: int | None) -> Ender:
    """The stop's end of the group: the mark, one line on standard error, the kill.

    `spare` is `probe-run`'s id while it is unreaped: the kill leaves its status to the
    `Popen` that holds it.
    """

    def end(stop: Stop) -> Kill:
        stops.begin_ending()
        print(stop.line(), file=sys.stderr)
        return end_processes_below(control, spare=spare)

    return end


def named_pid(fragment: Path) -> int | None:
    """The id the group's process wrote, or none while it has written none."""
    try:
        return int(pid_file(fragment).read_text())
    except OSError, ValueError:
        return None


def settled_at_group(
    probe_run: Process | None,
    fragment: Path,
    signum: int,
    control: ProcessControl,
) -> bool:
    """Whether the pid file names the group's process, which settles the stop there.

    The stop is sent only to a process whose parent is `probe-run`, checked by
    `signal_if_unchanged`; `timeout` may have reaped the group's process and the kernel
    reused its id. An id that names no process or another's child, and a check that
    cannot be made, send nothing: the stop stays recorded and the end-of-group kill
    follows. Where the id is not yet written or holds no number, the stop is not
    settled and goes to `probe-run`.
    """
    if (group_pid := named_pid(fragment)) is None:
        return False
    if probe_run is not None:
        with suppress(OSError):
            group = read_process(group_pid, control)
            if group is not None and group.parent == probe_run.pid:
                signal_if_unchanged((group, probe_run), signum, control)
    return True


def service_exists(fragment: Path, control: ProcessControl) -> bool:
    """Whether the group's process has started a child, which is its service."""
    group_pid = named_pid(fragment)
    return group_pid is not None and has_child(group_pid, control)


def run_child(
    command: list[str],
    group: str,
    fragment: Path,
    supervision: Supervision,
) -> int | None:
    """Run `command` as the group's process; a stop is recorded and, meanwhile, sent on.

    Once the process is reaped, or the group is ending for its memory, a stop is only
    recorded; the caller puts the handlers back. A card-mode run samples the card while
    it waits. Its status is none when the group was ended for its memory and
    `probe-run` outlived the kill.
    """
    stops, control, watch = (
        supervision.stops,
        supervision.control,
        supervision.watch,
    )
    probe_run: Process | None = None

    def send_on(signum: int) -> None:
        # Once the process is reaped the group's process has ended and its id may name
        # another process; once the group is ending the kill is about to free them.
        if stops.ending or child.returncode is not None:
            return
        # `timeout` kills its command at once on a signal, so the stop goes to the
        # group's process itself, which stops the service first and then exits.
        if not settled_at_group(probe_run, fragment, signum, control):
            child.send_signal(signum)

    def forward(signum: int, frame: object) -> None:
        stops.record(signum, frame)
        send_on(signum)

    # No `with`: its exit waits for `probe-run` without a bound, and a kill that failed
    # leaves it alive. The command's own exit then takes the parent-death chain to it.
    # The operator names the audio path that the command passes on.
    child = subprocess.Popen(  # NOSONAR(S8705)  # noqa: S603 - probe-run and this python
        command, env={**os.environ, CHILD_MARKER: group}
    )
    # The start time that tells `probe-run` from a process that took its id.
    with suppress(OSError):
        probe_run = read_process(child.pid, control)
    # The group's service is cleaned up by the child, so a stop reaches it first.
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, forward)
    # A stop recorded since the caller last looked, before `forward` was installed.
    if stops.recorded:
        send_on(stops.recorded[0])
    if watch is None:
        return child.wait()
    return watch.until_ended(
        child,
        service_exists=lambda: service_exists(fragment, control),
        end=ending(stops, control, child.pid),
    )


class Probed(NamedTuple):
    """Whether `probe-run` started for a group, and the status it left."""

    started: bool
    status: int | None


def started_under_probe_run(
    tail: list[str], group: str, fragment: Path, supervision: Supervision
) -> Probed:
    """Run `tail` as the group's process under `probe-run`: its start and status.

    A card-mode group the card has no room for is refused before `probe-run` starts, and
    so is a group after a stop that arrived before it: nothing would forward that stop,
    and the group would run through. Neither has started. A started group has no status
    when it was ended for its memory and `probe-run` outlived the kill.
    """
    for tool in (PROBE_RUN, "setpriv"):
        if shutil.which(tool) is None:
            message = f"{tool} is not on PATH; the command does not run without it"
            raise SystemExit(message)
    watch = supervision.watch
    if watch is not None and watch.refuse_first(
        ending(supervision.stops, supervision.control, None)
    ):
        return Probed(started=False, status=None)
    if supervision.stops.recorded:
        return Probed(started=False, status=None)
    command = [PROBE_RUN, "-k", str(KILL_GRACE_SECONDS), str(CAP_SECONDS), *tail]
    return Probed(started=True, status=run_child(command, group, fragment, supervision))


def ended_below(control: ProcessControl, watch: GroupWatch | None) -> Kill:
    """End what is left below the top once a group is over, as the watch ends it."""
    if watch is None:
        return end_processes_below(control)
    return watch.finish(lambda: end_processes_below(control))
