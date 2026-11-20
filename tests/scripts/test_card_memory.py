"""The card guard: when it refuses a group, when it ends one, what it keeps."""

from __future__ import annotations

import signal
import subprocess
from typing import TYPE_CHECKING, Final

import pytest

from card_drive import KILL_WITHIN_SECONDS, Kill
from card_memory import GroupWatch, Stop
from card_record import (
    FREE_MEMORY_LINE,
    MEMORY_STOP,
    MemoryFigure,
    Reason,
    StopReason,
    Verdict,
    value_of,
)
from support.prove_card_support import is_not_measured
from video_service.card import CardMemory, CardReadingError

if TYPE_CHECKING:
    from collections.abc import Callable

CAP: Final = 500
FLOOR: Final = 1024
TOTAL: Final = 24576
LEAST_FREE: Final = 2000
NOTHING_LEFT: Final = Kill(processes_left=1, processes_alive=0, failed=False)


def _memory(*, used: int, free: int) -> CardMemory:
    return CardMemory(used_mb=used, free_mb=free, total_mb=TOTAL)


def _sampler(*readings: CardMemory | type[Exception]) -> Callable[[], CardMemory]:
    """A sampler that gives `readings` in turn; an exception class is raised."""
    remaining = list(readings)

    def read() -> CardMemory:
        reading = remaining.pop(0)
        if isinstance(reading, CardMemory):
            return reading
        raise reading

    return read


class _Child:
    """The group's `probe-run`: it times out of `wait` `ticks` times, then ends."""

    name = "probe-run"

    def __init__(self, ticks: int, status: int = 0) -> None:
        self.ticks = ticks
        self.status = status

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None and self.ticks:
            self.ticks -= 1
            raise subprocess.TimeoutExpired(self.name, timeout)
        return self.status


class _Lingering:
    """A `probe-run` the kill did not end: no bounded wait on it ever returns."""

    name = "probe-run"

    def __init__(self) -> None:
        self.waited: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.waited.append(timeout)
        if timeout is None:
            message = "a wait on a child that outlived the kill has no bound"
            raise AssertionError(message)
        raise subprocess.TimeoutExpired(self.name, timeout)


def _watched(
    watch: GroupWatch, ticks: int, *, kill: Kill = NOTHING_LEFT
) -> tuple[int, list[Stop]]:
    """Let `watch` guard a group that is serving for `ticks` intervals."""
    stops: list[Stop] = []

    def end(stop: Stop) -> Kill:
        stops.append(stop)
        return kill

    status = watch.until_ended(
        _Child(ticks, -signal.SIGKILL), service_exists=lambda: True, end=end
    )
    return status, stops


@pytest.mark.parametrize(
    ("free", "refused"),
    [(CAP + FLOOR - 1, True), (CAP + FLOOR, False)],
)
def test_a_group_is_refused_while_free_memory_minus_the_cap_is_below_the_floor(
    *, free: int, refused: bool
) -> None:
    watch = GroupWatch(_sampler(_memory(used=100, free=free)), CAP, FLOOR)
    stops: list[Stop] = []

    def end(stop: Stop) -> Kill:
        stops.append(stop)
        return NOTHING_LEFT

    assert watch.refuse_first(end) is refused
    assert [stop.reason for stop in stops] == (
        [StopReason.SHORT_FOR_CAP] if refused else []
    )
    assert all(
        stop.refused and stop.cell_reason is Reason.MEMORY_REFUSED for stop in stops
    )


@pytest.mark.parametrize(("free", "breached"), [(FLOOR - 1, True), (FLOOR, False)])
def test_a_group_is_ended_once_free_memory_falls_below_the_floor(
    *, free: int, breached: bool
) -> None:
    watch = GroupWatch(_sampler(_memory(used=100, free=free)), CAP, FLOOR)

    status, stops = _watched(watch, ticks=1)

    assert status == -signal.SIGKILL
    assert [stop.reason for stop in stops] == (
        [StopReason.BELOW_FLOOR] if breached else []
    )
    assert all(
        not stop.refused and stop.cell_reason is Reason.WATCHDOG_BREACH
        for stop in stops
    )


@pytest.mark.parametrize("serving", [False, True])
def test_a_failed_sample_ends_the_group_before_and_after_its_service_exists(
    *,
    serving: bool,
) -> None:
    watch = GroupWatch(_sampler(CardReadingError), CAP, FLOOR)
    ended: list[Stop] = []

    def end(stop: Stop) -> Kill:
        ended.append(stop)
        return NOTHING_LEFT

    if serving:
        watch.until_ended(_Child(1), service_exists=lambda: True, end=end)
    else:
        watch.refuse_first(end)

    assert [stop.reason for stop in ended] == [StopReason.SAMPLE_FAILED]
    assert ended[0].refused is not serving
    assert ended[0].memory is None


def test_a_sampler_that_raises_ends_the_group_as_a_breach_of_the_guard_itself() -> None:
    group = subprocess.Popen(["sleep", "60"])  # noqa: S607 - the test's own group
    watch = GroupWatch(_sampler(RuntimeError), CAP, FLOOR)
    ended: list[Stop] = []

    def end(stop: Stop) -> Kill:
        ended.append(stop)
        group.kill()
        return NOTHING_LEFT

    try:
        status = watch.until_ended(group, service_exists=lambda: True, end=end)
    finally:
        group.kill()
        group.wait()

    assert status == -signal.SIGKILL
    assert [stop.reason for stop in ended] == [StopReason.WATCHDOG_FAILED]
    assert not ended[0].refused
    assert watch.entry()[MEMORY_STOP]["reason"] == StopReason.WATCHDOG_FAILED


