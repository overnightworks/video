"""The image pull: a login that lives only inside a directory the run removes."""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import stat
import subprocess
import sys
from typing import TYPE_CHECKING, Final, NamedTuple

import pytest

from support.prove_card_support import COMMAND

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

PULL_IMAGE: Final = COMMAND.with_name("pull_image.py")
IMAGE: Final = "ghcr.io/example/video:1.2.3"
TOKEN: Final = "ghp_secret-token-value"  # noqa: S105 - a stand-in's value
LOGIN: Final = "octocat"
WITHIN_SECONDS: Final = 60

# The stand-ins fail a step named in STAND_IN_FAILS, and a pull holds on a fifo the
# test names in STAND_IN_HOLD after it has written to the fifo named in STAND_IN_HELD.
GH: Final = f"""#!{sys.executable}
import os, sys
if os.environ.get("STAND_IN_FAILS") == " ".join(sys.argv[1:3]):
    sys.exit(4)
print(os.environ["STAND_IN_" + ("TOKEN" if sys.argv[1] == "auth" else "LOGIN")])
"""
DOCKER: Final = f"""#!{sys.executable}
import json, os, pathlib, stat, sys
config = pathlib.Path(os.environ["DOCKER_CONFIG"])
step = sys.argv[1]
saw = {{
    "argv": sys.argv[1:],
    "config": str(config),
    "mode": stat.S_IMODE(config.stat().st_mode),
    "stdin": sys.stdin.read() if step == "login" else None,
}}
with open(os.environ["STAND_IN_LOG"], "a") as log:
    log.write(json.dumps(saw) + "\\n")
if step == "login":
    (config / "config.json").write_text(saw["stdin"])
if os.environ.get("STAND_IN_FAILS") == "docker " + step:
    sys.exit(5)
if step == "pull" and "STAND_IN_HOLD" in os.environ:
    open(os.environ["STAND_IN_HELD"], "w").write("held")
    open(os.environ["STAND_IN_HOLD"]).read()
"""


class Seen(NamedTuple):
    """One call to the stand-in `docker`."""

    argv: list[str]
    config: str
    mode: int
    stdin: str | None


