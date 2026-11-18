"""The fake provider's process: the contract's worker loop over a `FakeProvider`."""

from __future__ import annotations

from typing import NoReturn

from video_provider_contract.worker import run_worker

from video_provider_fake import FakeProvider


def main() -> NoReturn:
    """Serve a fake provider until the service closes the pipe."""
    run_worker(FakeProvider())
