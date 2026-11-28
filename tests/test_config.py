"""What the process refuses to start on: settings and files."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from support.identity_support import (
    APP,
    caller_config_text,
    write_caller_config,
)
from support.images import picture
from video_service.config import load_caller_config, load_settings
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
    """CONFIG-1."""
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")

    settings = load_settings()

    assert settings.provider is ProviderName.FAKE
    assert settings.looks_file is None
    assert settings.look_sources == ()


def test_the_provider_root_is_an_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CONFIG-1."""
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")
    monkeypatch.setenv("VIDEO_PROVIDER_ROOT", str(tmp_path))

    assert load_settings().provider_root == tmp_path


def test_a_missing_provider_names_its_setting() -> None:
    """CONFIG-1."""
    with pytest.raises(ValueError, match=r"VIDEO_PROVIDER"):
        load_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VIDEO_PROVIDER", "no-such-provider"),
        ("VIDEO_PORT", "0"),
        ("VIDEO_PORT", "not-a-port"),
        ("VIDEO_MAX_SESSIONS", "0"),
        ("VIDEO_MAX_VIEWERS", "0"),
        ("VIDEO_MAX_QUEUED_AUDIO_SECONDS", "-1"),
        ("VIDEO_MAX_QUEUED_AUDIO_SECONDS", "nan"),
        ("VIDEO_MAX_LAG_MS", "0"),
        ("VIDEO_DRAIN_SECONDS", "-1"),
        ("VIDEO_CONFIG_FILE", "relative/config.toml"),
        ("VIDEO_LOOK_MAX_BYTES", "0"),
        ("VIDEO_LOOK_MAX_PIXELS", "0"),
        ("VIDEO_LOOKS_FILE", "relative/looks.toml"),
        ("VIDEO_PROVIDER_ROOT", "relative/providers"),
        ("VIDEO_LOOK_SOURCES", "not-json"),
        ("VIDEO_LOOK_SOURCES", "[1]"),
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
        ("VIDEO_CARD_MEMORY_MB", "0"),
        ("VIDEO_CARD_MEMORY_MB", "-3"),
        ("VIDEO_CARD_MEMORY_MB", "twelve"),
        ("VIDEO_CARD_MEMORY_MB", "1.5"),
    ],
)
def test_a_setting_that_is_not_valid_stops_the_start_naming_it_and_echoing_nothing(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONFIG-1."""
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name) as stopped:
        load_settings()

    assert value not in str(stopped.value)


def test_an_empty_card_memory_stops_the_start_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONFIG-3."""
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")
    monkeypatch.setenv("VIDEO_CARD_MEMORY_MB", "")

    with pytest.raises(ValueError, match="VIDEO_CARD_MEMORY_MB"):
        load_settings()


def test_flashhead_lite_without_a_card_memory_stops_the_start_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONFIG-3."""
    monkeypatch.setenv("VIDEO_PROVIDER", "flashhead-lite")

    with pytest.raises(ValueError, match="VIDEO_CARD_MEMORY_MB"):
        load_settings()


@pytest.mark.parametrize("cap", ["1024", str(10**12)])
def test_flashhead_lite_reads_any_card_memory_of_at_least_one(
    cap: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONFIG-3."""
    monkeypatch.setenv("VIDEO_PROVIDER", "flashhead-lite")
    monkeypatch.setenv("VIDEO_CARD_MEMORY_MB", cap)

    settings = load_settings()

    assert settings.provider is ProviderName.FLASHHEAD_LITE
    assert settings.card_memory_mb == int(cap)


def test_fake_needs_no_card_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONFIG-3."""
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")

    assert load_settings().card_memory_mb is None


@pytest.mark.parametrize(
    "prefix",
    ["https://images.example/", "http://127.0.0.1:8000/a/b-c_d.e~f/"],
)
def test_a_source_with_a_clean_path_is_allowed(
    prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONFIG-5."""
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")
    monkeypatch.setenv("VIDEO_LOOK_SOURCES", f'["{prefix}"]')

    assert load_settings().look_sources == (prefix,)


def _look_file(directory: Path, text: str) -> Path:
    path = directory / "looks.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_look_file_names_each_look_with_its_image_source(tmp_path: Path) -> None:
    """CONFIG-4."""
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
    """CONFIG-4."""
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
    """CONFIG-4."""
    path = _look_file(tmp_path, text)

    with pytest.raises(ValueError, match=r"VIDEO_LOOKS_FILE"):
        read_look_file(path)


def test_a_fault_inside_a_look_with_a_refused_name_names_no_look(
    tmp_path: Path,
) -> None:
    """CONFIG-4."""
    path = _look_file(tmp_path, '["a SENTINEL"]\nurl = 1\n')

    with pytest.raises(
        ValueError, match=r"VIDEO_LOOKS_FILE breaks .*\(Value error"
    ) as stopped:
        read_look_file(path)

    assert "SENTINEL" not in str(stopped.value)
    assert "url" not in str(stopped.value)


def test_a_fault_inside_a_look_with_a_valid_name_names_the_look(tmp_path: Path) -> None:
    """CONFIG-4."""
    path = _look_file(tmp_path, '[mozart]\nurl = "https://x/"\nsha256 = "ABC"\n')

    with pytest.raises(ValueError, match=r"mozart\.sha256"):
        read_look_file(path)


@pytest.mark.parametrize("content", [None, b"\xff\xfe"], ids=["missing", "not-utf8"])
def test_a_look_file_that_cannot_be_read_names_its_setting(
    content: bytes | None, tmp_path: Path
) -> None:
    """CONFIG-4."""
    path = tmp_path / "looks.toml"
    if content is not None:
        path.write_bytes(content)

    with pytest.raises(ValueError, match=r"VIDEO_LOOKS_FILE"):
        read_look_file(path)


def test_the_runtime_composes_the_provider_and_the_looks_the_settings_name(
    source: LookServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONFIG-1."""
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
    """CONFIG-4."""
    monkeypatch.setenv("VIDEO_PROVIDER", "fake")
    runtime = Runtime.from_settings(load_settings())

    runtime.load()

    assert runtime.health().looks == {}


def test_the_caller_file_names_the_issuer_and_the_apps(tmp_path: Path) -> None:
    """CONFIG-6."""
    config = load_caller_config(write_caller_config(tmp_path))

    assert list(config.apps) == [APP]


@pytest.mark.parametrize(
    ("text", "named"),
    [
        (caller_config_text(identity="extra = 1"), "extra"),
        (caller_config_text(apps=""), "apps"),
        ("apps = 1\n" + caller_config_text(apps=""), "apps"),
        (
            caller_config_text(
                apps="[apps.a]\nworkload_client = 'x'\n"
                "[apps.b]\nworkload_client = 'x'\n"
            ),
            "workload_client",
        ),
        (caller_config_text(apps="[apps.'a/b']\nworkload_client = 'x'\n"), "apps"),
        (
            caller_config_text(apps="[apps.' SENTINEL']\nworkload_client = 'x'\n"),
            "apps",
        ),
        (
            caller_config_text(
                apps="[apps.\"SENTINEL\\u0007\"]\nworkload_client = 'x'\n"
            ),
            "apps",
        ),
        (
            caller_config_text(
                apps="[apps.\"SENTINEL\\u200b\"]\nworkload_client = 'x'\n"
            ),
            "apps",
        ),
        (caller_config_text().replace("https://", "http://"), "jwks_url"),
        (caller_config_text().replace("/services/", "services/"), "service_group"),
        (
            caller_config_text().replace("/services/video-service", "/services//x"),
            "service_group",
        ),
        (
            caller_config_text().replace(
                "/services/video-service", "/services/ SENTINEL"
            ),
            "service_group",
        ),
        (
            caller_config_text().replace(
                "/services/video-service", "/services/SENTINEL\\u0007"
            ),
            "service_group",
        ),
    ],
    ids=[
        "unknown-key",
        "no-app",
        "apps-not-a-table",
        "shared-client",
        "app-name",
        "app-name-with-whitespace",
        "app-name-with-control-character",
        "app-name-with-format-character",
        "jwks-not-https",
        "group-not-a-path",
        "group-empty-segment",
        "group-with-whitespace",
        "group-with-control-character",
    ],
)
def test_a_caller_file_that_breaks_the_form_names_the_field_and_echoes_no_value(
    text: str, named: str, tmp_path: Path
) -> None:
    """CONFIG-6."""
    path = write_caller_config(tmp_path, text)

    with pytest.raises(ValueError, match=named) as stopped:
        load_caller_config(path)

    assert "SENTINEL" not in str(stopped.value)


@pytest.mark.parametrize("name", [" SENTINEL", "a/b SENTINEL"])
def test_a_fault_inside_an_app_with_a_refused_name_names_no_app(
    name: str, tmp_path: Path
) -> None:
    """CONFIG-6."""
    path = write_caller_config(
        tmp_path, caller_config_text(apps=f'[apps."{name}"]\nworkload_client = 1\n')
    )

    with pytest.raises(ValueError, match=r"apps") as stopped:
        load_caller_config(path)

    assert "SENTINEL" not in str(stopped.value)
    assert "workload_client" not in str(stopped.value)


def test_a_fault_inside_an_app_with_a_valid_name_names_the_app(tmp_path: Path) -> None:
    """CONFIG-6."""
    path = write_caller_config(
        tmp_path, caller_config_text(apps="[apps.quiz]\nworkload_client = 1\n")
    )

    with pytest.raises(ValueError, match=r"apps\.quiz\.workload_client"):
        load_caller_config(path)


@pytest.mark.parametrize("content", [None, b"\xff\xfe", b"not toml ["])
def test_a_caller_file_that_cannot_be_read_names_its_setting(
    content: bytes | None, tmp_path: Path
) -> None:
    """CONFIG-6."""
    path = tmp_path / "caller-config.toml"
    if content is not None:
        path.write_bytes(content)

    with pytest.raises(ValueError, match=r"VIDEO_CONFIG_FILE"):
        load_caller_config(path)
