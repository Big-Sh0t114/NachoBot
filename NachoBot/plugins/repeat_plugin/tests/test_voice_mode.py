import importlib.util
import sys
import unittest
from collections import deque
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


PLUGIN = Path(__file__).resolve().parents[1]
CORE = PLUGIN.parents[1]


def load_capabilities():
    spec = importlib.util.spec_from_file_location(
        "repeat_test_capabilities", CORE / "src/chat/runtime_capabilities.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {spec.name: module}):
        spec.loader.exec_module(module)
    return module


class VoiceModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.active = set()
        manager = SimpleNamespace(has_active_session=lambda stream: stream in self.active)
        system = ModuleType("src.plugin_system")
        system.BasePlugin = type("BasePlugin", (), {})
        system.BaseEventHandler = type("BaseEventHandler", (), {})
        system.register_plugin = lambda cls: cls
        system.ComponentInfo = object
        system.ConfigField = lambda **kwargs: kwargs
        system.EventType = SimpleNamespace(ON_MESSAGE="on_message")
        system.NachoMessages = object
        modules = {
            "src.plugin_system": system,
            "src.common.logger": SimpleNamespace(get_logger=lambda _: Mock()),
            "src.chat.runtime_capabilities": load_capabilities(),
            "src.chat.realtime_ingress": SimpleNamespace(realtime_ingress_manager=manager),
        }
        spec = importlib.util.spec_from_file_location("repeat_under_test", PLUGIN / "plugin.py")
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(self.module)
        self.handler = self.module.RepeatHandler()
        self.handler.chat_history = {}
        self.handler.last_repeated_message = None
        self.handler.get_config = lambda key, default: {
            "repeat.repeat_probability": 1.0,
            "repeat.skip_probability": 0.0,
        }.get(key, default)
        self.handler.send_text = AsyncMock(return_value=True)

    def message(self, stream="test", capabilities=None):
        return SimpleNamespace(
            stream_id=stream,
            raw_message="重复的文字",
            message_base_info={"group_id": "group"},
            additional_data={"runtime_capabilities": capabilities or {}},
            is_self=False,
        )

    async def repeat_three(self, message):
        with patch.object(self.module.random, "random", return_value=0.5):
            for _ in range(3):
                await self.handler.execute(message)

    async def test_ordinary_text_still_repeats(self):
        await self.repeat_three(self.message())
        self.handler.send_text.assert_awaited_once_with(stream_id="test", text="重复的文字")

    async def test_voice_and_tts_modes_neither_repeat_nor_seed_history(self):
        for capabilities in ({"voice_stream": True}, {"reply_delivery": "tts_text"}):
            with self.subTest(capabilities=capabilities):
                self.handler.chat_history["test"] = deque(["重复的文字"] * 2, maxlen=3)
                await self.repeat_three(self.message(capabilities=capabilities))
                self.assertNotIn("test", self.handler.chat_history)
        self.handler.send_text.assert_not_awaited()

    async def test_text_in_active_session_is_suppressed_without_affecting_other_chat(self):
        self.active.add("test")
        await self.repeat_three(self.message())
        self.handler.send_text.assert_not_awaited()
        await self.repeat_three(self.message(stream="other"))
        self.handler.send_text.assert_awaited_once_with(stream_id="other", text="重复的文字")

    async def test_call_history_does_not_trigger_repeat_after_session_ends(self):
        self.handler.chat_history["test"] = deque(["重复的文字"] * 2, maxlen=3)
        self.active.add("test")
        await self.handler.execute(self.message())
        self.active.clear()
        await self.handler.execute(self.message())
        self.handler.send_text.assert_not_awaited()
        with patch.object(self.module.random, "random", return_value=0.5):
            await self.handler.execute(self.message())
            await self.handler.execute(self.message())
        self.handler.send_text.assert_awaited_once()

    async def test_session_is_rechecked_before_send(self):
        self.handler.chat_history["test"] = deque(["重复的文字"] * 2, maxlen=3)

        def activate():
            self.active.add("test")
            return 0.5

        with patch.object(self.module.random, "random", side_effect=activate):
            await self.handler.execute(self.message())
        self.handler.send_text.assert_not_awaited()
        self.assertNotIn("test", self.handler.chat_history)


if __name__ == "__main__":
    unittest.main()
