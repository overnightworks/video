"""The card proof's lag sweep: its arguments, its summary and a chunked provider."""

from __future__ import annotations

import json
from typing import Final

import pytest

from card_record import Reason, lag_cell, not_measured, value_of
from support.prove_card_support import Run, is_not_measured

# A chunk of frames takes one render, shorter than the chunk plays. A value below the
# render starves a session; one beyond two renders and a frame period does not.
RENDER_SECONDS: Final = 0.2
CHUNK_FRAMES: Final = 25
BELOW_ONE_RENDER_MS: Final = 100
BEYOND_TWO_RENDERS_MS: Final = (1500, 2500)


@pytest.mark.parametrize("lags", [["0"], ["abc"], ["1.5"], ["500", "500"]])
def test_a_lag_that_is_refused_ends_the_command_with_one_line_before_any_service(
    run: Run, lags: list[str]
) -> None:
    status, error = run.finish(run.start("--dry-run", "--lag-ms", *lags))

    assert status != 0
    assert len(error.strip().splitlines()) == 1
    assert "--lag-ms" in error
    assert not run.probe_run_log.exists()
    assert not run.record.exists()
    assert run.processes() == []


def test_without_the_lag_cell_no_sweep_runs_and_the_wire_group_takes_the_largest_lag(
    run: Run,
) -> None:
    status, error = run.finish(
        run.start("--dry-run", "--cells", "sentences", "--lag-ms", "1600", "900")
    )
    record = json.loads(run.record.read_text())

    assert status == 0, error
    assert list(record["services"]) == ["wire"]
    assert record["services"]["wire"]["max_lag_ms"] == value_of(1600)
    assert list(record["cells"]) == ["sentences"]


def _swept(run: Run, *lags: int) -> tuple[int, dict]:
    """A card-mode sweep of `lags` and the wire group's sentences; the record."""
    run.in_card_mode()
    run.render_in_chunks(
        chunk_frames=CHUNK_FRAMES, render_seconds=RENDER_SECONDS, idle_render_seconds=0
    )
    status, error = run.finish(
        run.start("--cells", "sentences", "lag", "--lag-ms", *map(str, lags))
    )
    return status, json.loads(run.record.read_text()) | {"error": error}


def test_a_value_below_one_render_starves_and_larger_values_do_not(run: Run) -> None:
    high = max(BEYOND_TWO_RENDERS_MS)

    status, record = _swept(run, high, BELOW_ONE_RENDER_MS, min(BEYOND_TWO_RENDERS_MS))
    lag = record["cells"]["lag"]

    assert status == 0, record["error"]
    assert [value["max_lag_ms"]["value"] for value in lag["values"]] == [
        BELOW_ONE_RENDER_MS,
        *BEYOND_TWO_RENDERS_MS,
    ]
    assert [value["starved"]["value"] for value in lag["values"]] == [
        True,
        False,
        False,
    ]
    assert lag["smallest_without_starvation_ms"] == value_of(min(BEYOND_TWO_RENDERS_MS))
    assert lag["largest_starved_ms"] == value_of(BELOW_ONE_RENDER_MS)
    assert list(record["services"]) == [
        *(f"lag-{lag}" for lag in (BELOW_ONE_RENDER_MS, *BEYOND_TWO_RENDERS_MS)),
        "wire",
    ]
    assert record["services"]["wire"]["max_lag_ms"] == value_of(high)
    assert record["cells"]["sentences"]["sentences"][1]["outcome"] == value_of("ok")


def test_a_sweep_in_which_every_value_starves_ends_with_the_wire_group_failed(
    run: Run,
) -> None:
    status, record = _swept(run, BELOW_ONE_RENDER_MS, BELOW_ONE_RENDER_MS // 2)
    lag = record["cells"]["lag"]

    assert status != 0
    assert is_not_measured(
        lag["smallest_without_starvation_ms"], Reason.LARGEST_VALUE_STARVED
    )
    assert lag["largest_starved_ms"] == value_of(BELOW_ONE_RENDER_MS)
    assert is_not_measured(record["cells"]["sentences"], Reason.CELL_FAILED)


@pytest.mark.parametrize(
    ("starved", "smallest", "largest"),
    [
        ([False, False], 100, None),
        ([True, False, False], 200, 100),
        ([True, False, True, False], 400, 300),
        ([True, True], None, 200),
    ],
)
def test_the_summary_names_the_smallest_value_without_starvation_above_every_starved(
    starved: list[bool], smallest: int | None, largest: int | None
) -> None:
    sweep = {
        (position + 1) * 100: {"starved": value_of(flag), "sentences": []}
        for position, flag in enumerate(starved)
    }

    cell = lag_cell(sweep)

    assert cell["smallest_without_starvation_ms"] == (
        value_of(smallest) if smallest else not_measured(Reason.LARGEST_VALUE_STARVED)
    )
    assert cell["largest_starved_ms"] == (
        value_of(largest) if largest else not_measured(Reason.NO_VALUE_STARVED)
    )


def test_a_value_without_an_outcome_leaves_the_summary_unmeasured_for_it() -> None:
    sweep = {
        100: {"starved": value_of(True), "sentences": []},  # noqa: FBT003
        200: not_measured(Reason.PROVIDER_NOT_LOADED),
        300: not_measured(Reason.EARLIER_GROUP_FAILED),
    }

    cell = lag_cell(sweep)

    for figure in ("smallest_without_starvation_ms", "largest_starved_ms"):
        assert cell[figure] == not_measured(Reason.PROVIDER_NOT_LOADED)
    assert cell["values"][1]["max_lag_ms"] == value_of(200)
    assert cell["values"][1]["starved"] == not_measured(Reason.PROVIDER_NOT_LOADED)
