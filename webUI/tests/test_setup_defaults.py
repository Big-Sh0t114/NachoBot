from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import webUI.setup_deployment as setup_deployment


def _write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


BOT_TEMPLATE = """
[bot]
qq_account = "000001"
nickname = "Template Bot"
"""

BOT_LIVE = """
[bot]
qq_account = "000007"
nickname = "Live Bot"
"""

MODEL_TEMPLATE = """
[[api_providers]]
name = "TemplateProvider"
base_url = "https://template.invalid/v1"
api_key = "template-key"

[[models]]
model_identifier = "template-0001"
name = "Template Model"
api_provider = "TemplateProvider"

[model_task_config.replyer0]
model_list = ["template-0001"]
"""

MODEL_LIVE = """
[[api_providers]]
name = "LiveFirst"
base_url = "https://live-first.invalid/v1"
api_key = "live-first-key"

[[api_providers]]
name = "LiveSecond"
base_url = "https://live-second.invalid/v1"
api_key = "live-second-key"

[[models]]
model_identifier = "live-0009"
name = "Live Model"
api_provider = "LiveSecond"

[[models]]
model_identifier = "live-0010"
name = "Live Model Two"
api_provider = "LiveFirst"

[model_task_config.replyer0]
model_list = ["live-0009", "live-0010"]
"""

ENV_TEMPLATE = "HOST=127.0.0.1\nPORT=08000\nqq_adapter=napcat\n"
ENV_LIVE = "HOST=0.0.0.0\nPORT=09000\nqq_adapter=snowluma\n"

TTS_TEMPLATE = """
[enabled_tts]
enabled = ["GPT_Sovits"]
"""

TTS_LIVE = """
[enabled_tts]
enabled = ["Vox"]
"""

UVC_TEMPLATE = """
[capture]
target_process_name = "TemplateGame.exe"
[output]
device_name = "Template Device"
[denoise]
enabled = false
[speaker]
enabled = true
"""

UVC_LIVE = """
[capture]
target_process_name = "LiveGame.exe"
[output]
device_name = "Live Device"
[denoise]
enabled = true
[speaker]
enabled = false
"""


