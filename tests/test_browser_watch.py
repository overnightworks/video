"""A browser page watches a session through its app's route, in headless Chromium."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from support.browser_support import stage
from support.viewer_support import Service, beep

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.browser

_SENTENCE_MS = 400


def test_show_reports_loading_then_drawn_while_frames_advance_and_sound_arrives(
    live_in_browser: Callable[..., None], tmp_path: Path
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        async with stage(service, session, tmp_path / "profile") as page:
            await page.begin()
            assert await page.states() == ["loading"]
            await page.until("seen.states.includes('drawn')")
            await page.page.evaluate("stage.measure()")
            await session.speak("beep", beep(_SENTENCE_MS))

            await page.until("seen.heard.length >= 1")
            first = (await page.read()).presented
            await page.until(f"seen.presented > {first} + 5")
            assert await page.states() == ["loading", "drawn"]
            assert (await page.read()).thrown == []

    live_in_browser(scenario)
