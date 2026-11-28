"""The card proof's dry run, driven through the command's own entry point."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import typing
from pathlib import Path
from typing import TYPE_CHECKING, Final

import av
import pytest
from PIL import Image

import card_cells
import card_drive
import card_identity
import prove_card
from card_record import (
    KILL_FIGURES,
    MEMORY_FIGURES,
    SERVICE_FIGURES,
    CompilerHome,
    Reason,
    completed_service,
    step_figures,
    value_of,
)
from support.prove_card_support import (
    APPEARS_WITHIN_SECONDS,
    AUDIO_SECONDS,
    CELLS_BY_GROUP,
    DEFAULT_LAG_MS,
    GONE_WITHIN_SECONDS,
    LAG_SERVICE,
    MAX_QUEUED_AUDIO_SECONDS,
    RUN_WITHIN_SECONDS,
    Run,
    ended,
    is_cell_not_measured,
    is_not_measured,
    write_audio,
)
from video_client.session_wire import PcmFormat
from video_service.provider_process import ProcessBounds

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

FIVE: Final = 5
STOP_BOUND_SECONDS: Final = 1.0


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


def test_a_dry_run_sweeps_the_default_lag_and_no_session_starves_at_it(
    dry_run: tuple[Run, dict],
) -> None:
    lag = dry_run[1]["cells"]["lag"]

    (swept,) = lag["values"]
    assert swept["max_lag_ms"] == value_of(DEFAULT_LAG_MS)
    assert swept["starved"] == value_of(False)  # noqa: FBT003 - the figure's value
    assert [sentence["sentence"] for sentence in swept["sentences"]] == [
        "first",
        "second",
    ]
    assert lag["smallest_without_starvation_ms"] == value_of(DEFAULT_LAG_MS)
    assert is_not_measured(lag["largest_starved_ms"], Reason.NO_VALUE_STARVED)


def test_a_dry_runs_services_name_the_lag_each_runs_at(
    dry_run: tuple[Run, dict],
) -> None:
    services = dry_run[1]["services"]

    assert list(services) == [LAG_SERVICE, "wire"]
    assert all(
        service["max_lag_ms"] == value_of(DEFAULT_LAG_MS)
        for service in services.values()
    )


def test_the_first_service_of_a_run_starts_on_an_empty_compiler_home(
    dry_run: tuple[Run, dict],
) -> None:
    first = dry_run[1]["services"][LAG_SERVICE]

    assert first["compiler_home_at_start"] == value_of(CompilerHome.EMPTY)


def test_every_service_of_the_run_stops_and_none_is_left(
    dry_run: tuple[Run, dict],
) -> None:
    services = dry_run[1]["services"]

    assert set(services) == {LAG_SERVICE, "wire"}
    assert all(set(service) == set(SERVICE_FIGURES) for service in services.values())
    assert all(
        service["stop_to_exit_ms"]["value"] >= 0 for service in services.values()
    )
    assert all(
        service[count] == value_of(0)
        for service in services.values()
        for count in ("processes_left_after_stop", "processes_alive_after_kill")
    )
    assert dry_run[0].processes() == []
    assert list(dry_run[0].scratch.iterdir()) == []


def test_a_dry_run_records_the_providers_load_and_each_looks_preparation(
    dry_run: tuple[Run, dict],
) -> None:
    service = dry_run[1]["services"]["wire"]

    assert service["provider_load"]["outcome"]["value"] == "ok"
    assert service["provider_load"]["duration_ms"]["value"] > 0
    assert [look["look"]["value"] for look in service["look_preparations"]] == list(
        card_identity.LOOK_NAMES
    )
    assert all(
        look["outcome"]["value"] == "ok" and look["duration_ms"]["value"] > 0
        for look in service["look_preparations"]
    )


@pytest.mark.parametrize("looks", [0, 1, len(card_identity.LOOK_NAMES), 10])
def test_the_wait_for_ready_covers_the_services_bounds_for_every_look(
    looks: int,
) -> None:
    bounds = ProcessBounds()

    assert card_drive.ready_within_seconds(looks) >= (
        bounds.start_seconds + looks * bounds.prepare_seconds
    )


def test_a_failed_step_is_recorded_as_the_service_logged_it() -> None:
    logged = {"outcome": "failed", "reason": "provider_failed", "duration_ms": 12.5}

    assert step_figures(logged) == {
        "outcome": {"value": "failed"},
        "reason": {"value": "provider_failed"},
        "duration_ms": {"value": 12.5},
    }
    assert "reason" not in step_figures({"outcome": "ok", "duration_ms": 1})


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
    assert [log.name for log in run.logs()] == ["record.wire.log"]
    assert str(run.logs()[0]) in run.printed()


def test_one_viewer_receives_frames_in_both_windows_and_no_loud_audio_when_idle(
    dry_run: tuple[Run, dict],
) -> None:
    viewer = dry_run[1]["cells"]["viewer"]["viewer"]

    assert viewer["frames_per_second_between_sentences"]["value"] > 0
    assert viewer["frames_per_second_in_sentence"]["value"] > 0
    assert viewer["loud_audio_frames_between_sentences"]["value"] == 0
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


def test_a_clear_after_the_viewers_first_loud_frame_records_the_time_to_the_last_one(
    dry_run: tuple[Run, dict],
) -> None:
    figure = dry_run[1]["cells"]["clear"]["clear_to_last_loud_frame_ms"]

    assert figure["value"] >= 0
    assert figure["reason"] == Reason.NO_CARD


def test_a_clear_when_no_loud_frame_reached_the_viewer_is_not_measured(
    run: Run,
) -> None:
    write_audio(run.audio, seconds=AUDIO_SECONDS, amplitude=0)

    status, error = run.finish(run.start("--dry-run", "--cells", "clear"))
    cell = json.loads(run.record.read_text())["cells"]["clear"]

    assert status == 0, error
    assert cell["clear_to_last_loud_frame_ms"]["verdict"] == "not_measured"
    assert (
        cell["clear_to_last_loud_frame_ms"]["reason"]
        == Reason.NO_LOUD_AUDIO_BEFORE_CLEAR
    )
    assert cell["clear_to_confirmation_ms"]["value"] >= 0


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


def _audio_at_a_rate_the_wire_does_not_carry(run: Run) -> None:
    carried = typing.get_args(PcmFormat.model_fields["rate"].annotation)
    write_audio(run.audio, seconds=AUDIO_SECONDS, rate=max(carried) + 1)


def _text_audio(run: Run) -> None:
    run.audio.write_text("this is no sound")


def _empty_audio(run: Run) -> None:
    run.audio.write_bytes(b"")


def _audio_cut_inside_its_header(run: Run) -> None:
    run.audio.write_bytes(run.audio.read_bytes()[:20])


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
        (_audio_at_a_rate_the_wire_does_not_carry, "audio"),
        (_text_audio, "audio"),
        (_empty_audio, "audio"),
        (_audio_cut_inside_its_header, "audio"),
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
    assert "()" not in error
    assert not run.probe_run_log.exists()
    assert run.processes() == []


def test_a_service_that_stops_in_time_takes_its_worker_with_it(tmp_path: Path) -> None:
    with (
        card_identity.throwaway_identity(tmp_path) as identity,
        card_drive.running_service(
            identity,
            tmp_path / "service.log",
            max_lag_ms=500,
            compiler_home=tmp_path,
            service_pid_file=tmp_path / "service.record",
        ) as service,
    ):
        service_pid = service.process.pid
        workers = card_drive.worker_pids(service_pid)
        assert workers

    assert ended(service_pid)
    assert all(ended(worker) for worker in workers)
    assert service.stop_to_exit_ms < card_drive.STOP_WITHIN_SECONDS * 1000


def _stop_a_service_that_cannot_stop(directory: Path, pids: list[int]) -> None:
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
    assert all(ended(pid) for pid in pids)


def test_a_service_that_cannot_answer_its_stop_is_gone_within_the_stop_and_kill_bounds(
    run: Run,
) -> None:
    command = run.start("--dry-run", "--cells", "viewers")
    serving = run.wait_until(run.has_spoken, APPEARS_WITHIN_SECONDS)
    service = run.service_pid()
    group = [service, *card_drive.worker_pids(service)]
    # A stopped process holds the TERM the stop sends and never acts on it.
    os.kill(service, signal.SIGSTOP)
    os.kill(command.pid, signal.SIGINT)
    bound = card_drive.STOP_WITHIN_SECONDS + card_drive.KILL_MARGIN_SECONDS

    gone = run.wait_until(lambda: all(ended(pid) for pid in group), bound)
    status, error = run.finish(command)

    assert serving
    assert len(group) > 1
    assert gone
    assert status != 0
    assert len(error.strip().splitlines()) == 1
    assert [log.name for log in run.logs()] == ["record.wire.log"]
    assert str(run.logs()[0]) in run.printed()


def _fragment_cells(run: Run) -> list[str] | None:
    """The cells the wire group's fragment holds, once it is written."""
    for fragment in run.scratch.glob("prove-card-record-*/wire.json"):
        return list(json.loads(fragment.read_text())["cells"])
    return None


