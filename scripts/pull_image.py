"""Pull a released image from ghcr with a login that lives only for this run.

The login goes into a fresh temporary `DOCKER_CONFIG` that only this command's
children see; the operator's own docker configuration is never read or written. The
directory is removed on every exit, a stop included. The GitHub token passes from
`gh auth token` to `docker login` on standard input and is never printed.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, Final

from card_drive import STOP_SIGNALS, exit_on_stop

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

REGISTRY: Final = "ghcr.io"


def run_step(
    step: str,
    command: Sequence[str],
    environment: Mapping[str, str],
    *,
    standard_input: str | None = None,
    capture: bool = False,
) -> str:
    """Run one step: its output when captured; a failure ends with one line."""
    try:
        finished = subprocess.run(  # noqa: S603 - the operator's gh and docker with fixed arguments
            command,
            env=environment,
            input=standard_input,
            stdout=subprocess.PIPE if capture else None,
            text=True,
            check=False,
        )
    except OSError as error:
        message = f"{step} failed: {error.strerror}"
        raise SystemExit(message) from None
    if finished.returncode != 0:
        message = f"{step} failed with status {finished.returncode}"
        raise SystemExit(message)
    return finished.stdout.strip() if capture else ""


def pull(image: str, directory: str) -> None:
    """Log in to ghcr as the gh login inside `directory`, then pull `image`."""
    environment = os.environ | {"DOCKER_CONFIG": directory}
    token = run_step(
        "gh auth token", ["gh", "auth", "token"], environment, capture=True
    )
    login = run_step(
        "gh api user",
        ["gh", "api", "user", "--jq", ".login"],
        environment,
        capture=True,
    )
    run_step(
        "docker login",
        ["docker", "login", REGISTRY, "-u", login, "--password-stdin"],
        environment,
        standard_input=token,
    )
    run_step("docker pull", ["docker", "pull", image], environment)


def main(argv: Sequence[str] | None = None) -> int:
    """Pull the image; a failed step ends the command with one line naming it."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", help="the image to pull, with its tag")
    arguments = parser.parse_args(argv)
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, exit_on_stop)
    with tempfile.TemporaryDirectory(prefix="pull-image-") as directory:
        pull(arguments.image, directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
