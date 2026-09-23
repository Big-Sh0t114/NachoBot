import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import load_config


class DiscordCoreRoutingTests(unittest.TestCase):
    def _write_config(self, root: Path, *, host: str = "localhost", port: int = 8000) -> Path:
        path = root / "config.toml"
        path.write_text(
            f'[discord]\ntoken = "test"\n[nachobot]\nhost = "{host}"\nport = {port}\n',
            encoding="utf-8",
        )
        return path

    def test_core_port_is_read_directly_without_rewriting_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write_config(Path(temp_dir))
            original = path.read_text(encoding="utf-8")

            config = load_config(path)

            self.assertEqual(config.nachobot.port, 8000)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_container_core_endpoint_overrides_loopback_in_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"NACHOBOT_CORE_HOST": "core", "NACHOBOT_CORE_PORT": "8000"},
        ):
            config = load_config(self._write_config(Path(temp_dir)))

            self.assertEqual((config.nachobot.host, config.nachobot.port), ("core", 8000))


if __name__ == "__main__":
    unittest.main()
