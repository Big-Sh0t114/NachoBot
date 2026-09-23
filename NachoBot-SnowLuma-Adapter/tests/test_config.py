from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import src.config as config_module
from src.config import SnowLumaConfig


def test_ws_url_without_token():
    cfg = SnowLumaConfig(host="127.0.0.1", port=3001, token="")
    assert cfg.ws_url() == "ws://127.0.0.1:3001"


def test_ws_url_with_token():
    cfg = SnowLumaConfig(host="127.0.0.1", port=3001, token="a b")
    assert cfg.ws_url() == "ws://127.0.0.1:3001?access_token=a+b"


class CoreEndpointConfigTests(unittest.TestCase):
    def test_default_core_port_is_8000(self) -> None:
        self.assertEqual(config_module.NachoBotConfig().port, 8000)

    def test_core_port_is_read_directly_without_rewriting_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            original = (
                "[snowluma]\nhost = '127.0.0.1'\nport = 3001\n\n"
                "[nachobot_server]\nhost = '127.0.0.1'\nport = 8000\n\n"
                "[chat]\n[voice]\n[send]\n[debug]\n[visual]\n"
            )
            path.write_text(original, encoding="utf-8")
            with patch.object(config_module, "CONFIG_PATH", path):
                loaded = config_module.load_config()
            self.assertEqual(loaded.nachobot.port, 8000)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