def test_the_groups_figures_are_the_before_the_peak_the_after_and_what_they_say() -> (
    None
):
    watch = GroupWatch(
        _sampler(
            _memory(used=100, free=9000),
            _memory(used=300, free=5000),
            _memory(used=900, free=LEAST_FREE),
            _memory(used=150, free=9000),
        ),
        CAP,
        FLOOR,
    )

    assert watch.refuse_first(lambda _stop: NOTHING_LEFT) is False
    _watched(watch, ticks=2)
    kill = watch.finish(lambda: NOTHING_LEFT)
    figures = watch.entry()

    assert kill is NOTHING_LEFT
    assert figures[MemoryFigure.USED_BEFORE] == value_of(100)
    assert figures[MemoryFigure.USED_MOST] == value_of(900)
    assert figures[MemoryFigure.USED_AFTER] == value_of(150)
    assert figures[MemoryFigure.USED_AFTER_MINUS_BEFORE] == value_of(50)
    assert figures[MemoryFigure.USED_BEYOND_CAP] == value_of(900 - 100 - CAP)
    assert figures[MemoryFigure.FLOOR] == value_of(FLOOR)
    least = figures[MemoryFigure.FREE_LEAST]
    assert least["value"] == LEAST_FREE
    assert least["line"] == FREE_MEMORY_LINE.text
    assert least["verdict"] == Verdict.MET
    assert MEMORY_STOP not in figures


def test_a_free_peak_below_the_line_is_judged_missed_when_the_floor_is_set_lower() -> (
    None
):
    watch = GroupWatch(
        _sampler(_memory(used=100, free=9000), _memory(used=300, free=FLOOR - 1)),
        CAP,
        floor_mb=FLOOR - 2,
    )
    watch.refuse_first(lambda _stop: NOTHING_LEFT)

    _watched(watch, ticks=1)

    assert watch.stop is None
    assert watch.entry()[MemoryFigure.FREE_LEAST]["verdict"] == Verdict.MISSED


def test_a_first_sample_that_fails_leaves_the_figures_taken_from_it_unmeasured() -> (
    None
):
    watch = GroupWatch(_sampler(CardReadingError), CAP, FLOOR)

    watch.refuse_first(lambda _stop: NOTHING_LEFT)
    figures = watch.entry()

    assert all(
        is_not_measured(figures[name], Reason.SAMPLE_FAILED)
        for name in (
            MemoryFigure.USED_BEFORE,
            MemoryFigure.USED_AFTER_MINUS_BEFORE,
            MemoryFigure.USED_BEYOND_CAP,
        )
    )
    assert figures[MemoryFigure.FLOOR] == value_of(FLOOR)


def test_an_after_sample_that_fails_leaves_the_figures_derived_from_it_unmeasured() -> (
    None
):
    watch = GroupWatch(
        _sampler(
            _memory(used=100, free=9000),
            _memory(used=300, free=5000),
            CardReadingError,
        ),
        CAP,
        FLOOR,
    )
    watch.refuse_first(lambda _stop: NOTHING_LEFT)
    _watched(watch, ticks=1)

    watch.finish(lambda: NOTHING_LEFT)
    figures = watch.entry()

    assert is_not_measured(figures[MemoryFigure.USED_AFTER], Reason.SAMPLE_FAILED)
    assert is_not_measured(
        figures[MemoryFigure.USED_AFTER_MINUS_BEFORE], Reason.SAMPLE_FAILED
    )
    assert figures[MemoryFigure.USED_MOST] == value_of(300)


@pytest.mark.parametrize(
    ("kill", "measured"),
    [
        (NOTHING_LEFT, True),
        (Kill(processes_left=2, processes_alive=1, failed=False), False),
        (Kill(processes_left=2, processes_alive=0, failed=True), False),
    ],
)
def test_the_seconds_to_no_process_are_a_value_only_when_the_kill_ended_them_all(
    *, kill: Kill, measured: bool
) -> None:
    watch = GroupWatch(_sampler(_memory(used=100, free=FLOOR - 1)), CAP, FLOOR)

    _, stops = _watched(watch, ticks=1, kill=kill)
    record = watch.entry()[MEMORY_STOP]

    assert stops[0].reason is StopReason.BELOW_FLOOR
    assert record["reason"] == StopReason.BELOW_FLOOR
    assert record["free_mb"] == value_of(FLOOR - 1)
    assert record["floor_mb"] == value_of(FLOOR)
    assert record["cap_mb"] == value_of(CAP)
    seconds = record["seconds_to_no_process_left"]
    assert ("value" in seconds) is measured
    if not measured:
        assert is_not_measured(seconds, Reason.KILL_FAILED)


def test_a_probe_run_that_outlives_the_kill_gives_no_status_after_a_bounded_wait() -> (
    None
):
    watch = GroupWatch(_sampler(_memory(used=100, free=FLOOR - 1)), CAP, FLOOR)
    child = _Lingering()

    status = watch.until_ended(
        child, service_exists=lambda: True, end=lambda _stop: NOTHING_LEFT
    )

    assert status is None
    assert watch.stop is not None
    assert None not in child.waited
    assert child.waited[-1] <= KILL_WITHIN_SECONDS


def test_a_stopped_group_is_sampled_after_its_one_kill_and_killed_no_more() -> None:
    watch = GroupWatch(
        _sampler(_memory(used=100, free=FLOOR - 1), _memory(used=120, free=9000)),
        CAP,
        FLOOR,
    )
    kills: list[Kill] = []
    _watched(watch, ticks=1)

    kill = watch.finish(lambda: kills.append(NOTHING_LEFT) or NOTHING_LEFT)

    assert kill is NOTHING_LEFT
    assert kills == []
    assert watch.entry()[MemoryFigure.USED_AFTER] == value_of(120)
