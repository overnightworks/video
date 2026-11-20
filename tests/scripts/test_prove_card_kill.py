"""The card proof's end-of-group kill, driven through the command's own entry point."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from typing import TYPE_CHECKING, Final

import pytest

import card_drive
from card_record import Reason, value_of
from support.prove_card_support import (
    APPEARS_WITHIN_SECONDS,
    CELLS_BY_GROUP,
    GONE_WITHIN_SECONDS,
    Run,
    ended,
    is_not_measured,
)

if TYPE_CHECKING:
    from pathlib import Path

TEST_MARGIN_SECONDS: Final = 15

# The command's own entry point, run in a process that is the top, with the kernel
# calls of the kill replaced by what makes one thing go wrong: a stop that arrives while
# the end-of-group kill runs, a signal that is refused, a subreaper that cannot be set.
HARNESS: Final = """
import dataclasses
import os
import signal
import subprocess
import sys
from pathlib import Path

import card_drive
import prove_card

mode = sys.argv[1]
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
    "record": dataclasses.replace(kernel, send_signal=record_signal),
    "subreaper": dataclasses.replace(
        kernel, become_subreaper=subreaper_that_cannot_be_set
    ),
}
raise SystemExit(prove_card.main(sys.argv[2:], controls[mode]))
"""


@pytest.fixture
def harness(run: Run) -> Path:
    """The `HARNESS` script, written beside the run."""
    script = run.directory / "harness.py"
    script.write_text(HARNESS)
    return script


def _start_with(
    run: Run, harness: Path, mishap: str, *extra: str
) -> subprocess.Popen[bytes]:
    """Start the command as the top of the `HARNESS` case `mishap`."""
    arguments = run.arguments(*extra)
    return run.launch([arguments[0], str(harness), mishap, *arguments[2:]])


def test_a_worker_that_outlives_its_service_is_counted_and_ended_by_the_top(
    run: Run,
) -> None:
    command = run.start("--dry-run", "--cells", "viewer")
    serving = run.wait_until(run.has_spoken, APPEARS_WITHIN_SECONDS)
    service = run.service_pid()
    workers = card_drive.worker_pids(service)
    # A stopped worker cannot act on its closed input, so it outlives the service.
    for worker in workers:
        os.killpg(worker, signal.SIGSTOP)
    os.kill(service, signal.SIGKILL)

    status, _ = run.finish(command)
    record = json.loads(run.record.read_text())
    entry = record["services"]["wire"]

    assert serving
    assert workers
    assert status != 0
    assert entry["processes_left_after_stop"] == value_of(len(workers))
    assert entry["processes_alive_after_kill"] == value_of(0)
    assert is_not_measured(entry["stop_to_exit_ms"], Reason.EXITED_BEFORE_STOP)
    assert is_not_measured(record["cells"]["viewer"], Reason.CELL_FAILED)
    assert run.processes() == []


def test_a_subreaper_that_cannot_be_set_ends_the_command_before_any_group_starts(
    run: Run, harness: Path
) -> None:
    status, error = run.finish(_start_with(run, harness, "subreaper", "--dry-run"))

    assert status != 0
    assert len(error.strip().splitlines()) == 1
    assert "subreaper" in error
    assert not run.probe_run_log.exists()
    assert not run.record.exists()
    assert run.processes() == []


def test_a_stop_during_the_end_of_group_kill_is_held_and_sent_to_no_process(
    run: Run, harness: Path
) -> None:
    bystander = subprocess.Popen(["sleep", "60"])  # noqa: S607 - the test's own bystander
    run.environment["BYSTANDER_PID"] = str(bystander.pid)
    try:
        status, error = run.finish(
            _start_with(run, harness, "stop", "--dry-run", "--cells", "clear", "lag")
        )

        assert status != 0
        assert error.strip() == "interrupted by SIGTERM"
        assert not run.record.exists()
        assert len(run.probe_run_log.read_text().splitlines()) == 1
        # The process the stand-in left below the top is the kill's to end.
        assert run.processes() == []
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_a_stop_is_not_forwarded_to_a_process_that_is_no_child_of_probe_run(
    run: Run, harness: Path
) -> None:
    bystander = subprocess.Popen(["sleep", "60"])  # noqa: S607 - the test's own bystander
    signals = run.directory / "signals.log"
    run.environment["SIGNALS_LOG"] = str(signals)
    try:
        command = _start_with(
            run, harness, "record", "--dry-run", "--cells", "clear", "lag"
        )
        serving = run.wait_until(
            lambda: "process.ready" in _log_text(run, "wire"), APPEARS_WITHIN_SECONDS
        )
        # The id the group's process wrote now names a process probe-run did not start.
        (pid_file,) = run.scratch.glob("prove-card-record-*/*.pid")
        pid_file.write_text(str(bystander.pid))
        os.kill(command.pid, signal.SIGTERM)

        status, error = run.finish(command)

        assert serving
        assert status != 0
        assert error.strip() == "interrupted by SIGTERM"
        assert not run.record.exists()
        assert len(run.probe_run_log.read_text().splitlines()) == 1
        assert run.has_spoken()
        assert signal.SIGTERM.value not in _signals_sent(signals)
        assert run.processes() == []
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_a_stop_goes_to_probe_run_when_the_check_of_its_child_cannot_be_made(
    run: Run, harness: Path
) -> None:
    command = _start_with(run, harness, "blind", "--dry-run", "--cells", "clear", "lag")
    serving = run.wait_until(
        lambda: "process.ready" in _log_text(run, "wire"), APPEARS_WITHIN_SECONDS
    )
    os.kill(command.pid, signal.SIGTERM)

    status, error = run.finish(command)

    assert serving
    assert status != 0
    assert error.strip() == "interrupted by SIGTERM"
    assert not run.record.exists()
    assert not run.has_spoken()
    assert run.processes() == []


def test_a_kill_that_raises_is_recorded_as_failed_and_no_later_group_starts(
    run: Run, harness: Path
) -> None:
    status, error = run.finish(
        _start_with(run, harness, "raise", "--dry-run", "--cells", "clear", "lag")
    )
    record = json.loads(run.record.read_text())
    entry = record["services"]["wire"]

    assert status != 0
    assert len(error.strip().splitlines()) == 1
    assert "wire" in error
    assert is_not_measured(entry["processes_left_after_stop"], Reason.KILL_FAILED)
    assert is_not_measured(entry["processes_alive_after_kill"], Reason.KILL_FAILED)
    assert record["cells"]["clear"]["outcome"]["value"] == "cancelled"
    assert is_not_measured(record["cells"]["lag"], Reason.EARLIER_GROUP_FAILED)
    assert len(run.probe_run_log.read_text().splitlines()) == 1


def test_an_interrupt_during_the_second_group_ends_the_command_with_nothing_left(
    run: Run,
) -> None:
    command = run.start("--dry-run", "--cells", "clear", "lag")
    second_group_serves = run.wait_until(
        lambda: "process.ready" in _log_text(run, "lag"), APPEARS_WITHIN_SECONDS
    )
    os.kill(command.pid, signal.SIGINT)

    status, error = run.finish(command)

    assert second_group_serves
    assert status != 0
    assert error.strip() == "interrupted by SIGINT"
    assert not run.record.exists()
    assert len(run.probe_run_log.read_text().splitlines()) == len(CELLS_BY_GROUP)
    assert run.wait_until(lambda: run.processes() == [], GONE_WITHIN_SECONDS)
    assert list(run.scratch.iterdir()) == []


def test_a_top_killed_while_a_group_runs_has_its_service_and_worker_stopped(
    run: Run,
) -> None:
    command = run.start("--dry-run", "--cells", "viewers")
    serving = run.wait_until(run.has_spoken, APPEARS_WITHIN_SECONDS)
    service = run.service_pid()
    group = [service, *card_drive.worker_pids(service)]
    os.kill(command.pid, signal.SIGKILL)
    bound = (
        card_drive.STOP_WITHIN_SECONDS
        + card_drive.KILL_MARGIN_SECONDS
        + TEST_MARGIN_SECONDS
    )

    run.finish(command)
    gone = run.wait_until(lambda: all(ended(pid) for pid in group), bound)

    assert serving
    assert len(group) > 1
    assert gone


def _signals_sent(log: Path) -> list[int]:
    """The signal numbers the top sent through a pidfd, once it sent any."""
    return [int(line) for line in log.read_text().split()] if log.exists() else []


def _log_text(run: Run, group: str) -> str:
    """What the service of `group` has logged, once its log exists."""
    log = run.directory / f"{run.record.stem}.{group}.log"
    return log.read_text(errors="replace") if log.exists() else ""
