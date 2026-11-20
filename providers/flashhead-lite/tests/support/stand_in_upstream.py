"""Upstream's own pipeline on CPU: only its networks are tiny stand-ins.

`StandInPipeline` is upstream's `FlashHeadPipeline`, built by upstream's own
`__init__` with its four networks (model, VAE, audio encoder, feature extractor)
swapped for tiny CPU ones. Upstream's `prepare_params`, `reset_person_name`,
`generate` and the inference helpers run unchanged, so the state tests judge
upstream's state logic. The stand-in networks make a picture depend on the audio
window, the reference latent and the motion latent, which is what tells two
sessions apart.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest import mock

import torch
from flash_head.src.pipeline import flash_head_pipeline
from video_provider_flashhead_lite.upstream import import_inference
from video_provider_flashhead_lite.upstream_renderer import (
    CardCalls,
    CardMemory,
    LoadedUpstream,
)

if TYPE_CHECKING:
    import os
    from collections.abc import Mapping

    import pytest
    from video_provider_flashhead_lite.upstream import Weights

_LATENT_CHANNELS = 4
_TIME_STRIDE = 8
_SPACE_STRIDE = 32
_AUDIO_LAYERS = 2
_HIDDEN = 16
_RECEPTIVE_FIELD = 10
_NUM_TIMESTEPS = 1000
_AUDIO_GAIN = 500
_WEIGHTS_SEED = 7
_PICTURE_GAIN = 3

# The directories the loaders of the pipeline being built are handed, by network.
_DIRECTORIES_READ: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "directories_read"
)


def _note_directory(network: str, directory: str | os.PathLike[str]) -> None:
    with contextlib.suppress(LookupError):
        _DIRECTORIES_READ.get()[network] = str(directory)


def _weighted_mean(embedding: torch.Tensor) -> torch.Tensor:
    # A plain mean of a normalised embedding hardly moves; positions tell audio apart.
    weights = torch.linspace(-1, 1, embedding.numel()).view_as(embedding)
    return (embedding.float() * weights).mean()


class StandInModelError(Exception):
    """The stand-in model was told to fail."""


class _Model(torch.nn.Module):
    """Draws a flow that follows the audio, the reference and the motion."""

    config = SimpleNamespace(
        out_dim=_LATENT_CHANNELS,
        vae_stride=(_TIME_STRIDE, _SPACE_STRIDE, _SPACE_STRIDE),
    )

    def __init__(self) -> None:
        """A model that serves until `fails` is set."""
        super().__init__()
        self.fails = False

    @classmethod
    def from_pretrained(cls, directory: str) -> _Model:
        _note_directory("model", directory)
        return cls()

    def forward(
        self,
        x: torch.Tensor,
        timestep: torch.Tensor,
        context: torch.Tensor,
        y: torch.Tensor,
    ) -> tuple[torch.Tensor]:
        if self.fails:
            message = "the stand-in model was told to fail"
            raise StandInModelError(message)
        # Inverts the sampler's update, so the picture follows the audio, the
        # reference and the motion frames and never the noise.
        target = y[0] * (1 + _AUDIO_GAIN * _weighted_mean(context))
        return ((x[0] - target) / (timestep.float() / _NUM_TIMESTEPS),)


class _Vae:
    """A video of three channels and a latent of `_LATENT_CHANNELS`, both by stride."""

    def __init__(self, pretrained_model_type_or_path: str, **_options: object) -> None:
        """Take upstream's constructor arguments and ignore the weights."""
        _note_directory("vae", pretrained_model_type_or_path)
        self.fails = False

    def encode(self, video: torch.Tensor) -> torch.Tensor:
        if self.fails:
            message = "the stand-in VAE was told to fail"
            raise StandInModelError(message)
        frames = video.float().mean(dim=1)[0]
        kept = frames[::_TIME_STRIDE][:, None]
        pooled = torch.nn.functional.avg_pool2d(kept, _SPACE_STRIDE)[:, 0]
        weights = torch.arange(1, _LATENT_CHANNELS + 1) / _LATENT_CHANNELS
        return pooled[None] * weights[:, None, None, None]

    def decode(self, latent: torch.Tensor) -> torch.Tensor:
        frames = (latent.shape[1] - 1) * _TIME_STRIDE + 1
        plane = latent.float().mean(dim=0)[torch.arange(frames) // _TIME_STRIDE]
        plane = plane.repeat_interleave(_SPACE_STRIDE, dim=1).repeat_interleave(
            _SPACE_STRIDE, dim=2
        )
        colours = torch.tensor([-0.3, 0.0, 0.3]).view(1, 3, 1, 1, 1)
        return torch.tanh(_PICTURE_GAIN * plane[None, None] + colours)


def _tiny_audio_encoder_class() -> type:
    """Upstream's own audio encoder class at a size a CPU test can afford."""
    from flash_head.audio_analysis.wav2vec2 import Wav2Vec2Model  # noqa: PLC0415
    from transformers import Wav2Vec2Config  # noqa: PLC0415

    class TinyAudioEncoder(Wav2Vec2Model):
        @classmethod
        def from_pretrained(cls, directory: str, **_options: object) -> Wav2Vec2Model:
            _note_directory("audio_encoder", directory)
            configuration = Wav2Vec2Config(
                hidden_size=_HIDDEN,
                num_hidden_layers=_AUDIO_LAYERS,
                num_attention_heads=2,
                intermediate_size=2 * _HIDDEN,
                conv_dim=(8, 8),
                conv_stride=(5, 2),
                conv_kernel=(_RECEPTIVE_FIELD, 3),
                num_feat_extract_layers=2,
                num_conv_pos_embeddings=4,
                num_conv_pos_embedding_groups=2,
            )
            # The same weights every time, so two pipelines hear alike.
            with torch.random.fork_rng():
                torch.manual_seed(_WEIGHTS_SEED)
                return cls(configuration).eval()

    return TinyAudioEncoder


def _tiny_feature_extractor_class() -> type:
    from transformers import Wav2Vec2FeatureExtractor  # noqa: PLC0415

    class UntrainedFeatureExtractor(Wav2Vec2FeatureExtractor):
        @classmethod
        def from_pretrained(cls, directory: str, **_options: object) -> object:
            _note_directory("feature_extractor", directory)
            return cls()

    return UntrainedFeatureExtractor


class StandInPipeline(flash_head_pipeline.FlashHeadPipeline):
    """Upstream's pipeline with tiny CPU networks.

    `directories_read` names, by network, the directory each loader was handed.
    """

    def __init__(
        self,
        checkpoint_dir: str,
        model_type: str,
        wav2vec_dir: str,
        device: str = "cuda",
        *,
        use_usp: bool = False,
    ) -> None:
        """Build upstream's pipeline on the CPU; `device` is ignored."""
        del device
        self.heard: list[torch.Tensor] = []
        self.directories_read: dict[str, str] = {}
        from flash_head.ltx_video import ltx_vae  # noqa: PLC0415

        with contextlib.ExitStack() as stack:
            reading = _DIRECTORIES_READ.set(self.directories_read)
            stack.callback(_DIRECTORIES_READ.reset, reading)
            for owner, name, stand_in in (
                (flash_head_pipeline, "WanModelAudioProject", _Model),
                (flash_head_pipeline, "Wav2Vec2Model", _tiny_audio_encoder_class()),
                (
                    flash_head_pipeline,
                    "Wav2Vec2FeatureExtractor",
                    _tiny_feature_extractor_class(),
                ),
                (flash_head_pipeline, "COMPILE_MODEL", False),
                (flash_head_pipeline, "COMPILE_VAE", False),
                (ltx_vae, "LtxVAE", _Vae),
            ):
                stack.enter_context(mock.patch.object(owner, name, stand_in))
            super().__init__(
                checkpoint_dir,
                model_type,
                wav2vec_dir,
                device="cpu",
                param_dtype=torch.float32,
                use_usp=use_usp,
            )

    def generate(self, audio_embedding: torch.Tensor) -> torch.Tensor:
        """Upstream's `generate`, noting the audio it was given."""
        self.heard.append(audio_embedding.clone())
        return super().generate(audio_embedding)


@dataclass
class StandInCard:
    """The card's calls over upstream's pipeline, recording what it was asked.

    `overrides` replace upstream's inference parameters after `get_pipeline` sets
    its own, so a test can shrink the picture and the window.
    """

    monkeypatch: pytest.MonkeyPatch
    free_bytes: int = 8 << 30
    total_bytes: int = 16 << 30
    visible: bool = True
    overrides: Mapping[str, int] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    loaded: LoadedUpstream | None = None
    fraction: float | None = None
    weights: Weights | None = None

    def __post_init__(self) -> None:
        """Silence the card synchronisation upstream's `generate` calls."""
        # `generate` synchronises the card unconditionally.
        self.monkeypatch.setattr(torch.cuda, "synchronize", lambda *_a, **_k: None)

    def calls(self) -> CardCalls:
        """The calls the renderer is handed."""
        return CardCalls(
            memory=self._memory,
            limit_allocator=self._limit_allocator,
            load=self._load,
        )

    def _memory(self) -> CardMemory | None:
        self.events.append("memory")
        if not self.visible:
            return None
        return CardMemory(free=self.free_bytes, total=self.total_bytes)

    def _limit_allocator(self, fraction: float) -> None:
        self.events.append("limit")
        self.fraction = fraction

    def _load(
        self, weights: Weights, *, model_type: str, world_size: int
    ) -> LoadedUpstream:
        self.events.append("load")
        self.weights = weights
        inference = import_inference()
        with mock.patch.object(inference, "FlashHeadPipeline", StandInPipeline):
            pipeline = inference.get_pipeline(
                world_size, str(weights.checkpoint), model_type, str(weights.wav2vec)
            )
        for name, value in self.overrides.items():
            self.monkeypatch.setitem(inference.infer_params, name, value)
        self.loaded = LoadedUpstream(inference=inference, pipeline=pipeline)
        return self.loaded
