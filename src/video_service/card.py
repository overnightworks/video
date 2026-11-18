"""How much of the GPU the process can see."""

from __future__ import annotations

import shutil
import subprocess
from typing import Final

_QUERY: Final = [
    "--query-gpu=memory.used",
    "--format=csv,noheader,nounits",
]
_QUERY_SECONDS: Final = 2


def card_memory_mb() -> int:
    """Used memory on GPU 0 in mebibytes, or 0 when the card cannot be queried."""
    nvidia = shutil.which("nvidia-smi")
    if nvidia is None:
        return 0
    try:
        completed = subprocess.run(  # noqa: S603 - argv is the resolved nvidia-smi and a fixed query with no shell
            [nvidia, *_QUERY],
            check=True,
            capture_output=True,
            text=True,
            timeout=_QUERY_SECONDS,
        )
        return int(completed.stdout.splitlines()[0].strip())
    except OSError, subprocess.SubprocessError, IndexError, ValueError:
        return 0
