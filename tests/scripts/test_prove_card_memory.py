"""The card proof's card mode, driven through the command's own entry point.

The provider root holds the fake provider's worker as `flashhead-lite`'s, and the card
is a stand-in `nvidia-smi` that answers from a file; no run loads a model or touches
a card.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Final

import pytest

import card_drive
import card_identity
from card_memory import PEAK_FIGURES
from card_record import (
    FREE_MEMORY_LINE,
    MEMORY_FIGURES,
    MEMORY_STOP,
    CompilerHome,
    MemoryFigure,
    Reason,
    StopReason,
    Verdict,
    value_of,
)
from support.prove_card_support import (
    APPEARS_WITHIN_SECONDS,
    BELOW_THE_FLOOR,
    CAP_MB,
    FLOOR_MB,
    GONE_WITHIN_SECONDS,
    NO_ROOM_FOR_THE_CAP,
    ROOMY,
    USED_MB,
    Run,
    is_not_measured,
    lines,
)

FLOOR_ARGUMENT: Final = 3000
FIRST_SAMPLE_SECONDS: Final = 1
KILL_BOUND_SECONDS: Final = card_drive.KILL_WITHIN_SECONDS + 1


def _record(run: Run) -> dict:
    return json.loads(run.record.read_text())


def _cells_are(record: dict, reason: Reason) -> bool:
    return all(is_not_measured(cell, reason) for cell in record["cells"].values())


def test_a_card_run_with_room_records_the_cards_memory_and_judges_its_lines(
    run: Run,
) -> None:
    run.in_card_mode()
    run.environment["VIDEO_PORT"] = "not a port"

    status, error = run.finish(
        run.start("--cells", "lag", "--memory-floor-mb", str(FLOOR_ARGUMENT))
    )
    record = _record(run)
    entry = record["services"]["lag"]

    assert status == 0, error
    assert record["mode"] == "card"
    assert record["cells"]["lag"]["ended_unavailable"]["value"] is True
    assert entry[MemoryFigure.USED_BEFORE] == value_of(USED_MB)
    assert entry[MemoryFigure.USED_MOST] == value_of(USED_MB)
    assert entry[MemoryFigure.USED_AFTER] == value_of(USED_MB)
    assert entry[MemoryFigure.USED_AFTER_MINUS_BEFORE] == value_of(0)
    assert entry[MemoryFigure.USED_BEYOND_CAP] == value_of(-CAP_MB)
    assert entry[MemoryFigure.FLOOR] == value_of(FLOOR_ARGUMENT)
    assert entry[MemoryFigure.FREE_LEAST]["verdict"] == Verdict.MET
    assert entry[MemoryFigure.FREE_LEAST]["line"] == FREE_MEMORY_LINE.text
    assert entry["start_to_ready_ms"]["verdict"] in {Verdict.MET, Verdict.MISSED}
    assert entry["processes_left_after_stop"] == value_of(0)
    assert entry["processes_alive_after_kill"] == value_of(0)
    assert MEMORY_STOP not in entry
    assert run.processes() == []


def test_a_card_run_starts_its_services_on_one_compiler_home_that_is_gone_after_it(
    run: Run,
) -> None:
    run.in_card_mode()

    status, error = run.finish(run.start("--cells", "clear", "lag"))
    services = _record(run)["services"]
    homes = {saw["HOME"] for saw in run.workers_saw()}
    (home,) = homes

    assert status == 0, error
    assert services["wire"]["compiler_home_at_start"] == value_of(CompilerHome.EMPTY)
    assert services["lag"]["compiler_home_at_start"] == value_of(CompilerHome.FILLED)
    assert all(saw["HOME"] == saw["TMPDIR"] for saw in run.workers_saw())
    assert len(run.workers_saw()) == len(services)
    assert run.scratch in Path(home).parents
    assert not Path(home).exists()


@pytest.mark.parametrize(
    ("spoil", "named"),
    [
        ({"VIDEO_PROVIDER_ROOT": None}, "VIDEO_PROVIDER_ROOT"),
        ({"VIDEO_PROVIDER_ROOT": "relative/secret-4711"}, "VIDEO_PROVIDER_ROOT"),
        ({"VIDEO_CARD_MEMORY_MB": None}, "VIDEO_CARD_MEMORY_MB"),
        ({"VIDEO_CARD_MEMORY_MB": "0"}, "VIDEO_CARD_MEMORY_MB"),
        ({"VIDEO_CARD_MEMORY_MB": "secret-4711"}, "VIDEO_CARD_MEMORY_MB"),
    ],
)
def test_a_missing_or_invalid_provider_setting_ends_the_command_naming_only_the_setting(
    run: Run, spoil: dict[str, str | None], named: str
) -> None:
    run.in_card_mode()
    for variable, value in spoil.items():
        if value is None:
            del run.environment[variable]
        else:
            run.environment[variable] = value

    status, error = run.finish(run.start())

    assert status != 0
    assert len(lines(error)) == 1
    assert named in error
    assert "secret-4711" not in error + run.printed()
    assert not run.probe_run_log.exists()
    assert not run.record.exists()


def test_a_negative_floor_is_refused_by_the_argument_parser_before_anything_starts(
    run: Run,
) -> None:
    run.in_card_mode()

    status, error = run.finish(run.start("--cells", "lag", "--memory-floor-mb", "-1"))

    assert status != 0
    assert "--memory-floor-mb" in lines(error)[-1]
    assert not run.probe_run_log.exists()
    assert not run.record.exists()
    assert run.processes() == []


def test_a_floor_of_zero_is_the_operators_choice_and_the_group_runs(run: Run) -> None:
    run.in_card_mode()

    status, error = run.finish(run.start("--cells", "lag", "--memory-floor-mb", "0"))

    assert status == 0, error
    assert _record(run)["services"]["lag"][MemoryFigure.FLOOR] == value_of(0)


def test_a_group_the_card_has_no_room_for_is_refused_and_nothing_of_it_starts(
    run: Run,
) -> None:
    run.in_card_mode()
    run.write_reading(NO_ROOM_FOR_THE_CAP)

    status, error = run.finish(run.start("--cells", "clear", "lag"))
    record = _record(run)
    entry = record["services"]["wire"]

    assert status != 0
    assert len(lines(error)) == 1
    assert all(str(number) in error for number in (CAP_MB - 1, CAP_MB, FLOOR_MB))
    assert not run.probe_run_log.exists()
    assert _cells_are(record, Reason.MEMORY_REFUSED)
    assert is_not_measured(entry["start_to_ready_ms"], Reason.MEMORY_REFUSED)
    assert is_not_measured(entry["compiler_home_at_start"], Reason.MEMORY_REFUSED)
    assert is_not_measured(
        record["services"]["lag"]["card_free_least_mb"], Reason.MEMORY_REFUSED
    )
    assert entry[MEMORY_STOP]["reason"] == StopReason.SHORT_FOR_CAP
    assert entry[MEMORY_STOP]["refused"] is True
    assert entry[MemoryFigure.USED_BEFORE] == value_of(USED_MB)
    assert entry["processes_alive_after_kill"] == value_of(0)
    assert MEMORY_STOP not in record["services"]["lag"]
    assert run.logs() == []
    assert run.processes() == []


@pytest.mark.parametrize("reading", ["fail", "not a reading", "2000, -1, 24576"])
def test_a_first_sample_that_fails_refuses_the_group_and_names_the_failed_sample(
    run: Run, reading: str
) -> None:
    run.in_card_mode()
    run.write_reading(reading)

    status, error = run.finish(run.start("--cells", "lag"))
    entry = _record(run)["services"]["lag"]
    stop = entry[MEMORY_STOP]

    assert status != 0
    assert len(lines(error)) == 1
    assert "no sample" in error
    assert is_not_measured(entry[MemoryFigure.USED_BEFORE], Reason.SAMPLE_FAILED)
    assert stop["reason"] == StopReason.SAMPLE_FAILED
    assert is_not_measured(stop["free_mb"], Reason.SAMPLE_FAILED)
    assert not run.probe_run_log.exists()


def test_the_floor_argument_is_what_the_refusal_judges_by(run: Run) -> None:
    run.in_card_mode()
    floor = int(ROOMY.split(",")[1]) - CAP_MB + 1

    status, error = run.finish(
        run.start("--cells", "lag", "--memory-floor-mb", str(floor))
    )

    assert status != 0
    assert str(floor) in error
    assert _record(run)["services"]["lag"][MEMORY_STOP]["floor_mb"] == value_of(floor)


def test_a_second_group_the_card_has_no_room_for_is_refused_after_the_first_ran(
    run: Run,
) -> None:
    run.in_card_mode()
    # The first group's worker leaves the card with no room for another cap, which the
    # group itself may still run beside: it is above the floor.
    run.environment["WORKER_READING_AT_SESSION"] = NO_ROOM_FOR_THE_CAP

    status, error = run.finish(run.start("--cells", "clear", "lag"))
    record = _record(run)

    assert status != 0
    assert "refused" in error
    assert record["cells"]["clear"]["outcome"]["value"] == "cancelled"
    assert is_not_measured(record["cells"]["lag"], Reason.MEMORY_REFUSED)
    assert MEMORY_STOP in record["services"]["lag"]
    assert MEMORY_STOP not in record["services"]["wire"]
    assert len(run.probe_run_log.read_text().splitlines()) == 1
    assert [log.name for log in run.logs()] == ["record.wire.log"]


def test_a_group_is_refused_when_the_memory_falls_before_its_service_exists(
    run: Run, tmp_path: Path
) -> None:
    run.in_card_mode()
    release = tmp_path / "release"
    run.environment["PROBE_RUN_HOLD"] = str(release)
    command = run.start("--cells", "lag")
    held = run.wait_until(run.probe_run_log.exists, APPEARS_WITHIN_SECONDS)
    run.write_reading(NO_ROOM_FOR_THE_CAP)

    status, error = run.finish(command)

    assert held
    assert status != 0
    assert "refused" in error
    assert _record(run)["services"]["lag"][MEMORY_STOP]["refused"] is True
    assert is_not_measured(
        _record(run)["services"]["lag"][MemoryFigure.USED_AFTER],
        Reason.MEMORY_REFUSED,
    )
    assert run.answers_given(NO_ROOM_FOR_THE_CAP) == 1
    assert run.logs() == []
    assert run.processes() == []


@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM])
def test_a_stop_during_the_first_sample_ends_the_command_before_probe_run_starts(
    run: Run, stop: signal.Signals
) -> None:
    run.in_card_mode()
    run.environment["NVIDIA_SMI_DELAY"] = str(FIRST_SAMPLE_SECONDS)
    command = run.start("--cells", "lag")
    sampling = run.wait_until(run.nvidia_smi_log.exists, APPEARS_WITHIN_SECONDS)
    os.kill(command.pid, stop)

    status, error = run.finish(command)

    assert sampling
    assert status != 0
    assert error.strip() == f"interrupted by {stop.name}"
    assert not run.probe_run_log.exists()
    assert not run.record.exists()
    assert len(run.nvidia_smi_log.read_text().splitlines()) == 1
    assert run.processes() == []


def test_a_breach_while_the_provider_loads_ends_the_tree_and_starts_no_second_group(
    run: Run,
) -> None:
    run.in_card_mode()
    run.breach_while_loading()

    status, error = run.finish(run.start("--cells", "clear", "lag"))
    record = _record(run)
    entry = record["services"]["wire"]
    stop = entry[MEMORY_STOP]

    assert status != 0
    assert "watchdog breach (below_floor)" in lines(error)[0]
    assert list(record["cells"]) == ["clear", "lag"]
    assert _cells_are(record, Reason.WATCHDOG_BREACH)
    assert is_not_measured(entry["start_to_ready_ms"], Reason.WATCHDOG_BREACH)
    assert all(
        is_not_measured(figure, Reason.WATCHDOG_BREACH)
        for figure in record["services"]["lag"].values()
    )
    assert stop["reason"] == StopReason.BELOW_FLOOR
    assert stop["refused"] is False
    assert stop["free_mb"] == value_of(FLOOR_MB - 1)
    assert 0 <= stop["seconds_to_no_process_left"]["value"] <= KILL_BOUND_SECONDS
    assert entry["processes_left_after_stop"]["value"] > 0
    assert entry["processes_alive_after_kill"] == value_of(0)
    assert entry[MemoryFigure.FREE_LEAST]["verdict"] == Verdict.MISSED
    assert is_not_measured(entry[MemoryFigure.USED_AFTER], Reason.WATCHDOG_BREACH)
    assert is_not_measured(
        entry[MemoryFigure.USED_AFTER_MINUS_BEFORE], Reason.WATCHDOG_BREACH
    )
    assert run.answers_given(BELOW_THE_FLOOR) == 1
    assert len(run.probe_run_log.read_text().splitlines()) == 1
    assert [log.name for log in run.logs()] == ["record.wire.log"]
    assert str(run.logs()[0]) in run.printed()
    assert run.processes() == []
    assert list(run.scratch.iterdir()) == []


@pytest.mark.parametrize(
    ("reading", "reason", "peaks_unmeasured_for"),
    [
        (BELOW_THE_FLOOR, StopReason.BELOW_FLOOR, None),
        ("fail", StopReason.SAMPLE_FAILED, Reason.SAMPLE_FAILED),
        ("hang", StopReason.SAMPLE_FAILED, Reason.SAMPLE_FAILED),
    ],
)
def test_a_breach_while_sessions_run_keeps_the_cells_finished_before_it(
    run: Run,
    reading: str,
    reason: StopReason,
    peaks_unmeasured_for: Reason | None,
) -> None:
    run.in_card_mode()
    command = run.start("--cells", "sentences", "viewers")
    first = run.wait_until(
        lambda: run.fragment_cells() == ["sentences"], APPEARS_WITHIN_SECONDS
    )
    run.write_reading(reading)

    status, error = run.finish(command)
    record = _record(run)
    entry = record["services"]["wire"]

    assert first
    assert status != 0
    assert f"({reason})" in error
    assert record["cells"]["sentences"]["sentences"][0]["sentence"] == "first"
    # The cell in progress is `cell_failed` when the group's process saw its service die
    # before the kill reached it.
    assert record["cells"]["viewers"]["reason"] in {
        Reason.WATCHDOG_BREACH,
        Reason.CELL_FAILED,
    }
    assert entry[MEMORY_STOP]["reason"] == reason
    assert entry["start_to_ready_ms"]["value"] > 0
    assert entry["processes_alive_after_kill"] == value_of(0)
    assert is_not_measured(entry[MemoryFigure.USED_AFTER], Reason.WATCHDOG_BREACH)
    peaks = (
        MemoryFigure.FREE_LEAST,
        MemoryFigure.USED_MOST,
        MemoryFigure.USED_BEYOND_CAP,
    )
    if peaks_unmeasured_for is None:
        assert entry[MemoryFigure.FREE_LEAST]["verdict"] == Verdict.MISSED
    else:
        assert all(is_not_measured(entry[name], peaks_unmeasured_for) for name in peaks)
    assert run.answers_given(reading) == 1
    assert run.wait_until(lambda: run.processes() == [], GONE_WITHIN_SECONDS)


def test_a_breach_while_the_group_stops_after_an_interrupt_kills_at_once(
    run: Run,
) -> None:
    run.in_card_mode()
    command = run.start("--cells", "viewers")
    serving = run.wait_until(run.has_spoken, APPEARS_WITHIN_SECONDS)
    # A stopped service holds the group's own stop for its whole bound.
    os.kill(run.service_pid(), signal.SIGSTOP)
    os.kill(command.pid, signal.SIGINT)
    run.write_reading(BELOW_THE_FLOOR)

    status, error = run.finish(command)

    assert serving
    assert status != 0
    assert lines(error)[-1] == "interrupted by SIGINT"
    assert "watchdog breach" in error
    assert not run.record.exists()
    assert run.processes() == []
    assert list(run.scratch.iterdir()) == []


def test_a_dry_run_records_the_memory_figures_as_not_measured_and_samples_no_card(
    run: Run,
) -> None:
    status, error = run.finish(run.start("--dry-run", "--cells", "lag"))
    entry = _record(run)["services"]["lag"]

    assert status == 0, error
    assert all(is_not_measured(entry[name], Reason.NO_CARD) for name in MEMORY_FIGURES)
    assert MEMORY_STOP not in entry
    assert not run.nvidia_smi_log.exists()


def _unmeasured_for(entry: dict, reason: Reason) -> bool:
    """Whether the start (line and value kept) and the peaks are not measured."""
    start = entry["start_to_ready_ms"]
    return (
        {"value", "line"} <= start.keys()
        and (start["verdict"], start["reason"]) == (Verdict.NOT_MEASURED, reason)
        and all(is_not_measured(entry[name], reason) for name in PEAK_FIGURES)
    )


def _ended_by_a_step(error: str, group: str) -> str:
    """The one line the group's process wrote for a failed step; no traceback."""
    ended = [line for line in lines(error) if line.startswith(f"the {group} group")]

    assert len(ended) == 1
    assert "Traceback" not in error
    return ended[0]


