"""CPU contention for the card proof's hold cell: busy workers and run-queue delay."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

type Waits = dict[tuple[int, str], int]

_BUSY_LOOP: Final = "while True: pass"
_WAITING_NANOSECONDS_FIELD: Final = 1
_NANOSECONDS: Final = 1e9
_END_WITHIN_SECONDS: Final = 5


def cpu_set(text: str) -> frozenset[int]:
    """The CPUs `text` names, as `0,2` or `0-3`, once the command may run on them."""
    cpus: set[int] = set()
    try:
        for part in text.split(","):
            first, _, last = part.partition("-")
            cpus.update(range(int(first), int(last or first) + 1))
    except ValueError:
        message = f"{text!r} is not a list of CPUs such as 0,2 or 0-3"
        raise ValueError(message) from None
    allowed = os.sched_getaffinity(0)
    if not cpus or not cpus <= allowed:
        message = f"{text!r} names CPUs outside {sorted(allowed)}"
        raise ValueError(message)
    return frozenset(cpus)


@contextmanager
def busy_workers(count: int) -> Iterator[None]:
    """Start `count` processes that spin on the CPUs this process may use.

    They inherit its affinity and environment, so the group's supervision finds
    them if this block is cut short; the block's end ends them.
    """
    workers = [
        subprocess.Popen([sys.executable, "-c", _BUSY_LOOP])  # noqa: S603 - this interpreter
        for _ in range(count)
    ]
    try:
        yield
    finally:
        for worker in workers:
            worker.kill()
        for worker in workers:
            worker.wait(timeout=_END_WITHIN_SECONDS)


def _threads(pid: int) -> list[Path]:
    with suppress(OSError):
        return list(Path(f"/proc/{pid}/task").iterdir())
    return []


def thread_count(pids: Iterable[int]) -> int:
    """The threads the processes `pids` have now."""
    return sum(len(_threads(pid)) for pid in pids)


def waits(pids: Iterable[int]) -> Waits:
    """The nanoseconds each thread of `pids` has waited to run, by the kernel."""
    found = {}
    for pid in pids:
        for thread in _threads(pid):
            with suppress(OSError):
                fields = (thread / "schedstat").read_text().split()
                found[pid, thread.name] = int(fields[_WAITING_NANOSECONDS_FIELD])
    return found


def run_queue_delay_seconds(before: Waits, after: Waits) -> float:
    """The time the threads waited to run between two readings.

    A thread that ended in between has no reading at the end and counts for nothing.
    """
    waited = sum(wait - before.get(thread, 0) for thread, wait in after.items())
    return waited / _NANOSECONDS
