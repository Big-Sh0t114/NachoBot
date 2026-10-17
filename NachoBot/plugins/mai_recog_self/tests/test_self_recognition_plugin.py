"""Isolated lifecycle tests for the native MaiSelf adaptation."""

from __future__ import annotations

import asyncio
import base64
import copy
import importlib.util
import json
import subprocess
import sys
import threading
import time
import types
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


CORE_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_PATH = CORE_ROOT / "plugins" / "mai_recog_self" / "plugin.py"


@dataclass
class Seg:
    type: str
    data: Any


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _EventType(Enum):
    ON_MESSAGE_PRE_PROCESS = "on_message_pre_process"
    ON_MESSAGE = "on_message"


class _ToolParamType(Enum):
    STRING = "string"
    INTEGER = "integer"


class _ConfigField:
    def __init__(self, **values):
        self.values = values


@dataclass
class _ComponentInfo:
    name: str
    description: str = ""
    event_type: Any = None
    intercept_message: bool = False
    weight: int = 0


class _BasePlugin:
    def __init__(self, _plugin_dir):
        self.config = {
            "plugin": {"enabled": True},
            "recognition": {
                "character_name": "",
                "self_character_id": "",
                "worker_python": "",
                "data_dir": "",
                "auto_recognize": True,
                "budget_seconds": 1.0,
            },
        }

    def get_config(self, key, default=None):
        value = self.config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class _BaseEventHandler:
    def __init__(self):
        self.plugin_config = None

    def get_config(self, key, default=None):
        value = self.plugin_config or {}
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    @classmethod
    def get_handler_info(cls):
        return _ComponentInfo(
            cls.handler_name,
            cls.handler_description,
            cls.event_type,
            cls.intercept_message,
            cls.weight,
        )


class _BaseTool:
    @classmethod
    def get_tool_info(cls):
        return _ComponentInfo(cls.name, cls.description)


class _InferenceService:
    def __init__(self, root: Path, worker_python: str = ""):
        self.root = root
        self.worker_python = worker_python
        self.state = {"desired_version": "v1", "loaded_version": "v1", "ready": True}
        self.result = {
            "status": "ok",
            "version": "v1",
            "score": 0.91,
            "judgment": "match",
            "character": "Nacho",
        }
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.closed = False

    def status(self):
        return dict(self.state)

    def infer(self, _raw, _timeout=6):
        self.calls += 1
        self.started.set()
        if self.block:
            self.release.wait(1)
        return dict(self.result)

    def close(self):
        self.closed = True
        self.release.set()


class _FakeMessage:
    def __init__(self, segments, stream_id="chat-a", message_id="msg-a", plain_text=""):
        self.message_segments = segments
        self.stream_id = stream_id
        self.message_base_info = {"message_id": message_id}
        self.additional_data = {}
        self.plain_text = plain_text

    def deepcopy(self):
        return copy.deepcopy(self)

    def modify_message_segments(self, segments, **_kwargs):
        self.message_segments = segments


