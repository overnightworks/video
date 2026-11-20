"""The card proof's test surroundings: the command's run, its stand-ins and waits."""

from __future__ import annotations

import contextlib
import json
import os
import struct
import subprocess
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Final

import prove_card
from card_record import FREE_MEMORY_LINE, Verdict

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
    group: list(cells) for group, cells in prove_card.GROUPS.items()
}
SERVICE_COMMAND: Final = "video_service"
# A stand-in for the fleet's `probe-run`, which waits on the machine's load and then
# starts the command on this line.
PROBE_RUN_STAND_IN: Final = """#!/usr/bin/env bash
echo "$*" >> "$PROBE_RUN_LOG"
while [ -n "$PROBE_RUN_HOLD" ] && [ ! -e "$PROBE_RUN_HOLD" ]; do sleep 0.05; done
exec setpriv --pdeathsig TERM timeout "$@"
"""
# A stand-in for `nvidia-smi` that answers with what the test wrote to the reading file:
# a line `used, free, total`, `fail` or `hang`, after the delay the test sets; every
# answer it gives is logged, so a test can tell which starts came after a reading.
NVIDIA_SMI_STAND_IN: Final = """#!/usr/bin/env bash
echo "$*" >> "$NVIDIA_SMI_LOG"
sleep "${NVIDIA_SMI_DELAY:-0}"
reading=$(cat "$NVIDIA_SMI_READING") || exit 1
echo "$reading" >> "$NVIDIA_SMI_ANSWERS"
case "$reading" in
    fail) exit 1 ;;
    hang) exec sleep 600 ;;
    *) echo "$reading" ;;
esac
"""
# The `flashhead-lite` worker of a provider root: the fake provider with the steps a
# case scripts through the environment, each writing a reading for the stand-in. It
# fails its load for the group whose lag the case names, and fails the preparation of
# the looks the case lists.
WORKER_STAND_IN: Final = f"""#!{sys.executable}
import os
import time
from pathlib import Path

from video_provider_contract import ProviderFailedError
from video_provider_contract.worker import run_worker
from video_provider_fake import FakeProvider


def write_reading(name):
    if name in os.environ:
        reading = Path(os.environ["NVIDIA_SMI_READING"])
        temporary = reading.with_suffix(".tmp")
        temporary.write_text(os.environ[name])
        temporary.replace(reading)


class Scripted(FakeProvider):
    def start(self):
        write_reading("WORKER_READING_AT_LOAD")
        time.sleep(float(os.environ.get("WORKER_LOAD_SECONDS", 0)))
        failing_lag = os.environ.get("WORKER_FAILS_TO_START_AT_LAG")
        if failing_lag == os.environ["VIDEO_MAX_LAG_MS"]:
            raise ProviderFailedError("the load was scripted to fail")
        return super().start()

    def open_session(self, look):
        write_reading("WORKER_READING_AT_SESSION")
        return super().open_session(look)


run_worker(Scripted(refuses=os.environ.get("WORKER_REFUSES", "").split()))
"""
CAP_MB: Final = 4000
# Long enough that the cells never finish before the watchdog has sampled.
LOAD_SECONDS: Final = 30
USED_MB: Final = 2000
FLOOR_MB: Final = int(FREE_MEMORY_LINE.limit)
# Used, free and total in MiB.
ROOMY: Final = f"{USED_MB}, 20000, 24576"
# Free memory the floor and the cap cannot both be kept beside.
NO_ROOM_FOR_THE_CAP: Final = f"{USED_MB}, {CAP_MB - 1}, 24576"
BELOW_THE_FLOOR: Final = f"{USED_MB}, {FLOOR_MB - 1}, 24576"


