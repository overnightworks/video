"""Fetch Lite's pinned weights and notices, each kept only at its digest."""

# ruff: noqa: T201 - this command prints what it kept and what it fetched

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from huggingface_hub import hf_hub_download

from video_provider_flashhead_lite.upstream import PINNED_FILES, locate_weights

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from video_provider_flashhead_lite.upstream import PinnedFile, Weights

_CHUNK_BYTES = 1 << 20
# Upstream's VAE loader prefers this file over the safetensors one and unpickles it.
_PICKLED_CHECKPOINT = "autoencoder.pth"


class FetchError(Exception):
    """A pinned file could not be placed; the message names it."""


def fetch_weights(
    weights: Weights,
    pins: Iterable[PinnedFile],
    download: Callable[..., str] = hf_hub_download,
) -> None:
    """Place every pinned file under `weights.root`, each only at its pinned digest.

    A file there with its digest is kept; one with another digest, or a link, is
    removed before its download. A directory that holds a pickled VAE checkpoint
    next to a pinned file is refused before any download. Each download is a real file in `weights.staging`,
    which sits beside `weights.root`, and is renamed into place once its digest
    matches, so the weights directory never holds a partial file. The hub's own
    metadata stays in staging, which is cleared first and removed at the end.
    `download` is the Hugging Face file downloader.

    Raises:
        FetchError: a file could not be downloaded or is not the pinned bytes, or a
            pinned file's directory holds a pickled VAE checkpoint.
    """
    pins = tuple(pins)
    _refuse_pickled_checkpoints(pins, weights)
    if weights.staging.exists():
        shutil.rmtree(weights.staging)
    weights.staging.mkdir(parents=True)
    try:
        for pin in pins:
            _place(pin, weights, download)
    finally:
        shutil.rmtree(weights.staging)


def main(download: Callable[..., str] = hf_hub_download) -> None:
    """Fetch the pinned files beside this environment; a failure ends non-zero."""
    try:
        fetch_weights(locate_weights(), PINNED_FILES, download)
    except FetchError as error:
        raise SystemExit(str(error)) from error


def _refuse_pickled_checkpoints(pins: Iterable[PinnedFile], weights: Weights) -> None:
    for directory in {(weights.root / pin.destination).parent for pin in pins}:
        pickled = directory / _PICKLED_CHECKPOINT
        if os.path.lexists(pickled):
            message = (
                f"{pickled}: upstream would load this pickle instead of the "
                "pinned safetensors; remove it and run the fetch again"
            )
            raise FetchError(message)


def _place(pin: PinnedFile, weights: Weights, download: Callable[..., str]) -> None:
    target = weights.root / pin.destination
    if _is_pinned_file(target, pin):
        print(f"kept {pin.destination}")
        return
    target.unlink(missing_ok=True)
    staged = _download(pin, weights, download)
    if _sha256_of(staged) != pin.sha256:
        message = f"{pin.destination}: the download is not the pinned file"
        raise FetchError(message)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged.replace(target)
    print(f"fetched {pin.destination}")


def _download(pin: PinnedFile, weights: Weights, download: Callable[..., str]) -> Path:
    directory = tempfile.mkdtemp(dir=weights.staging)
    try:
        staged = Path(
            download(
                pin.repository,
                pin.path,
                revision=pin.commit,
                local_dir=directory,
                token=False,
            )
        )
    except Exception as error:
        message = f"{pin.destination}: the download failed: {error}"
        raise FetchError(message) from error
    if staged.is_symlink() or not staged.is_file():
        message = f"{pin.destination}: the download is not a regular file"
        raise FetchError(message)
    return staged


def _is_pinned_file(path: Path, pin: PinnedFile) -> bool:
    return path.is_file() and not path.is_symlink() and _sha256_of(path) == pin.sha256


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
