"""The FlashHead lite provider's process: the contract's worker loop over the driver."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, NoReturn

from video_provider_contract.worker import (
    CARD_MEMORY_MB_ENV,
    read_required_positive_integer,
    run_worker,
)

from video_provider_flashhead_lite.driver import FlashHeadLiteProvider
from video_provider_flashhead_lite.upstream import locate_weights
from video_provider_flashhead_lite.upstream_renderer import CardCalls, UpstreamRenderer

if TYPE_CHECKING:
    from collections.abc import Callable

    from video_provider_contract import Provider


def main(serve: Callable[[Provider], NoReturn] = run_worker) -> NoReturn:
    """Serve the provider through `serve` until the service closes the pipe.

    The card memory cap is read before anything is built; a bad one ends the
    process before the first frame.
    """
    cap_mebibytes = read_required_positive_integer(CARD_MEMORY_MB_ENV)
    # huggingface_hub reads this once, at import; nothing here imports it before.
    os.environ["HF_HUB_OFFLINE"] = "1"
    from video_provider_flashhead_lite import card  # noqa: PLC0415 - loads torch

    renderer = UpstreamRenderer(
        cap_mebibytes=cap_mebibytes,
        weights=locate_weights(),
        card=CardCalls(
            memory=card.memory, limit_allocator=card.limit_allocator, load=card.load
        ),
    )
    serve(FlashHeadLiteProvider(renderer))
