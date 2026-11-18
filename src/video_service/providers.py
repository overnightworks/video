"""The closed catalogue of providers, and the one `VIDEO_PROVIDER` names."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable

    from video_provider_contract import Provider


class ProviderName(StrEnum):
    """The providers this service can run."""

    FAKE = "fake"


def _fake() -> Provider:
    # A development dependency that no image installs, so it is imported when asked for.
    from video_provider_fake import FakeProvider  # noqa: PLC0415

    return FakeProvider()


_PROVIDERS: Final[dict[ProviderName, Callable[[], Provider]]] = {
    ProviderName.FAKE: _fake,
}


def provider_for(name: ProviderName) -> Provider:
    """The provider `name` names, built and not yet started."""
    return _PROVIDERS[name]()
