"""The card proof's test surroundings: the command's run, its stand-ins and waits."""

from __future__ import annotations

import contextlib
import os
import struct
import subprocess
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Final

from card_record import Verdict

if TYPE_CHECKING:
    from collections.abc import Callable

    from card_record import Reason

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPTS: Final = REPOSITORY_ROOT / "scripts"
COMMAND: Final = SCRIPTS / "prove_card.py"
RUN_WITHIN_SECONDS: Final = 300
APPEARS_WITHIN_SECONDS: Final = 120
GONE_WITHIN_SECONDS: Final = 15
POLL_SECONDS: Final = 0.05
AUDIO_RATE: Final = 16000
AUDIO_SECONDS: Final = 2
MARKER: Final = "PROVE_CARD_TEST_MARKER"
CELLS_BY_GROUP: Final = {
    "wire": ["sentences", "clear", "looks", "viewer", "viewers"],
    "lag": ["lag"],
}
SERVICE_COMMAND: Final = "video_service"
# A stand-in for the fleet's `probe-run`, which waits on the machine's load and then
# starts the command on this line.
PROBE_RUN_STAND_IN: Final = """#!/usr/bin/env bash
echo "$*" >> "$PROBE_RUN_LOG"
exec setpriv --pdeathsig TERM timeout "$@"
"""


def write_audio(path: Path, *, seconds: float, channels: int = 1) -> None:
    """A WAV of a square wave, which a viewer hears as loud."""
    square = struct.pack("<8h", *([9000] * 4 + [-9000] * 4)) * channels
    with wave.open(str(path), "wb") as sound:
        sound.setnchannels(channels)
        sound.setsampwidth(2)
        sound.setframerate(AUDIO_RATE)
        sound.writeframes(square * int(AUDIO_RATE * seconds // 8))


def process_state(pid: int) -> str | None:
    """The process's state letter, or none once nothing has the id."""
    try:
        return (
            Path(f"/proc/{pid}/stat")
            .read_bytes()
            .rsplit(b")", 1)[1]
            .split()[0]
            .decode()
        )
    except OSError:
        return None


def marked_processes(name: str, value: str) -> dict[int, str]:
    """The live processes whose environment holds `name=value`, by pid, with state."""
    found = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environment = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if f"{name}={value}".encode() in environment:
            state = process_state(int(entry.name))
            if state not in {None, "Z"}:
                found[int(entry.name)] = state
    return found


def ended(pid: int) -> bool:
    """Whether the process is gone or a zombie that only waits to be reaped."""
    return process_state(pid) in {None, "Z"}


def is_not_measured(figure: dict, reason: Reason) -> bool:
    """Whether `figure` is nothing measured, for `reason`."""
    return figure == {"verdict": Verdict.NOT_MEASURED, "reason": reason}


def wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    """Whether `condition` holds within `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(POLL_SECONDS)
    return condition()


class Run:
    """The command's surroundings: a `probe-run` on its path and a process marker."""

    def __init__(self, directory: Path) -> None:
        """Prepare `directory` with the stand-in, the audio and a private scratch."""
        self.directory = directory
        self.record = directory / "record.json"
        self.audio = directory / "speech.wav"
        self.scratch = directory / "scratch"
        self.probe_run_log = directory / "probe-run.log"
        self.output = directory / "stdout.txt"
        self.marker = uuid.uuid4().hex
        binaries = directory / "bin"
        for path in (binaries, self.scratch):
            path.mkdir()
        stand_in = binaries / "probe-run"
        stand_in.write_text(PROBE_RUN_STAND_IN)
        stand_in.chmod(0o755)
        write_audio(self.audio, seconds=AUDIO_SECONDS)
        self.environment = os.environ | {
            "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(SCRIPTS),
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
        return self.launch(self.arguments(*extra))

    def launch(self, arguments: list[str]) -> subprocess.Popen[bytes]:
        """Start `arguments` as the command, in a process group of its own."""
        with self.output.open("wb") as output:
            return subprocess.Popen(  # noqa: S603 - this checkout's interpreter and script
                arguments,
                env=self.environment,
                stdout=output,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

    def finish(self, command: subprocess.Popen[bytes]) -> tuple[int, str]:
        """Wait for the command: its exit status and what it wrote to standard error."""
        _, error = command.communicate(timeout=RUN_WITHIN_SECONDS)
        return command.returncode, error.decode()

    def alive(self) -> dict[int, str]:
        """The processes of this run, still alive, with their command lines."""
        found = {}
        for pid in marked_processes(MARKER, self.marker):
            with contextlib.suppress(OSError):
                line = (
                    Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
                )
                if line:
                    found[pid] = line
        return found

    def processes(self) -> list[str]:
        """The command lines of the processes of this run, still alive."""
        return list(self.alive().values())

    def service_pid(self) -> int:
        """The process id of the service of this run."""
        (pid,) = (pid for pid, line in self.alive().items() if SERVICE_COMMAND in line)
        return pid

    def group_pid(self) -> int:
        """The process id of the group's process, as it names itself."""
        (pid_file,) = self.scratch.glob("prove-card-record-*/*.pid")
        return int(pid_file.read_text())

    def logs(self) -> list[Path]:
        """The service logs beside the record."""
        return sorted(self.directory.glob(f"{self.record.stem}.*.log"))

    def printed(self) -> str:
        """What the command wrote to standard output."""
        return self.output.read_text()

    def has_spoken(self) -> bool:
        """Whether a service of this run has ended a sentence, viewers attached."""
        return any(
            '"sentence.ended"' in log.read_text(errors="replace") for log in self.logs()
        )

    def wait_until(self, condition: Callable[[], bool], seconds: float) -> bool:
        """Whether `condition` holds within `seconds`."""
        return wait_until(condition, seconds)
