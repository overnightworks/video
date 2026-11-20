"""The fetch puts the pinned files, each at its digest, into the weights directory."""

from __future__ import annotations

import hashlib
import importlib.metadata
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from diffusers.utils import CONFIG_NAME as DIFFUSERS_CONFIG_NAME
from diffusers.utils import SAFETENSORS_WEIGHTS_NAME
from stand_in_upstream import StandInCard
from transformers.feature_extraction_utils import FEATURE_EXTRACTOR_NAME
from transformers.utils import CONFIG_NAME as TRANSFORMERS_CONFIG_NAME
from transformers.utils import SAFE_WEIGHTS_NAME
from video_provider_flashhead_lite import fetch_weights
from video_provider_flashhead_lite.fetch_weights import FetchError
from video_provider_flashhead_lite.upstream import (
    PINNED_FILES,
    PinnedFile,
    Weights,
    locate_weights,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_DISTRIBUTION = "overnightworks-video-provider-flashhead-lite"
_REPOSITORY_ROOT = Path(__file__).parents[3]
# What a pin may name: configurations, safetensors and notices, and no code or pickle.
_ADMITTED_SUFFIXES = {".json", ".safetensors", ".txt", ".md"}
_REFUSED_SUFFIXES = (".py", ".bin", ".pth", ".pkl")
_A_COMMIT = "a" * 40


@pytest.fixture(autouse=True, scope="module")
def _no_card() -> Iterator[None]:
    # Set before torch is first used, so no test in this module can reach a card.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CUDA_VISIBLE_DEVICES", "")
        yield


class _DownloadFailedError(Exception):
    """The stand-in download was told to fail."""


@dataclass
class Hub:
    """Pins of scenario bytes, and a download that writes them as the hub's does.

    The hub's downloader writes the file at `local_dir/<path>` and its own
    metadata under `local_dir/.cache`; a request is served only for the
    repository, commit and path of a pin made here.
    """

    served: dict[tuple[str, str, str], bytes] = field(default_factory=dict)
    fails_midway: set[str] = field(default_factory=set)
    links_into_a_cache: bool = False

    def serve(
        self, pin: PinnedFile, content: bytes, *, as_served: bytes | None = None
    ) -> PinnedFile:
        """Serve `content` (or `as_served`) for `pin`, which gets its digest."""
        self.served[pin.repository, pin.commit, pin.path] = (
            content if as_served is None else as_served
        )
        return replace(pin, sha256=hashlib.sha256(content).hexdigest())

    def pin(
        self, content: bytes, destination: str, *, served: bytes | None = None
    ) -> PinnedFile:
        """A pin of `content` at `destination`; the hub serves `served` when given."""
        return self.serve(
            PinnedFile(
                repository=f"owner/{destination.replace('/', '-')}",
                commit=_A_COMMIT,
                path=f"in-repository/{Path(destination).name}",
                sha256="",
                destination=Path(destination),
            ),
            content,
            as_served=served,
        )

    def __call__(
        self,
        repo_id: str,
        filename: str,
        *,
        revision: str,
        local_dir: str,
        token: bool,
    ) -> str:
        """Write the file for the request, and the hub's metadata beside it."""
        assert token is False
        content = self.served[repo_id, revision, filename]
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        metadata = (
            Path(local_dir)
            / ".cache"
            / "huggingface"
            / "download"
            / f"{filename}.metadata"
        )
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text("etag")
        if Path(filename).name in self.fails_midway:
            target.write_bytes(content[: len(content) // 2])
            message = "the connection dropped"
            raise _DownloadFailedError(message)
        if self.links_into_a_cache:
            blob = Path(local_dir) / ".cache" / "blob"
            blob.write_bytes(content)
            target.symlink_to(blob)
        else:
            target.write_bytes(content)
        return str(target)


@pytest.fixture
def hub() -> Hub:
    return Hub()


@pytest.fixture
def weights(tmp_path: Path) -> Weights:
    return locate_weights(tmp_path / "provider" / ".venv")


def _files_under(directory: Path) -> set[Path]:
    return {
        path.relative_to(directory)
        for path in directory.rglob("*")
        if path.is_file() or path.is_symlink()
    }


def test_every_pinned_file_arrives_whole_and_nothing_else_is_in_the_weights(
    hub: Hub, weights: Weights
) -> None:
    pins = (
        hub.pin(b"model", "checkpoint/model.safetensors"),
        hub.pin(b"{}", "checkpoint/config.json"),
        hub.pin(b"licence", "notices/licence.txt"),
    )

    fetch_weights.fetch_weights(weights, pins, hub)

    assert _files_under(weights.root) == {pin.destination for pin in pins}
    for pin in pins:
        stored = weights.root / pin.destination
        assert not stored.is_symlink()
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == pin.sha256
    assert not weights.staging.exists()


def test_a_digest_that_differs_ends_the_fetch_naming_the_file_and_places_nothing(
    hub: Hub, weights: Weights
) -> None:
    pin = hub.pin(b"expected", "checkpoint/model.safetensors", served=b"tampered")

    with pytest.raises(FetchError, match=pin.destination.name):
        fetch_weights.fetch_weights(weights, (pin,), hub)

    assert not (weights.root / pin.destination).exists()


def test_a_download_that_fails_midway_names_the_file_and_leaves_nothing_partial(
    hub: Hub, weights: Weights
) -> None:
    pin = hub.pin(b"0123456789", "checkpoint/model.safetensors")
    hub.fails_midway.add(pin.destination.name)

    with pytest.raises(FetchError, match=pin.destination.name):
        fetch_weights.fetch_weights(weights, (pin,), hub)

    assert not weights.root.exists() or not _files_under(weights.root)


def test_a_file_present_with_its_digest_is_kept_without_a_download(
    weights: Weights,
) -> None:
    hub = Hub()
    pin = hub.pin(b"model", "checkpoint/model.safetensors")
    present = weights.root / pin.destination
    present.parent.mkdir(parents=True)
    present.write_bytes(b"model")

    # An empty hub serves nothing: a download would fail the fetch.
    fetch_weights.fetch_weights(weights, (pin,), Hub())

    assert present.read_bytes() == b"model"


def test_a_file_present_with_another_digest_is_replaced_by_a_matching_one(
    hub: Hub, weights: Weights
) -> None:
    pin = hub.pin(b"model", "checkpoint/model.safetensors")
    present = weights.root / pin.destination
    present.parent.mkdir(parents=True)
    present.write_bytes(b"stale")

    fetch_weights.fetch_weights(weights, (pin,), hub)

    assert present.read_bytes() == b"model"


def test_a_file_present_with_another_digest_is_gone_when_the_download_does_not_match(
    hub: Hub, weights: Weights
) -> None:
    pin = hub.pin(b"model", "checkpoint/model.safetensors", served=b"tampered")
    present = weights.root / pin.destination
    present.parent.mkdir(parents=True)
    present.write_bytes(b"stale")

    with pytest.raises(FetchError):
        fetch_weights.fetch_weights(weights, (pin,), hub)

    assert not present.exists()


def test_a_link_in_place_of_a_file_is_replaced_by_a_regular_file(
    hub: Hub, weights: Weights, tmp_path: Path
) -> None:
    pin = hub.pin(b"model", "checkpoint/model.safetensors")
    cached = tmp_path / "hub-cache-blob"
    cached.write_bytes(b"model")
    link = weights.root / pin.destination
    link.parent.mkdir(parents=True)
    link.symlink_to(cached)

    fetch_weights.fetch_weights(weights, (pin,), hub)

    assert link.is_file()
    assert not link.is_symlink()


def test_a_download_that_is_a_link_into_a_cache_is_refused_and_places_nothing(
    hub: Hub, weights: Weights
) -> None:
    pin = hub.pin(b"model", "checkpoint/model.safetensors")
    hub.links_into_a_cache = True

    with pytest.raises(FetchError, match=pin.destination.name):
        fetch_weights.fetch_weights(weights, (pin,), hub)

    assert not (weights.root / pin.destination).exists()


def test_staging_left_by_an_interrupted_run_is_cleared_at_the_next_start(
    hub: Hub, weights: Weights
) -> None:
    pin = hub.pin(b"model", "checkpoint/model.safetensors")
    leftover = weights.staging / "interrupted" / "model.safetensors"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"half")

    fetch_weights.fetch_weights(weights, (pin,), hub)

    assert not leftover.exists()


def test_the_command_ends_non_zero_naming_the_file_the_download_did_not_deliver(
    hub: Hub, weights: Weights, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = PINNED_FILES[0]
    hub.serve(pin, b"tampered")
    monkeypatch.setattr(sys, "prefix", str(weights.root.parent / ".venv"))

    with pytest.raises(SystemExit) as ended:
        fetch_weights.main(hub)

    assert str(pin.destination) in str(ended.value.code)
    assert not (weights.root / pin.destination).exists()


@pytest.mark.parametrize("suffix", _REFUSED_SUFFIXES)
@pytest.mark.parametrize("where", ["path", "destination"])
def test_a_pin_of_code_or_a_pickle_cannot_be_made(suffix: str, where: str) -> None:
    pin = PINNED_FILES[0]
    changed = (
        {"path": f"module{suffix}"}
        if where == "path"
        else {"destination": Path(f"checkpoint/module{suffix}")}
    )

    with pytest.raises(ValueError, match=suffix):
        replace(pin, **changed)


@pytest.mark.parametrize("pin", PINNED_FILES, ids=lambda pin: str(pin.destination))
def test_every_pin_is_a_configuration_a_safetensors_file_or_a_notice(
    pin: PinnedFile,
) -> None:
    assert Path(pin.path).suffix in _ADMITTED_SUFFIXES
    assert pin.destination.suffix in _ADMITTED_SUFFIXES


def test_pinned_destinations_are_relative_and_distinct() -> None:
    destinations = [pin.destination for pin in PINNED_FILES]

    assert len(set(destinations)) == len(destinations)
    assert not any(
        destination.is_absolute() or ".." in destination.parts
        for destination in destinations
    )


def test_every_repository_that_supplies_weights_has_its_notice_pinned(
    weights: Weights,
) -> None:
    notices = {
        pin.repository
        for pin in PINNED_FILES
        if weights.notices in (weights.root / pin.destination).parents
    }
    suppliers = {
        pin.repository
        for pin in PINNED_FILES
        if pin.destination.suffix == ".safetensors"
    }

    assert suppliers <= notices
    assert notices - suppliers


def _fetched_under_the_real_layout(hub: Hub, weights: Weights) -> None:
    """Fetch the real pins' paths, with scenario bytes at scenario digests."""
    pins = tuple(hub.serve(pin, pin.destination.name.encode()) for pin in PINNED_FILES)
    fetch_weights.fetch_weights(weights, pins, hub)


def test_upstreams_pipeline_is_handed_directories_holding_the_files_its_loaders_name(
    hub: Hub, weights: Weights, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fetched_under_the_real_layout(hub, weights)
    card = StandInCard(monkeypatch)

    card.calls().load(weights, model_type="lite", world_size=1)

    assert card.loaded is not None
    read = {
        network: Path(directory)
        for network, directory in card.loaded.pipeline.directories_read.items()
    }
    named = {
        "model": (DIFFUSERS_CONFIG_NAME, SAFETENSORS_WEIGHTS_NAME),
        "vae": (DIFFUSERS_CONFIG_NAME, SAFETENSORS_WEIGHTS_NAME),
        "audio_encoder": (TRANSFORMERS_CONFIG_NAME, SAFE_WEIGHTS_NAME),
        "feature_extractor": (FEATURE_EXTRACTOR_NAME,),
    }
    assert read.keys() == named.keys()
    pinned = {weights.root / pin.destination for pin in PINNED_FILES}
    for network, names in named.items():
        for name in names:
            assert read[network] / name in pinned
            assert (read[network] / name).is_file()


def test_the_installed_distribution_carries_both_licence_texts() -> None:
    distribution = importlib.metadata.distribution(_DISTRIBUTION)

    mit = distribution.read_text("licenses/LICENSE")
    apache = distribution.read_text("licenses/soulx-flashhead/LICENSE")

    assert mit == (_REPOSITORY_ROOT / "LICENSE").read_text()
    assert apache is not None
    assert "Apache License" in apache