def test_a_provider_that_fails_to_load_ends_its_group_before_its_first_cell(
    run: Run,
) -> None:
    run.in_card_mode()
    run.fail_the_load_of("wire")

    status, error = run.finish(run.start("--cells", "clear", "lag"))
    record = _record(run)
    entry = record["services"]["wire"]

    assert status != 0
    assert "provider_failed" in _ended_by_a_step(error, "wire")
    assert is_not_measured(record["cells"]["clear"], Reason.PROVIDER_NOT_LOADED)
    assert is_not_measured(record["cells"]["lag"], Reason.EARLIER_GROUP_FAILED)
    assert entry["provider_load"]["outcome"] == value_of("failed")
    assert entry["provider_load"]["reason"] == value_of("provider_failed")
    assert _unmeasured_for(entry, Reason.PROVIDER_NOT_LOADED)
    assert entry["compiler_home_at_start"] == value_of(CompilerHome.EMPTY)
    assert is_not_measured(
        record["services"]["lag"]["compiler_home_at_start"],
        Reason.EARLIER_GROUP_FAILED,
    )
    assert entry["look_preparations"] == []
    assert len(run.probe_run_log.read_text().splitlines()) == 1
    assert [log.name for log in run.logs()] == ["record.wire.log"]
    assert run.processes() == []
    assert list(run.scratch.iterdir()) == []


