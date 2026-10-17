import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "universal_deployment_check", Path(__file__).resolve().parents[1] / "check_deployment.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class UniversalDeploymentCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.vad = self.root / "custom-vad.onnx"
        self.speaker = self.root / "custom-speaker.onnx"
        self.settings = SimpleNamespace(vad_model_path=self.vad, speaker_model_path=self.speaker)
        self.loader = Mock(return_value=self.settings)
        self.sherpa = SimpleNamespace(
            VadModelConfig=Mock(side_effect=lambda: SimpleNamespace(silero_vad=SimpleNamespace())),
            VoiceActivityDetector=Mock(), SpeakerEmbeddingExtractorConfig=Mock(),
            SpeakerEmbeddingExtractor=Mock(),
        )
        modules = {
            "universalvc_service.runtime_compat": SimpleNamespace(preload_onnxruntime=Mock()),
            "universalvc_service.service_config": SimpleNamespace(load_service_settings=self.loader),
            "universalvc_service.denoise": SimpleNamespace(DenoiseProcessor=Mock(return_value=SimpleNamespace(enabled=True))),
            "sherpa_onnx": self.sherpa,
        }
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, modules).start()
        self.importer = patch.object(probe.importlib, "import_module", return_value=Mock()).start()

    def test_missing_and_empty_models_are_reported(self):
        self.vad.touch()
        result = probe.check_deployment(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"], ["vad_model_missing", "speaker_model_missing"])
        self.sherpa.VoiceActivityDetector.assert_not_called()

    def test_configured_models_are_loaded_without_downloading(self):
        self.vad.write_bytes(b"model")
        self.speaker.write_bytes(b"model")
        (self.root / "service.toml").write_text("# live config")
        self.assertTrue(probe.check_deployment(self.root)["ok"])
        self.assertEqual(self.loader.call_args.args[0], self.root / "service.toml")
        self.assertEqual(self.sherpa.VoiceActivityDetector.call_args.args[0].silero_vad.model, str(self.vad))
        self.sherpa.SpeakerEmbeddingExtractorConfig.assert_called_once_with(model=str(self.speaker))

    def test_invalid_model_fails_closed_without_echoing_exception(self):
        self.vad.write_bytes(b"bad")
        self.speaker.write_bytes(b"model")
        self.sherpa.VoiceActivityDetector.side_effect = ValueError("private path or token")
        result = probe.check_deployment(self.root)
        self.assertEqual(result["issues"], ["vad_model_invalid"])

    def test_unavailable_optional_denoise_does_not_block_start(self):
        self.vad.write_bytes(b"model")
        self.speaker.write_bytes(b"model")
        def imports(name):
            if name == "pyrnnoise":
                raise ImportError("optional denoise dependency unavailable")
            return Mock()
        self.importer.side_effect = imports
        denoiser = sys.modules["universalvc_service.denoise"].DenoiseProcessor
        denoiser.side_effect = RuntimeError("optional denoise initialization failed")
        self.assertTrue(probe.check_deployment(self.root)["ok"])
        denoiser.assert_not_called()

    def test_invalid_live_config_does_not_fall_back_to_defaults(self):
        self.loader.side_effect = ValueError("private config")
        self.assertEqual(probe.check_deployment(self.root)["issues"], ["service_config_invalid"])

    def test_native_dependency_failure_and_missing_models_aggregate(self):
        self.importer.side_effect = OSError("DLL unavailable")
        self.assertEqual(probe.check_deployment(self.root)["issues"],
                         ["dependencies_unavailable", "vad_model_missing", "speaker_model_missing"])


if __name__ == "__main__":
    unittest.main()
