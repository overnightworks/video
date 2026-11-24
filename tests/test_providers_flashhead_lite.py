"""The service starts the real FlashHead lite worker, here on a host without a card."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from video_provider_contract import ProviderFailedError
from video_provider_flashhead_lite.renderer import NoCardError

from video_service.config import Settings
from video_service.provider_process import ProcessFailure
from video_service.providers import ProviderName, provider_for

pytestmark = pytest.mark.flashhead_lite

_PROVIDERS = Path(__file__).resolve().parents[1] / "providers"


def test_without_a_card_the_real_worker_refuses_and_the_provider_is_failed(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    settings = Settings(
        provider=ProviderName.FLASHHEAD_LITE,
        provider_root=_PROVIDERS,
        card_memory_mb=1024,
    )
    provider = provider_for(settings)

    with caplog.at_level(logging.WARNING), pytest.raises(ProviderFailedError):
        provider.start()
    provider.close()

    assert provider.failed
    (record,) = caplog.records
    assert (record.reason, record.phase, record.error_type) == (  # type: ignore[attr-defined]
        ProcessFailure.REPORTED.value,
        "before_ready",
        NoCardError.__name__,
    )
    assert record.getMessage() == "provider.failed"
    stock = vars(logging.makeLogRecord({}))
    assert set(vars(record)) - set(stock) - {"message"} == {
        "reason",
        "phase",
        "exit_status",
        "error_type",
    }
