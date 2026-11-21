"""The image's inputs: the changed paths the build context holds."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest

from image_paths import REPOSITORY_ROOT, BuildContext, main

if TYPE_CHECKING:
    from pathlib import Path

_SHIPPED = REPOSITORY_ROOT / ".dockerignore"


@pytest.fixture(scope="module")
def shipped() -> BuildContext:
    return BuildContext(_SHIPPED.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "path",
    [
        "Dockerfile",
        ".dockerignore",
        "uv.lock",
        "providers/flashhead-lite/src/x.py",
        "src/video_service/app.py",
        "packages/client/src/x.py",
        "pyproject.toml",
        "LICENSE",
        "providers/flashhead-lite/soulx-flashhead",
    ],
)
def test_the_shipped_allowlist_holds_what_the_image_is_built_from(
    shipped: BuildContext, path: str
) -> None:
    assert shipped.holds(path)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/wire.md",
        "tests/test_x.py",
        "packages/browser/src/watch.ts",
        "src/video_service/.venv/lib/x.py",
        "src/video_service/key.pem",
    ],
)
def test_the_shipped_allowlist_leaves_out_what_the_image_is_not_built_from(
    shipped: BuildContext, path: str
) -> None:
    assert not shipped.holds(path)


@pytest.mark.parametrize(
    ("dockerignore", "path", "held"),
    [
        ("*\n!a.txt\n", "a.txt", True),
        ("*\n!a.txt\n", "b.txt", False),
        ("*\n!src/**/*.py\n", "src/a.py", True),
        ("*\n!src/**/*.py\n", "src/deep/er/a.py", True),
        ("*\n!src/**/*.py\n", "src/a.txt", False),
        ("*\n!src/*.py\n", "src/deep/a.py", False),
        ("*\n!a?.txt\n", "ab.txt", True),
        ("*\n!a?.txt\n", "a/.txt", False),
        ("*\n!src/**\n**/skip/**\n", "src/skip/a.py", False),
        ("*\n!src/**\n**/skip/**\n", "src/keep/a.py", True),
        ("# comment\n\n*\n!a.txt\n", "a.txt", True),
        ("!/a.txt\n*\n", "a.txt", False),
        ("*\n!a.txt\n", "Dockerfile", True),
        ("", "a.txt", False),
        ("*\n!sub/tree/**\nsub/**\n", "sub/tree", False),
        ("*\n!sub/tree/**\n", "sub/tree", True),
    ],
)
def test_the_last_matching_pattern_decides_whether_the_context_holds_a_path(
    dockerignore: str, path: str, *, held: bool
) -> None:
    assert BuildContext(dockerignore).holds(path) is held


def test_a_path_above_an_allowlisted_pattern_is_held_as_a_submodule_pointer() -> None:
    context = BuildContext("*\n!sub/tree/**\n")

    assert context.holds("sub/tree")
    assert not context.holds("subtree")


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    changed: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    dockerignore = tmp_path / ".dockerignore"
    dockerignore.write_text("*\n!src/**/*.py\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(changed))
    code = main([str(dockerignore)])
    return code, capsys.readouterr().out


def test_the_command_prints_the_held_paths_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, output = _run(
        monkeypatch, tmp_path, "README.md\nsrc/a.py\n\nDockerfile\n", capsys
    )

    assert (code, output) == (0, "src/a.py\nDockerfile\n")


def test_the_command_fails_when_no_changed_path_is_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, output = _run(monkeypatch, tmp_path, "README.md\ndocs/wire.md\n", capsys)

    assert (code, output) == (1, "")
