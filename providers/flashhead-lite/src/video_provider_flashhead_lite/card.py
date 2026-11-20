"""The calls that need a card: its memory, the allocator and upstream's load.

This module holds nothing else, and the card proof drives it; the renderer is
handed these calls and imports neither this module nor torch's card API.
"""

from __future__ import annotations

import torch

from video_provider_flashhead_lite.upstream import Weights, import_inference
from video_provider_flashhead_lite.upstream_renderer import CardMemory, LoadedUpstream


def memory() -> CardMemory | None:
    """The visible card's free and total memory, or `None` without a card."""
    if not torch.cuda.is_available():
        return None
    free, total = torch.cuda.mem_get_info()
    return CardMemory(free=free, total=total)


def limit_allocator(fraction: float) -> None:
    """Cap torch's allocator on the visible card at `fraction` of its total memory."""
    torch.cuda.set_per_process_memory_fraction(fraction)


def load(weights: Weights, *, model_type: str, world_size: int) -> LoadedUpstream:
    """Import upstream and build its pipeline from `weights`."""
    inference = import_inference()
    pipeline = inference.get_pipeline(
        world_size, str(weights.checkpoint), model_type, str(weights.wav2vec)
    )
    return LoadedUpstream(inference=inference, pipeline=pipeline)
