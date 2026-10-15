import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nachobot_multimodal.utils import emotion_classifier as classifier
from transformers import modeling_utils


class ClassifierLoadingTests(unittest.TestCase):
    def test_windows_cpu_hook_keeps_loader_options_and_restores_after_failure(self):
        for fails in (False, True):
            original = mock.Mock(return_value="handle")
            def load(source, **kwargs):
                self.assertEqual(source, "chosen-model")
                self.assertEqual(kwargs, {"local_files_only": True})
                self.assertEqual(modeling_utils.safe_open("weights", framework="pt", device="cpu", backend="mmap"), "handle")
                if fails:
                    raise RuntimeError("load failed")
                return "model"
            with self.subTest(fails=fails), mock.patch.object(classifier.os, "name", "nt"), mock.patch.object(modeling_utils, "safe_open", original), mock.patch.object(classifier.AutoModelForSequenceClassification, "from_pretrained", side_effect=load):
                if fails:
                    with self.assertRaisesRegex(RuntimeError, "load failed"):
                        classifier._load_classifier_model("chosen-model", device="cpu", local_files_only=True)
                else:
                    self.assertEqual(classifier._load_classifier_model("chosen-model", device="cpu", local_files_only=True), "model")
                self.assertIs(modeling_utils.safe_open, original)
                original.assert_called_once_with("weights", framework="pt", device="cpu", backend="pread")

    def test_cuda_and_non_windows_keep_existing_loader(self):
        for platform, device in (("nt", "cuda:0"), ("posix", "cpu")):
            with self.subTest(platform=platform, device=device), mock.patch.object(classifier.os, "name", platform), mock.patch.object(classifier.AutoModelForSequenceClassification, "from_pretrained", return_value="model") as load:
                original = modeling_utils.safe_open
                self.assertEqual(classifier._load_classifier_model("chosen-model", device=device, dtype="chosen-dtype"), "model")
                load.assert_called_once_with("chosen-model", dtype="chosen-dtype")
                self.assertIs(modeling_utils.safe_open, original)


if __name__ == "__main__":
    unittest.main()
