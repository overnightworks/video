"""The one place that binds upstream: its inference module and its weights.

The weights live in `weights` beside the provider's `.venv`, so recreating the
environment keeps them and an image layer can carry them before the
environment's; the fetch fills the two directories `Weights` names.

Upstream reads `flash_head/configs/infer_params.yaml` relative to the working
directory when its inference module is imported, so the import runs from the
directory that holds the installed `flash_head` package and restores the caller's
working directory afterwards, also when the import fails.

The working directory belongs to the whole process: the import is safe only while
no other thread resolves a relative path during it.
"""

from __future__ import annotations

import importlib
import sys
from contextlib import chdir
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_PACKAGE = "flash_head"
_WEIGHTS = "weights"
_CHECKPOINT = "SoulX-FlashHead-1_3B"
_WAV2VEC = "wav2vec2-base-960h"


@dataclass(frozen=True, slots=True)
class Weights:
    """Where the model's weights are: the directory and the two trees under it."""

    root: Path
    checkpoint: Path
    wav2vec: Path


def locate_weights(environment_prefix: Path | None = None) -> Weights:
    """The weights beside the `.venv` that `environment_prefix` names.

    The prefix defaults to this interpreter's environment.
    """
    prefix = Path(sys.prefix) if environment_prefix is None else environment_prefix
    root = prefix.parent / _WEIGHTS
    return Weights(root=root, checkpoint=root / _CHECKPOINT, wav2vec=root / _WAV2VEC)


def import_inference(package_root: Path | None = None) -> ModuleType:
    """Import `flash_head.inference` from the directory that holds `flash_head`.

    `package_root` names that directory when it is not the installed one.

    Raises:
        ModuleNotFoundError: `flash_head` is not installed.
    """
    with chdir(_installed_root() if package_root is None else package_root):
        return importlib.import_module(f"{_PACKAGE}.inference")


def _installed_root() -> Path:
    # `flash_head` is a namespace package: importing it runs no upstream code.
    package = importlib.import_module(_PACKAGE)
    return Path(next(iter(package.__path__))).parent
