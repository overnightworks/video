"""Starting the built image as the deployment starts it, for the image tests.

Every container runs with a read-only root, `/tmp` writable and executable, no
capability, no new privilege and no network; `container` removes what it starts when
its block ends.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

import pytest
from video_provider_contract.worker import CARD_MEMORY_MB_ENV

from support.identity_support import write_caller_config
from video_service.config import Settings
from video_service.providers import ProviderName

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

RUN_AS_THE_DEPLOYMENT: Final = (
    "--read-only",
    "--tmpfs",
    "/tmp:exec",  # noqa: S108 - the container's own memory-backed directory
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--network",
    "none",
)
CALLER_DIRECTORY: Final = "/run/callers"
PORT: Final = Settings.model_fields["port"].default
POLL_SECONDS: Final = 1.0
HEALTH: Final = f"""
from video_client.health import fetch_health
outcome = fetch_health("http://127.0.0.1:{PORT}", deadline_seconds=5, cap_bytes=65536)
print(outcome.model_dump_json())
"""


def image_under_test() -> str:
    """The image `VIDEO_IMAGE` names; fail the test naming it when unset."""
    named = os.environ.get("VIDEO_IMAGE")
    if not named:
        pytest.fail(
            "VIDEO_IMAGE must name the image under test: build or pull it first"
        )
    return named


def caller_file_in(directory: Path) -> Path:
    """A valid caller file in `directory`, which the image's user reads."""
    directory.chmod(0o755)
    file = write_caller_config(directory)
    file.chmod(0o644)
    return file


def docker(*arguments: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """Run docker with the test's own arguments."""
    return subprocess.run(  # noqa: S603 - docker, with the test's own arguments
        ["docker", *arguments],  # noqa: S607 - the runner's docker
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def events(logs: str) -> list[dict[str, object]]:
    """The JSON event lines of a container's log."""
    return [json.loads(line) for line in logs.splitlines() if line.startswith("{")]


def logs_of(name: str) -> str:
    """Everything the container wrote, both streams."""
    logged = docker("logs", name)
    return logged.stdout + logged.stderr


def wait_for_event(name: str, event: str, *, within_seconds: float) -> None:
    """Return once the container logged `event`; fail the test after the bound."""
    deadline = time.monotonic() + within_seconds
    while time.monotonic() < deadline:
        if any(record["event"] == event for record in events(logs_of(name))):
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(f"the service wrote no {event} line:\n{logs_of(name)}")


@contextmanager
def container(
    image: str,
    caller_file: Path,
    *,
    cap_mb: int,
    name_prefix: str,
    with_card: bool = False,
) -> Iterator[str]:
    """A detached container of the service with `flashhead-lite`, removed at the end."""
    name = f"{name_prefix}-{uuid.uuid4().hex[:12]}"
    variables = [
        f"VIDEO_PROVIDER={ProviderName.FLASHHEAD_LITE}",
        f"{CARD_MEMORY_MB_ENV}={cap_mb}",
        f"VIDEO_CONFIG_FILE={CALLER_DIRECTORY}/{caller_file.name}",
    ]
    try:
        started = docker(
            "run",
            "--detach",
            "--name",
            name,
            *RUN_AS_THE_DEPLOYMENT,
            *(("--gpus", "all") if with_card else ()),
            "--volume",
            f"{caller_file.parent}:{CALLER_DIRECTORY}:ro",
            *(part for variable in variables for part in ("--env", variable)),
            image,
        )
        assert started.returncode == 0, started.stderr
        yield name
    finally:
        docker("rm", "--force", name)
