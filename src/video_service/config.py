"""What varies by machine; everything else is a constant beside its owner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from video_service.looks import SourcePrefix
from video_service.providers import ProviderName

_LOOKS_FILE_ERROR: Final = "VIDEO_LOOKS_FILE must be an absolute path"
_SOURCES_ERROR: Final = "must be a JSON list of source prefixes"
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

    @field_validator("look_sources", mode="before")
    @classmethod
    def _look_sources_are_json(cls, raw: Any) -> Any:  # noqa: ANN401 - the raw environment value
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
