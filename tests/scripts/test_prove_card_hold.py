"""The card proof's hold cell: one session under CPU contention and its figures."""

from __future__ import annotations

import json
import os
import signal
from typing import TYPE_CHECKING

import pytest
from video_provider_fake import worker as fake_worker

import card_drive
import prove_card
from support.prove_card_support import APPEARS_WITHIN_SECONDS, HOLD_LAG_MS

if TYPE_CHECKING:
    from support.prove_card_support import Run

_HOLD_SECONDS = "3"
# Longer than the test takes to end the session.
_LONG_HOLD = "120"
_FIGURES = (
    "held_seconds",
    "fps",
    "ended_by",
    "max_behind_ms",
    "max_idle_call_ms",
    "threads",
    "cpus",
    "busy_workers_per_cpu",
    "service_run_queue_delay_seconds",
    "provider_run_queue_delay_seconds",
)


def test_a_dry_run_hold_records_every_figure_and_leaves_no_busy_worker(
    run: Run,
) -> None:
    cpu = min(os.sched_getaffinity(0))

    status, error = run.finish(
        run.start(
            "--dry-run",
            "--cells",
            "hold",
            "--hold-seconds",
            _HOLD_SECONDS,
            "--lag-ms",
            str(HOLD_LAG_MS),
            "--cpus",
            str(cpu),
            "--busy-workers",
            "1",
        )
    )
    hold = json.loads(run.record.read_text())["cells"]["hold"]

    assert status == 0, error
    assert set(_FIGURES) <= set(hold)
    assert hold["cpus"]["value"] == [cpu]
    assert hold["busy_workers_per_cpu"]["value"] == 1
    one_render_ms = float(prove_card._CARD_COST[fake_worker.IDLE_RENDER_SECONDS]) * 1000  # noqa: SLF001
    assert hold["max_idle_call_ms"]["value"] >= one_render_ms
    assert hold["ended_by"]["value"] == "none"
    assert run.processes() == []


def test_a_hold_whose_provider_dies_ends_early_with_its_figures_and_exits_zero(
    run: Run,
) -> None:
    command = run.start(
        "--dry-run",
        "--cells",
        "hold",
        "--hold-seconds",
        _LONG_HOLD,
        "--lag-ms",
        str(HOLD_LAG_MS),
    )
    serving = run.wait_until(run.has_spoken, APPEARS_WITHIN_SECONDS)
    for worker in card_drive.worker_pids(run.service_pid()):
        os.killpg(worker, signal.SIGKILL)

    status, error = run.finish(command)
    hold = json.loads(run.record.read_text())["cells"]["hold"]

    assert serving
    assert status == 0, error
    assert hold["ended_by"]["value"] != "none"
    assert set(_FIGURES) <= set(hold)
    assert run.processes() == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["--hold-seconds", "0"],
        ["--cpus", "9999"],
        ["--busy-workers", "-1"],
        ["--busy-workers", "2"],
        ["--hold-seconds", "abc"],
        ["--cpus", "0-x"],
        ["--busy-workers", "abc"],
    ],
    ids=[
        "no-seconds",
        "no-such-cpu",
        "negative-workers",
        "workers-without-cpus",
        "seconds-not-a-number",
        "cpus-not-a-list",
        "workers-not-a-number",
    ],
)
def test_a_hold_setting_that_is_refused_ends_the_command_before_any_service(
    run: Run, arguments: list[str]
) -> None:
    status, error = run.finish(run.start("--dry-run", "--cells", "hold", *arguments))

    assert status != 0
    assert arguments[0] in error
    assert not run.probe_run_log.exists()
