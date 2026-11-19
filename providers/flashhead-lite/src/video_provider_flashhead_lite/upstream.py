"""The one place that imports upstream's FlashHead inference module.

Upstream reads `flash_head/configs/infer_params.yaml` relative to the working
directory when its inference module is imported, so the import runs from the
directory that holds the installed `flash_head` package and restores the caller's
working directory afterwards, also when the import fails.

The working directory belongs to the whole process: the import is safe only while
no other thread resolves a relative path during it.
"""

from __future__ import annotations

import importlib
from contextlib import chdir
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_PACKAGE = "flash_head"


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
