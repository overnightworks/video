"""How much of the GPU the process can see."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Final

_QUERY: Final = [
    "--query-gpu=memory.used,memory.free,memory.total",
    "--format=csv,noheader,nounits",
]
QUERY_SECONDS: Final = 2.0
# A killed `nvidia-smi` is given this long to end and close its output.
REAP_SECONDS: Final = 1.0


class CardReadingError(Exception):
    """The card's memory could not be read: no query tool, a failure, or too slow."""


@dataclass(frozen=True)
class CardMemory:
    """GPU 0's memory in mebibytes."""

    used_mb: int
    free_mb: int
    total_mb: int


def read_card_memory() -> CardMemory:
    """GPU 0's memory, read within the query bound plus the reap bound.

    A child that outlives its kill and the reap bound is left behind, never waited
    for, so a request that reads the card is not held by a stuck driver.
    """
    nvidia = shutil.which("nvidia-smi")
    if nvidia is None:
        message = "no nvidia-smi is on PATH"
        raise CardReadingError(message)
    return _parsed(_output_of([nvidia, *_QUERY]))


def card_memory_mb() -> int:
    """Used memory on GPU 0 in mebibytes, or 0 when the card cannot be queried."""
    try:
        return read_card_memory().used_mb
    except CardReadingError:
        return 0


def _output_of(command: list[str]) -> str:
    """What `command` prints when it succeeds; any other end of it is an error."""
    try:
        process = subprocess.Popen(  # noqa: S603 - argv is the resolved nvidia-smi and a fixed query with no shell
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as error:
        message = "nvidia-smi did not start"
        raise CardReadingError(message) from error
    try:
        output, _ = process.communicate(timeout=QUERY_SECONDS)
    except subprocess.TimeoutExpired as error:
        _abandon(process)
        message = "nvidia-smi did not answer within its bound"
        raise CardReadingError(message) from error
    if process.returncode != 0:
        message = "nvidia-smi failed"
        raise CardReadingError(message)
    return output


def _abandon(process: subprocess.Popen[str]) -> None:
    """Kill `process`, wait at most the reap bound for it and its output, let go."""
    process.kill()
    try:
        process.communicate(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    finally:
        if process.stdout is not None:
            process.stdout.close()
        process.poll()


def _parsed(output: str) -> CardMemory:
    """GPU 0's line of `output`: used, free and total."""
    try:
        used, free, total = (int(field) for field in output.splitlines()[0].split(","))
    except (IndexError, ValueError) as error:
        message = "nvidia-smi printed no memory line"
        raise CardReadingError(message) from error
    return CardMemory(used_mb=used, free_mb=free, total_mb=total)