class ConfigInitializerDefaultsTests(unittest.TestCase):
    def _write_templates(self, root: Path) -> None:
        _write(root, "NachoBot/template/bot_config_template.toml", BOT_TEMPLATE)
        _write(root, "NachoBot/template/model_config_template.toml", MODEL_TEMPLATE)
        _write(root, "NachoBot/template/template.env", ENV_TEMPLATE)
        _write(
            root,
            "NachoBot-Multimodal-Adapter/template_configs/base_template.toml",
            TTS_TEMPLATE,
        )
        _write(
            root,
            "NachoBot-UniversalVC-Adapter/template/config_template.toml",
            UVC_TEMPLATE,
        )

    def _write_live_configs(self, root: Path) -> None:
        _write(root, "NachoBot/config/bot_config.toml", BOT_LIVE)
        _write(root, "NachoBot/config/model_config.toml", MODEL_LIVE)
        _write(root, "NachoBot/.env", ENV_LIVE)
        _write(
            root,
            "NachoBot-Multimodal-Adapter/configs/base.toml",
            TTS_LIVE,
        )
        _write(root, "NachoBot-UniversalVC-Adapter/config.toml", UVC_LIVE)

    def test_live_config_values_override_templates_and_secrets_stay_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_templates(root)
            self._write_live_configs(root)
            _write(
                root,
                "NachoBot-DiscordVC-Adapter/config.toml",
                '[discord]\ntoken = "live-discord-token"\n',
            )
            _write(
                root,
                "NachoBot-Bilibili-Adapter/config.toml",
                '[bilibili]\nbot_account = "000123"\nsessdata = "qr-secret"\n',
            )

            with mock.patch.object(setup_deployment, "ROOT_DIR", root):
                defaults = setup_deployment.ConfigInitializer.get_defaults()

        self.assertEqual(defaults["core"], {"qq_account": "000007", "nickname": "Live Bot"})
        self.assertEqual(
            defaults["providers"],
            [
                {
                    "name": "LiveFirst",
                    "base_url": "https://live-first.invalid/v1",
                    "api_key": "live-first-key",
                },
                {
                    "name": "LiveSecond",
                    "base_url": "https://live-second.invalid/v1",
                    "api_key": "live-second-key",
                },
            ],
        )
        self.assertEqual(
            defaults["models"],
            [
                {
                    "model_identifier": "live-0009",
                    "model_name": "Live Model",
                    "api_provider": "LiveSecond",
                },
                {
                    "model_identifier": "live-0010",
                    "model_name": "Live Model Two",
                    "api_provider": "LiveFirst",
                },
            ],
        )
        self.assertEqual(defaults["model_groups"], {"replyer0": "live-0009, live-0010"})
        self.assertEqual(
            defaults["env"],
            {"host": "0.0.0.0", "port": "09000", "qq_adapter": "snowluma"},
        )
        self.assertEqual(defaults["tts"], {"engine": "Vox"})
        self.assertEqual(
            defaults["universalvc"],
            {
                "target_process_name": "LiveGame.exe",
                "output_device": "Live Device",
                "denoise_enabled": True,
                "speaker_enabled": False,
            },
        )
        self.assertEqual(defaults["discord"]["token"], "")
        self.assertEqual(defaults["bilibili"], {"bot_account": ""})

    def test_missing_live_configs_fall_back_to_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_templates(root)

            with mock.patch.object(setup_deployment, "ROOT_DIR", root):
                defaults = setup_deployment.ConfigInitializer.get_defaults()

        self.assertEqual(defaults["core"], {"qq_account": "000001", "nickname": "Template Bot"})
        self.assertEqual(
            defaults["providers"],
            [
                {
                    "name": "TemplateProvider",
                    "base_url": "https://template.invalid/v1",
                    "api_key": "template-key",
                }
            ],
        )
        self.assertEqual(
            defaults["models"],
            [
                {
                    "model_identifier": "template-0001",
                    "model_name": "Template Model",
                    "api_provider": "TemplateProvider",
                }
            ],
        )
        self.assertEqual(defaults["model_groups"], {"replyer0": "template-0001"})
        self.assertEqual(
            defaults["env"],
            {"host": "127.0.0.1", "port": "08000", "qq_adapter": "napcat"},
        )
        self.assertEqual(defaults["tts"], {"engine": "GPT_Sovits"})
        self.assertEqual(
            defaults["universalvc"],
            {
                "target_process_name": "TemplateGame.exe",
                "output_device": "Template Device",
                "denoise_enabled": False,
                "speaker_enabled": True,
            },
        )

    def test_invalid_live_selector_keeps_template_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_templates(root)
            _write(root, "NachoBot/.env", "HOST=127.0.0.2\nPORT=09001\nqq_adapter=invalid\n")

            with mock.patch.object(setup_deployment, "ROOT_DIR", root):
                defaults = setup_deployment.ConfigInitializer.get_defaults()

        self.assertEqual(defaults["env"]["host"], "127.0.0.2")
        self.assertEqual(defaults["env"]["port"], "09001")
        self.assertEqual(defaults["env"]["qq_adapter"], "napcat")

    def test_invalid_live_model_schema_fails_closed_without_partial_providers(self) -> None:
        malformed_model = """
[[api_providers]]
name = "LiveFirst"
base_url = "https://live-first.invalid/v1"
api_key = "partial-key"

[[api_providers]]
name = ["schema-invalid"]
base_url = "https://live-second.invalid/v1"
api_key = "live-second-key"

[[models]]
model_identifier = "live-0009"
name = "Live Model"
api_provider = "LiveFirst"
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_templates(root)
            _write(root, "NachoBot/config/model_config.toml", malformed_model)

            with mock.patch.object(setup_deployment, "ROOT_DIR", root):
                with self.assertRaisesRegex(
                    ValueError,
                    r"^Invalid live model_config configuration$",
                ) as context:
                    setup_deployment.ConfigInitializer.get_defaults()

        self.assertNotIn("partial-key", str(context.exception))

    def test_malformed_live_universalvc_fails_closed(self) -> None:
        malformed_universalvc = """
[capture]
target_process_name = "LiveGame.exe"
[output]
device_name = "Live Device"
[denoise]
enabled = "not-a-boolean"
[speaker]
enabled = false
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_templates(root)
            _write(root, "NachoBot-UniversalVC-Adapter/config.toml", malformed_universalvc)

            with mock.patch.object(setup_deployment, "ROOT_DIR", root):
                with self.assertRaisesRegex(
                    ValueError,
                    r"^Invalid live UniversalVC configuration$",
                ):
                    setup_deployment.ConfigInitializer.get_defaults()

    def test_syntactically_malformed_live_toml_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_templates(root)
            _write(root, "NachoBot/config/bot_config.toml", "[bot\nqq_account = \"hidden\"\n")

            with mock.patch.object(setup_deployment, "ROOT_DIR", root):
                with self.assertRaisesRegex(
                    ValueError,
                    r"^Invalid live bot_config configuration$",
                ):
                    setup_deployment.ConfigInitializer.get_defaults()


if __name__ == "__main__":
    unittest.main()
