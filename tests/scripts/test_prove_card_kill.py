"""The card proof's end-of-group kill, driven through the command's own entry point."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from typing import TYPE_CHECKING, Final

import pytest

import card_drive
from card_record import MEMORY_STOP, CompilerHome, Reason, StopReason, value_of
from card_supervision import CHILD_MARKER
from support.kill_harness import top_command
from support.prove_card_support import (
    APPEARS_WITHIN_SECONDS,
    BELOW_THE_FLOOR,
    CELLS_BY_GROUP,
    DEFAULT_LAG_MS,
    GONE_WITHIN_SECONDS,
    LAG_SERVICE,
    Run,
    ended,
    is_cell_not_measured,
    is_not_measured,
    lines,
)

if TYPE_CHECKING:
    from pathlib import Path

TEST_MARGIN_SECONDS: Final = 15
GROUP_RUNS_THROUGH_SECONDS: Final = 240
TOP_ENDS_WITHIN_SECONDS: Final = 60
GROUP_FOLLOWS_SECONDS: Final = (
    card_drive.STOP_WITHIN_SECONDS
    + card_drive.KILL_MARGIN_SECONDS
    + TEST_MARGIN_SECONDS
)

# A thread that is no daemon, as aiortc's decoder threads are, keeps the group's process
# from ending after its cleanups; a hook the group's interpreter runs at its start.
LINGERING_THREAD: Final = f"""
import os
import sys
import threading
import time

if os.environ.get({CHILD_MARKER!r}) and sys.argv[0].endswith("prove_card.py"):
    threading.Thread(target=time.sleep, args=(3600,)).start()
