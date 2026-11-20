"""The one place that binds upstream: its inference module and its weights.

The weights live in `weights` beside the provider's `.venv`, so recreating the
environment keeps them and an image layer can carry them before the
environment's; the fetch fills the two directories `Weights` names and the
notices directory beside them, through a staging directory beside `weights`.
The pins say which file of which repository, at which commit and digest, lands
where.

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
_STAGING = "weights-staging"
_CHECKPOINT = "SoulX-FlashHead-1_3B"
_WAV2VEC = "wav2vec2-base-960h"
_NOTICES = "notices"
# A pin is a configuration, a safetensors file or a licence notice, never code or a
# pickle: upstream's `torch.load` runs only on a file this set leaves out.
_ADMITTED_SUFFIXES = frozenset({".json", ".safetensors", ".txt", ".md"})


@dataclass(frozen=True, slots=True)
class Weights:
    """Where the model's weights are: the directory, its trees and its staging."""

    root: Path
    checkpoint: Path
    wav2vec: Path
    notices: Path
    staging: Path


@dataclass(frozen=True, slots=True)
class PinnedFile:
    """One file of a public repository at a commit, and where it lands in `Weights`."""

    repository: str
    commit: str
    path: str
    sha256: str
    destination: Path

    def __post_init__(self) -> None:
        """Refuse a file whose kind is not admitted.

        Raises:
            ValueError: the path or the destination is not a configuration, a
                safetensors file or a notice.
        """
        for name in (self.path, self.destination.name):
            suffix = Path(name).suffix.lower()
            if suffix not in _ADMITTED_SUFFIXES:
                message = (
                    f"{name}: {suffix!r} is no configuration, safetensors or notice"
                )
                raise ValueError(message)


_FLASHHEAD = "Soul-AILab/SoulX-FlashHead-1_3B"
_FLASHHEAD_COMMIT = "74d81686a7bd750b276ec1ade295bc4077c5868a"
_WAV2VEC_REPOSITORY = "facebook/wav2vec2-base-960h"
_WAV2VEC_COMMIT = "22aad52d435eb6dbaf354bdad9b0da84ce7d6156"
_LTX_VIDEO = "Lightricks/LTX-Video"
_LTX_VIDEO_COMMIT = "8984fa25007f376c1a299016d0957a37a2f797bb"

# Lite reads `Model_Lite` and `VAE_LTX` under the checkpoint directory and the
# wav2vec2 directory, each as a local directory. The digests of the safetensors
# files are the hub's `lfs.oid` at the commit; the others are the sha256 of the file
# at the commit.
PINNED_FILES: tuple[PinnedFile, ...] = (
    PinnedFile(
        _FLASHHEAD,
        _FLASHHEAD_COMMIT,
        "Model_Lite/config.json",
        "2ac56ba975a80da49f8ef1b27bfe740be473a1b250fced52f8f1df9ac101bc2c",
        Path(_CHECKPOINT, "Model_Lite", "config.json"),
    ),
    PinnedFile(
        _FLASHHEAD,
        _FLASHHEAD_COMMIT,
        "Model_Lite/diffusion_pytorch_model.safetensors",
        "aaf1cde6e80ca23f740aae236c47954249f65b151db133cc0f77d3a138ccdf6e",
        Path(_CHECKPOINT, "Model_Lite", "diffusion_pytorch_model.safetensors"),
    ),
    PinnedFile(
        _FLASHHEAD,
        _FLASHHEAD_COMMIT,
        "VAE_LTX/config.json",
        "e8f12ad5305b1508f6e456797d48ab8e9fdd091c686ccf814a293ee96ce89dbb",
        Path(_CHECKPOINT, "VAE_LTX", "config.json"),
    ),
    PinnedFile(
        _FLASHHEAD,
        _FLASHHEAD_COMMIT,
        "VAE_LTX/diffusion_pytorch_model.safetensors",
        "265ca87cb5dff5e37f924286e957324e282fe7710a952a7dafc0df43883e2010",
        Path(_CHECKPOINT, "VAE_LTX", "diffusion_pytorch_model.safetensors"),
    ),
    PinnedFile(
        _WAV2VEC_REPOSITORY,
        _WAV2VEC_COMMIT,
        "config.json",
        "d3ec255c063d9f95057b553b19c20135b259875834a4fe9deb218a6be25b4cf3",
        Path(_WAV2VEC, "config.json"),
    ),
    PinnedFile(
        _WAV2VEC_REPOSITORY,
        _WAV2VEC_COMMIT,
        "model.safetensors",
        "8aa76ab2243c81747a1f832954586bc566090c83a0ac167df6f31f0fa917d74a",
        Path(_WAV2VEC, "model.safetensors"),
    ),
    PinnedFile(
        _WAV2VEC_REPOSITORY,
        _WAV2VEC_COMMIT,
        "preprocessor_config.json",
        "b225d617c025463b9e157e06afea8b90dc7078fc70b013c533328423e0486b4a",
        Path(_WAV2VEC, "preprocessor_config.json"),
    ),
    # The notices the components' licences ask to travel with the files: the model
    # card that names them, the licence text of the VAE (byte-identical to
    # LTX-Video 0.9's) and wav2vec2's model card.
    PinnedFile(
        _FLASHHEAD,
        _FLASHHEAD_COMMIT,
        "README.md",
        "b2108bd19581a9930a78568f06d1d01b95225d20d5642ec3764dbb756ec35f04",
        Path(_NOTICES, "SoulX-FlashHead-1_3B-README.md"),
    ),
    PinnedFile(
        _LTX_VIDEO,
        _LTX_VIDEO_COMMIT,
        "ltx-video-2b-v0.9.license.txt",
        "dec004198b977478897006d2646aec31d8bcec1715cb25134e815922d5b04fe4",
        Path(_NOTICES, "ltx-video-2b-v0.9.license.txt"),
    ),
    PinnedFile(
        _WAV2VEC_REPOSITORY,
        _WAV2VEC_COMMIT,
        "README.md",
        "fc52a0adfa64e446800f9944c6cf9b90da3127aba548860ea5714516ec8c18c7",
        Path(_NOTICES, "wav2vec2-base-960h-README.md"),
    ),
)


def locate_weights(environment_prefix: Path | None = None) -> Weights:
    """The weights beside the `.venv` that `environment_prefix` names.

    The prefix defaults to this interpreter's environment.
    """
    prefix = Path(sys.prefix) if environment_prefix is None else environment_prefix
    root = prefix.parent / _WEIGHTS
    return Weights(
        root=root,
        checkpoint=root / _CHECKPOINT,
        wav2vec=root / _WAV2VEC,
        notices=root / _NOTICES,
        staging=prefix.parent / _STAGING,
    )


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
