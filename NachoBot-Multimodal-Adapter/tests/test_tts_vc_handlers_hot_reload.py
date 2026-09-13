import asyncio
import importlib.util
import logging
from pathlib import Path
import sys
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _Model:
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def tts(self, **kwargs):
        self.calls.append(kwargs)
        return self.name.encode("ascii")


class _Runtime:
    def __init__(self, model):
        self.model = model
        self.entries = 0
        self.error = None

    @property
    def ready(self):
        return self.model is not None

    def ensure_tts_model(self):
        return self.model is not None

    def model_context(self):
        runtime = self

        class Context:
            async def __aenter__(self):
                runtime.entries += 1
                return runtime.model

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return Context()


def _load_handler(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.TTSHandler


class VcHandlerHotReloadTests(unittest.TestCase):
    def test_discord_and_universal_refresh_their_model_each_request(self):
        async def scenario():
            for adapter, module_name in (
                ("NachoBot-DiscordVC-Adapter", "discord_hot_reload_handler"),
                ("NachoBot-UniversalVC-Adapter", "universal_hot_reload_handler"),
            ):
                handler_cls = _load_handler(ROOT / adapter / "tts_handler.py", module_name)
                first = _Model("first")
                second = _Model("second")
                runtime = _Runtime(first)
                with mock.patch(
                    "nachobot_multimodal.utils.tts_runtime.TTSRuntime",
                    return_value=runtime,
                ):
                    handler = handler_cls(logging.getLogger(module_name))
                    first_path = await handler.generate_speech("one", preset_name="p1", split_method="cut0")
                    runtime.model = second
                    second_path = await handler.generate_speech("two", preset_name="p2", split_method="cut1")

                self.assertTrue(first_path)
                self.assertTrue(second_path)
                self.assertEqual(runtime.entries, 2)
                self.assertIs(handler.tts_model, second)
                self.assertEqual(first.calls[0]["platform"], "discord" if "Discord" in adapter else "universal_vc")
                self.assertEqual(second.calls[0]["platform"], "discord" if "Discord" in adapter else "universal_vc")
                self.assertEqual(first.calls[0]["text_lang"], "zh")
                self.assertEqual(first.calls[0]["prompt_lang"], "ja")
                Path(first_path).unlink(missing_ok=True)
                Path(second_path).unlink(missing_ok=True)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
