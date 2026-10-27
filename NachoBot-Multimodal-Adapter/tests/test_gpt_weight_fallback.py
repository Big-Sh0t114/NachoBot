from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nachobot_multimodal.tts.backends.GPT_Sovits import tts_model  # noqa: E402
from nachobot_multimodal.utils.gpt_weights import FALLBACK_GPT, FALLBACK_SOVITS  # noqa: E402
from scripts import tts_runtime_manager as manager  # noqa: E402


class GPTWeightFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "configs" / "gpt-sovits.toml"
        self.config.parent.mkdir()
        text = (PROJECT_ROOT / "template_configs" / "gpt-sovits_template.toml").read_text(encoding="utf-8")
        self.config.write_text(text.replace('prompt_text = ""', 'prompt_text = "test"'), encoding="utf-8")
        self.source = self.root / ".runtime" / "tts" / "gpt-sovits" / "source"
        self.pretrained = self.source / "GPT_SoVITS" / "pretrained_models"
        self.base_pair = [self.pretrained / name for name in (FALLBACK_GPT, FALLBACK_SOVITS)]
        for path in self.base_pair:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"01")
        self.model_file = self.root / "src" / "tts" / "backends" / "GPT_Sovits" / "tts_model.py"
        patcher = patch.object(tts_model, "__file__", str(self.model_file))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.requests = patch.object(tts_model.requests, "get")
        self.http = self.requests.start()
        self.http.return_value.status_code = 200
        self.addCleanup(self.requests.stop)

    def make_model(self):
        return tts_model.TTSModel(self.config, engine_host="127.0.0.1", engine_port=9881)

    def sent_weights(self):
        return [call.kwargs["params"]["weights_path"] for call in self.http.call_args_list]

    def custom_pair(self):
        paths = [self.root / "models" / "tts" / "gpt-sovits" / name for name in ("ncnk.ckpt", "ncnk.pth")]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"01")
        return paths

    def test_missing_custom_weights_initialize_with_default_pair_without_editing_config(self):
        original = self.config.read_bytes()
        model = self.make_model()
        self.assertTrue(model._initialized)
        self.assertEqual(self.sent_weights(), [str(path.resolve()) for path in self.base_pair])
        self.assertEqual(self.config.read_bytes(), original)

    def test_partial_custom_pair_never_mixes_generations(self):
        paths = self.custom_pair()
        paths[1].unlink()
        self.make_model()
        self.assertEqual(self.sent_weights(), [str(path.resolve()) for path in self.base_pair])

    def test_complete_custom_pair_still_takes_precedence(self):
        paths = self.custom_pair()
        self.make_model()
        self.assertEqual(self.sent_weights(), [str(path.resolve()) for path in paths])

    def test_switching_from_custom_preset_back_to_missing_weights_restores_base_pair(self):
        self.custom_pair()
        model = self.make_model()
        model.config.tts.models.presets["missing"] = replace(
            model.get_preset("default"), name="missing", gpt_model="missing.ckpt", sovits_model="missing.pth",
        )
        model.load_preset("missing")
        self.assertEqual(self.sent_weights()[-2:], [str(path.resolve()) for path in self.base_pair])

    def test_missing_base_pair_fails_before_any_weight_change(self):
        self.base_pair[1].unlink()
        with self.assertRaisesRegex(FileNotFoundError, "默认底模也缺失"):
            self.make_model()
        self.http.assert_not_called()

    def test_remote_api_paths_keep_their_original_semantics(self):
        with patch.dict("os.environ", {"NACHOBOT_TTS_ENGINE_HOST": ""}):
            tts_model.TTSModel(self.config)
        self.assertTrue(self.sent_weights()[0].endswith("ncnk.ckpt"))

    def test_raw_engine_and_public_client_use_the_same_fallback_pair(self):
        runtime = self.source.parent
        with patch.object(manager, "detect_sovits_version", return_value="v2"):
            inference_config = manager.make_gpt_infer_config(self.source, runtime, self.config)
        self.make_model()
        content = inference_config.read_text(encoding="utf-8")
        for sent in self.sent_weights():
            self.assertIn(Path(sent).as_posix(), content)
        self.assertNotIn("ncnk", content)


if __name__ == "__main__":
    unittest.main()
