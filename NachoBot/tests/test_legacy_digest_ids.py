from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _package(name: str, path: Path | None = None) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = [str(path)] if path is not None else []
    return module


def _load_source(module_name: str, path: Path, stubs: dict[str, types.ModuleType]):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load source module: {path}")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**stubs, module_name: module}):
        spec.loader.exec_module(module)
    return module


_qq_identity = _load_source(
    "_legacy_qq_history_identity_test_source",
    PROJECT_ROOT / "ncnk_message" / "qq_history_identity.py",
    {},
)
_memorix_hash = _load_source(
    "_legacy_memorix_hash_test_source",
    PROJECT_ROOT / "src" / "A_memorix" / "core" / "utils" / "hash.py",
    {},
)


def _chat_stream_stubs() -> dict[str, types.ModuleType]:
    src = _package("src", PROJECT_ROOT / "src")
    common = _package("src.common", PROJECT_ROOT / "src" / "common")
    database = _package("src.common.database", PROJECT_ROOT / "src" / "common" / "database")
    logger = types.ModuleType("src.common.logger")
    logger.get_logger = logging.getLogger
    database_module = types.ModuleType("src.common.database.database")
    database_module.db = SimpleNamespace(connect=lambda **_kwargs: None, create_tables=lambda *_args, **_kwargs: None)
    model_module = types.ModuleType("src.common.database.database_model")
    model_module.ChatStreams = type("ChatStreams", (), {})
    ncnk_message = _package("ncnk_message")
    ncnk_message.UserInfo = type("UserInfo", (), {})
    ncnk_message.GroupInfo = type("GroupInfo", (), {})
    rich = _package("rich")
    rich_traceback = types.ModuleType("rich.traceback")
    rich_traceback.install = lambda **_kwargs: None
    return {
        "src": src,
        "src.common": common,
        "src.common.logger": logger,
        "src.common.database": database,
        "src.common.database.database": database_module,
        "src.common.database.database_model": model_module,
        "ncnk_message": ncnk_message,
        "rich": rich,
        "rich.traceback": rich_traceback,
    }


_chat_stream = _load_source(
    "_legacy_chat_stream_test_source",
    PROJECT_ROOT / "src" / "chat" / "message_receive" / "chat_stream.py",
    _chat_stream_stubs(),
)


class _Field:
    def __eq__(self, other):
        return ("person_id", other)


class _PersonInfoModel:
    person_id = _Field()

    @classmethod
    def get_or_none(cls, *_args):
        return None


def _messenger_stubs() -> dict[str, types.ModuleType]:
    stubs = _chat_stream_stubs()
    stubs["src.common.database.database_model"].PersonInfo = _PersonInfoModel

    person_info_package = _package("src.person_info", PROJECT_ROOT / "src" / "person_info")
    person_info = types.ModuleType("src.person_info.person_info")
    person_info.person_info_manager = None
    person_info.calculate_string_similarity = lambda *_args: 0.0
    person_info.Person = type("Person", (), {})

    chat = _package("src.chat", PROJECT_ROOT / "src" / "chat")
    messages = _package("src.chat.message_receive", PROJECT_ROOT / "src" / "chat" / "message_receive")
    chat_stream = types.ModuleType("src.chat.message_receive.chat_stream")
    chat_stream.get_chat_manager = lambda: None
    advanced = _package("src.chat.advanced", PROJECT_ROOT / "src" / "chat" / "advanced")
    advanced_manager = types.ModuleType("src.chat.advanced.advanced_manager")
    advanced_manager.advanced_manager = None

    plugin_system = _package("src.plugin_system", PROJECT_ROOT / "src" / "plugin_system")
    apis = types.ModuleType("src.plugin_system.apis")
    apis.send_api = SimpleNamespace()
    apis.message_api = SimpleNamespace()
    base = _package("src.plugin_system.base", PROJECT_ROOT / "src" / "plugin_system" / "base")
    base_action = types.ModuleType("src.plugin_system.base.base_action")
    base_action.BaseAction = type("BaseAction", (), {})
    base_action.ActionActivationType = SimpleNamespace(ALWAYS="always")
    base_command = types.ModuleType("src.plugin_system.base.base_command")
    base_command.BaseCommand = type("BaseCommand", (), {})

    stubs.update(
        {
            "src.person_info": person_info_package,
            "src.person_info.person_info": person_info,
            "src.chat": chat,
            "src.chat.message_receive": messages,
            "src.chat.message_receive.chat_stream": chat_stream,
            "src.chat.advanced": advanced,
            "src.chat.advanced.advanced_manager": advanced_manager,
            "src.plugin_system": plugin_system,
            "src.plugin_system.apis": apis,
            "src.plugin_system.base": base,
            "src.plugin_system.base.base_action": base_action,
            "src.plugin_system.base.base_command": base_command,
        }
    )
    return stubs