def test_a_killed_group_process_takes_its_service_and_worker_with_it(run: Run) -> None:
    command = run.start("--dry-run", "--cells", "viewers")
    serving = run.wait_until(run.has_spoken, APPEARS_WITHIN_SECONDS)
    service = run.service_pid()
    group = [service, *card_drive.worker_pids(service)]
    os.kill(run.group_pid(), signal.SIGKILL)

    status, error = run.finish(command)

    assert serving
    assert len(group) > 1
    assert status != 0
    # `timeout` reports the kill of its command in its own status.
    assert error.strip().splitlines()[-1].startswith("the wire cells ended with status")
    assert run.wait_until(lambda: all(ended(pid) for pid in group), GONE_WITHIN_SECONDS)
    assert list(run.scratch.iterdir()) == []


def test_a_group_process_killed_before_its_fragment_measures_what_the_top_ends(
    run: Run,
) -> None:
    command = run.start("--dry-run", "--cells", "viewer")
    serving = run.wait_until(run.has_spoken, APPEARS_WITHIN_SECONDS)
    os.kill(run.group_pid(), signal.SIGKILL)

    status, _ = run.finish(command)
    record = json.loads(run.record.read_text())
    service = record["services"]["wire"]
    measured_by_the_top = {*KILL_FIGURES, *MEMORY_FIGURES, "compiler_home_at_start"}

    assert serving
    assert status != 0
    assert list(record["cells"]) == ["viewer"]
    assert is_not_measured(record["cells"]["viewer"], Reason.NO_FRAGMENT)
    assert all(
        is_not_measured(figure, Reason.NO_FRAGMENT)
        for name, figure in service.items()
        if name not in measured_by_the_top
    )
    assert service["compiler_home_at_start"] == value_of(CompilerHome.EMPTY)
    assert service["processes_left_after_stop"]["value"] >= 0
    assert service["processes_alive_after_kill"] == value_of(0)
    assert [log.name for log in run.logs()] == ["record.wire.log"]


