"""Sound and picture reach a browser inside BT.1359's detectability window."""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING

import pytest

from support.browser_support import stage
from support.viewer_support import Service, tone

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.browser

# ITU-R BT.1359-1: sound may lead the picture by 45 ms and lag it by 125 ms. A beep is
# heard once the page's AudioContext has played it out, `outputLatency` included; the
# device's own output latency beyond `outputLatency` is a gap the page cannot measure.
EARLIEST_MS = -45
LATEST_MS = 125
PAIRS = 50
_TONE_MS = 80
_SENTENCE_MS = 400


def test_sound_stays_inside_the_detectability_window_over_fifty_flash_and_beep_pairs(
    live_in_browser: Callable[..., None], tmp_path: Path
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        async with stage(service, session, tmp_path / "profile") as page:
            await page.begin()
            await page.until("seen.states.includes('drawn')")
            await page.page.evaluate("stage.measure()")
            for number in range(PAIRS):
                await session.speak(f"pair-{number}", tone(_TONE_MS, _SENTENCE_MS))
            await page.until(
                f"seen.flashes.length >= {PAIRS} && seen.heard.length >= {PAIRS}",
                PAIRS * _SENTENCE_MS * 2,
            )

            seen = await page.read()
            offsets = [
                heard - flashed
                for heard, flashed in zip(seen.heard, seen.flashes, strict=False)
            ]
            assert len(seen.flashes) == len(seen.heard) == PAIRS
            percentiles = statistics.quantiles(offsets, n=20)
            for name, offset in (
                ("median", statistics.median(offsets)),
                ("5th percentile", percentiles[0]),
                ("95th percentile", percentiles[-1]),
            ):
                assert EARLIEST_MS <= offset <= LATEST_MS, name

    live_in_browser(scenario)
