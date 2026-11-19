"""The import owner binds upstream's inference module and its audio encoder works."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from video_provider_flashhead_lite.upstream import import_inference

if TYPE_CHECKING:
    from collections.abc import Iterator

_UPSTREAM_ENTRY_POINTS = (
    "get_pipeline",
    "get_base_data",
    "get_infer_params",
    "get_audio_embedding",
    "run_pipeline",
)


@pytest.fixture(autouse=True, scope="module")
def _no_card() -> Iterator[None]:
    # Set before torch is first imported, so no test in this module can reach a card.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CUDA_VISIBLE_DEVICES", "")
        yield


@pytest.fixture
def fresh_import(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "flash_head.inference", raising=False)


@pytest.mark.usefixtures("fresh_import")
@pytest.mark.parametrize("entry_point", _UPSTREAM_ENTRY_POINTS)
def test_upstream_inference_offers_its_entry_points(
    entry_point: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    inference = import_inference()

    assert callable(getattr(inference, entry_point))


@pytest.mark.usefixtures("fresh_import")
def test_import_leaves_the_working_directory_as_it_found_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    import_inference()

    assert Path.cwd() == tmp_path


@pytest.mark.usefixtures("fresh_import")
def test_failed_import_leaves_the_working_directory_as_it_found_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callers_directory = tmp_path / "callers"
    callers_directory.mkdir()
    directory_without_the_package = tmp_path / "empty"
    directory_without_the_package.mkdir()
    monkeypatch.chdir(callers_directory)

    with pytest.raises(FileNotFoundError):
        import_inference(directory_without_the_package)

    assert Path.cwd() == callers_directory


def test_audio_encoder_returns_the_hidden_states_of_every_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import torch  # noqa: PLC0415 - after the module's fixture hid the card
    from transformers import Wav2Vec2Config  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    import_inference()
    from flash_head.audio_analysis.wav2vec2 import (  # noqa: PLC0415
        Wav2Vec2Model,
    )

    layers = 2
    hidden_size = 16
    sequence_length = 10
    encoder = Wav2Vec2Model(
        Wav2Vec2Config(
            hidden_size=hidden_size,
            num_hidden_layers=layers,
            num_attention_heads=2,
            intermediate_size=32,
            conv_dim=(8, 8),
            conv_stride=(5, 2),
            conv_kernel=(10, 3),
            num_feat_extract_layers=2,
            num_conv_pos_embeddings=4,
            num_conv_pos_embedding_groups=2,
        ),
    ).eval()

    with torch.no_grad():
        output = encoder(
            torch.zeros(1, 4000),
            seq_len=sequence_length,
            output_hidden_states=True,
        )

    assert output.hidden_states is not None
    assert len(output.hidden_states) == layers + 1
    assert output.hidden_states[-1].shape == (1, sequence_length, hidden_size)
