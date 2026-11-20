"""A cell of the card proof, driven directly against a real service."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import card_cells
import card_drive
import card_identity
from support.prove_card_support import AUDIO_SECONDS, write_audio

if TYPE_CHECKING:
    from pathlib import Path


def test_a_provider_that_cannot_answer_ends_the_session_as_unavailable_at_any_lag(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "speech.wav"
    write_audio(audio, seconds=AUDIO_SECONDS)

    async def drive_the_lag_cell() -> object:
        # No jitter exceeds this lag, so only the held worker ends the session.
        with (
            card_identity.throwaway_identity(tmp_path) as identity,
            card_drive.running_service(
                identity, tmp_path / "service.log", max_lag_ms=card_cells.DEFAULT_LAG_MS
            ) as service,
        ):
            drive = card_cells.Drive(
                service,
                identity,
                card_drive.read_audio(audio),
                dry_run=True,
                record=tmp_path / "record.json",
            )
            return await card_cells._lag(drive)  # noqa: SLF001 - the cell is the unit

    cell = asyncio.run(drive_the_lag_cell())

    assert cell["ended_unavailable"]["value"] is True