class Pull:
    """One run of the command with stand-in `gh` and `docker` under a temporary home."""

    def __init__(self, directory: Path) -> None:
        """Lay out the home, the stand-ins and the operator's own configurations."""
        self.home = directory / "home"
        self.temporary = directory / "temporary"
        self.operator_config = directory / "operator-config"
        binaries = directory / "bin"
        self.log = directory / "docker.log"
        self.held = directory / "held"
        self.hold = directory / "hold"
        for path in (self.home / ".docker", self.temporary, self.operator_config):
            path.mkdir(parents=True)
        binaries.mkdir()
        for name, text in (("gh", GH), ("docker", DOCKER)):
            (binaries / name).write_text(text)
            (binaries / name).chmod(0o755)
        self.sentinel = self.home / ".docker" / "config.json"
        self.sentinel.write_text("operator's login")
        self.binaries = binaries
        self.environment = os.environ | {
            "PATH": str(binaries),
            "HOME": str(self.home),
            "TMPDIR": str(self.temporary),
            "DOCKER_CONFIG": str(self.operator_config),
            "STAND_IN_TOKEN": TOKEN,
            "STAND_IN_LOGIN": LOGIN,
            "STAND_IN_LOG": str(self.log),
        }

    def fail(self, step: str) -> None:
        """Have the stand-in fail the step, named by its first two words."""
        self.environment["STAND_IN_FAILS"] = step

    def hold_the_pull(self) -> None:
        """Have the pull wait on a fifo that nothing writes to."""
        os.mkfifo(self.held)
        os.mkfifo(self.hold)
        self.environment["STAND_IN_HELD"] = str(self.held)
        self.environment["STAND_IN_HOLD"] = str(self.hold)

    def start(self) -> subprocess.Popen[str]:
        """Start the command for the image, with its output piped."""
        return subprocess.Popen(  # noqa: S603 - this checkout's interpreter and script
            [sys.executable, str(PULL_IMAGE), IMAGE],
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def wait_until_held(self) -> None:
        """Return once the pull has started and waits."""
        descriptor = os.open(self.held, os.O_RDONLY | os.O_NONBLOCK)
        try:
            ready, _, _ = select.select([descriptor], [], [], WITHIN_SECONDS)
        finally:
            os.close(descriptor)
        assert ready, "the pull never started"

    def release_the_pull(self) -> None:
        """Let a pull that still waits end; there is none once the command killed it."""
        with contextlib.suppress(OSError):
            os.close(os.open(self.hold, os.O_WRONLY | os.O_NONBLOCK))

    def docker_calls(self) -> list[Seen]:
        """What each call of the stand-in `docker` saw, in order."""
        if not self.log.exists():
            return []
        return [Seen(**json.loads(line)) for line in self.log.read_text().splitlines()]

    def assert_untouched(self) -> None:
        """The operator's docker files and the temporary directory are as they were."""
        assert self.sentinel.read_text() == "operator's login"
        assert [path.name for path in (self.home / ".docker").iterdir()] == [
            "config.json"
        ]
        assert list(self.operator_config.iterdir()) == []
        assert list(self.temporary.iterdir()) == []


@pytest.fixture
def pull(tmp_path: Path) -> Iterator[Pull]:
    prepared = Pull(tmp_path)
    yield prepared
    prepared.release_the_pull()


def test_a_pull_logs_in_and_pulls_inside_one_directory_that_is_gone_afterwards(
    pull: Pull,
) -> None:
    command = pull.start()
    output, error = command.communicate(timeout=WITHIN_SECONDS)

    login, pulled = pull.docker_calls()
    assert command.returncode == 0
    assert login.argv == ["login", "ghcr.io", "-u", LOGIN, "--password-stdin"]
    assert login.stdin == TOKEN
    assert pulled.argv == ["pull", IMAGE]
    assert login.config == pulled.config
    assert login.config not in {str(pull.home / ".docker"), str(pull.operator_config)}
    assert login.mode == stat.S_IRWXU
    assert TOKEN not in output + error
    pull.assert_untouched()


@pytest.mark.parametrize(
    ("stand_in", "step", "calls"),
    [
        pytest.param("auth token", "gh auth token", 0, id="the-token-is-refused"),
        pytest.param("api user", "gh api user", 0, id="the-login-is-refused"),
        pytest.param("docker login", "docker login", 1, id="the-login-is-rejected"),
        pytest.param("docker pull", "docker pull", 2, id="the-pull-fails"),
    ],
)
def test_a_failed_step_ends_with_one_line_naming_it_and_leaves_nothing(
    pull: Pull, stand_in: str, step: str, calls: int
) -> None:
    pull.fail(stand_in)
    command = pull.start()
    output, error = command.communicate(timeout=WITHIN_SECONDS)

    assert command.returncode != 0
    assert len(error.splitlines()) == 1
    assert step in error
    assert TOKEN not in output + error
    assert len(pull.docker_calls()) == calls
    pull.assert_untouched()


def test_a_missing_tool_ends_with_one_line_naming_it(pull: Pull) -> None:
    (pull.binaries / "gh").unlink()
    command = pull.start()
    _, error = command.communicate(timeout=WITHIN_SECONDS)

    assert command.returncode != 0
    assert len(error.splitlines()) == 1
    assert "gh auth token" in error
    assert pull.docker_calls() == []
    pull.assert_untouched()


@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM])
def test_a_stop_during_the_pull_ends_the_run_and_removes_the_directory(
    pull: Pull, stop: signal.Signals
) -> None:
    pull.hold_the_pull()
    command = pull.start()
    pull.wait_until_held()

    command.send_signal(stop)
    output, error = command.communicate(timeout=WITHIN_SECONDS)

    assert command.returncode == 128 + stop
    assert TOKEN not in output + error
    assert [call.argv[0] for call in pull.docker_calls()] == ["login", "pull"]
    pull.assert_untouched()
