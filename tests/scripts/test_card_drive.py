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
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

import card_drive
from support.prove_card_support import (
    APPEARS_WITHIN_SECONDS,
    GONE_WITHIN_SECONDS,
    ended,
    marked_processes,
    process_state,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

SCRIPTS: Final = Path(__file__).resolve().parents[2] / "scripts"
MARKER: Final = "CARD_DRIVE_TEST_MARKER"
ENDS_WITHIN_SECONDS: Final = 60
TREE_PROCESSES: Final = 6
PARENT_OF_A_SLEEPER: Final = """
import subprocess

sleeper = subprocess.Popen(["sleep", "60"])
print(sleeper.pid, flush=True)
sleeper.wait()
"""
NAMED_WITH_A_SPLIT_LETTER: Final = """
import ctypes
import time

ctypes.CDLL(None).prctl(15, ("a" * 14 + "\\u00fc").encode())
print("named", flush=True)
time.sleep(60)
"""
# The stand-in top marks itself a subreaper through the code under test, starts what
# its mode needs and runs the kill when the test says so.
STAND_IN_TOP: Final = """
import dataclasses
import json
import os
import signal
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


def killed(control=card_drive.KERNEL, **keywords):
    kill = card_drive.end_processes_below(control, **keywords)
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
    card_drive.signal_if_unchanged((other_start,), signal.SIGKILL)
    card_drive.signal_if_unchanged((other_parent,), signal.SIGKILL)
    card_drive.signal_if_unchanged((seen[2],), signal.SIGKILL)
    report({"reused": seen[0].pid, "reparented": seen[1].pid, "unchanged": seen[2].pid})
elif mode in ("ancestry-stale", "ancestry-intact", "ancestry-term"):
    parent = subprocess.Popen(
        [sys.executable, "-c", PARENT_OF_A_SLEEPER], stdout=subprocess.PIPE, text=True
    )
    grandchild = int(parent.stdout.readline())
    lineage = (card_drive.read_process(grandchild), card_drive.read_process(parent.pid))
    if mode == "ancestry-stale":
        # The parent the read showed is gone and its id names another process.
        replaced = dataclasses.replace(lineage[1], started=lineage[1].started + 1)
        lineage = (lineage[0], replaced)
    signals = []

    def recorded(pidfd, signum):
        signals.append(signum)
        card_drive.KERNEL.send_signal(pidfd, signum)

    control = dataclasses.replace(card_drive.KERNEL, send_signal=recorded)
    wanted = signal.SIGTERM if mode == "ancestry-term" else signal.SIGKILL
    card_drive.signal_if_unchanged(lineage, wanted, control)
    report({"grandchild": grandchild, "signals": signals})
elif mode == "spare":
    spared, reaped = sleeper(), sleeper()
    result = killed(spare=spared.pid)
    # The kill ends the spared child and reaps none but the others: only its own
    # `wait` collects the status.
    report(result | {"spared": spared.wait(), "reaped": reaped.pid})
elif mode == "raising":
    unkillable, killable = sleeper(), sleeper()

    def refused(pid):
        if pid == unkillable.pid:
            raise PermissionError
        return card_drive.KERNEL.open(pid)

    control = dataclasses.replace(card_drive.KERNEL, open=refused)
    report(killed(control) | {"unkillable": unkillable.pid, "killable": killable.pid})
elif mode == "unreadable":
    unreadable, killable = sleeper(), sleeper()

    def refused(pid):
        if pid == unreadable.pid:
            raise PermissionError
        return card_drive.KERNEL.read_stat(pid)

    control = dataclasses.replace(card_drive.KERNEL, read_stat=refused)
    report(killed(control) | {"unreadable": unreadable.pid, "killable": killable.pid})
elif mode == "unlistable":
    sleeper()

    def refused():
        raise PermissionError

    control = dataclasses.replace(card_drive.KERNEL, list_processes=refused)
    report(killed(control))
elif mode == "unlistable-briefly":
    sleeping = sleeper()
    refusals = [PermissionError]

    def refused_once():
        if refusals:
            raise refusals.pop()
        return card_drive.KERNEL.list_processes()

    control = dataclasses.replace(card_drive.KERNEL, list_processes=refused_once)
    report(killed(control) | {"sleeper": sleeping.pid})
elif mode == "unreadable-briefly":
    sleeping = sleeper()
    refusals = [PermissionError]

    def refused_once(pid):
        if pid == sleeping.pid and refusals:
            raise refusals.pop()
        return card_drive.KERNEL.read_stat(pid)

    control = dataclasses.replace(card_drive.KERNEL, read_stat=refused_once)
    report(killed(control) | {"sleeper": sleeping.pid})
elif mode == "split-name":
    # The kernel cuts a name at 15 bytes, here inside the two bytes of a letter.
    named = subprocess.Popen(
        [sys.executable, "-c", NAMED_WITH_A_SPLIT_LETTER],
        stdout=subprocess.PIPE,
        text=True,
    )
    named.stdout.readline()
    report(killed() | {"named": named.pid})
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
        identity,
        directory / "service.log",
        max_lag_ms=500,
        compiler_home=directory,
        service_pid_file=directory / "service.record",
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
        return marked_processes(MARKER, self.marker)

    def result(self) -> dict[str, object]:
        """What the stand-in reported, once it has."""
        path = self.directory / "result.json"
        assert wait_until(path.exists, ENDS_WITHIN_SECONDS)
        return json.loads(path.read_text())

    def end(self) -> None:
        """Let the stand-in finish, then end what it left running.

        The stand-in ends on its own, so it saves its coverage data; what it left
        are sleepers and their parents, which do not end by themselves.
        """
        (self.directory / "end").touch()
        try:
            if self.process is not None:
                self.process.wait(timeout=ENDS_WITHIN_SECONDS)
        finally:
            for pid in self.marked():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            if self.process is not None:
                self.process.kill()
                self.process.wait(timeout=ENDS_WITHIN_SECONDS)


@pytest.fixture
def top(tmp_path: Path) -> Iterator[StandInTop]:
    children = (
        f"PARENT_OF_A_SLEEPER = {PARENT_OF_A_SLEEPER!r}\n"
        f"NAMED_WITH_A_SPLIT_LETTER = {NAMED_WITH_A_SPLIT_LETTER!r}\n"
    )
    stand_in = StandInTop(tmp_path, children + STAND_IN_TOP)
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


@pytest.mark.parametrize("mode", ["ancestry-stale", "ancestry-intact"])
def test_a_process_is_signalled_only_while_its_parents_are_still_those_that_were_read(
    top: StandInTop, mode: str
) -> None:
    ended_by_kill = mode == "ancestry-intact"
    top.start(mode)

    result = top.result()

    assert bool(result["signals"]) == ended_by_kill
    assert wait_until(
        lambda: ended(result["grandchild"]) == ended_by_kill, GONE_WITHIN_SECONDS
    )
    assert ended(result["grandchild"]) == ended_by_kill


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


def test_a_process_that_cannot_be_read_fails_the_kill_which_goes_on_with_the_others(
    top: StandInTop,
) -> None:
    top.start("unreadable")

    result = top.result()

    assert result["failed"] is True
    assert process_state(result["unreadable"]) not in {None, "Z"}
    assert ended(result["killable"])


def test_a_kill_that_cannot_list_the_processes_fails_instead_of_raising(
    top: StandInTop,
) -> None:
    top.start("unlistable")

    assert top.result()["failed"] is True


@pytest.mark.parametrize("mode", ["unlistable-briefly", "unreadable-briefly"])
def test_a_kill_that_cannot_see_the_processes_at_first_retries_until_it_ends_them(
    top: StandInTop, mode: str
) -> None:
    top.start(mode)

    result = top.result()

    assert result["failed"] is True
    assert wait_until(lambda: ended(result["sleeper"]), GONE_WITHIN_SECONDS)


def test_a_process_whose_name_the_kernel_cut_inside_a_letter_is_ended(
    top: StandInTop,
) -> None:
    top.start("split-name")

    result = top.result()

    assert result["failed"] is False
    assert result["alive"] == 0
    assert wait_until(lambda: ended(result["named"]), GONE_WITHIN_SECONDS)


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
    try:
        assert wait_until(pids_file.exists, APPEARS_WITHIN_SECONDS)
        pids = json.loads(pids_file.read_text())
    finally:
        holder.kill()
        holder.wait(timeout=ENDS_WITHIN_SECONDS)

    gone = wait_until(lambda: all(ended(pid) for pid in pids), GONE_WITHIN_SECONDS)

    assert len(pids) > 1
    assert gone


def test_a_process_is_sent_the_signal_asked_for_through_the_same_check(
    top: StandInTop,
) -> None:
    top.start("ancestry-term")

    result = top.result()

    assert result["signals"] == [signal.SIGTERM]
    assert wait_until(lambda: ended(result["grandchild"]), GONE_WITHIN_SECONDS)


def test_the_kill_ends_the_spared_child_but_leaves_its_status_to_its_own_wait(
    top: StandInTop,
) -> None:
    top.start("spare")

    result = top.result()

    assert result["failed"] is False
    assert result["alive"] == 0
    assert result["spared"] == -signal.SIGKILL
    assert wait_until(lambda: ended(result["reaped"]), GONE_WITHIN_SECONDS)
    assert process_state(result["reaped"]) is None


def test_a_line_the_service_has_not_ended_is_read_on_a_later_poll(
    tmp_path: Path,
) -> None:
    log = tmp_path / "service.log"
    log.write_text(
        '{"event": "session.started", "id": 1}\n{"event": "session.started", "id',
        encoding="utf-8",
    )
    service = card_drive.Service(None, 0, log)  # type: ignore[arg-type]

    assert service.lines("session.started") == [{"event": "session.started", "id": 1}]

    log.write_text(log.read_text(encoding="utf-8") + '": 2}\n', encoding="utf-8")

    assert [record["id"] for record in service.lines("session.started")] == [1, 2]
