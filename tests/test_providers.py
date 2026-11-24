"""Where a provider's worker is: under the root or beside the interpreter."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

import pytest

from video_service.providers import ProviderName, worker_executable

_PROVIDERS = Path(__file__).resolve().parents[1] / "providers"


def _worker_script(name: ProviderName) -> str:
    pyproject = (_PROVIDERS / name.value / "pyproject.toml").read_text(encoding="utf-8")
    scripts = tomllib.loads(pyproject)["project"]["scripts"]
    (worker,) = (
        script for script, entry in scripts.items() if entry.endswith(".worker:main")
    )
    return worker


@pytest.mark.parametrize("name", list(ProviderName))
def test_with_a_provider_root_the_worker_is_a_script_the_provider_declares(
    name: ProviderName,
) -> None:
    """CONFIG-2."""
    root = Path("/opt/providers")

    worker = worker_executable(name, root)

    assert worker.parent == root / name.value / ".venv" / "bin"
    assert worker.name == _worker_script(name)


def test_without_a_provider_root_the_worker_is_beside_the_interpreter() -> None:
    """CONFIG-2."""
    worker = worker_executable(ProviderName.FAKE, None)

    assert worker.parent == Path(sys.executable).parent
    assert os.access(worker, os.X_OK)
