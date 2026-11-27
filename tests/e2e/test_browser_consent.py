"""A viewer whose connected peer goes silent ends when the ICE consent expires."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Final

import pytest

from support.browser_support import stage
from support.viewer_support import OpenSession, Service, Watcher
from video_service.media import ViewerEnd

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.browser

# aioice 0.13.0 fails an ICE connection after six rounds of 4 to 6 s and 0.5 s each,
# about 39 s at worst; this is the test's bound, not a claim about the library.
CONSENT_EXPIRES_WITHIN_SECONDS: Final = 45.0


async def _ends_by_expiry_and_frees_the_place(
    service: Service, session: OpenSession
) -> None:
    await service.logged.wait_for(
        "viewer.ended", within_seconds=CONSENT_EXPIRES_WITHIN_SECONDS
    )

    assert service.viewer_ends() == [ViewerEnd.PEER_CLOSED]
    replacement = await service.post(session, await Watcher().offer())
    assert replacement.status_code == HTTPStatus.CREATED


def test_a_browser_killed_without_a_dtls_close_ends_its_viewer_when_the_consent_expires(
    live_in_browser: Callable[..., None], tmp_path: Path
) -> None:
    async def scenario(service: Service) -> None:
        session = await service.open_session()
        async with stage(service, session, tmp_path / "profile") as page:
            await page.begin()
            await page.until("seen.states.includes('drawn')")

            page.kill()

            await _ends_by_expiry_and_frees_the_place(service, session)

    live_in_browser(scenario, max_viewers=1)
