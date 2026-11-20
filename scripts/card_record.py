"""The card proof's record: its shape, the lines it is judged against and the verdicts.

Every figure is a JSON object. A measured figure holds its `value`; one judged against
a line adds the line's text and a verdict. A figure nothing measured holds only
`verdict` `not_measured` and the `reason`. The record is closed: no member is left out
or holds a bare null.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from logline import Outcome

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence
    from pathlib import Path

    from card_drive import Service

Figure = dict[str, object]


class Verdict(StrEnum):
    """What a figure came to against its line."""

    MET = "met"
    MISSED = "missed"
    NOT_MEASURED = "not_measured"


class Reason(StrEnum):
    """Why a figure was not measured: the proof ended before it or it had no subject."""

    CELL_FAILED = "cell_failed"
    GROUP_ENDED = "group_ended"
    EARLIER_GROUP_FAILED = "earlier_group_failed"
    NO_FRAGMENT = "no_fragment"
    EXITED_BEFORE_STOP = "exited_before_stop"
    KILL_FAILED = "kill_failed"
    MEMORY_REFUSED = "memory_refused"
    WATCHDOG_BREACH = "watchdog_breach"
    SAMPLE_FAILED = "sample_failed"
    NO_CARD = "no_card"
    NO_LOUD_AUDIO_BEFORE_CLEAR = "no_loud_audio_before_clear"
    PROVIDER_NOT_LOADED = "provider_not_loaded"
    LOOK_NOT_PREPARED = "look_not_prepared"


class StopReason(StrEnum):
    """Why the top ended a group for memory; a code set apart from `Reason`."""

    SHORT_FOR_CAP = "short_for_cap"
    BELOW_FLOOR = "below_floor"
    SAMPLE_FAILED = "sample_failed"
    WATCHDOG_FAILED = "watchdog_failed"


@dataclass(frozen=True)
class Line:
    """A limit a figure is judged against, with the text the record shows."""

    text: str
    limit: float
    compare: Callable[[float, float], bool]

    def verdict(self, value: float) -> Verdict:
        """Whether `value` is within the limit."""
        return Verdict.MET if self.compare(value, self.limit) else Verdict.MISSED


# Each line is a budget row the card proof confirms: the record meets it or names
# the miss.
FIRST_OUTPUT_LINE: Final = Line(
    "first PCM byte to first frame out at most 1000 ms", 1000, operator.le
)
CLEAR_TO_SILENCE_LINE: Final = Line(
    "clear to the last loud audio frame at the peer at most 500 ms", 500, operator.le
)
FRAMES_PER_SECOND_LINE: Final = Line(
    "steady state at least 25 frames per second, no starved session", 25, operator.ge
)
START_TO_READY_LINE: Final = Line(
    "start to ready, every look prepared, at most 300000 ms", 300_000, operator.le
)
ENCODER_CORES_LINE: Final = Line(
    "the service's CPU at five viewers beyond none at most 2 cores", 2, operator.le
)
# In MiB, the unit `nvidia-smi` reports; the limit is also the floor's default.
FREE_MEMORY_LINE: Final = Line(
    "free card memory at least 1024 MiB at the peak", 1024, operator.ge
)

# What the top counts below itself at a group's end: before and after its kill.
KILL_FIGURES: Final = ("processes_left_after_stop", "processes_alive_after_kill")


class MemoryFigure(StrEnum):
    """What the top's samples say of one group, in MiB."""

    USED_BEFORE = "card_used_before_mb"
    FREE_LEAST = "card_free_least_mb"
    USED_MOST = "card_used_most_mb"
    USED_AFTER = "card_used_after_mb"
    USED_AFTER_MINUS_BEFORE = "card_used_after_minus_before_mb"
    USED_BEYOND_CAP = "card_used_beyond_cap_mb"
    FLOOR = "card_floor_mb"