def test_a_group_process_killed_after_its_first_cell_keeps_that_cells_figures(
    run: Run,
) -> None:
    command = run.start("--dry-run", "--cells", "sentences", "viewers")
    first = run.wait_until(
        lambda: _fragment_cells(run) == ["sentences"], APPEARS_WITHIN_SECONDS
    )
    os.kill(run.group_pid(), signal.SIGKILL)

    status, _ = run.finish(command)
    record = json.loads(run.record.read_text())
    service = record["services"]["wire"]

    assert first
    assert status != 0
    assert [
        sentence["sentence"] for sentence in record["cells"]["sentences"]["sentences"]
    ] == [
        "first",
        "second",
    ]
    assert is_not_measured(record["cells"]["viewers"], Reason.NO_FRAGMENT)
    assert service["start_to_ready_ms"]["value"] > 0
    assert service["provider_load"]["outcome"]["value"] == "ok"
    assert is_not_measured(service["stop_to_exit_ms"], Reason.NO_FRAGMENT)


def _fail_the_cell_being_driven(run: Run) -> None:
    serving = run.wait_until(run.has_spoken, APPEARS_WITHIN_SECONDS)
    assert serving
    os.kill(run.service_pid(), signal.SIGKILL)


def test_a_failed_cell_is_recorded_with_the_later_cells_of_its_group_ended(
    run: Run,
) -> None:
    command = run.start("--dry-run", "--cells", "viewer", "viewers")
    _fail_the_cell_being_driven(run)

    status, error = run.finish(command)
    record = json.loads(run.record.read_text())

    assert status != 0
    assert error.strip().splitlines()[-1] == "the wire cells ended with status 1"
    assert is_not_measured(record["cells"]["viewer"], Reason.CELL_FAILED)
    assert is_not_measured(record["cells"]["viewers"], Reason.GROUP_ENDED)
    service = record["services"]["wire"]
    assert service["start_to_ready_ms"]["value"] > 0
    assert is_not_measured(service["stop_to_exit_ms"], Reason.EXITED_BEFORE_STOP)
    assert service["processes_alive_after_kill"] == value_of(0)
    assert [log.name for log in run.logs()] == ["record.wire.log"]
    assert str(run.logs()[0]) in run.printed()
    assert str(run.record) in run.printed()
    assert run.processes() == []


