"""The top of a card command's group, run with one kernel call going wrong.

Both commands share `main(argv, control)`, so one script runs either of them as the
top with the `ProcessControl` a case replaces.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

# A command's own entry point, run in a process that is the top, with the kernel calls
# of the kill replaced by what makes one thing go wrong: a stop that arrives while the
# end-of-group kill runs, a signal that is refused, a subreaper that cannot be set.
# The grace a stopped group has is cut to a second in one case.
HARNESS: Final = """
import dataclasses
import importlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import card_drive
from card_record import Reason, not_measured

command = importlib.import_module(sys.argv[1])
mode = sys.argv[2]
kernel = card_drive.KERNEL
opened = []


def subreaper_that_leaves_a_process_below():
    kernel.become_subreaper()
    # It must not hold the command's standard error open once the command has ended.
    subprocess.Popen(["sleep", "60"], stderr=subprocess.DEVNULL)


def subreaper_that_cannot_be_set():
    raise OSError(1, "Operation not permitted")


def open_during_a_stop_that_names_a_bystander(pid):
    if not opened:
        opened.append(pid)
        # The group's process has ended, so the id it wrote may name another process.
        for pid_file in Path(os.environ["TMPDIR"]).glob("prove-card-record-*/*.pid"):
            pid_file.write_text(os.environ["BYSTANDER_PID"])
        signal.raise_signal(signal.SIGTERM)
    return kernel.open(pid)


def refuse_to_signal(_pidfd, _signum):
    raise PermissionError


def open_during_a_stop_that_is_to_be_held(pid):
    # The first pidfd the top opens is the kill's, once the breach has marked the group
    # as ending.
    if not opened:
        opened.append(pid)
        signal.raise_signal(signal.SIGTERM)
    return kernel.open(pid)


fragment_written = []


def write_the_fragment_of_a_group_that_saw_its_service_die_first(pidfd, signum):
    # The group's process, ahead of the kill, failed the cell in progress and ended.
    if not fragment_written:
        fragment_written.append(pidfd)
        for pid_file in Path(os.environ["TMPDIR"]).glob("prove-card-record-*/*.pid"):
            fragment = pid_file.with_suffix(".json")
            written = json.loads(fragment.read_text())
            written["cells"] |= {
                "viewer": not_measured(Reason.CELL_FAILED),
                "viewers": not_measured(Reason.GROUP_ENDED),
            }
            fragment.write_text(json.dumps(written))
    kernel.send_signal(pidfd, signum)


def record_signal(pidfd, signum):
    with Path(os.environ["SIGNALS_LOG"]).open("a") as log:
        log.write(f"{signum}\\n")
    kernel.send_signal(pidfd, signum)


reads = []


def read_stat_that_fails_at_first(pid):
    reads.append(pid)
    if len(reads) == 1:
        raise PermissionError
    return kernel.read_stat(pid)


def read_stat_that_is_stopped_at_first(pid):
    # The first read is of `probe-run`, right after its start and before the top
    # forwards stops; `probe-run` has logged its line by then, so the stop lands in it.
    reads.append(pid)
    if len(reads) == 1:
        while not Path(os.environ["PROBE_RUN_LOG"]).exists():
            time.sleep(0.01)
        signal.raise_signal(signal.SIGTERM)
    return kernel.read_stat(pid)


controls = {
    "stop": dataclasses.replace(
        kernel,
        become_subreaper=subreaper_that_leaves_a_process_below,
        open=open_during_a_stop_that_names_a_bystander,
    ),
    "raise": dataclasses.replace(
        kernel,
        become_subreaper=subreaper_that_leaves_a_process_below,
        send_signal=refuse_to_signal,
    ),
    "blind": dataclasses.replace(kernel, read_stat=read_stat_that_fails_at_first),
    "stopped-at-start": dataclasses.replace(
        kernel, read_stat=read_stat_that_is_stopped_at_first
    ),
    "record": dataclasses.replace(kernel, send_signal=record_signal),
    "refused": dataclasses.replace(kernel, send_signal=refuse_to_signal),
    "breach-stop": dataclasses.replace(
        kernel,
        open=open_during_a_stop_that_is_to_be_held,
        send_signal=record_signal,
    ),
    "cell-failed-first": dataclasses.replace(
        kernel, send_signal=write_the_fragment_of_a_group_that_saw_its_service_die_first
    ),
    "subreaper": dataclasses.replace(
        kernel, become_subreaper=subreaper_that_cannot_be_set
    ),
    "short-grace": dataclasses.replace(kernel, kill_grace_seconds=1),
}
raise SystemExit(command.main(sys.argv[3:], controls[mode]))
"""


def written_harness(directory: Path) -> Path:
    """The `HARNESS` script, written into `directory`."""
    script = directory / "harness.py"
    script.write_text(HARNESS)
    return script


def top_command(
    script: Path, module: str, mishap: str, arguments: list[str]
) -> list[str]:
    """The line that runs `module`'s `main(arguments)` as the case `mishap`."""
    return [sys.executable, str(script), module, mishap, *arguments]