_messenger = _load_source(
    "_legacy_messenger_test_source",
    PROJECT_ROOT / "plugins" / "messenger" / "messenger.py",
    _messenger_stubs(),
)


class LegacyDigestIdTests(unittest.TestCase):
    def test_chat_stream_private_and_group_ids_remain_stable(self):
        self.assertEqual(
            _chat_stream.ChatManager._generate_stream_id("qq", SimpleNamespace(user_id="123456")),
            "c3274d16e5efdbc47295676061f27666",
        )
        self.assertEqual(
            _chat_stream.ChatManager.get_stream_id(None, "qq", "123456", is_group=False),
            "c3274d16e5efdbc47295676061f27666",
        )
        self.assertEqual(
            _chat_stream.ChatManager._generate_stream_id(
                "qq",
                None,
                group_info=SimpleNamespace(group_id="9988"),
            ),
            "b43565c2c8603fac04d879e03a4356a5",
        )

    def test_messenger_private_stream_lookup_keeps_existing_id(self):
        record = SimpleNamespace(platform="qq", user_id="123456")
        manager = SimpleNamespace(get_stream=lambda stream_id: stream_id)
        relay = object.__new__(_messenger.MessengerRelayAction)
        with patch.object(_messenger.PersonInfoModel, "get_or_none", return_value=record), patch.object(
            _messenger, "get_chat_manager", return_value=manager
        ):
            self.assertEqual(
                relay._find_private_stream_id("person-1"),
                "c3274d16e5efdbc47295676061f27666",
            )

    def test_convey_targets_the_existing_qq_private_stream_id(self):
        async def scenario():
            relay = object.__new__(_messenger.ConveyCommand)
            relay.matched_groups = {"convey": "#convey_123456 hello"}
            relay.message = None
            looked_up = []

            def get_stream(stream_id):
                looked_up.append(stream_id)
                return SimpleNamespace(user_info=SimpleNamespace(user_nickname="QQ user"))

            manager = SimpleNamespace(get_stream=get_stream)
            with patch.object(
                _messenger,
                "advanced_manager",
                SimpleNamespace(is_allowed=lambda _user_id: True),
            ), patch.object(_messenger, "get_chat_manager", return_value=manager), patch.object(
                _messenger.ConveyCommand,
                "_inject_convey_message",
                new_callable=AsyncMock,
            ):
                result = await relay.execute()
            self.assertTrue(result[0])
            self.assertEqual(looked_up, ["c3274d16e5efdbc47295676061f27666"])

        asyncio.run(scenario())

    def test_snowluma_native_message_id_remains_upstream_compatible(self):
        self.assertEqual(
            _qq_identity.snow_message_id_for_native_sequence("123456789", "987654321"),
            "-182637900",
        )

    def test_memory_hash_keeps_legacy_md5_and_secure_default(self):
        self.assertEqual(
            _memorix_hash.compute_hash("remember me", "md5"),
            "59c8d663e5947d42a8be6fba8a8ed510",
        )
        self.assertEqual(
            _memorix_hash.compute_hash("remember me"),
            "5e6c46d290765cd5fe3aeb06de2f9598cc1095e0673007d45670761584d98f6d",
        )


if __name__ == "__main__":
    unittest.main()
