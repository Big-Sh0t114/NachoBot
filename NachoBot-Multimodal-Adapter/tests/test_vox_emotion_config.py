import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.tts.backends.Vox.tts_config import VoxBaseConfigData  # noqa: E402


class VoxEmotionConfigTests(unittest.TestCase):
    def test_fixed_emotion_label_map_is_loaded(self) -> None:
        config = VoxBaseConfigData.from_dict(
            {
                "tts": {
                    "host": "127.0.0.1",
                    "port": 9880,
                    "model_dir": "",
                    "models": {
                        "presets": {
                            "default": {"name": "default"},
                            "happy": {"name": "happy"},
                            "sad": {"name": "sad"},
                            "disgust": {"name": "disgust"},
                            "caring": {"name": "caring"},
                        }
                    },
                },
                "pipeline": {
                    "default_preset": "default",
                    "platform_presets": {},
                },
                "emotion": {
                    "enabled": True,
                    "classifier_model": "tabularisai/multilingual-emotion-classification",
                    "confidence_threshold": 0.6,
                    "default_emotion": "default",
                    "label_preset_map": {
                        "anger": "disgust",
                        "contempt": "disgust",
                        "disgust": "disgust",
                        "fear": "sad",
                        "frustration": "disgust",
                        "gratitude": "caring",
                        "joy": "happy",
                        "love": "caring",
                        "neutral": "default",
                        "sadness": "sad",
                        "surprise": "happy",
                    },
                },
            }
        )

        self.assertEqual(
            config.emotion.classifier_model,
            "tabularisai/multilingual-emotion-classification",
        )
        self.assertEqual(config.emotion.confidence_threshold, 0.6)
        self.assertEqual(config.emotion.default_emotion, "default")
        self.assertEqual(config.emotion.label_preset_map["joy"], "happy")
        self.assertEqual(config.emotion.label_preset_map["sadness"], "sad")
        self.assertEqual(config.emotion.label_preset_map["neutral"], "default")

    def test_tabularisai_model_is_default(self) -> None:
        config = VoxBaseConfigData.from_dict(
            {
                "tts": {
                    "host": "127.0.0.1",
                    "port": 9880,
                    "model_dir": "",
                    "models": {"presets": {"default": {"name": "default"}}},
                },
                "pipeline": {
                    "default_preset": "default",
                    "platform_presets": {},
                },
                "emotion": {},
            }
        )

        self.assertEqual(
            config.emotion.classifier_model,
            "tabularisai/multilingual-emotion-classification",
        )
        self.assertEqual(config.emotion.label_preset_map, {})


if __name__ == "__main__":
    unittest.main()
