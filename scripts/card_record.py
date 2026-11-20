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
    from collections.abc import Callable

Figure = dict[str, object]


class Verdict(StrEnum):
    """What a figure came to against its line."""

    MET = "met"
    MISSED = "missed"
    NOT_MEASURED = "not_measured"


@dataclass(frozen=True)
class Line:
    """A limit a figure is judged against, with the text the record shows."""

    text: str
    limit: float
    compare: Callable[[float, float], bool]

    def verdict(self, value: float) -> Verdict:
        """Whether `value` is within the limit."""
        return Verdict.MET if self.compare(value, self.limit) else Verdict.MISSED


# Each line is the program plan's section 7 budget row (overnightworks/video#1);
# the parent item (#16) requires the record to meet them or name each miss.
FIRST_OUTPUT_LINE: Final = Line(
    "first PCM byte to first frame out at most 1000 ms", 1000, operator.le
)
CLEAR_TO_SILENCE_LINE: Final = Line(
    "clear to silence and idle at most 500 ms", 500, operator.le
)
FRAMES_PER_SECOND_LINE: Final = Line(
    "steady state at least 25 frames per second, no starved session", 25, operator.ge
)
START_TO_READY_LINE: Final = Line(
    "start to ready, every look prepared, at most 300000 ms", 300_000, operator.le
)

NO_CARD_REASON: Final = "no card is driven, so a timing here is the fake's"
NO_VIEWER_REASON: Final = "frames per second at the peer needs a viewer"


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
    """The whole record: the cells run, each service's start and stop, what is open."""
    return {
        "mode": "dry-run" if dry_run else "card",
        "cells": cells,
        "services": services,
        "viewers": {
            "frames_per_second": not_measured(NO_VIEWER_REASON, FRAMES_PER_SECOND_LINE)
        },
    }