def write_audio(
    path: Path, *, seconds: float, channels: int = 1, amplitude: int = 9000
) -> None:
    """A WAV of a square wave, which a viewer hears as loud; amplitude 0 is quiet."""
    square = struct.pack("<8h", *([amplitude] * 4 + [-amplitude] * 4)) * channels
    with wave.open(str(path), "wb") as sound:
        sound.setnchannels(channels)
        sound.setsampwidth(2)
        sound.setframerate(AUDIO_RATE)
        sound.writeframes(square * int(AUDIO_RATE * seconds // 8))


def lines(error: str) -> list[str]:
    """The lines the command wrote to standard error."""
    return error.strip().splitlines()


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
        for name, text in (
            ("probe-run", PROBE_RUN_STAND_IN),
            ("nvidia-smi", NVIDIA_SMI_STAND_IN),
        ):
            stand_in = binaries / name
            stand_in.write_text(text)
            stand_in.chmod(0o755)
        self.reading = directory / "nvidia-smi.reading"
        self.write_reading(ROOMY)
        self.nvidia_smi_log = directory / "nvidia-smi.log"
        self.nvidia_smi_answers = directory / "nvidia-smi.answers"
        self.provider_root = directory / "providers"
        bin_directory = self.provider_root / "flashhead-lite" / ".venv" / "bin"
        bin_directory.mkdir(parents=True)
        self.worker = bin_directory / "video-flashhead-lite-worker"
        self.worker.write_text(WORKER_STAND_IN)
        self.worker.chmod(0o755)
        write_audio(self.audio, seconds=AUDIO_SECONDS)
        self.environment = os.environ | {
            "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(SCRIPTS),
            "TMPDIR": str(self.scratch),
            MARKER: self.marker,
            "PROBE_RUN_LOG": str(self.probe_run_log),
            "NVIDIA_SMI_READING": str(self.reading),
            "NVIDIA_SMI_LOG": str(self.nvidia_smi_log),
            "NVIDIA_SMI_ANSWERS": str(self.nvidia_smi_answers),
        }

    def breach_while_loading(self) -> None:
        """Have the provider's load write a reading below the floor and then go on."""
        self.environment = self.environment | {
            "WORKER_READING_AT_LOAD": BELOW_THE_FLOOR,
            "WORKER_LOAD_SECONDS": str(LOAD_SECONDS),
        }

    def fail_the_load_of(self, group: str) -> None:
        """Have the provider's load fail in `group`, which the service's lag tells."""
        self.environment = self.environment | {
            "WORKER_FAILS_TO_START_AT_LAG": str(prove_card.LAGS[group])
        }

    def fail_the_preparation_of(self, look: str) -> None:
        """Have the provider fail to prepare `look`."""
        self.environment = self.environment | {"WORKER_REFUSES": look}

    def without_worker(self) -> None:
        """Take the worker out of the provider root, so the provider cannot start."""
        self.worker.unlink()

    def write_reading(self, reading: str) -> None:
        """Replace the stand-in's reading whole, so it never reads half of one."""
        temporary = self.reading.with_suffix(".tmp")
        temporary.write_text(reading)
        temporary.replace(self.reading)

    def answers_given(self, reading: str) -> int:
        """How many times the `nvidia-smi` stand-in answered with `reading`."""
        if not self.nvidia_smi_answers.exists():
            return 0
        return self.nvidia_smi_answers.read_text().splitlines().count(reading)

    def in_card_mode(self, *, cap_mb: int = CAP_MB) -> None:
        """Set the operator's two provider settings, for a run without `--dry-run`."""
        self.environment = self.environment | {
            "VIDEO_PROVIDER_ROOT": str(self.provider_root),
            "VIDEO_CARD_MEMORY_MB": str(cap_mb),
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

    def fragment_cells(self) -> list[str] | None:
        """The cells the wire group's fragment holds, once it is written."""
        for fragment in self.scratch.glob("prove-card-record-*/wire.json"):
            return list(json.loads(fragment.read_text())["cells"])
        return None

    def has_spoken(self) -> bool:
        """Whether a service of this run has ended a sentence, viewers attached."""
        return any(
            '"sentence.ended"' in log.read_text(errors="replace") for log in self.logs()
        )

    def wait_until(self, condition: Callable[[], bool], seconds: float) -> bool:
        """Whether `condition` holds within `seconds`."""
        return wait_until(condition, seconds)
