"""How much of the card the service reports used."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from video_service.card import card_memory_mb

if TYPE_CHECKING:
    from pathlib import Path


USED_MB = 7123


def _nvidia_smi(directory: Path, script: str) -> None:
    program = directory / "nvidia-smi"
    program.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    program.chmod(0o755)


def test_a_host_without_nvidia_smi_reports_no_memory_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    assert card_memory_mb() == 0


def test_the_first_line_nvidia_smi_prints_is_the_memory_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _nvidia_smi(tmp_path, f"printf '{USED_MB}\\n9\\n'")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert card_memory_mb() == USED_MB


@pytest.mark.parametrize(
    "script",
    ["exit 1", "printf ''", "printf 'not a number\\n'"],
    ids=["fails", "prints-nothing", "prints-garbage"],
)
def test_an_nvidia_smi_that_cannot_say_reports_no_memory_used(
    script: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _nvidia_smi(tmp_path, script)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert card_memory_mb() == 0
