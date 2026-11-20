"""The card proof's dry run, driven through the command's own entry point."""

from __future__ import annotations

import json
import os
import signal
import struct
import subprocess
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
COMMAND: Final = REPOSITORY_ROOT / "scripts" / "prove_card.py"
RUN_WITHIN_SECONDS: Final = 300
APPEARS_WITHIN_SECONDS: Final = 120
GONE_WITHIN_SECONDS: Final = 15
POLL_SECONDS: Final = 0.05
AUDIO_RATE: Final = 16000
AUDIO_SECONDS: Final = 2
MARKER: Final = "PROVE_CARD_TEST_MARKER"
CELLS_BY_GROUP: Final = {"wire": ["sentences", "clear", "looks"], "lag": ["lag"]}
# A stand-in for the fleet's `probe-run`, which waits on the machine's load.
PROBE_RUN_STAND_IN: Final = """#!/usr/bin/env bash
echo "$*" >> "$PROBE_RUN_LOG"
[[ $1 == -k ]] && shift 3
exec "$@"
"""


class Run:
    """The command's surroundings: a `probe-run` on its path and a process marker."""

    def __init__(self, directory: Path) -> None:
        """Prepare `directory` with the stand-in, the audio and a private scratch."""
        self.directory = directory
        self.record = directory / "record.json"
        self.audio = directory / "speech.wav"
        self.scratch = directory / "scratch"
        self.probe_run_log = directory / "probe-run.log"
        self.marker = uuid.uuid4().hex
        binaries = directory / "bin"
        for path in (binaries, self.scratch):
            path.mkdir()
        stand_in = binaries / "probe-run"
        stand_in.write_text(PROBE_RUN_STAND_IN)
        stand_in.chmod(0o755)
        with wave.open(str(self.audio), "wb") as sound:
            sound.setnchannels(1)
            sound.setsampwidth(2)
            sound.setframerate(AUDIO_RATE)
            sound.writeframes(struct.pack("<h", 9000) * AUDIO_RATE * AUDIO_SECONDS)
        self.environment = os.environ | {
            "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(self.scratch),
            MARKER: self.marker,
            "PROBE_RUN_LOG": str(self.probe_run_log),
        }

    def arguments(self, *extra: str) -> list[str]:
        """The command line with the record and the audio, then `extra`."""
        return [
            sys.executable,
            str(COMMAND),
            "--record",
            str(self.record),
            "--audio",
            str(self.audio),
            *extra,
        ]

    def start(self, *extra: str) -> subprocess.Popen[bytes]:
        """Start the command in a process group of its own."""
        return subprocess.Popen(  # noqa: S603 - this checkout's interpreter and script
            self.arguments(*extra),
            env=self.environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def finish(self, command: subprocess.Popen[bytes]) -> tuple[int, str]:
        """Wait for the command: its exit status and what it wrote to standard error."""
        _, error = command.communicate(timeout=RUN_WITHIN_SECONDS)
        return command.returncode, error.decode()

    def processes(self) -> list[str]:
        """The command lines of the processes of this run, still alive."""
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                environment = (entry / "environ").read_bytes().split(b"\0")
                if f"{MARKER}={self.marker}".encode() in environment:
                    found.append((entry / "cmdline").read_bytes().decode())
            except OSError:
                continue
        return [line for line in found if line and "defunct" not in line]

    def wait_until(self, condition: Callable[[], bool], seconds: float) -> bool:
        """Whether `condition` holds within `seconds`."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(POLL_SECONDS)
        return condition()


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(tmp_path)


@pytest.fixture(scope="module")
def dry_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Run, dict]:
    """One dry run of every cell, which the tests below read."""
    prepared = Run(tmp_path_factory.mktemp("dry-run"))
    status, error = prepared.finish(prepared.start("--dry-run"))
    assert status == 0, error
    return prepared, json.loads(prepared.record.read_text())


def test_a_dry_run_records_each_sentence_against_the_audio(
    dry_run: tuple[Run, dict],
) -> None:
    sentences = dry_run[1]["cells"]["sentences"]["sentences"]

    assert [sentence["sentence"] for sentence in sentences] == ["first", "second"]
    for sentence in sentences:
        assert (
            sentence["frame_count"]["value"]
            == (sentence["frames_the_audio_needs"]["value"])
        )
        assert sentence["late_frame_count"]["value"] >= 0
        assert sentence["first_output_ms"]["value"] > 0


def test_a_dry_run_gives_no_verdict_on_a_card_line(dry_run: tuple[Run, dict]) -> None:
    cells = dry_run[1]["cells"]
    judged = [
        cells["sentences"]["sentences"][0]["first_output_ms"],
        cells["clear"]["clear_to_confirmation_ms"],
        dry_run[1]["services"]["wire"]["start_to_ready_ms"],
        dry_run[1]["viewers"]["frames_per_second"],
    ]

    assert [figure["verdict"] for figure in judged] == ["not_measured"] * 4
    assert all(figure["reason"] and figure["line"] for figure in judged)


def test_a_clear_inside_a_sentence_is_confirmed_and_cancels_it(
    dry_run: tuple[Run, dict],
) -> None:
    cleared = dry_run[1]["cells"]["clear"]

    assert cleared["outcome"]["value"] == "cancelled"
    assert cleared["clear_to_confirmation_ms"]["value"] >= 0


def test_two_looks_in_sequence_are_each_recorded_under_their_look(
    dry_run: tuple[Run, dict],
) -> None:
    looks = dry_run[1]["cells"]["looks"]

    assert len({session["look"] for session in looks}) == len(looks) > 1
    assert all(session["frame_count"]["value"] > 0 for session in looks)


def test_a_lag_far_too_small_is_recorded_as_unavailable(
    dry_run: tuple[Run, dict],
) -> None:
    assert dry_run[1]["cells"]["lag"]["ended_unavailable"]["value"] is True


def test_every_service_of_the_run_stops_and_none_is_left(
    dry_run: tuple[Run, dict],
) -> None:
    services = dry_run[1]["services"]

    assert set(services) == {"wire", "lag"}
    assert all(
        service["stop_to_exit_ms"]["value"] >= 0 for service in services.values()
    )
    assert dry_run[0].processes() == []
    assert list(dry_run[0].scratch.iterdir()) == []


@pytest.mark.parametrize("named", ["lag", "clear"])
def test_only_the_named_cells_run(run: Run, named: str) -> None:
    status, error = run.finish(run.start("--dry-run", "--cells", named))

    assert status == 0, error
    assert list(json.loads(run.record.read_text())["cells"]) == [named]


def test_each_group_runs_under_probe_run_with_its_kill_grace_and_cap(
    dry_run: tuple[Run, dict],
) -> None:
    calls = dry_run[0].probe_run_log.read_text().splitlines()

    assert len(calls) == len(CELLS_BY_GROUP)
    for call, cells in zip(calls, CELLS_BY_GROUP.values(), strict=True):
        arguments = call.split()
        assert arguments[0] == "-k"
        assert arguments[1].isdigit()
        assert arguments[2].isdigit()
        assert arguments[arguments.index("--cells") + 1 :][: len(cells)] == cells


def test_without_probe_run_on_the_path_the_command_refuses_and_names_it(
    run: Run, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    run.environment = run.environment | {"PATH": str(empty)}

    status, error = run.finish(run.start("--dry-run"))

    assert status != 0
    assert "probe-run is not on PATH" in error
    assert not run.record.exists()


def test_without_dry_run_the_command_refuses_and_names_what_is_missing(
    run: Run,
) -> None:
    status, error = run.finish(run.start())

    assert status != 0
    assert "--dry-run" in error
    assert "viewers" in error
    assert not run.record.exists()


def test_an_interrupted_run_leaves_no_process_and_no_file_but_the_record(
    run: Run,
) -> None:
    command = run.start("--dry-run")
    serving = run.wait_until(
        lambda: any("video_service" in line for line in run.processes()),
        APPEARS_WITHIN_SECONDS,
    )
    os.killpg(command.pid, signal.SIGTERM)
    run.finish(command)

    assert serving
    assert run.wait_until(lambda: run.processes() == [], GONE_WITHIN_SECONDS)
    assert run.wait_until(lambda: not any(run.scratch.iterdir()), GONE_WITHIN_SECONDS)
    assert not run.record.exists()
