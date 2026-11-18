"""The release check: every project of the repository stands at one version."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from check_release import REPOSITORY_ROOT, main, release_faults

if TYPE_CHECKING:
    from pathlib import Path


def _project(root: Path, directory: str, version: str) -> None:
    (root / directory).mkdir(parents=True, exist_ok=True)
    (root / directory / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8"
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _project(tmp_path, ".", "1.2.3")
    _project(tmp_path, "packages/client", "1.2.3")
    _project(tmp_path, "providers/fake", "1.2.3")
    return tmp_path


def test_projects_at_one_version_have_no_fault(repository: Path) -> None:
    assert release_faults(repository, None) == []
    assert release_faults(repository, "1.2.3") == []


def test_a_project_at_another_version_is_named(repository: Path) -> None:
    _project(repository, "providers/fake", "1.2.4")

    assert release_faults(repository, None)
    assert release_faults(repository, "1.2.3") == [
        "providers/fake/pyproject.toml stands at 1.2.4, the release is 1.2.3"
    ]


def test_this_repository_stands_at_one_version() -> None:
    assert release_faults(REPOSITORY_ROOT, None) == []


def test_main_returns_nonzero_for_a_release_the_projects_are_not_at() -> None:
    assert main(["999.0.0"]) == 1
    assert main([]) == 0
