"""The built image on the card: READY as the deployment runs it, and the cap enforced.

`-m image_card` selects this module, which no default run and no CI job selects.
`VIDEO_IMAGE` names the image and `VIDEO_CARD_MEMORY_MB` the provider's cap; the
card must be free, since a start is refused while its free memory minus the cap is
below the card proof's floor.
"""

from __future__ import annotations

import os
import time
from contextlib import ExitStack
from typing import TYPE_CHECKING

import pytest

from card_drive import Kill
from card_memory import GroupWatch
from card_record import FREE_MEMORY_LINE
from support.image_support import (
    HEALTH,
    POLL_SECONDS,
    caller_file_in,
    container,
    docker,
    logs_of,
    wait_for_event,
)
from video_client.wire import Health
from video_service.card import read_card_memory
from video_service.provider_process import ProcessBounds
from video_service.providers import ProviderName

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

pytestmark = pytest.mark.image_card

_CAP_ENVIRONMENT = "VIDEO_CARD_MEMORY_MB"
_START_WITHIN_SECONDS = ProcessBounds().start_seconds
# Scenario data: far below what the model needs, as the card proof's cap 1 was.
_TOO_SMALL_CAP_MB = 1
_NOTHING_LEFT = Kill(processes_left=0, processes_alive=0, failed=False)


def _required(name: str, purpose: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must name {purpose}")
    return value


@pytest.fixture(scope="module")
def image() -> str:
    """The image under test, which `VIDEO_IMAGE` names."""
    return _required("VIDEO_IMAGE", "the image under test: pull it first")


@pytest.fixture(scope="module")
def cap_mb() -> int:
    """The provider's cap this run gives, in MiB."""
    return int(_required(_CAP_ENVIRONMENT, "the provider's cap in MiB"))


@pytest.fixture(scope="module", autouse=True)
def room_on_the_card(request: pytest.FixtureRequest, cap_mb: int) -> None:
    """Refuse the run, before any container starts, when the card has no room."""
    request.getfixturevalue("image")
    watch = GroupWatch(read_card_memory, cap_mb, int(FREE_MEMORY_LINE.limit))
    refused = watch.refuse_first(lambda _stop: _NOTHING_LEFT)
    if refused:
        assert watch.stop is not None
        pytest.fail(f"the card has no room: {watch.stop.line()}")


@pytest.fixture(scope="module")
def caller_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A valid caller file in a directory the image's user reads."""
    return caller_file_in(tmp_path_factory.mktemp("callers"))


@pytest.fixture
def started(image: str, caller_file: Path) -> Iterator[Callable[[int], str]]:
    """Starts a container with the card at a cap; each is removed when the test ends."""
    with ExitStack() as stack:
        yield lambda cap: stack.enter_context(
            container(
                image,
                caller_file,
                cap_mb=cap,
                name_prefix="video-image-card",
                with_card=True,
            )
        )


def _health(name: str) -> Health:
    ran = docker("exec", name, "python", "-c", HEALTH)
    assert ran.returncode == 0, f"{ran.stderr}\n{logs_of(name)}"
    return Health.model_validate_json(ran.stdout)


def _is_running(name: str) -> bool:
    inspected = docker("inspect", "--format", "{{.State.Running}}", name)
    return inspected.stdout.strip() == "true"


def test_the_image_reaches_ready_on_the_card_within_the_start_bound(
    started: Callable[[int], str], cap_mb: int
) -> None:
    name = started(cap_mb)

    deadline = time.monotonic() + _START_WITHIN_SECONDS
    while _is_running(name) and time.monotonic() < deadline:
        if _health(name).ready:
            break
        time.sleep(POLL_SECONDS)

    health = _health(name)
    assert health.ready, logs_of(name)
    assert health.provider.name == ProviderName.FLASHHEAD_LITE


def test_a_cap_too_small_for_the_model_fails_the_provider_and_health_never_ready(
    started: Callable[[int], str],
) -> None:
    name = started(_TOO_SMALL_CAP_MB)

    wait_for_event(name, "provider.failed", within_seconds=_START_WITHIN_SECONDS)

    assert not _health(name).ready
