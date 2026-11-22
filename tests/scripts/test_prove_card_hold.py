"""The card proof's hold cell: one session under CPU contention and its figures."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from support.prove_card_support import Run

_HOLD_SECONDS = "3"
# Beyond one render of the fake at the card's cost.
_LAG_MS = "1500"
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
            _LAG_MS,
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
    assert hold["max_idle_call_ms"]["value"] > 0
    assert hold["ended_by"]["value"] == "none"
    assert run.processes() == []


@pytest.mark.parametrize(
    "arguments",
    [["--hold-seconds", "0"], ["--cpus", "9999"], ["--busy-workers", "-1"]],
)
def test_a_hold_setting_that_is_refused_ends_the_command_before_any_service(
    run: Run, arguments: list[str]
) -> None:
    status, error = run.finish(run.start("--dry-run", "--cells", "hold", *arguments))

    assert status != 0
    assert arguments[0] in error
    assert not run.probe_run_log.exists()
