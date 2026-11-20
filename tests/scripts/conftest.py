"""The command's run, which the proof's tests share."""

from __future__ import annotations

import contextlib
import os
import signal
from typing import TYPE_CHECKING

import pytest

from support.prove_card_support import Run

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    prepared = Run(tmp_path)
    yield prepared
    # A case that makes the kill fail leaves the process it could not end.
    for pid in prepared.alive():
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
