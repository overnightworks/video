"""What varies by machine; everything else is a constant beside its owner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from webauth.https_client import is_https_url

from video_service.looks import SourcePrefix
from video_service.providers import ProviderName
from video_service.toml_file import read_toml_file

_LOOKS_FILE_ERROR: Final = "VIDEO_LOOKS_FILE must be an absolute path"
_SOURCES_ERROR: Final = "must be a JSON list of source prefixes"
_CONFIG_FILE_ERROR: Final = "VIDEO_CONFIG_FILE must be an absolute path"
_KEY_SET_URL_ERROR: Final = "jwks_url must be an https URL"
_SERVICE_GROUP_ERROR: Final = (
    "service_group must be a group path such as /services/name"
)
_APP_NAME_ERROR: Final = "an app name must be one group path segment"
_SHARED_CLIENT_ERROR: Final = "workload_client must belong to one app"
_ENV_PREFIX: Final = "VIDEO_"
_DEFAULT_LOOK_MAX_BYTES: Final = 8 * 1024 * 1024
_DEFAULT_LOOK_MAX_PIXELS: Final = 4096 * 4096


class Settings(BaseSettings):
    """Read from `VIDEO_*` in the environment."""

    model_config = SettingsConfigDict(env_prefix=_ENV_PREFIX, hide_input_in_errors=True)

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    provider: ProviderName
    looks_file: Path | None = None
    look_sources: Annotated[tuple[SourcePrefix, ...], NoDecode] = ()
    look_max_bytes: int = Field(default=_DEFAULT_LOOK_MAX_BYTES, ge=1)
    look_max_pixels: int = Field(default=_DEFAULT_LOOK_MAX_PIXELS, ge=1)
    max_sessions: int = Field(default=1, ge=1)
    max_viewers: int = Field(default=5, ge=1)
    max_queued_audio_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    max_lag_ms: int = Field(default=500, ge=1)
    drain_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    config_file: Path | None = None

    @field_validator("look_sources", mode="before")
    @classmethod
    def _look_sources_are_json(cls, raw: object) -> object:
        if not isinstance(raw, str):
            return raw
        try:
            return json.loads(raw)
        except ValueError:
            raise ValueError(_SOURCES_ERROR) from None

    @field_validator("looks_file")
    @classmethod
    def _looks_file_is_absolute(cls, path: Path | None) -> Path | None:
        if path is not None and not path.is_absolute():
            raise ValueError(_LOOKS_FILE_ERROR)
        return path

    @field_validator("config_file")
    @classmethod
    def _config_file_is_absolute(cls, path: Path | None) -> Path | None:
        if path is not None and not path.is_absolute():
            raise ValueError(_CONFIG_FILE_ERROR)
        return path


def load_settings() -> Settings:
    """Read `VIDEO_*`; a fault names its setting, never its value."""
    try:
        return Settings()
    except ValidationError as error:
        faults = [
            f"{_ENV_PREFIX}{str(fault['loc'][0]).upper()}: {fault['msg']}"
            for fault in error.errors()
        ]
        raise ValueError("; ".join(faults)) from None


class _FileModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, strict=True
    )


class IdentityConfig(_FileModel):
    """Who vouches for the callers, and the one group that may use this service."""

    issuer: str
    jwks_url: str = Field(repr=False)
    audience: str
    workload_role: str
    group_claim: str
    service_group: str

    @field_validator("jwks_url")
    @classmethod
    def _key_set_is_served_over_https(cls, url: str) -> str:
        if not is_https_url(url):
            raise ValueError(_KEY_SET_URL_ERROR)
        return url

    @field_validator("service_group")
    @classmethod
    def _service_group_is_a_group_path(cls, group: str) -> str:
        if not group.startswith("/") or "" in group[1:].split("/"):
            raise ValueError(_SERVICE_GROUP_ERROR)
        return group


class AppConfig(_FileModel):
    """An app the service admits: its workload client."""

    workload_client: str


class CallerConfig(_FileModel):
    """The service's trust in its callers, read from `VIDEO_CONFIG_FILE`."""

    identity: IdentityConfig
    apps: Annotated[dict[str, AppConfig], Field(min_length=1)]

    @field_validator("apps")
    @classmethod
    def _app_names_are_one_segment(
        cls, apps: dict[str, AppConfig]
    ) -> dict[str, AppConfig]:
        if any(not name or "/" in name for name in apps):
            raise ValueError(_APP_NAME_ERROR)
        return apps

    @model_validator(mode="after")
    def _each_client_belongs_to_one_app(self) -> Self:
        clients = [app.workload_client for app in self.apps.values()]
        if len(set(clients)) != len(clients):
            raise ValueError(_SHARED_CLIENT_ERROR)
        return self


_CALLER_CONFIG = TypeAdapter(CallerConfig)


def load_caller_config(path: Path) -> CallerConfig:
    """Read the caller configuration; a bad file fails naming a field, not a value."""
    return read_toml_file(
        path,
        _CALLER_CONFIG,
        variable="VIDEO_CONFIG_FILE",
        form="the caller configuration",
    )
