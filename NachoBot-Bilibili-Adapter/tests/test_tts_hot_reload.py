import asyncio
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "NachoBot-Multimodal-Adapter") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "NachoBot-Multimodal-Adapter"))

from bili_src.audio.tts_manager import TTSManager


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

    def model_context(self):
        runtime = self

        class Context:
            async def __aenter__(self):
                runtime.entries += 1
                return runtime.model

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return Context()


class BiliTTSHotReloadTests(unittest.TestCase):
    def test_idle_and_buffered_segments_share_refresh_boundary(self):
        async def scenario():
            first = _Model("vox")
            second = _Model("gpt")
            runtime = _Runtime(first)
            manager = TTSManager.__new__(TTSManager)
            manager._tts_runtime = runtime
            manager.tts_model = first

            idle_audio = await manager._synthesize_tts_segment(
                "idle", platform="bilibili", preset_name=None, split_method="cut0"
            )
            runtime.model = second
            buffered_audio = await manager._synthesize_tts_segment(
                "buffered", platform="bilibili", preset_name=None, split_method="cut0"
            )

            self.assertEqual(idle_audio, b"vox")
            self.assertEqual(buffered_audio, b"gpt")
            self.assertEqual(runtime.entries, 2)
            self.assertIs(manager.tts_model, second)
            self.assertEqual(first.calls[0]["platform"], "bilibili")
            self.assertEqual(second.calls[0]["platform"], "bilibili")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
