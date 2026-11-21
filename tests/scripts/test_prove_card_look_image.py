"""The card proof's look image: served as alpha, refused before any group starts."""

from __future__ import annotations

import hashlib
import io
import ssl
import urllib.request
from typing import TYPE_CHECKING

import pytest
from PIL import Image

import card_identity
from support.images import picture
from video_service.config import Settings
from video_service.looks import LookFailure, read_look_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from support.prove_card_support import Run


def _served(identity: card_identity.Identity, name: str) -> tuple[bytes, str]:
    """What the look server answers for `name`, and the digest its looks file names."""
    source = read_look_file(identity.looks_file)[name]
    context = ssl.create_default_context(cafile=identity.ca_file)
    with urllib.request.urlopen(source.url, context=context) as response:  # noqa: S310 - the run's own loopback server
        return response.read(), source.sha256


def test_the_given_image_is_served_unchanged_as_alpha_and_beta_stays_a_square(
    tmp_path: Path,
) -> None:
    face = picture(32, 24, fmt="JPEG").data

    with card_identity.throwaway_identity(tmp_path, alpha_image=face) as identity:
        alpha, alpha_digest = _served(identity, "alpha")
        beta, _ = _served(identity, "beta")

    assert alpha == face
    assert alpha_digest == hashlib.sha256(face).hexdigest()
    with Image.open(io.BytesIO(beta)) as square:
        assert square.size == (16, 16)


def test_without_an_image_both_looks_are_squares(tmp_path: Path) -> None:
    with card_identity.throwaway_identity(tmp_path) as identity:
        served = [_served(identity, name)[0] for name in card_identity.LOOK_NAMES]

    for data in served:
        with Image.open(io.BytesIO(data)) as square:
            assert square.size == (16, 16)


def test_only_the_looks_asked_for_are_in_the_looks_file(tmp_path: Path) -> None:
    with card_identity.throwaway_identity(tmp_path, looks=("beta",)) as identity:
        assert list(read_look_file(identity.looks_file)) == ["beta"]


def test_every_group_is_given_the_look_image_and_the_run_ends_well(run: Run) -> None:
    face = run.directory / "face.webp"
    face.write_bytes(picture(32, 24, fmt="WEBP").data)

    status, error = run.finish(
        run.start("--dry-run", "--look-image", str(face), "--cells", "looks", "lag")
    )

    assert status == 0, error
    calls = run.probe_run_log.read_text().splitlines()
    assert len(calls) > 1
    assert all(f"--look-image {face}" in call for call in calls)


def _no_such_image(run: Run) -> Path:
    return run.directory / "missing.png"


def _a_text_file(run: Run) -> Path:
    path = run.directory / "face.png"
    path.write_text("this is no image")
    return path


def _more_pixels_than_the_service_takes(run: Run) -> Path:
    path = run.directory / "huge.png"
    side = int(Settings.model_fields["look_max_pixels"].default ** 0.5) + 1
    path.write_bytes(picture(side, side).data)
    return path


def _more_bytes_than_the_service_takes(run: Run) -> Path:
    path = run.directory / "heavy.png"
    limit = Settings.model_fields["look_max_bytes"].default
    path.write_bytes(picture(8, 8).data + bytes(limit))
    return path


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (_no_such_image, "cannot be read"),
        (_a_text_file, LookFailure.NOT_AN_IMAGE.value),
        (_more_pixels_than_the_service_takes, LookFailure.TOO_MANY_PIXELS.value),
        (_more_bytes_than_the_service_takes, LookFailure.TOO_LARGE.value),
    ],
)
def test_a_look_image_the_service_would_refuse_ends_the_command_with_one_line_first(
    run: Run, make: Callable[[Run], Path], reason: str
) -> None:
    status, error = run.finish(run.start("--dry-run", "--look-image", str(make(run))))

    assert status != 0
    assert len(error.strip().splitlines()) == 1
    assert "--look-image" in error
    assert reason in error
    assert not run.probe_run_log.exists()
    assert run.processes() == []
