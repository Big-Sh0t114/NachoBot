import os
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
    def test_container_overrides_bind_and_core_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "base.toml"
            config_path.write_text(
                """
[server]
host = "127.0.0.1"
port = 8070
[routes]
qq = "ws://127.0.0.1:8000/ws"
discord = "ws://127.0.0.1:8000/ws"
[probability]
voice_probability = 1.0
[enabled_tts]
enabled = []
[tts_base_config]
stream_mode = false
post_process = false
[debug]
logging_level = "INFO"
""".strip(),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "NACHOBOT_MULTIMODAL_HOST": "0.0.0.0",
                    "NACHOBOT_MULTIMODAL_PORT": "8070",
                    "NACHOBOT_MULTIMODAL_CORE_URL": "ws://core:8000/ws",
                },
            ):
                config = Config(str(config_path))

            self.assertEqual(config.server.host, "0.0.0.0")
            self.assertEqual(config.server.port, 8070)
            self.assertEqual(
                config.routes,
                {"qq": "ws://core:8000/ws", "discord": "ws://core:8000/ws"},
            )


if __name__ == "__main__":
    unittest.main()
