"""The card proof's guard of the card's memory: the refusal, the watchdog, the figures.

The command that starts a group samples the card itself, from outside the group. It
refuses a group while free memory minus the cap is below the floor, and from the first
sample that finds the group's service it ends the group when free memory falls below
the floor, a sample fails, or the guard itself fails. Ending a group is the caller's
kill; this module decides when, and keeps what the samples said.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from card_drive import KILL_WITHIN_SECONDS
from card_record import (
    FREE_MEMORY_LINE,
    MEMORY_STOP,
    MemoryFigure,
    Reason,
    StopReason,
    judged,
    not_measured,
    value_of,
)
from video_service.card import CardMemory, CardReadingError

if TYPE_CHECKING:
    from collections.abc import Callable

    from card_drive import Kill

SAMPLE_INTERVAL_SECONDS: Final = 0.5

type Sampler = Callable[[], CardMemory]
# Ends every process below the command for a stop, and says what was left.
type Ender = Callable[[Stop], Kill]


@dataclass(frozen=True)
class Stop:
    """A group the command ended for the card's memory, with the sample that said so."""

    reason: StopReason
    refused: bool
    memory: CardMemory | None
    cap_mb: int
    floor_mb: int

    @property
    def cell_reason(self) -> Reason:
        """Why the cells and figures the group did not reach are not measured."""
        return Reason.MEMORY_REFUSED if self.refused else Reason.WATCHDOG_BREACH

    def line(self) -> str:
        """The one plain line the command prints for the stop."""
        subject = "refused" if self.refused else "watchdog breach"
        free = (
            "no sample"
            if self.memory is None
            else f"free card memory {self.memory.free_mb} MiB"
        )
        return (
            f"{subject} ({self.reason}): {free}, cap {self.cap_mb} MiB, "
            f"floor {self.floor_mb} MiB"
        )

    def record(self, kill: Kill, seconds: float) -> dict[str, object]:
        """The group's entry for the stop; the seconds count a kill that ended all."""
        ended = not kill.failed and kill.processes_alive == 0
        return {
            "reason": self.reason,
            "refused": self.refused,
            "free_mb": (
                not_measured(Reason.SAMPLE_FAILED)
                if self.memory is None
                else value_of(self.memory.free_mb)
            ),
            "floor_mb": value_of(self.floor_mb),
            "cap_mb": value_of(self.cap_mb),
            "seconds_to_no_process_left": (
                value_of(seconds) if ended else not_measured(Reason.KILL_FAILED)
            ),
        }


class GroupWatch:
    """The card as one group sees it: each sample judged, and what the samples said."""

    def __init__(self, read: Sampler, cap_mb: int, floor_mb: int) -> None:
        """Watch with `read` for a group whose provider may take `cap_mb`."""
        self.read = read
        self.cap_mb = cap_mb
        self.floor_mb = floor_mb
        self.stop: Stop | None = None
        self.kill: Kill | None = None
        self._seconds = 0.0
        self._started = False
        self._serving = False
        self._before: int | None = None
        self._least_free: int | None = None
        self._most_used: int | None = None
        self._after_taken = False
        self._after: int | None = None

    def refuse_first(self, end: Ender) -> bool:
        """Take the sample before the group starts; whether it refused the group."""
        stop = self._judge()
        if stop is not None:
            self._end(stop, end)
        return stop is not None

    def until_ended(
        self,
        child: subprocess.Popen[bytes],
        *,
        service_exists: Callable[[], bool],
        end: Ender,
    ) -> int | None:
        """Wait for `child`, sampling every interval; its status.

        A stop ends everything below the command at once and then waits for `child` no
        longer than that kill's bound allows; a `child` that still lives then has no
        status, and the command goes on to its record and its exit, which takes the
        parent-death chain to the group.
        """
        self._started = True
        while True:
            try:
                return child.wait(SAMPLE_INTERVAL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            try:
                self._serving = self._serving or service_exists()
                stop = self._judge()
            except Exception:  # noqa: BLE001 - a guard that fails is a breach
                stop = self._stop(StopReason.WATCHDOG_FAILED, None, refused=False)
            if stop is not None:
                self._end(stop, end)
                return self._status_within_the_kill_bound(child)

    def finish(self, end_of_group_kill: Callable[[], Kill]) -> Kill:
        """Sample the card once the group has ended; the group's one kill.

        A stop has made the kill already. Otherwise the sample comes first, since the
        end-of-group kill ends a reading it left behind.
        """
        if self.kill is None:
            self._sample_after()
            self.kill = end_of_group_kill()
        elif self._started:
            self._sample_after()
        return self.kill

    def entry(self) -> dict[str, object]:
        """What the samples say, as the group's service entry names it."""
        figures: dict[str, object] = {MemoryFigure.FLOOR: value_of(self.floor_mb)}
        before, least, most = self._before, self._least_free, self._most_used
        if before is not None:
            figures[MemoryFigure.USED_BEFORE] = value_of(before)
        if least is not None:
            figures[MemoryFigure.FREE_LEAST] = judged(
                least, FREE_MEMORY_LINE, dry_run=False
            )
        if most is not None:
            figures[MemoryFigure.USED_MOST] = value_of(most)
            if before is not None:
                figures[MemoryFigure.USED_BEYOND_CAP] = value_of(
                    most - before - self.cap_mb
                )
        figures |= self._after_figures(before)
        if self.stop is not None and self.kill is not None:
            figures[MEMORY_STOP] = self.stop.record(self.kill, self._seconds)
        return figures

    def _after_figures(self, before: int | None) -> dict[str, object]:
        if not self._after_taken:
            return {}
        after = self._after
        if after is None:
            return {
                name: not_measured(Reason.SAMPLE_FAILED)
                for name in (
                    MemoryFigure.USED_AFTER,
                    MemoryFigure.USED_AFTER_MINUS_BEFORE,
                )
            }
        figures = {MemoryFigure.USED_AFTER: value_of(after)}
        if before is not None:
            figures[MemoryFigure.USED_AFTER_MINUS_BEFORE] = value_of(after - before)
        return figures

    def _stop(
        self, reason: StopReason, memory: CardMemory | None, *, refused: bool
    ) -> Stop:
        return Stop(reason, refused, memory, self.cap_mb, self.floor_mb)

    def _judge(self) -> Stop | None:
        """One sample against the rule of the group's phase."""
        refused = not self._serving
        try:
            memory = self.read()
        except CardReadingError:
            return self._stop(StopReason.SAMPLE_FAILED, None, refused=refused)
        if refused:
            self._before = memory.used_mb
            if memory.free_mb - self.cap_mb < self.floor_mb:
                return self._stop(StopReason.SHORT_FOR_CAP, memory, refused=True)
            return None
        free = memory.free_mb
        self._least_free = (
            free if self._least_free is None else min(free, self._least_free)
        )
        self._most_used = max(memory.used_mb, self._most_used or 0)
        if memory.free_mb < self.floor_mb:
            return self._stop(StopReason.BELOW_FLOOR, memory, refused=False)
        return None

    def _end(self, stop: Stop, end: Ender) -> None:
        started = time.monotonic()
        self.stop = stop
        self.kill = end(stop)
        self._seconds = time.monotonic() - started

    def _status_within_the_kill_bound(
        self, child: subprocess.Popen[bytes]
    ) -> int | None:
        left = max(KILL_WITHIN_SECONDS - self._seconds, 0)
        try:
            return child.wait(left)
        except subprocess.TimeoutExpired:
            return None

    def _sample_after(self) -> None:
        self._after_taken = True
        try:
            self._after = self.read().used_mb
        except CardReadingError:
            self._after = None