def test_a_provider_that_fails_to_load_in_a_later_group_keeps_the_cells_before_it(
    run: Run,
) -> None:
    run.in_card_mode()
    run.fail_the_load_of("lag")

    status, error = run.finish(run.start("--cells", "clear", "lag"))
    record = _record(run)

    assert status != 0
    assert "provider_failed" in _ended_by_a_step(error, "lag")
    assert record["cells"]["clear"]["outcome"]["value"] == "cancelled"
    assert is_not_measured(record["cells"]["lag"], Reason.PROVIDER_NOT_LOADED)
    assert record["services"]["lag"]["provider_load"]["outcome"] == value_of("failed")
    assert _unmeasured_for(record["services"]["lag"], Reason.PROVIDER_NOT_LOADED)
    assert record["services"]["wire"]["start_to_ready_ms"]["verdict"] in {
        Verdict.MET,
        Verdict.MISSED,
    }
    assert run.processes() == []


def test_a_look_that_is_not_prepared_ends_its_group_naming_the_look_and_the_reason(
    run: Run,
) -> None:
    look = card_identity.LOOK_NAMES[-1]
    run.in_card_mode()
    run.fail_the_preparation_of(look)

    status, error = run.finish(run.start("--cells", "clear", "lag"))
    record = _record(run)
    entry = record["services"]["wire"]
    prepared = {
        preparation["look"]["value"]: preparation["outcome"]["value"]
        for preparation in entry["look_preparations"]
    }
    line = _ended_by_a_step(error, "wire")

    assert status != 0
    assert look in line
    assert "provider_failed" in line
    assert is_not_measured(record["cells"]["clear"], Reason.LOOK_NOT_PREPARED)
    assert is_not_measured(record["cells"]["lag"], Reason.EARLIER_GROUP_FAILED)
    assert entry["provider_load"]["outcome"] == value_of("ok")
    assert _unmeasured_for(entry, Reason.LOOK_NOT_PREPARED)
    assert prepared == {
        name: "failed" if name == look else "ok" for name in card_identity.LOOK_NAMES
    }
    assert run.processes() == []


def test_a_provider_root_without_the_worker_ends_the_group_as_a_failed_load(
    run: Run,
) -> None:
    run.in_card_mode()
    run.without_worker()

    status, error = run.finish(run.start("--cells", "lag"))
    record = _record(run)

    assert status != 0
    assert "provider_failed" in _ended_by_a_step(error, "lag")
    assert is_not_measured(record["cells"]["lag"], Reason.PROVIDER_NOT_LOADED)
    assert _unmeasured_for(record["services"]["lag"], Reason.PROVIDER_NOT_LOADED)
    assert run.processes() == []