"""


def _start_with(
    run: Run, harness: Path, mishap: str, *extra: str
) -> subprocess.Popen[bytes]:
    """Start the command as the top of the `HARNESS` case `mishap`."""
    return run.launch(
        top_command(harness, "prove_card", mishap, run.arguments(*extra)[2:])
    )


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


@pytest.mark.parametrize(
    "gone", [False, True], ids=["a_process_probe_run_did_not_start", "a_gone_process"]
)
def test_a_stop_is_not_forwarded_to_a_process_that_is_no_child_of_probe_run(
    run: Run, harness: Path, *, gone: bool
) -> None:
    bystander = subprocess.Popen(["sleep", "60"])  # noqa: S607 - the test's own bystander
    signals = run.directory / "signals.log"
    run.environment["SIGNALS_LOG"] = str(signals)
    try:
        command = _start_with(
            run, harness, "record", "--dry-run", "--cells", "clear", "lag"
        )
        serving = run.wait_until(
            lambda: "process.ready" in _log_text(run, LAG_SERVICE),
            APPEARS_WITHIN_SECONDS,
        )
        if gone:
            bystander.kill()
            bystander.wait()
        # The id the group's process wrote now names a process probe-run did not start,
        # or none.
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
        assert (bystander.poll() is None) is not gone
    finally:
        bystander.kill()
        bystander.wait()


def test_a_stop_while_probe_run_waits_for_the_load_goes_to_probe_run(
    run: Run, tmp_path: Path
) -> None:
    run.environment["PROBE_RUN_HOLD"] = str(tmp_path / "never-released")
    command = run.start("--dry-run", "--cells", "lag")
    held = run.wait_until(run.probe_run_log.exists, APPEARS_WITHIN_SECONDS)
    os.kill(command.pid, signal.SIGTERM)

    status, error = run.finish(command)

    assert held
    assert status != 0
    assert error.strip() == "interrupted by SIGTERM"
    assert len(run.probe_run_log.read_text().splitlines()) == 1
    assert run.logs() == []
    assert run.processes() == []


def test_a_stop_that_arrives_before_the_top_forwards_stops_goes_to_probe_run(
    run: Run, harness: Path, tmp_path: Path
) -> None:
    run.environment["PROBE_RUN_HOLD"] = str(tmp_path / "never-released")

    status, error = run.finish(
        _start_with(run, harness, "stopped-at-start", "--dry-run", "--cells", "lag")
    )

    assert status != 0
    assert error.strip() == "interrupted by SIGTERM"
    assert len(run.probe_run_log.read_text().splitlines()) == 1
    assert run.logs() == []
    assert run.processes() == []


def test_a_stop_is_forwarded_through_the_checked_send_to_the_child_of_probe_run(
    run: Run, harness: Path
) -> None:
    signals = run.directory / "signals.log"
    run.environment["SIGNALS_LOG"] = str(signals)
    command = _start_with(
        run, harness, "record", "--dry-run", "--cells", "clear", "lag"
    )
    serving = run.wait_until(
        lambda: "process.ready" in _log_text(run, LAG_SERVICE), APPEARS_WITHIN_SECONDS
    )
    os.kill(command.pid, signal.SIGTERM)

    status, error = run.finish(command)

    assert serving
    assert status != 0
    assert error.strip() == "interrupted by SIGTERM"
    assert not run.record.exists()
    assert len(run.probe_run_log.read_text().splitlines()) == 1
    assert signal.SIGTERM.value in _signals_sent(signals)
    assert not run.has_spoken()
    assert run.processes() == []


def test_a_stop_is_sent_to_no_process_when_the_check_of_its_child_cannot_be_made(
    run: Run, harness: Path
) -> None:
    command = _start_with(run, harness, "blind", "--dry-run", "--cells", "clear", "lag")
    serving = run.wait_until(
        lambda: "process.ready" in _log_text(run, LAG_SERVICE), APPEARS_WITHIN_SECONDS
    )
    os.kill(command.pid, signal.SIGTERM)

    status, error = run.finish(command)

    assert serving
    assert status != 0
    assert error.strip() == "interrupted by SIGTERM"
    assert not run.record.exists()
    assert len(run.probe_run_log.read_text().splitlines()) == 1
    assert run.has_spoken()
    assert run.processes() == []


def test_a_kill_that_raises_is_recorded_as_failed_and_no_later_group_starts(
    run: Run, harness: Path
) -> None:
    status, error = run.finish(
        _start_with(run, harness, "raise", "--dry-run", "--cells", "clear", "lag")
    )
    record = json.loads(run.record.read_text())
    entry = record["services"][LAG_SERVICE]

    assert status != 0
    assert len(error.strip().splitlines()) == 1
    assert LAG_SERVICE in error
    assert is_not_measured(entry["processes_left_after_stop"], Reason.KILL_FAILED)
    assert is_not_measured(entry["processes_alive_after_kill"], Reason.KILL_FAILED)
    assert record["cells"]["lag"]["smallest_without_starvation_ms"] == value_of(
        DEFAULT_LAG_MS
    )
    assert is_cell_not_measured(record, "clear", Reason.EARLIER_GROUP_FAILED)
    assert len(run.probe_run_log.read_text().splitlines()) == 1


@pytest.mark.parametrize("mode", ["--dry-run", "card"])
def test_an_interrupt_during_the_second_group_ends_the_command_with_nothing_left(
    run: Run, mode: str
) -> None:
    if mode == "card":
        run.in_card_mode()
    command = run.start(
        *(["--dry-run"] if mode == "--dry-run" else []), "--cells", "clear", "lag"
    )
    second_group_serves = run.wait_until(
        lambda: "process.ready" in _log_text(run, "wire"), APPEARS_WITHIN_SECONDS
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


def test_a_stop_during_the_breachs_kill_is_held_and_forwarded_to_no_process(
    run: Run, harness: Path
) -> None:
    run.in_card_mode()
    run.breach_while_loading()
    signals = run.directory / "signals.log"
    run.environment["SIGNALS_LOG"] = str(signals)

    status, error = run.finish(
        _start_with(run, harness, "breach-stop", "--cells", "lag")
    )
    record = json.loads(run.record.read_text())

    assert status == 1
    assert "watchdog breach" in error
    assert "interrupted" not in error
    assert is_cell_not_measured(record, "lag", Reason.WATCHDOG_BREACH)
    assert record["services"][LAG_SERVICE]["processes_alive_after_kill"] == value_of(0)
    assert set(_signals_sent(signals)) <= {signal.SIGKILL.value}
    assert run.processes() == []


def test_a_breach_replaces_the_group_ended_of_the_cells_a_failed_cell_left_unfinished(
    run: Run, harness: Path
) -> None:
    run.in_card_mode()
    command = _start_with(
        run,
        harness,
        "cell-failed-first",
        "--cells",
        "sentences",
        "viewer",
        "viewers",
    )
    first = run.wait_until(
        lambda: run.fragment_cells() == ["sentences"], APPEARS_WITHIN_SECONDS
    )
    run.write_reading(BELOW_THE_FLOOR)

    status, error = run.finish(command)
    cells = json.loads(run.record.read_text())["cells"]

    assert first
    assert status != 0
    assert "watchdog breach" in error
    assert is_not_measured(cells["viewer"], Reason.CELL_FAILED)
    assert is_not_measured(cells["viewers"], Reason.WATCHDOG_BREACH)
    assert cells["sentences"]["sentences"]
    assert run.wait_until(lambda: run.processes() == [], GONE_WITHIN_SECONDS)


def test_a_kill_that_fails_on_the_breach_path_is_closed_and_the_record_is_written(
    run: Run, harness: Path
) -> None:
    run.in_card_mode()
    run.breach_while_loading()
    # The group would run through only after this long: the top must not wait for it.
    run.environment["WORKER_LOAD_SECONDS"] = str(GROUP_RUNS_THROUGH_SECONDS)
    command = _start_with(run, harness, "refused", "--cells", "lag")

    ended = run.wait_until(lambda: command.poll() is not None, TOP_ENDS_WITHIN_SECONDS)
    assert ended
    # Its exit takes the parent-death chain to the group.
    group_follows = run.wait_until(lambda: run.processes() == [], GROUP_FOLLOWS_SECONDS)
    status, error = run.finish(command)
    record = json.loads(run.record.read_text())
    entry = record["services"][LAG_SERVICE]

    assert status != 0
    assert "watchdog breach" in error
    assert lines(error)[-1] == (
        f"the {LAG_SERVICE} group left a process below the top that was not ended"
    )
    assert is_not_measured(entry["processes_left_after_stop"], Reason.KILL_FAILED)
    assert is_not_measured(entry["processes_alive_after_kill"], Reason.KILL_FAILED)
    assert is_not_measured(
        entry[MEMORY_STOP]["seconds_to_no_process_left"], Reason.KILL_FAILED
    )
    assert entry[MEMORY_STOP]["reason"] == StopReason.BELOW_FLOOR
    # `probe-run` started and outlived the kill: its group's reading stands.
    assert entry["compiler_home_at_start"] == value_of(CompilerHome.EMPTY)
    assert group_follows


def _signals_sent(log: Path) -> list[int]:
    """The signal numbers the top sent through a pidfd, once it sent any."""
    return [int(line) for line in log.read_text().split()] if log.exists() else []


def test_a_stop_ends_the_command_when_the_group_cannot_end_after_it(
    run: Run, harness: Path, tmp_path: Path
) -> None:
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(LINGERING_THREAD)
    run.environment["PYTHONPATH"] = os.pathsep.join(
        [str(hook), run.environment["PYTHONPATH"]]
    )
    command = _start_with(
        run, harness, "short-grace", "--dry-run", "--cells", "clear", "lag"
    )
    serving = run.wait_until(
        lambda: "process.ready" in _log_text(run, LAG_SERVICE), APPEARS_WITHIN_SECONDS
    )
    os.kill(command.pid, signal.SIGTERM)
    # Signals of one kind sent before the first is handled coalesce into one stop.
    forwarded = run.wait_until(
        lambda: "process.stopped" in _log_text(run, LAG_SERVICE), GONE_WITHIN_SECONDS
    )
    os.kill(command.pid, signal.SIGTERM)

    status, error = run.finish(command)

    assert serving
    assert forwarded
    assert status != 0
    assert error.strip() == "interrupted by SIGTERM"
    assert not run.record.exists()
    assert run.wait_until(lambda: run.processes() == [], GONE_WITHIN_SECONDS)
    assert list(run.scratch.iterdir()) == []


def _log_text(run: Run, group: str) -> str:
    """What the service of `group` has logged, once its log exists."""
    log = run.directory / f"{run.record.stem}.{group}.log"
    return log.read_text(errors="replace") if log.exists() else ""
