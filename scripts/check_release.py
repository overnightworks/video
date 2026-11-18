"""Check that every project and package of the repository stands at the released version.

The release workflow runs this with the version before it creates the tag. Without a
version the check only requires that they agree, which the default test run
does on every pull request.
"""

# ruff: noqa: T201 - the release workflow reads this command's output

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final = Path(__file__).resolve().parent.parent
_PROJECT_FILES: Final = ("pyproject.toml", "*/*/pyproject.toml")
_PACKAGE_FILES: Final = ("packages/*/package.json",)


def project_versions(root: Path) -> dict[str, str]:
    """The version each `pyproject.toml` and `package.json` declares, by its path."""
    versions = {
        file.relative_to(root).as_posix(): tomllib.loads(
            file.read_text(encoding="utf-8")
        )["project"]["version"]
        for pattern in _PROJECT_FILES
        for file in sorted(root.glob(pattern))
    }
    versions |= {
        file.relative_to(root).as_posix(): json.loads(file.read_text(encoding="utf-8"))[
            "version"
        ]
        for pattern in _PACKAGE_FILES
        for file in sorted(root.glob(pattern))
    }
    return versions


def release_faults(root: Path, expected: str | None) -> list[str]:
    """One line per project or package whose version is off, each naming its file."""
    versions = project_versions(root)
    if expected is not None:
        return [
            f"{file} stands at {version}, the release is {expected}"
            for file, version in versions.items()
            if version != expected
        ]
    if len(set(versions.values())) > 1:
        return [f"{file} stands at {version}" for file, version in versions.items()]
    return []


def main(argv: list[str]) -> int:
    """Print each fault and return 1, or return 0 when the files stand as asked."""
    faults = release_faults(REPOSITORY_ROOT, argv[0] if argv else None)
    for fault in faults:
        print(fault, file=sys.stderr)
    return 1 if faults else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