@pytest.mark.parametrize("cell", ["lag", "hold"])
def test_a_session_the_service_refuses_as_too_large_fails_its_cell_and_the_command(
    run: Run, cell: str
) -> None:
    write_audio(run.audio, seconds=MAX_QUEUED_AUDIO_SECONDS + 1)

    status, _ = run.finish(run.start("--dry-run", "--cells", cell))
    record = json.loads(run.record.read_text())

    assert status != 0
    assert is_cell_not_measured(record, cell, Reason.CELL_FAILED)
    assert run.processes() == []


def test_a_failed_group_ends_the_command_and_no_later_group_starts(run: Run) -> None:
    command = run.start("--dry-run", "--cells", "viewer", "lag")
    _fail_the_cell_being_driven(run)

    status, _ = run.finish(command)
    record = json.loads(run.record.read_text())

    assert status != 0
    assert list(record["cells"]) == ["viewer", "lag"]
    assert is_not_measured(record["cells"]["viewer"], Reason.EARLIER_GROUP_FAILED)
    assert is_cell_not_measured(record, "lag", Reason.CELL_FAILED)
    assert all(
        is_not_measured(figure, Reason.EARLIER_GROUP_FAILED)
        for figure in record["services"]["wire"].values()
    )
    assert len(run.probe_run_log.read_text().splitlines()) == 1
    assert [log.name for log in run.logs()] == [f"record.{LAG_SERVICE}.log"]


class _ServiceAtItsStop:
    """What the proof reads of a service whose cells have ended."""

    start_to_ready_ms = 1
    stop_to_exit_ms = 0

    def lines(self, event: str) -> list[dict[str, object]]:
        return [{"outcome": "ok"}] if event == "provider_load.ended" else []


def test_a_failed_cells_reasons_are_in_the_fragment_before_the_service_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_audio(tmp_path / "speech.wav", seconds=AUDIO_SECONDS)
    arguments = prove_card._arguments(  # noqa: SLF001 - the group's run is the unit
        [
            *("--record", str(tmp_path / "record.json")),
            *("--audio", str(tmp_path / "speech.wav")),
            *("--dry-run", "--cells", "sentences", "clear", "looks"),
        ]
    )
    arguments.fragment = tmp_path / "wire.json"
    held_at_the_stop: dict = {}

    async def finishes(_drive: object) -> dict:
        return {"done": True}

    async def raises(_drive: object) -> dict:
        message = "the cell broke"
        raise RuntimeError(message)

    @contextlib.contextmanager
    def service_whose_stop_fails(
        *_arguments: object, **_keywords: object
    ) -> Iterator[_ServiceAtItsStop]:
        yield _ServiceAtItsStop()
        held_at_the_stop.update(json.loads(arguments.fragment.read_text()))
        raise card_drive.DriveError

    monkeypatch.setitem(
        card_cells.GROUPS,
        "wire",
        {"sentences": finishes, "clear": raises, "looks": finishes},
    )

    (wire,) = prove_card._groups(arguments)  # noqa: SLF001
    with pytest.raises(card_drive.DriveError):
        prove_card._run_group(  # noqa: SLF001
            wire, arguments, tmp_path, tmp_path, service_whose_stop_fails
        )

    assert held_at_the_stop["cells"]["sentences"] == {"done": True}
    assert is_not_measured(held_at_the_stop["cells"]["clear"], Reason.CELL_FAILED)
    assert is_not_measured(held_at_the_stop["cells"]["looks"], Reason.GROUP_ENDED)


def test_a_service_figure_the_fragment_holds_is_never_dropped_from_the_record() -> None:
    entry = completed_service({"added": value_of(1)}, Reason.NO_FRAGMENT)

    assert entry["added"] == value_of(1)
    assert set(SERVICE_FIGURES) <= set(entry)


def test_a_fragment_is_whole_or_absent_when_its_rename_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fragment = tmp_path / "wire.json"
    prove_card._write_fragment(fragment, {"cells": {"first": 1}})  # noqa: SLF001 - the write is the unit

    def interrupted(*_arguments: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        prove_card._write_fragment(fragment, {"cells": {"first": 1, "second": 2}})  # noqa: SLF001

    assert json.loads(fragment.read_text()) == {"cells": {"first": 1}}
