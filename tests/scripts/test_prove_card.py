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

import av
import pytest
from PIL import Image

import card_drive
import card_identity

if TYPE_CHECKING:
    from collections.abc import Callable

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
FIVE: Final = 5
STOP_BOUND_SECONDS: Final = 1.0
# A stand-in for the fleet's `probe-run`, which waits on the machine's load; like
# its `timeout`, it kills the command at once on a signal.
PROBE_RUN_STAND_IN: Final = """#!/usr/bin/env bash
echo "$*" >> "$PROBE_RUN_LOG"
[[ $1 == -k ]] && shift 3
"$@" &
trap 'kill -9 $!' INT TERM
wait $!
"""


def write_audio(path: Path, *, seconds: float, channels: int = 1) -> None:
    """A WAV of a square wave, which a viewer hears as loud."""
    square = struct.pack("<8h", *([9000] * 4 + [-9000] * 4)) * channels
    with wave.open(str(path), "wb") as sound:
        sound.setnchannels(channels)
        sound.setsampwidth(2)
        sound.setframerate(AUDIO_RATE)
        sound.writeframes(square * int(AUDIO_RATE * seconds // 8))


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
        write_audio(self.audio, seconds=AUDIO_SECONDS)
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

    def has_spoken(self) -> bool:
        """Whether a service of this run has ended a sentence, viewers attached."""
        return any(
            '"sentence.ended"' in log.read_text(errors="replace")
            for log in self.scratch.glob("*/service-*.log")
        )

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
        cells["clear"]["clear_to_last_loud_frame_ms"],
        dry_run[1]["services"]["wire"]["start_to_ready_ms"],
        cells["viewer"]["viewer"]["frames_per_second_in_sentence"],
        cells["viewers"]["encoders_cores"],
    ]

    assert [figure["verdict"] for figure in judged] == ["not_measured"] * 5
    assert all(figure["reason"] and figure["line"] for figure in judged)


def test_a_clear_inside_a_sentence_is_confirmed_and_cancels_it(
    dry_run: tuple[Run, dict],
) -> None:
    cleared = dry_run[1]["cells"]["clear"]

    assert cleared["outcome"]["value"] == "cancelled"
    assert cleared["clear_to_confirmation_ms"]["value"] >= 0
    assert cleared["clear_to_last_loud_frame_ms"]["value"] >= 0
    assert "verdict" not in cleared["clear_to_confirmation_ms"]


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


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_an_interrupted_run_ends_with_one_line_and_leaves_nothing(
    run: Run, interrupt: signal.Signals
) -> None:
    command = run.start("--dry-run", "--cells", "viewers")
    serving = run.wait_until(run.has_spoken, APPEARS_WITHIN_SECONDS)
    os.kill(command.pid, interrupt)
    status, error = run.finish(command)

    assert serving
    assert status != 0
    assert len(error.strip().splitlines()) == 1
    assert "Traceback" not in error
    assert run.wait_until(lambda: run.processes() == [], GONE_WITHIN_SECONDS)
    assert run.wait_until(lambda: not any(run.scratch.iterdir()), GONE_WITHIN_SECONDS)
    assert not run.record.exists()


def test_one_viewer_receives_frames_in_both_windows_and_no_loud_audio_when_idle(
    dry_run: tuple[Run, dict],
) -> None:
    viewer = dry_run[1]["cells"]["viewer"]["viewer"]

    assert viewer["frames_per_second_between_sentences"]["value"] > 0
    assert viewer["frames_per_second_in_sentence"]["value"] > 0
    assert viewer["loud_audio_frames_between_sentences"]["value"] == 0
    assert viewer["video_timestamp_gaps"]["value"] >= 0
    assert viewer["longest_video_timestamp_gap_ms"]["value"] > 0
    assert viewer["frame_size"]["value"]["width"] > 0


def test_five_viewers_each_have_their_figures_and_the_encoders_share_is_recorded(
    dry_run: tuple[Run, dict],
) -> None:
    cell = dry_run[1]["cells"]["viewers"]
    cores = cell["service_cpu_cores"]

    assert len(cell["viewers"]) == FIVE
    assert all(
        viewer["frames_per_second_in_sentence"]["value"] > 0
        for viewer in cell["viewers"]
    )
    assert cores["no_viewers"]["value"] >= 0
    assert cores["five_viewers"]["value"] > 0
    assert cell["encoders_cores"]["value"] == pytest.approx(
        cores["five_viewers"]["value"] - cores["no_viewers"]["value"]
    )
    assert dry_run[1]["cells"]["viewer"]["service_cpu_cores"]["one_viewer"]["value"] > 0


def test_a_clear_with_a_viewer_records_the_time_to_the_last_loud_frame(
    dry_run: tuple[Run, dict],
) -> None:
    assert dry_run[1]["cells"]["clear"]["clear_to_last_loud_frame_ms"]["value"] > 0


def test_the_recording_is_playable_and_its_sheets_are_images_named_in_the_record(
    dry_run: tuple[Run, dict],
) -> None:
    run, record = dry_run
    files = record["cells"]["viewer"]["files"]
    video = run.directory / files["video"]["value"]
    sheets = [run.directory / name for name in files["sheets"]["value"]]

    with av.open(str(video)) as container:
        assert {stream.type for stream in container.streams} == {"audio", "video"}
        assert sum(1 for _ in container.decode(video=0)) > 0
    assert sheets
    for sheet in sheets:
        with Image.open(sheet) as image:
            assert image.width > 0


def test_a_finished_run_leaves_each_groups_log_beside_the_record_and_prints_the_files(
    run: Run,
) -> None:
    command = subprocess.run(  # noqa: S603 - this checkout's interpreter and script
        run.arguments("--dry-run", "--cells", "viewer"),
        env=run.environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=RUN_WITHIN_SECONDS,
    )
    record = json.loads(run.record.read_text())
    logged = record["services"]["wire"]["log"]["value"]
    beside = sorted(path.name for path in run.directory.iterdir() if path.is_file())

    assert (run.directory / logged).read_text().count("process.ready") == 1
    for name in (logged, record["cells"]["viewer"]["files"]["video"]["value"]):
        assert name in beside
        assert str(run.directory / name) in command.stdout
    assert str(run.record) in command.stdout


def test_cells_naming_only_a_viewer_cell_run_only_that_cell(run: Run) -> None:
    status, error = run.finish(run.start("--dry-run", "--cells", "viewers"))

    assert status == 0, error
    assert list(json.loads(run.record.read_text())["cells"]) == ["viewers"]


def _short_audio(run: Run) -> None:
    write_audio(run.audio, seconds=0.5)


def _stereo_audio(run: Run) -> None:
    write_audio(run.audio, seconds=2, channels=2)


def _missing_audio(run: Run) -> None:
    run.audio.unlink()


def _unreadable_audio(run: Run) -> None:
    run.audio.chmod(0)


def _record_in_a_missing_directory(run: Run) -> None:
    run.record = run.directory / "missing" / "record.json"


def _record_in_a_read_only_directory(run: Run) -> None:
    (run.directory / "locked").mkdir(mode=0o555)
    run.record = run.directory / "locked" / "record.json"


@pytest.mark.parametrize(
    ("spoil", "named"),
    [
        (_short_audio, "audio"),
        (_stereo_audio, "audio"),
        (_missing_audio, "audio"),
        (_unreadable_audio, "audio"),
        (_record_in_a_missing_directory, "--record"),
        (_record_in_a_read_only_directory, "--record"),
    ],
)
def test_a_bad_input_ends_the_command_with_one_line_before_any_group_starts(
    run: Run, spoil: Callable[[Run], None], named: str
) -> None:
    spoil(run)

    status, error = run.finish(run.start("--dry-run"))

    assert status != 0
    assert len(error.strip().splitlines()) == 1
    assert named in error
    assert not run.probe_run_log.exists()
    assert run.processes() == []


def _gone(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return True
    return state == "Z"


def test_a_service_that_stops_in_time_takes_its_worker_with_it(tmp_path: Path) -> None:
    with (
        card_identity.throwaway_identity(tmp_path) as identity,
        card_drive.running_service(identity, tmp_path, max_lag_ms=500) as service,
    ):
        service_pid = service.process.pid
        workers = card_drive.worker_pids(service_pid)
        assert workers

    assert _gone(service_pid)
    assert all(_gone(worker) for worker in workers)
    assert service.stop_to_exit_ms < STOP_BOUND_SECONDS * 1000


def _stop_a_service_that_cannot_stop(directory: Path, pids: list[int]) -> None:
    with (
        card_identity.throwaway_identity(directory) as identity,
        card_drive.running_service(identity, directory, max_lag_ms=500) as service,
    ):
        pids.append(service.process.pid)
        pids.extend(card_drive.worker_pids(service.process.pid))
        # A stopped process holds the TERM the stop sends and never acts on it.
        os.kill(service.process.pid, signal.SIGSTOP)


def test_a_service_that_outlives_its_stop_is_killed_with_its_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(card_drive, "STOP_WITHIN_SECONDS", STOP_BOUND_SECONDS)
    pids: list[int] = []

    with pytest.raises(card_drive.DriveError):
        _stop_a_service_that_cannot_stop(tmp_path, pids)

    assert len(pids) > 1
    assert all(_gone(pid) for pid in pids)
