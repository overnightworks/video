"""The end-of-group kill against real processes, and the service's parent-death signal.

The kill ends every process below its caller, so each case runs it in a stand-in top,
a process the test starts, and never in the pytest worker.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from support.prove_card_support import (
    APPEARS_WITHIN_SECONDS,
    GONE_WITHIN_SECONDS,
    POLL_SECONDS,
    ended,
    process_state,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

SCRIPTS: Final = Path(__file__).resolve().parents[2] / "scripts"
MARKER: Final = "CARD_DRIVE_TEST_MARKER"
ENDS_WITHIN_SECONDS: Final = 60
TREE_PROCESSES: Final = 6
# The stand-in top marks itself a subreaper through the code under test, starts what
# its mode needs and runs the kill when the test says so.
STAND_IN_TOP: Final = """
import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import card_drive

directory = Path(sys.argv[1])
mode = sys.argv[2]


def wait_for(name):
    while not (directory / name).exists():
        time.sleep(0.01)


def report(result):
    (directory / "result.tmp").write_text(json.dumps(result))
    (directory / "result.tmp").replace(directory / "result.json")
    wait_for("end")


def level(depth):
    if depth:
        subprocess.Popen(
            [sys.executable, __file__, str(directory), "level", str(depth - 1)],
            start_new_session=True,
        )
        time.sleep(3600)
    while True:
        subprocess.Popen(["sleep", "5"])
        time.sleep(0.05)


def sleeper():
    return subprocess.Popen(["sleep", "60"])


def killed():
    kill = card_drive.end_processes_below()
    return {
        "left": kill.processes_left,
        "alive": kill.processes_alive,
        "failed": kill.failed,
    }


if mode == "level":
    level(int(sys.argv[3]))
card_drive.become_subreaper()
if mode == "tree":
    subprocess.Popen([sys.executable, __file__, str(directory), "level", "2"])
    wait_for("go")
    report(killed())
elif mode == "guard":
    reused, reparented, unchanged = sleeper(), sleeper(), sleeper()
    seen = [card_drive.read_process(c.pid) for c in (reused, reparented, unchanged)]
    other_start = dataclasses.replace(seen[0], started=seen[0].started + 1)
    other_parent = dataclasses.replace(seen[1], parent=seen[1].parent + 1)
    card_drive.end_if_unchanged(other_start)
    card_drive.end_if_unchanged(other_parent)
    card_drive.end_if_unchanged(seen[2])
    report({"reused": seen[0].pid, "reparented": seen[1].pid, "unchanged": seen[2].pid})
elif mode == "raising":
    unkillable, killable = sleeper(), sleeper()
    opened = card_drive._pidfd_open

    def refused(pid):
        if pid == unkillable.pid:
            raise PermissionError
        return opened(pid)

    card_drive._pidfd_open = refused
    report(killed() | {"unkillable": unkillable.pid, "killable": killable.pid})
"""
# Holds the service the way the proof does and reports the pids it should take with it.
SERVICE_HOLDER: Final = """
import json
import sys
import time
from pathlib import Path

import card_drive
import card_identity

directory = Path(sys.argv[1])
with (
    card_identity.throwaway_identity(directory) as identity,
    card_drive.running_service(
        identity, directory / "service.log", max_lag_ms=500
    ) as service,
):
    pids = [service.process.pid, *card_drive.worker_pids(service.process.pid)]
    (directory / "pids.json").write_text(json.dumps(pids))
    time.sleep(3600)
