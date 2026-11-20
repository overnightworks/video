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

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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

# What the top counts below itself at a group's end: before and after its kill.
KILL_FIGURES: Final = ("processes_left_after_stop", "processes_alive_after_kill")
# What a service's entry in the record names, in its order.
SERVICE_FIGURES: Final = (
    "start_to_ready_ms",
    "provider_load",
    "look_preparations",
    "stop_to_exit_ms",
    *KILL_FIGURES,
    "log",
)

NO_CARD_REASON: Final = "no card is driven, so a timing here is the fake's"


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
        return value_of(value) | not_measured(NO_CARD_REASON, line)
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
    names: Sequence[str], cells: dict[str, object], reason: Reason
) -> dict[str, object]:
    """Every cell of `names`: its figures in `cells`, or not measured for `reason`."""
    return {name: cells.get(name, not_measured(reason)) for name in names}


def completed_service(figures: dict[str, object], reason: Reason) -> dict[str, object]:
    """A service's every figure: those in `figures`, the rest not measured for `reason`.

    No figure in `figures` is dropped, so one the proof adds without naming it in
    `SERVICE_FIGURES` shows in the record, where a finished group's keys are checked.
    """
    return {name: not_measured(reason) for name in SERVICE_FIGURES} | figures
