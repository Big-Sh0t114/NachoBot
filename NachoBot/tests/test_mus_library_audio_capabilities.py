import asyncio
import base64
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


NACHOBOT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = NACHOBOT_ROOT / "plugins" / "mus_library" / "plugin.py"
_STUBBED_MODULES = (
    "src",
    "src.plugin_system",
    "src.plugin_system.apis",
    "src.common",
    "src.common.media_paths",
)
_ORIGINAL_MODULES = {}
plugin: types.ModuleType


def setUpModule():
    """Import the plugin against local stubs without initializing Core services."""
    global plugin
    for name in _STUBBED_MODULES:
        _ORIGINAL_MODULES[name] = sys.modules.get(name)

    src = types.ModuleType("src")
    src.__path__ = [str(NACHOBOT_ROOT / "src")]
    plugin_system = types.ModuleType("src.plugin_system")
    plugin_system.BasePlugin = type("BasePlugin", (), {})
    plugin_system.BaseCommand = type("BaseCommand", (), {})
    plugin_system.register_plugin = lambda cls: cls
    apis = types.ModuleType("src.plugin_system.apis")
    apis.send_api = SimpleNamespace()
    common = types.ModuleType("src.common")
    common.__path__ = [str(NACHOBOT_ROOT / "src" / "common")]
    media_paths = types.ModuleType("src.common.media_paths")
    media_paths.get_shared_media_temp_dir = lambda: Path(tempfile.gettempdir())
    sys.modules.update(
        {
            "src": src,
            "src.plugin_system": plugin_system,
            "src.plugin_system.apis": apis,
            "src.common": common,
            "src.common.media_paths": media_paths,
        }
    )
    spec = importlib.util.spec_from_file_location("mus_library_audio_capability_test_target", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    plugin = module


def tearDownModule():
    for name, original in _ORIGINAL_MODULES.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    sys.modules.pop("mus_library_audio_capability_test_target", None)


class FakeCommand:
    def __init__(self, additional_config):
        self.config = {"prefer_silk": True}
        self.message = SimpleNamespace(
            message_info=SimpleNamespace(additional_config=additional_config),
            chat_stream=SimpleNamespace(stream_id="isolated-audio-test-stream"),
        )
        self.sent_text = []

    def get_config(self, _key, default=None):
        return default

    async def send_text(self, text):
        self.sent_text.append(text)


class MusicLibraryCodecCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def _play_with_metadata(self, additional_config):
        with tempfile.TemporaryDirectory(prefix="mus-library-audio-capability-") as folder:
            root = Path(folder)
            wav = root / "local-song.wav"
            wav.write_bytes(b"fixture WAV bytes")
            command = FakeCommand(additional_config)
            send_api = SimpleNamespace(
                custom_to_stream_receipt=AsyncMock(return_value=SimpleNamespace(delivered=True)),
                local_media_to_stream_receipt=AsyncMock(return_value=SimpleNamespace(delivered=True)),
            )
            with (
                patch.object(plugin, "CORE_DIR", root),
                patch.object(plugin, "send_api", send_api),
                patch.object(plugin, "_silk_cache_path", return_value=wav.with_suffix(".silk.cache")),
                patch.object(plugin, "_get_or_build_silk", new_callable=AsyncMock, return_value=(b"SILK_PAYLOAD", False)) as silk,
            ):
                result = await plugin._play_song(command, {"title": "fixture", "file": wav.name})
            sent = send_api.custom_to_stream_receipt.await_args
            return result, silk, sent

    async def test_declared_wav_capability_skips_preferred_silk(self):
        result, silk, sent = await self._play_with_metadata(
            {"runtime_capabilities": {"voice_payload_formats": ["wav"]}}
        )

        self.assertTrue(result[0])
        silk.assert_not_awaited()
        self.assertEqual(sent.args[0], "voice")
        self.assertEqual(sent.args[1], base64.b64encode(b"fixture WAV bytes").decode("ascii"))

    async def test_missing_capability_keeps_legacy_preferred_silk_behavior(self):
        result, silk, sent = await self._play_with_metadata({})

        self.assertTrue(result[0])
        silk.assert_awaited_once()
        self.assertEqual(sent.args[0], "voice")
        self.assertEqual(sent.args[1], base64.b64encode(b"SILK_PAYLOAD").decode("ascii"))


if __name__ == "__main__":
    unittest.main()
