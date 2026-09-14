from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch

ADAPTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER_DIR))

from src.tts.backends.Vox.tts_config import VoxPreset
from src.tts.backends.Vox.tts_model import TTSModel
from src.tts.backends.Vox.vox_api_server import _apply_inference_seed
from src.utils.text_cleaner import clean_text_for_tts


class FixedVoiceTests(unittest.TestCase):
    def _model(self, *, default_seed: int = -1) -> TTSModel:
        model = TTSModel.__new__(TTSModel)
        model._config_dir = ADAPTER_DIR / "configs"
        model.config = SimpleNamespace(
            vox=SimpleNamespace(
                cfg_value=3.0,
                inference_timesteps=10,
                normalize=False,
                seed=default_seed,
                split_method="cut3",
                max_split_length=80,
                segment_gap_ms=100,
            )
        )
        return model

    def test_preset_seed_is_sent_to_voxcpm_server(self):
        preset = VoxPreset(name="fixed", seed=24680)
        params = self._model().build_parameters("你好", preset)
        self.assertEqual(params["seed"], 24680)

    def test_global_seed_is_used_when_preset_is_random(self):
        preset = VoxPreset(name="fixed", seed=-1)
        params = self._model(default_seed=13579).build_parameters("你好", preset)
        self.assertEqual(params["seed"], 13579)

    def test_external_segmentation_can_disable_voxcpm_resplitting(self):
        preset = VoxPreset(name="fixed", seed=24680)
        params = self._model().build_parameters(
            "逗号前已经由桌宠切好，",
            preset,
            split_method="cut0",
        )
        self.assertEqual(params["split_method"], "cut0")

    def test_inference_seed_repeats_random_sources(self):
        _apply_inference_seed(24680)
        first = (random.random(), float(np.random.rand()), float(torch.rand(1)))
        _apply_inference_seed(24680)
        second = (random.random(), float(np.random.rand()), float(torch.rand(1)))
        self.assertEqual(first, second)

    def test_desktop_pet_kaomoji_is_removed_before_tts(self):
        cleaned = clean_text_for_tts("唔…现在速度还算正常啦，可能有点卡(´-ω-`)")
        self.assertEqual(cleaned, "唔…现在速度还算正常啦，可能有点卡")


if __name__ == "__main__":
    unittest.main()
