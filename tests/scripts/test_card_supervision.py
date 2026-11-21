"""The card guard's knowledge of a group's service."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from card_drive import KERNEL
from card_supervision import pid_file, service_exists, service_pid_file

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
_GROUP = [
    sys.executable,
    "-c",
    (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen({_SLEEP!r})\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    ),
]


class _Group:
    """A group's process with one child that is not the service."""

    def __init__(self, fragment: Path) -> None:
        self.fragment = fragment
        self.process = subprocess.Popen(_GROUP, stdout=subprocess.PIPE, text=True)  # noqa: S603
        assert self.process.stdout is not None
        self.child = int(self.process.stdout.readline())
        pid_file(fragment).write_text(str(self.process.pid))

    def name_in_service_file(self, content: str) -> None:
        service_pid_file(self.fragment).write_text(content)


@pytest.fixture
def group(tmp_path: Path) -> Iterator[_Group]:
    started = _Group(tmp_path / "group.json")
    yield started
    for pid in (started.child, started.process.pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    started.process.wait()


def test_a_child_of_the_group_is_no_service_while_no_service_file_names_it(
    group: _Group,
) -> None:
    assert not service_exists(group.fragment, KERNEL)


def test_the_process_the_service_file_names_is_the_service_when_it_is_the_groups_child(
    group: _Group,
) -> None:
    group.name_in_service_file(str(group.child))

    assert service_exists(group.fragment, KERNEL)


def _gone_pid() -> str:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return str(process.pid)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(
            lambda _group: str(os.getpid()), id="a_live_process_that_is_not_its_child"
        ),
        pytest.param(lambda _group: _gone_pid(), id="a_gone_process"),
        pytest.param(lambda _group: "not a number", id="no_number"),
    ],
)
def test_a_service_file_that_does_not_name_a_child_of_the_group_leaves_no_service(
    group: _Group, content: object
) -> None:
    group.name_in_service_file(content(group))  # type: ignore[operator]

    assert not service_exists(group.fragment, KERNEL)
