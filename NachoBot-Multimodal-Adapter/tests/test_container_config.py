import os
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nachobot_multimodal.config import Config  # noqa: E402


class ContainerConfigTests(unittest.TestCase):
    def test_compose_profiles_match_runtime_topology(self) -> None:
        compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn('profiles: ["full", "lite"]', compose)
        self.assertIn('profiles: ["full"]', compose)
        self.assertNotIn('profiles: ["potato"]', compose)
        self.assertNotIn("multimodal-potato", compose)
        self.assertNotIn("--no-local-models", compose)
        self.assertNotIn('NACHOBOT_MULTIMODAL_PORT', compose)
        self.assertIn('multimodal-tts-runtime', compose)
        self.assertNotIn('multimodal-tts-emotion', compose)
        self.assertIn('"9880"', compose)
        self.assertIn("/api/health", compose)
        self.assertRegex(compose, r"payload\.get\('ready'\)\s+is\s+True")
        self.assertRegex(compose, r"payload\.get\('model_loaded'\)\s+is\s+True")
        self.assertIn('PORT=9874', compose)
        self.assertIn("scripts/container_tts_entrypoint.py", compose)
        self.assertNotIn("NACHOBOT_CORE_TOKEN", compose)
        self.assertNotIn("NACHOBOT_MULTIMODAL_CORE_URL", compose)

    def test_container_engine_selection_follows_live_base_config(self) -> None:
        script = PROJECT_ROOT / "scripts" / "container_tts_entrypoint.py"
        spec = importlib.util.spec_from_file_location("container_tts_entrypoint", script)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "base.toml"
            config.write_text('[enabled_tts]\nenabled = ["Vox"]\n', encoding="utf-8")

            self.assertEqual(module.resolve_engine(config), "voxcpm")
            self.assertEqual(module.resolve_engine(config, "gpt-sovits"), "gpt-sovits")

    def test_container_overrides_bind_without_relay_config_coupling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "base.toml"
            config_path.write_text(
                """
[server]
host = "127.0.0.1"
port = 9880
[enabled_tts]
enabled = ["Vox"]
[tts_base_config]
stream_mode = false
post_process = false
[debug]
logging_level = "INFO"
[routes]
qq = "ws://127.0.0.1:8000/ws"
discord = "ws://127.0.0.1:8000/ws"
[probability]
voice_probability = 1.0
""".strip(),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "NACHOBOT_MULTIMODAL_HOST": "0.0.0.0",
                    "NACHOBOT_MULTIMODAL_PORT": "9880",
                    "NACHOBOT_MULTIMODAL_CORE_URL": "not-a-websocket-url",
                },
            ):
                config = Config(str(config_path))

            self.assertEqual(config.server.host, "0.0.0.0")
            self.assertEqual(config.server.port, 9880)
            self.assertEqual(config.enabled_plugin.enabled, ["Vox"])
            self.assertFalse(config.tts_base_config.stream_mode)
            self.assertFalse(hasattr(config, "routes"))
            self.assertFalse(hasattr(config, "probability"))
            self.assertEqual(
                config.config_data["routes"],
                {
                    "qq": "ws://127.0.0.1:8000/ws",
                    "discord": "ws://127.0.0.1:8000/ws",
                },
            )
            self.assertEqual(config.config_data["probability"]["voice_probability"], 1.0)


if __name__ == "__main__":
    unittest.main()
