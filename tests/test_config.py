"""The settings and the look file: what the process refuses to start on."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from support.images import picture
from video_service.config import load_settings
from video_service.looks import read_look_file
from video_service.providers import ProviderName
from video_service.runtime import Runtime

if TYPE_CHECKING:
    from pathlib import Path

    from support.look_server import LookServer

_SHA256 = "a" * 64


@pytest.fixture(autouse=True)
def _no_video_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith("VIDEO_")]:
        monkeypatch.delenv(name)


def test_the_provider_alone_is_enough_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")

    settings = load_settings()

    assert settings.provider is ProviderName.FAKE
    assert settings.looks_file is None
    assert settings.look_sources == ()


def test_a_missing_provider_names_its_setting() -> None:
    with pytest.raises(ValueError, match=r"VIDEO_PROVIDER"):
        load_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VIDEO_PROVIDER", "no-such-provider"),
        ("VIDEO_PORT", "0"),
        ("VIDEO_PORT", "not-a-port"),
        ("VIDEO_MAX_SESSIONS", "0"),
        ("VIDEO_LOOK_MAX_BYTES", "0"),
        ("VIDEO_LOOK_MAX_PIXELS", "0"),
        ("VIDEO_LOOKS_FILE", "relative/looks.toml"),
        ("VIDEO_LOOK_SOURCES", '["ftp://images.example/looks/"]'),
        ("VIDEO_LOOK_SOURCES", '["https://images.example/looks"]'),
        ("VIDEO_LOOK_SOURCES", '["https://Images.example/looks/"]'),
        ("VIDEO_LOOK_SOURCES", '["https://user@images.example/looks/"]'),
        ("VIDEO_LOOK_SOURCES", '["https://images.example/looks/?x=1"]'),
        ("VIDEO_LOOK_SOURCES", '["https://images.example/looks/#x"]'),
        ("VIDEO_LOOK_SOURCES", '["https://images.example/../looks/"]'),
        ("VIDEO_LOOK_SOURCES", '["https://images.example//looks/"]'),
        ("VIDEO_LOOK_SOURCES", '["https://images.example/lo%6fks/"]'),
        ("VIDEO_LOOK_SOURCES", '["https:///looks/"]'),
    ],
)
def test_a_setting_that_is_not_valid_stops_the_start_naming_it_and_echoing_nothing(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name) as stopped:
        load_settings()

    assert value not in str(stopped.value)


@pytest.mark.parametrize(
    "prefix",
    ["https://images.example/", "http://127.0.0.1:8000/a/b-c_d.e~f/"],
)
def test_a_source_with_a_clean_path_is_allowed(
    prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")
    monkeypatch.setenv("VIDEO_LOOK_SOURCES", f'["{prefix}"]')

    assert load_settings().look_sources == (prefix,)


def _look_file(directory: Path, text: str) -> Path:
    path = directory / "looks.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_look_file_names_each_look_with_its_image_source(tmp_path: Path) -> None:
    path = _look_file(
        tmp_path,
        f'[mozart]\nurl = "https://images.example/looks/m.png"\nsha256 = "{_SHA256}"\n'
        '["bach.v2"]\nurl = "https://images.example/looks/b.png"\n'
        f'sha256 = "{_SHA256}"\n',
    )

    looks = read_look_file(path)

    assert list(looks) == ["mozart", "bach.v2"]
    assert looks["mozart"].url == "https://images.example/looks/m.png"


def test_an_empty_look_file_offers_no_look(tmp_path: Path) -> None:
    assert read_look_file(_look_file(tmp_path, "")) == {}


@pytest.mark.parametrize(
    "text",
    [
        "not toml [",
        f'["a name with spaces"]\nurl = "https://x/"\nsha256 = "{_SHA256}"\n',
        f'["{"n" * 65}"]\nurl = "https://x/"\nsha256 = "{_SHA256}"\n',
        f'[mozart]\nurl = "https://x/"\nsha256 = "{_SHA256}"\nextra = 1\n',
        '[mozart]\nurl = "https://x/"\nsha256 = "ABC"\n',
        f'[mozart]\nsha256 = "{_SHA256}"\n',
        'mozart = "https://x/"\n',
    ],
    ids=["not-toml", "name", "long-name", "extra-key", "digest", "no-url", "no-table"],
)
def test_a_look_file_that_breaks_the_form_stops_the_start(
    text: str, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match=r"."):
        read_look_file(_look_file(tmp_path, text))


@pytest.mark.parametrize("content", [None, b"\xff\xfe"], ids=["missing", "not-utf8"])
def test_a_look_file_that_cannot_be_read_names_its_setting(
    content: bytes | None, tmp_path: Path
) -> None:
    path = tmp_path / "looks.toml"
    if content is not None:
        path.write_bytes(content)

    with pytest.raises(ValueError, match=r"VIDEO_LOOKS_FILE"):
        read_look_file(path)


def test_the_runtime_composes_the_provider_and_the_looks_the_settings_name(
    source: LookServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = source.host("mozart.png", picture().data)
    path = _look_file(
        tmp_path, f'[mozart]\nurl = "{shown.url}"\nsha256 = "{shown.sha256}"\n'
    )
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")
    monkeypatch.setenv("VIDEO_LOOKS_FILE", str(path))
    monkeypatch.setenv("VIDEO_LOOK_SOURCES", f'["{source.base_url}"]')
    runtime = Runtime.from_settings(load_settings())

    runtime.load()

    health = runtime.health()
    assert health.ready
    assert {name: look.ready for name, look in health.looks.items()} == {"mozart": True}


def test_the_runtime_offers_no_look_without_a_look_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")
    runtime = Runtime.from_settings(load_settings())

    runtime.load()

    assert runtime.health().looks == {}
