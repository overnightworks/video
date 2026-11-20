"""How much of the card the service reads, and what health reports of it."""

from __future__ import annotations

import os
import shutil
import signal
import time
from typing import TYPE_CHECKING, Final

import pytest

from video_service import card
from video_service.card import (
    CardMemory,
    CardReadingError,
    card_memory_mb,
    read_card_memory,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


# Long enough that only the reading's own bound ends a call that waits on it.
STAND_IN_SECONDS: Final = 60
# What a loaded machine may add to the bounds before the reading counts as held up.
MARGIN_SECONDS: Final = 10
# The stand-in's `PATH` holds no other tool.
SLEEP: Final = shutil.which("sleep")


@pytest.fixture
def memory() -> CardMemory:
    """A card's memory as one scenario states it."""
    return CardMemory(used_mb=7123, free_mb=17000, total_mb=24576)


def _line_of(memory: CardMemory) -> str:
    """The line `nvidia-smi` prints for `memory`."""
    return f"{memory.used_mb}, {memory.free_mb}, {memory.total_mb}"


def _nvidia_smi(directory: Path, script: str) -> None:
    program = directory / "nvidia-smi"
    program.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    program.chmod(0o755)


def test_the_reading_is_the_used_free_and_total_memory_of_the_first_line(
    memory: CardMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _nvidia_smi(tmp_path, f"printf '{_line_of(memory)}\\n1, 2, 3\\n'")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert read_card_memory() == memory


def test_health_reports_the_used_memory_of_the_reading(
    memory: CardMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _nvidia_smi(tmp_path, f"printf '{_line_of(memory)}\\n'")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert card_memory_mb() == memory.used_mb


@pytest.mark.parametrize(
    "script",
    [
        None,
        "exit 1",
        "printf ''",
        "printf 'not a number\\n'",
        "printf '7123\\n'",
        "printf '7123, 4096\\n'",
        "printf '[N/A], [N/A], [N/A]\\n'",
        "printf '\\377\\376\\n'",
        "echo '-1, 2, 3'",
        "printf '1, -2, 3\\n'",
        "printf '1, 2, -3\\n'",
    ],
    ids=[
        "is-missing",
        "fails",
        "prints-nothing",
        "prints-garbage",
        "prints-one-figure",
        "prints-two-figures",
        "prints-not-available",
        "prints-undecodable-bytes",
        "prints-negative-used",
        "prints-negative-free",
        "prints-negative-total",
    ],
)
def test_an_nvidia_smi_that_cannot_say_is_an_error_and_health_reports_no_memory_used(
    script: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if script is not None:
        _nvidia_smi(tmp_path, script)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(CardReadingError):
        read_card_memory()
    assert card_memory_mb() == 0


@pytest.fixture
def stand_in_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """The only directory on `PATH`; a child its stand-in leaves is ended after."""
    monkeypatch.setenv("PATH", str(tmp_path))
    yield tmp_path
    left = tmp_path / "child.pid"
    if left.exists():
        os.kill(int(left.read_text()), signal.SIGKILL)


def _reading_raises() -> None:
    with pytest.raises(CardReadingError):
        read_card_memory()


def _health_reports_no_memory_used() -> None:
    assert card_memory_mb() == 0


@pytest.mark.parametrize(
    "view", [_reading_raises, _health_reports_no_memory_used], ids=["reading", "health"]
)
@pytest.mark.parametrize(
    "script",
    [
        f"exec {SLEEP} {STAND_IN_SECONDS}",
        f"{SLEEP} {STAND_IN_SECONDS} &\necho $! > child.pid\nwait",
    ],
    ids=["sleeps-past-the-bound", "child-keeps-the-output-open"],
)
def test_a_nvidia_smi_that_does_not_answer_is_given_up_within_the_bounds(
    view: Callable[[], None], script: str, stand_in_directory: Path
) -> None:
    _nvidia_smi(stand_in_directory, f"cd '{stand_in_directory}'\n{script}")
    limit = card.QUERY_SECONDS + card.REAP_SECONDS + MARGIN_SECONDS

    started = time.monotonic()
    view()

    assert time.monotonic() - started < limit
    child = stand_in_directory / "child.pid"
    if child.exists():
        # The child that kept the output open is left behind, not waited for.
        os.kill(int(child.read_text()), 0)