MEMORY_FIGURES: Final = tuple(MemoryFigure)
# Only a group the top ended for memory has this entry.
MEMORY_STOP: Final = "memory_stop"
# What a service's entry in the record names, in its order.
SERVICE_FIGURES: Final = (
    "start_to_ready_ms",
    "provider_load",
    "look_preparations",
    "stop_to_exit_ms",
    *KILL_FIGURES,
    *MEMORY_FIGURES,
    "log",
)


def value_of(value: object) -> Figure:
    """A figure with no line to judge it by."""
    return {"value": value}


def not_measured(reason: str, line: Line | None = None) -> Figure:
    """A figure nothing measured, with why."""
    figure: Figure = {"verdict": Verdict.NOT_MEASURED, "reason": reason}
    return figure if line is None else {"line": line.text, **figure}


def judged(value: float, line: Line, *, dry_run: bool) -> Figure:
    """`value` against `line`; a run with no card gives the value, no verdict."""
    if dry_run:
        return value_of(value) | not_measured(Reason.NO_CARD, line)
    return value_of(value) | {"line": line.text, "verdict": line.verdict(value)}


def closed_record(
    *, dry_run: bool, cells: dict[str, object], services: dict[str, object]
) -> dict[str, object]:
    """The whole record: the cells run and each service's start and stop."""
    return {
        "mode": "dry-run" if dry_run else "card",
        "cells": cells,
        "services": services,
    }


def completed_cells(
    names: Sequence[str],
    cells: dict[str, object],
    reason: Reason,
    *,
    replacing: Collection[Reason] = (),
) -> dict[str, object]:
    """Every cell of `names`: its figures in `cells`, or not measured for `reason`.

    A cell `cells` holds as not measured for a reason in `replacing` counts as missing.
    """
    return {
        name: (
            not_measured(reason)
            if _not_measured_for(cells.get(name), replacing)
            else cells[name]
        )
        for name in names
    }


def _not_measured_for(figure: object, reasons: Collection[Reason]) -> bool:
    """Whether `figure` is missing, or not measured for one of `reasons`."""
    return figure is None or (
        isinstance(figure, dict)
        and figure.get("verdict") == Verdict.NOT_MEASURED
        and figure.get("reason") in reasons
    )


def completed_service(figures: dict[str, object], reason: Reason) -> dict[str, object]:
    """A service's every figure: those in `figures`, the rest not measured for `reason`.

    No figure in `figures` is dropped, so one the proof adds without naming it in
    `SERVICE_FIGURES` shows in the record, where a finished group's keys are checked.
    """
    return {name: not_measured(reason) for name in SERVICE_FIGURES} | figures


def step_figures(line: dict[str, object]) -> dict[str, object]:
    """A load or preparation as the service logged it: outcome, reason, duration."""
    return {
        name: value_of(line[name])
        for name in ("outcome", "reason", "duration_ms")
        if name in line
    }


def service_figures(service: Service, log: Path, *, dry_run: bool) -> dict[str, object]:
    """The service's start, load and preparations; its stop is added once it ends."""
    return {
        "start_to_ready_ms": judged(
            service.start_to_ready_ms, START_TO_READY_LINE, dry_run=dry_run
        ),
        "provider_load": step_figures(service.lines("provider_load.ended")[0]),
        "look_preparations": [
            {"look": value_of(line["look"])} | step_figures(line)
            for line in service.lines("look_preparation.ended")
        ],
        "log": value_of(log.name),
    }


def failed_load(service: Service) -> tuple[Reason, str] | None:
    """Why the cells cannot run, and the line that says so, when the load failed.

    A step is failed by its logged outcome; a failed step read after ready carries the
    reason the service logged.
    """
    load = service.lines("provider_load.ended")[0]
    if load["outcome"] == Outcome.FAILED:
        return (
            Reason.PROVIDER_NOT_LOADED,
            f"the provider did not load ({load['reason']})",
        )
    for line in service.lines("look_preparation.ended"):
        if line["outcome"] == Outcome.FAILED:
            return (
                Reason.LOOK_NOT_PREPARED,
                f"the look {line['look']} was not prepared ({line['reason']})",
            )
    return None
