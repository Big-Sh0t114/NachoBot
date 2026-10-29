from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from nachobot_multimodal.tts.backends.GPT_Sovits.tts_model import TTSModel  # noqa: E402


class GPTPromptLanguageTests(unittest.TestCase):
    def setUp(self):
        self.preset = SimpleNamespace(
            text_language="auto", prompt_language="ja", prompt_text="reference",
            aux_ref_audio_paths=[], speed_factor=1.0,
        )
        config = SimpleNamespace(
            models=SimpleNamespace(presets={"test": self.preset}),
            top_k=12, top_p=1.0, temperature=1.0, text_split_method="cut5",
            batch_size=1, batch_threshold=0.75, media_type="wav",
            repetition_penalty=1.35, sample_steps=32, super_sampling=False,
        )
        self.model = object.__new__(TTSModel)
        self.model.config = SimpleNamespace(tts=config)
        self.model._initialized = True
        self.model._config_dir = PROJECT_ROOT
        self.model._ref_audio_path = "reference.wav"
        self.model._prompt_text = "reference"

    def test_output_language_does_not_relabel_reference_audio(self):
        for output in ("zh", "en", "ja", None):
            with self.subTest(output=output):
                params = self.model.build_parameters("test", text_lang=output, preset_name="test")
                self.assertEqual(params["text_lang"], output or "auto")
                self.assertEqual(params["prompt_lang"], "ja")

    def test_explicit_reference_language_override_is_preserved(self):
        params = self.model.build_parameters("test", text_lang="zh", prompt_lang="en", preset_name="test")
        self.assertEqual(params["prompt_lang"], "en")
        self.assertEqual(self.preset.prompt_language, "ja")

    def test_empty_reference_language_uses_reference_default(self):
        self.preset.prompt_language = ""
        params = self.model.build_parameters("test", text_lang="en", preset_name="test")
        self.assertEqual(params["prompt_lang"], "zh")