"""


class StandInTop:
    """A stand-in top the test started, and the processes marked as its own."""

    def __init__(self, directory: Path, script: str) -> None:
        """Write `script` into `directory` and prepare the marked environment."""
        self.directory = directory
        self.marker = uuid.uuid4().hex
        self.script = directory / "stand_in.py"
        self.script.write_text(script)
        self.environment = os.environ | {
            MARKER: self.marker,
            "PYTHONPATH": str(SCRIPTS),
        }
        self.process: subprocess.Popen[bytes] | None = None

    def start(self, *arguments: str) -> subprocess.Popen[bytes]:
        """Start the script in a session of its own."""
        self.process = subprocess.Popen(  # noqa: S603 - this interpreter and a script the test wrote
            [sys.executable, str(self.script), str(self.directory), *arguments],
            env=self.environment,
            start_new_session=True,
        )
        return self.process

    def marked(self) -> dict[int, str]:
        """Every live process of this test, by pid, with its state letter."""
        found = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                environment = (entry / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if f"{MARKER}={self.marker}".encode() in environment:
                state = process_state(int(entry.name))
                if state not in {None, "Z"}:
                    found[int(entry.name)] = state
        return found

    def result(self) -> dict[str, object]:
        """What the stand-in reported, once it has."""
        path = self.directory / "result.json"
        assert wait_until(path.exists, ENDS_WITHIN_SECONDS)
        return json.loads(path.read_text())

    def end(self) -> None:
        """Let the stand-in finish and end every process of this test."""
        (self.directory / "end").touch()
        for pid in self.marked():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        if self.process is not None:
            self.process.wait(timeout=ENDS_WITHIN_SECONDS)


@pytest.fixture
def top(tmp_path: Path) -> Iterator[StandInTop]:
    stand_in = StandInTop(tmp_path, STAND_IN_TOP)
    yield stand_in
    stand_in.end()


def test_the_kill_ends_a_tree_of_three_levels_and_spares_an_unrelated_sibling(
    top: StandInTop,
) -> None:
    sibling = subprocess.Popen(["sleep", "60"])  # noqa: S607 - the test's own sibling
    try:
        top.start("tree")
        formed = wait_until(
            lambda: len(top.marked()) >= TREE_PROCESSES, APPEARS_WITHIN_SECONDS
        )
        tree = set(top.marked()) - {top.process.pid}
        (top.directory / "go").touch()
        stopped: list[int] = []

        def reported() -> bool:
            stopped.extend(
                pid for pid, state in top.marked().items() if state in {"T", "t"}
            )
            return (top.directory / "result.json").exists()

        wait_until(reported, ENDS_WITHIN_SECONDS)
        result = top.result()

        assert formed
        assert len(tree) >= TREE_PROCESSES - 1
        assert result["failed"] is False
        assert result["left"] > 0
        assert result["alive"] == 0
        assert all(ended(pid) for pid in tree)
        assert stopped == []
        assert sibling.poll() is None
    finally:
        sibling.kill()
        sibling.wait()


def test_a_pid_that_is_not_the_process_that_was_read_is_not_signalled(
    top: StandInTop,
) -> None:
    top.start("guard")

    result = top.result()

    assert process_state(result["reused"]) not in {None, "Z"}
    assert process_state(result["reparented"]) not in {None, "Z"}
    assert ended(result["unchanged"])


def test_a_kill_that_raises_for_one_process_goes_on_with_the_others(
    top: StandInTop,
) -> None:
    top.start("raising")

    result = top.result()

    assert result["failed"] is True
    assert result["left"] == len({result["unkillable"], result["killable"]})
    assert result["alive"] == 1
    assert process_state(result["unkillable"]) not in {None, "Z"}
    assert ended(result["killable"])


def test_the_service_and_its_worker_end_with_the_process_that_started_them(
    tmp_path: Path,
) -> None:
    script = tmp_path / "service_holder.py"
    script.write_text(SERVICE_HOLDER)
    holder = subprocess.Popen(  # noqa: S603 - this interpreter and a script the test wrote
        [sys.executable, str(script), str(tmp_path)],
        env=os.environ | {"PYTHONPATH": str(SCRIPTS)},
    )
    pids_file = tmp_path / "pids.json"
    deadline = time.monotonic() + APPEARS_WITHIN_SECONDS
    try:
        while not pids_file.exists() and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        assert pids_file.exists()
        pids = json.loads(pids_file.read_text())
    finally:
        holder.kill()
        holder.wait(timeout=ENDS_WITHIN_SECONDS)

    deadline = time.monotonic() + GONE_WITHIN_SECONDS
    while not all(ended(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)

    assert len(pids) > 1
    assert all(ended(pid) for pid in pids)
