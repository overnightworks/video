"""The fake provider's process: the contract's worker loop over a `FakeProvider`."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, NoReturn, TypedDict

from video_provider_contract.worker import run_worker

from video_provider_fake import FakeProvider

if TYPE_CHECKING:
    from collections.abc import Mapping

RENDER_SECONDS = "FAKE_RENDER_SECONDS"
IDLE_RENDER_SECONDS = "FAKE_IDLE_RENDER_SECONDS"
CHUNK_FRAMES = "FAKE_CHUNK_FRAMES"
WIDTH = "FAKE_WIDTH"
HEIGHT = "FAKE_HEIGHT"


class Settings(TypedDict, total=False):
    """The `FakeProvider` arguments the environment sets; the rest are defaults."""

    render_seconds: float
    idle_render_seconds: float
    chunk_frames: int
    width: int
    height: int


def settings_from(environment: Mapping[str, str]) -> Settings:
    """The settings `environment` names: render costs, chunk frames, picture size."""
    settings = Settings()
    if RENDER_SECONDS in environment:
        settings["render_seconds"] = float(environment[RENDER_SECONDS])
    if IDLE_RENDER_SECONDS in environment:
        settings["idle_render_seconds"] = float(environment[IDLE_RENDER_SECONDS])
    if CHUNK_FRAMES in environment:
        settings["chunk_frames"] = int(environment[CHUNK_FRAMES])
    if WIDTH in environment:
        settings["width"] = int(environment[WIDTH])
    if HEIGHT in environment:
        settings["height"] = int(environment[HEIGHT])
    return settings


def main() -> NoReturn:
    """Serve a fake provider the environment sets until the pipe closes."""
    run_worker(FakeProvider(**settings_from(os.environ)))
