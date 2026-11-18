"""Where a provider's worker is: under the provider root, or beside the interpreter."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from video_service.providers import ProviderName, worker_executable


def test_with_a_provider_root_the_worker_is_in_the_providers_own_environment() -> None:
    root = Path("/opt/providers")

    assert worker_executable(ProviderName.FAKE, root) == Path(
        "/opt/providers/fake/.venv/bin/video-fake-worker"
    )


def test_without_a_provider_root_the_worker_is_beside_the_interpreter() -> None:
    worker = worker_executable(ProviderName.FAKE, None)

    assert worker.parent == Path(sys.executable).parent
    assert os.access(worker, os.X_OK)