@contextmanager
def isolated_plugin(*, stub_backend=True):
    """Load plugin.py with small host/backend stubs and restore imports after."""
    names = (
        "ncnk_message",
        "src",
        "src.common",
        "src.common.logger",
        "src.common.media_paths",
        "src.config",
        "src.config.config",
        "src.plugin_system",
        "src.plugin_system.base",
        "src.plugin_system.base.component_types",
        "src.plugin_system.base.tool_execution_context",
        "src.plugin_system.core",
        "src.plugin_system.core.plugin_manager",
        "plugins",
        "plugins.mai_recog_self",
        "plugins.mai_recog_self.backend",
        "plugins.mai_recog_self.backend.paths",
        "plugins.mai_recog_self.backend.runtime",
        "plugins.mai_recog_self.plugin",
    )
    old = {name: sys.modules.get(name) for name in names}

    def module(name, **attrs):
        result = types.ModuleType(name)
        result.__dict__.update(attrs)
        return result

    for name in ("src", "src.common", "src.config", "src.plugin_system", "src.plugin_system.base", "src.plugin_system.core"):
        package = module(name)
        package.__path__ = []
        sys.modules[name] = package

    sys.modules["plugins"] = module("plugins")
    sys.modules["plugins"].__path__ = [str(CORE_ROOT / "plugins")]
    sys.modules["plugins.mai_recog_self"] = module("plugins.mai_recog_self")
    sys.modules["plugins.mai_recog_self"].__path__ = [str(PLUGIN_PATH.parent)]

    def register_plugin(cls):
        return cls

    sys.modules["ncnk_message"] = module("ncnk_message", Seg=Seg)
    sys.modules["src.common.logger"] = module("src.common.logger", get_logger=lambda _name: _Logger())
    sys.modules["src.common.media_paths"] = module("src.common.media_paths", CORE_ROOT=Path("C:/Core"))
    sys.modules["src.config.config"] = module(
        "src.config.config", global_config=SimpleNamespace(bot=SimpleNamespace(nickname="Nacho"))
    )
    sys.modules["src.plugin_system"].__dict__.update(
        BaseEventHandler=_BaseEventHandler,
        BasePlugin=_BasePlugin,
        BaseTool=_BaseTool,
        ConfigField=_ConfigField,
        EventType=_EventType,
        ToolParamType=_ToolParamType,
        register_plugin=register_plugin,
    )
    sys.modules["src.plugin_system.base.component_types"] = module(
        "src.plugin_system.base.component_types", EventHandlerInfo=_ComponentInfo, ToolInfo=_ComponentInfo
    )

    @dataclass(frozen=True)
    class Preparation:
        prompt_supplement: str = ""
        context: Any = None
        cache_sensitive: bool = False

    sys.modules["src.plugin_system.base.tool_execution_context"] = module(
        "src.plugin_system.base.tool_execution_context", ToolExecutionPreparation=Preparation
    )
    plugin_manager = SimpleNamespace(instance=None)
    plugin_manager.get_plugin_instance = lambda _name: plugin_manager.instance
    sys.modules["src.plugin_system.core.plugin_manager"] = module(
        "src.plugin_system.core.plugin_manager", plugin_manager=plugin_manager
    )
    service_holder = SimpleNamespace(service=None)

    class ServiceFactory:
        def __new__(cls, root, worker_python=""):
            service_holder.service = _InferenceService(root, worker_python)
            return service_holder.service

    if stub_backend:
        sys.modules["plugins.mai_recog_self.backend.paths"] = module(
            "plugins.mai_recog_self.backend.paths", DEFAULT_DATA_ROOT=Path("C:/Core/data/plugins/mai_recog_self")
        )
        sys.modules["plugins.mai_recog_self.backend.runtime"] = module(
            "plugins.mai_recog_self.backend.runtime", InferenceService=ServiceFactory
        )

    spec = importlib.util.spec_from_file_location("plugins.mai_recog_self.plugin", PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    try:
        yield loaded, plugin_manager, service_holder
    finally:
        for name, previous in old.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def _png(seed: bytes = b"x") -> bytes:
    return b"\x89PNG\r\n\x1a\n" + seed


def _image(raw: bytes, kind: str = "image") -> Seg:
    return Seg(type=kind, data=base64.b64encode(raw).decode("ascii"))


class SelfRecognitionPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_package_import_resolves_real_relative_backend_without_torch(self):
        code = r"""
import sys
from pathlib import Path
from plugins.mai_recog_self.tests.test_self_recognition_plugin import isolated_plugin

with isolated_plugin(stub_backend=False) as (module, _manager, _service_holder):
    from plugins.mai_recog_self.backend import paths, runtime
    plugin_dir = Path(module.__file__).resolve().parent
    assert module.__name__ == "plugins.mai_recog_self.plugin"
    assert module.__package__ == "plugins.mai_recog_self"
    assert Path(paths.__file__).resolve() == plugin_dir / "backend" / "paths.py"
    assert Path(runtime.__file__).resolve() == plugin_dir / "backend" / "runtime.py"
    assert paths.DEFAULT_DATA_ROOT == paths.CORE_ROOT / "data" / "plugins" / "mai_recog_self"
    assert paths.DEFAULT_RAW_ROOT == paths.CORE_ROOT / "data" / "self_recognition_raw"
    assert "torch" not in sys.modules
print("native-relative-backend torch-free")
"""
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=CORE_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("native-relative-backend torch-free", completed.stdout)

    async def test_registration_config_and_recursive_annotation_preserve_original(self):
        with isolated_plugin() as (module, manager, _service_holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            components = plugin.get_plugin_components()
            self.assertEqual([info.name for info, _ in components], [
                "mai_recog_self_pre_process",
                "mai_recog_self_media_refs",
                "recognize_self_image",
                "recognize_character_image",
            ])
            self.assertTrue(components[0][1].intercept_message)
            self.assertTrue(components[1][1].intercept_message)
            self.assertEqual(plugin._effective_character_name(), "Nacho")
            self.assertEqual(plugin.config["recognition"]["budget_seconds"], 1.0)

            raw1, raw2, raw3 = _png(b"1"), _png(b"2"), _png(b"3")
            untouched_node = {"content": "preserve", "raw_message": "preserve-raw"}
            forward_node = {
                "content": "forward metadata",
                "raw_message": "forward raw text",
                "message_segment": Seg(type="seglist", data=[_image(raw2), Seg(type="text", data="kept")]),
            }
            original_segments = [_image(raw1), _image(raw3, "emoji"), Seg(type="forward", data=[untouched_node, forward_node])]
            message = _FakeMessage(original_segments)
            handler = module.SelfRecognitionPreProcess()
            handler.plugin_config = {"plugin": {"enabled": True}, "recognition": {"auto_recognize": True, "budget_seconds": 2}}

            ok, should_continue, _text, _custom, modified = await handler.execute(message)

            self.assertTrue(ok and should_continue)
            self.assertEqual(message.message_segments, original_segments)
            self.assertIsNot(modified, message)
            self.assertEqual(sum(seg.type == "text" and "看起来像是" in seg.data for seg in modified.message_segments), 2)
            updated_forward = modified.message_segments[4].data[1]
            self.assertEqual(updated_forward["content"], "forward metadata")
            self.assertEqual(updated_forward["raw_message"], "forward raw text")
            nested = updated_forward["message_segment"].data
            self.assertEqual(nested[2], Seg(type="text", data="kept"))
            self.assertIn("看起来像是Nacho", nested[1].data)
            self.assertEqual(plugin._service.calls, 3)
            await plugin.on_unload()

    async def test_no_model_invalid_image_and_timeout_leave_message_unchanged(self):
        with isolated_plugin() as (module, manager, _holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            handler = module.SelfRecognitionPreProcess()
            handler.plugin_config = {"plugin": {"enabled": True}, "recognition": {"auto_recognize": True, "budget_seconds": 0.02}}

            service = _InferenceService(Path("unused"))
            service.state = {"desired_version": None, "loaded_version": None}
            plugin._service = service
            message = _FakeMessage([_image(_png())])
            result = await handler.execute(message)
            self.assertIsNone(result[4])
            self.assertEqual(message.message_segments, [_image(_png())])
            self.assertEqual(service.calls, 0)

            service.state = {"desired_version": "v1", "loaded_version": "v1"}
            invalid = _FakeMessage([Seg(type="image", data=base64.b64encode(b"not an image").decode())])
            result = await handler.execute(invalid)
            self.assertIsNone(result[4])
            self.assertEqual(service.calls, 0)

            service.block = True
            timeout_message = _FakeMessage([_image(_png(b"timeout"))])
            result = await handler.execute(timeout_message)
            self.assertIsNone(result[4])
            self.assertEqual(timeout_message.message_segments, [_image(_png(b"timeout"))])
            await plugin.on_unload()

    async def test_loaded_version_fallback_cache_switch_and_successful_activation(self):
        with isolated_plugin() as (module, manager, _holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            service = _InferenceService(Path("unused"))
            service.state = {
                "desired_version": "v2",
                "loaded_version": "v1",
                "failed_candidate_version": "v2",
                "last_error": "candidate load failed",
            }
            service.result = {"status": "ok", "version": "v1", "score": 0.7}
            plugin._service = service
            raw = _png(b"same bytes")

            fallback = await plugin.classify_image(raw, "Nacho", time.monotonic() + 1)
            self.assertEqual(fallback["status"], "ok")
            self.assertTrue(fallback["fallback"])
            self.assertEqual(fallback["loaded_version"], "v1")
            self.assertEqual(fallback["desired_version"], "v2")
            self.assertEqual(fallback["fallback_error"], "candidate load failed")
            cached = await plugin.classify_image(raw, "Nacho", time.monotonic() + 1)
            self.assertEqual(cached["version"], "v1")
            self.assertEqual(service.calls, 1)

            service.state = {"desired_version": "v3", "loaded_version": "v1"}
            service.result = {"status": "ok", "version": "v3", "score": 0.8}
            # The backend may activate the requested version while this inference is running.
            service.status = _status_sequence(
                {"desired_version": "v3", "loaded_version": "v1"},
                {"desired_version": "v3", "loaded_version": "v3"},
            )
            activated = await plugin.classify_image(_png(b"activation"), "Nacho", time.monotonic() + 1)
            self.assertEqual(activated["status"], "ok")
            self.assertEqual(activated["version"], "v3")
            self.assertFalse(activated["fallback"])
            self.assertEqual(service.calls, 2)
            await plugin.on_unload()

    async def test_desired_model_race_is_discarded_and_unload_blocks_late_result(self):
        with isolated_plugin() as (module, manager, _holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            service = _InferenceService(Path("unused"))
            service.state = {"desired_version": "v1", "loaded_version": "v1"}
            service.result = {"status": "ok", "version": "v1", "score": 0.9}
            service.status = _status_sequence(
                {"desired_version": "v1", "loaded_version": "v1"},
                {"desired_version": "v2", "loaded_version": "v1"},
            )
            plugin._service = service
            raced = await plugin.classify_image(_png(b"raced"), deadline=time.monotonic() + 1)
            self.assertEqual(raced["status"], "stale")

            service.status = lambda: {"desired_version": "v1", "loaded_version": "v1"}
            service.block = True
            active = asyncio.create_task(plugin.classify_image(_png(b"unload"), deadline=time.monotonic() + 1))
            self.assertTrue(await asyncio.to_thread(service.started.wait, 1))
            await plugin.on_unload()
            result = await active
            self.assertEqual(result["status"], "stopped")
            self.assertTrue(service.closed)
            self.assertEqual(plugin._service, None)

    async def test_scoped_media_context_expiry_indices_and_oversized_status(self):
        with isolated_plugin() as (module, manager, _holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            service = _InferenceService(Path("unused"))
            plugin._service = service
            segments = [_image(_png(b"one")), Seg(type="text", data="text"), _image(_png(b"two"), "emoji")]
            message = _FakeMessage(segments, stream_id="chat-a", message_id="canonical-42", plain_text="the latest picture")
            self.assertEqual(plugin.record_message_media(message), 2)
            tool = module.RecognizeSelfImageTool()
            preparation = tool.prepare_execution_context(SimpleNamespace(stream_id="chat-a"))
            self.assertIn("canonical-42", preparation.prompt_supplement)
            self.assertIn("不是 Planner 别名", preparation.prompt_supplement)
            self.assertEqual(len(preparation.context.messages), 1)
            self.assertFalse(module.RecognizeSelfImageTool.parameters[1][3])

            cross_chat = tool.prepare_execution_context(SimpleNamespace(stream_id="chat-b"))
            result = await tool.execute_with_context(
                {"message_id": "canonical-42", "image_index": 0}, cross_chat.context
            )
            self.assertEqual(json.loads(result["content"])["status"], "not_found")
            self.assertEqual(json.loads((await tool.execute({"message_id": "canonical-42", "image_index": 0}))[
                "content"
            ])["status"], "context_required")

            for index in (-1, 2, True):
                invalid = await tool.execute_with_context(
                    {"message_id": "canonical-42", "image_index": index}, preparation.context
                )
                self.assertEqual(json.loads(invalid["content"])["status"], "invalid_image_index")

            valid = await tool.execute_with_context(
                {"message_id": "canonical-42", "image_index": 0}, preparation.context
            )
            self.assertEqual(json.loads(valid["content"])["status"], "ok")
            default_index = await tool.execute_with_context(
                {"message_id": "canonical-42"}, preparation.context
            )
            self.assertEqual(json.loads(default_index["content"])["status"], "ok")
            self.assertEqual(json.loads(default_index["content"])["image_index"], 0)
            ref = preparation.context.messages[0].images[0]
            with patch.object(module.time, "monotonic", return_value=ref.expires_at + 1):
                expired = await tool.execute_with_context(
                    {"message_id": "canonical-42", "image_index": 0}, preparation.context
                )
            self.assertEqual(json.loads(expired["content"])["status"], "expired")

            old_max = module.MAX_IMAGE_BYTES
            try:
                module.MAX_IMAGE_BYTES = 4
                oversized_message = _FakeMessage([_image(_png(b"too big"))], message_id="oversized")
                plugin.record_message_media(oversized_message)
            finally:
                module.MAX_IMAGE_BYTES = old_max
            oversized_context = tool.prepare_execution_context(SimpleNamespace(stream_id="chat-a")).context
            oversized = await tool.execute_with_context(
                {"message_id": "oversized", "image_index": 0}, oversized_context
            )
            self.assertEqual(json.loads(oversized["content"])["status"], "oversized")
            await plugin.on_unload()

    async def test_snapshot_exposure_is_newest_first_and_bounded(self):
        with isolated_plugin() as (module, manager, _holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            for index in range(module.MAX_TOOL_CONTEXT_MESSAGES + 5):
                message = _FakeMessage(
                    [_image(_png(str(index).encode()))],
                    message_id=f"canonical-{index}",
                    plain_text="preview " * 60,
                )
                plugin.record_message_media(message)
                time.sleep(0.0001)
            tool = module.RecognizeSelfImageTool()
            preparation = tool.prepare_execution_context(SimpleNamespace(stream_id="chat-a"))
            self.assertEqual(len(preparation.context.messages), module.MAX_TOOL_CONTEXT_MESSAGES)
            self.assertIn("canonical-28", preparation.prompt_supplement)
            self.assertNotIn('"message_id":"canonical-0"', preparation.prompt_supplement)
            self.assertLessEqual(len(preparation.prompt_supplement), module.MAX_TOOL_CONTEXT_CHARS)
            await plugin.on_unload()

    async def test_node_budget_matches_media_capture_for_top_level_nested_and_forward_floods(self):
        with isolated_plugin() as (module, manager, _holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            service = _InferenceService(Path("unused"))
            plugin._service = service
            handler = module.SelfRecognitionPreProcess()
            handler.plugin_config = {
                "plugin": {"enabled": True},
                "recognition": {"auto_recognize": True, "budget_seconds": 1.0},
            }
            raw = _png(b"beyond traversal bound")
            cases = [
                (
                    "top-level",
                    [Seg(type="text", data=str(index)) for index in range(module.MAX_SEGMENT_NODES)]
                    + [_image(raw)],
                ),
                (
                    "nested-seglist",
                    [
                        Seg(
                            type="seglist",
                            data=[Seg(type="text", data=str(index)) for index in range(module.MAX_SEGMENT_NODES)]
                            + [_image(raw)],
                        )
                    ],
                ),
                (
                    "forward-node",
                    [
                        Seg(
                            type="forward",
                            data=[
                                {"content": str(index), "raw_message": "ignored"}
                                for index in range(module.MAX_SEGMENT_NODES - 1)
                            ]
                            + [{"message_segment": _image(raw)}],
                        )
                    ],
                ),
            ]

            for label, segments in cases:
                with self.subTest(label=label):
                    message = _FakeMessage(segments, message_id=f"bounded-{label}")
                    result = await handler.execute(message)
                    self.assertIsNone(result[4])
                    self.assertEqual(message.message_segments, segments)
                    self.assertEqual(service.calls, 0)
                    self.assertEqual(plugin.record_message_media(message), 0)
                    self.assertEqual(plugin._media_refs, {})
            await plugin.on_unload()

    async def test_media_ref_pressure_evicts_whole_messages_without_renumbering(self):
        with isolated_plugin() as (module, manager, _holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            service = _InferenceService(Path("unused"))
            plugin._service = service
            old_capacity = module.MAX_MEDIA_REF_ENTRIES
            module.MAX_MEDIA_REF_ENTRIES = 3
            try:
                old_message = _FakeMessage(
                    [_image(_png(b"old-0")), _image(_png(b"old-1"))],
                    message_id="old-message",
                )
                new_message = _FakeMessage(
                    [_image(_png(b"new-0")), _image(_png(b"new-1"))],
                    message_id="new-message",
                )
                self.assertEqual(plugin.record_message_media(old_message), 2)
                self.assertEqual(plugin.record_message_media(new_message), 2)
                snapshot = plugin.media_snapshot("chat-a")
                self.assertEqual([item.message_id for item in snapshot.messages], ["new-message"])
                self.assertEqual([ref.image_index for ref in snapshot.messages[0].images], [0, 1])

                tool = module.RecognizeSelfImageTool()
                preparation = tool.prepare_execution_context(SimpleNamespace(stream_id="chat-a"))
                self.assertNotIn("old-message", preparation.prompt_supplement)
                missing_old = await tool.execute_with_context(
                    {"message_id": "old-message"}, preparation.context
                )
                self.assertEqual(json.loads(missing_old["content"])["status"], "not_found")

                selected_zero = await tool.execute_with_context(
                    {"message_id": "new-message"}, preparation.context
                )
                self.assertEqual(json.loads(selected_zero["content"])["status"], "ok")
                self.assertEqual(json.loads(selected_zero["content"])["image_index"], 0)
                self.assertEqual(service.calls, 1)
            finally:
                module.MAX_MEDIA_REF_ENTRIES = old_capacity
                await plugin.on_unload()


def _status_sequence(*statuses):
    sequence = list(statuses)
    calls = 0

    def status():
        nonlocal calls
        result = sequence[min(calls, len(sequence) - 1)]
        calls += 1
        return dict(result)

    return status


if __name__ == "__main__":
    unittest.main()
