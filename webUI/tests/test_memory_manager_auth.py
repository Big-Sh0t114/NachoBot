from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import memory_manager  # noqa: E402


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return b'{"ok": true}'


class MemoryManagerAuthTests(unittest.TestCase):
    def test_core_api_request_sends_first_configured_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "bot_config.toml"
            config.write_text(
                '[ncnk_message]\nauth_token = ["first-secret", "second-secret"]\n',
                encoding="utf-8",
            )
            captured = {}

            def open_request(request, timeout):
                captured["authorization"] = request.get_header("Authorization")
                captured["timeout"] = timeout
                return _Response()

            with (
                patch.object(memory_manager, "_BOT_CONFIG_PATH", config),
                patch.object(memory_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"),
                patch.object(memory_manager.urlrequest, "urlopen", side_effect=open_request),
            ):
                result = memory_manager._core_api_request_sync("GET", "/api/memory/stats")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured["authorization"], "Bearer first-secret")
        self.assertEqual(captured["timeout"], 10)

    def test_invalid_config_fails_without_leaking_or_sending_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "bot_config.toml"
            config.write_text('[ncnk_message\nauth_token = ["secret"]\n', encoding="utf-8")
            captured = {}

            def open_request(request, _timeout=None, **_kwargs):
                captured["authorization"] = request.get_header("Authorization")
                return _Response()

            with (
                patch.object(memory_manager, "_BOT_CONFIG_PATH", config),
                patch.object(memory_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"),
                patch.object(memory_manager.urlrequest, "urlopen", side_effect=open_request),
            ):
                result = memory_manager._core_api_request_sync("GET", "/api/memory/stats")

        self.assertEqual(json.dumps(result), '{"ok": true}')
        self.assertIsNone(captured["authorization"])


if __name__ == "__main__":
    unittest.main()
