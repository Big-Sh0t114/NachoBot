"""插件生命周期的隔离单元测试；用最小 stub 避免导入应用启动链。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class ComponentType(Enum):
    ACTION = "action"
    COMMAND = "command"
    TOOL = "tool"
    SCHEDULER = "scheduler"
    EVENT_HANDLER = "event_handler"

    def __str__(self) -> str:
        return self.value


class EventType(Enum):
    ON_START = "on_start"
    ON_STOP = "on_stop"
    ON_MESSAGE_PRE_PROCESS = "on_message_pre_process"
    ON_MESSAGE = "on_message"
    ON_PLAN = "on_plan"
    POST_LLM = "post_llm"
    AFTER_LLM = "after_llm"
    POST_SEND_PRE_PROCESS = "post_send_pre_process"
    POST_SEND = "post_send"
    AFTER_SEND = "after_send"
    UNKNOWN = "unknown"


@dataclass
class ComponentInfo:
    name: str
    component_type: ComponentType
    enabled: bool = True
    plugin_name: str = "test_plugin"


@dataclass
class ActionInfo(ComponentInfo):
    component_type: ComponentType = field(default=ComponentType.ACTION, init=False)


@dataclass
class CommandInfo(ComponentInfo):
    component_type: ComponentType = field(default=ComponentType.COMMAND, init=False)
    command_pattern: str = ""


@dataclass
class ToolInfo(ComponentInfo):
    component_type: ComponentType = field(default=ComponentType.TOOL, init=False)


@dataclass
class EventHandlerInfo(ComponentInfo):
    component_type: ComponentType = field(default=ComponentType.EVENT_HANDLER, init=False)
    event_type: EventType = EventType.ON_MESSAGE
    intercept_message: bool = False
    weight: int = 0


@dataclass
class PluginInfo:
    name: str
    components: list[ComponentInfo] = field(default_factory=list)
    enabled: bool = True


class BaseAction:
    pass


class BaseCommand:
    pass


class BaseTool:
    pass


class BaseEventHandler:
    event_type = EventType.ON_MESSAGE
    handler_name = "handler"
    weight = 0
    intercept_message = False

    def __init__(self):
        self.plugin_name = "test_plugin"

    def set_plugin_name(self, plugin_name: str) -> None:
        self.plugin_name = plugin_name

    def set_plugin_config(self, _config: dict) -> None:
        pass


class PluginBase:
    def __init__(self, *_args, **_kwargs):
        pass

    def _check_dependencies(self) -> bool:
        return True


class NachoMessages:
    pass


class CustomEventHandlerResult:
    pass


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


@contextmanager
def isolated_plugin_modules():
    prefix = "_nachobot_lifecycle_test_core"
    names = {
        "src",
        "src.chat",
        "src.chat.message_receive",
        "src.chat.message_receive.message",
        "src.chat.message_receive.chat_stream",
        "src.common",
        "src.common.logger",
        "src.common.data_models",
        "src.common.data_models.llm_data_model",
        "src.plugin_system",
        "src.plugin_system.base",
        "src.plugin_system.base.component_types",
        "src.plugin_system.base.base_command",
        "src.plugin_system.base.base_action",
        "src.plugin_system.base.base_tool",
        "src.plugin_system.base.base_events_handler",
        "src.plugin_system.base.plugin_base",
        "src.plugin_system.base.base_plugin",
        "src.plugin_system.core",
        "src.plugin_system.core.component_registry",
        prefix,
        f"{prefix}.events_manager",
        f"{prefix}.component_registry",
        f"{prefix}.global_announcement_manager",
    }
    previous = {name: sys.modules.get(name) for name in names}
    for package_name in (
        "src",
        "src.chat",
        "src.chat.message_receive",
        "src.common",
        "src.common.data_models",
        "src.plugin_system",
        "src.plugin_system.base",
        "src.plugin_system.core",
    ):
        package = _module(package_name)
        package.__path__ = []
        sys.modules[package_name] = package

    component_types = _module(
        "src.plugin_system.base.component_types",
        ComponentInfo=ComponentInfo,
        ActionInfo=ActionInfo,
        ToolInfo=ToolInfo,
        CommandInfo=CommandInfo,
        EventHandlerInfo=EventHandlerInfo,
        PluginInfo=PluginInfo,
        ComponentType=ComponentType,
        EventType=EventType,
        NachoMessages=NachoMessages,
        CustomEventHandlerResult=CustomEventHandlerResult,
    )
    sys.modules[component_types.__name__] = component_types
    sys.modules["src.plugin_system.base.base_action"] = _module(
        "src.plugin_system.base.base_action", BaseAction=BaseAction
    )
    sys.modules["src.plugin_system.base.base_command"] = _module(
        "src.plugin_system.base.base_command", BaseCommand=BaseCommand
    )
    sys.modules["src.plugin_system.base.base_tool"] = _module(
        "src.plugin_system.base.base_tool", BaseTool=BaseTool
    )
    sys.modules["src.plugin_system.base.base_events_handler"] = _module(
        "src.plugin_system.base.base_events_handler", BaseEventHandler=BaseEventHandler
    )
    sys.modules["src.plugin_system.base.plugin_base"] = _module(
        "src.plugin_system.base.plugin_base", PluginBase=PluginBase
    )
    sys.modules["src.common.logger"] = _module("src.common.logger", get_logger=lambda _name: _Logger())
    sys.modules["src.chat.message_receive.message"] = _module(
        "src.chat.message_receive.message", MessageRecv=object, MessageSending=object
    )
    sys.modules["src.chat.message_receive.chat_stream"] = _module(
        "src.chat.message_receive.chat_stream", get_chat_manager=lambda: None
    )
    sys.modules["src.common.data_models.llm_data_model"] = _module(
        "src.common.data_models.llm_data_model", LLMGenerationDataModel=object
    )

    package = _module(prefix)
    package.__path__ = [str(PROJECT_ROOT / "src" / "plugin_system" / "core")]
    sys.modules[prefix] = package
    announcement = types.SimpleNamespace(get_disabled_chat_event_handlers=lambda _stream_id: set())
    sys.modules[f"{prefix}.global_announcement_manager"] = _module(
        f"{prefix}.global_announcement_manager", global_announcement_manager=announcement
    )

    def load(name: str, relative_path: str) -> types.ModuleType:
        spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relative_path)
        if spec is None or spec.loader is None:
            raise AssertionError(f"cannot load {relative_path}")
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[name] = loaded
        spec.loader.exec_module(loaded)
        return loaded

    try:
        events_module = load(f"{prefix}.events_manager", "src/plugin_system/core/events_manager.py")
        registry_module = load(f"{prefix}.component_registry", "src/plugin_system/core/component_registry.py")
        sys.modules["src.plugin_system.core.component_registry"] = _module(
            "src.plugin_system.core.component_registry",
            component_registry=registry_module.ComponentRegistry(),
        )
        base_plugin_module = load(
            "src.plugin_system.base.base_plugin",
            "src/plugin_system/base/base_plugin.py",
        )
        registry_module.BasePluginForTest = base_plugin_module.BasePlugin
        yield events_module, registry_module
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class EventsManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_generation_blocks_old_nonblocking_handler_after_replacement(self) -> None:
        with isolated_plugin_modules() as (events_module, _):
            manager = events_module.EventsManager()
            sys.modules["src.plugin_system.core.component_registry"].get_plugin_config = lambda _name: {}
            old_calls: list[str] = []
            new_calls: list[str] = []
            snapshot_taken = asyncio.Event()
            resume_snapshot = asyncio.Event()

            class OldHandler(BaseEventHandler):
                handler_name = "generation_handler"
                event_type = EventType.ON_START

                async def execute(self, _message):
                    old_calls.append("old")
                    return True, True, "old", None, None

            class NewHandler(BaseEventHandler):
                handler_name = "generation_handler"
                event_type = EventType.ON_START

                async def execute(self, _message):
                    new_calls.append("new")
                    return True, True, "new", None, None

            async def pause_after_snapshot() -> None:
                snapshot_taken.set()
                await resume_snapshot.wait()

            self.assertTrue(manager.register_event_subscriber(EventHandlerInfo(name="generation_handler"), OldHandler))
            manager._after_handler_snapshot = pause_after_snapshot
            dispatch = asyncio.create_task(manager.handle_nacho_events(EventType.ON_START))
            await snapshot_taken.wait()
            self.assertTrue(await manager.unregister_event_subscriber("generation_handler"))
            self.assertTrue(manager.register_event_subscriber(EventHandlerInfo(name="generation_handler"), NewHandler))
            resume_snapshot.set()
            await dispatch
            await asyncio.sleep(0)
            self.assertEqual(old_calls, [])
            self.assertEqual(new_calls, [])

            await manager.handle_nacho_events(EventType.ON_START)
            await asyncio.sleep(0)
            self.assertEqual(old_calls, [])
            self.assertEqual(new_calls, ["new"])

    async def test_snapshot_generation_blocks_old_interceptor_after_replacement(self) -> None:
        with isolated_plugin_modules() as (events_module, _):
            manager = events_module.EventsManager()
            sys.modules["src.plugin_system.core.component_registry"].get_plugin_config = lambda _name: {}
            old_calls: list[str] = []
            new_calls: list[str] = []
            snapshot_taken = asyncio.Event()
            resume_snapshot = asyncio.Event()

            class OldHandler(BaseEventHandler):
                handler_name = "intercept_generation_handler"
                event_type = EventType.ON_STOP
                intercept_message = True

                async def execute(self, _message):
                    old_calls.append("old")
                    return True, True, "old", None, None

            class NewHandler(BaseEventHandler):
                handler_name = "intercept_generation_handler"
                event_type = EventType.ON_STOP
                intercept_message = True

                async def execute(self, _message):
                    new_calls.append("new")
                    return True, True, "new", None, None

            async def pause_after_snapshot() -> None:
                snapshot_taken.set()
                await resume_snapshot.wait()

            self.assertTrue(
                manager.register_event_subscriber(
                    EventHandlerInfo(name="intercept_generation_handler", intercept_message=True),
                    OldHandler,
                )
            )
            manager._after_handler_snapshot = pause_after_snapshot
            dispatch = asyncio.create_task(manager.handle_nacho_events(EventType.ON_STOP))
            await snapshot_taken.wait()
            self.assertTrue(await manager.unregister_event_subscriber("intercept_generation_handler"))
            self.assertTrue(
                manager.register_event_subscriber(
                    EventHandlerInfo(name="intercept_generation_handler", intercept_message=True),
                    NewHandler,
                )
            )
            resume_snapshot.set()
            await dispatch
            self.assertEqual(old_calls, [])
            self.assertEqual(new_calls, [])

            await manager.handle_nacho_events(EventType.ON_STOP)
            self.assertEqual(old_calls, [])
            self.assertEqual(new_calls, ["new"])

    async def test_completed_task_is_removed_from_handler_bucket(self) -> None:
        with isolated_plugin_modules() as (events_module, _):
            manager = events_module.EventsManager()

            class Handler(BaseEventHandler):
                async def execute(self, _message):
                    return True, True, "ok", None, None

            self.assertTrue(manager.register_event_subscriber(EventHandlerInfo(name=Handler.handler_name), Handler))
            handler = manager._events_subscribers[EventType.ON_MESSAGE][0]
            manager._dispatch_handler_task(handler, EventType.ON_MESSAGE)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertNotIn(handler.handler_name, manager._handler_tasks)

    async def test_unregister_cancels_running_tasks_and_is_idempotent(self) -> None:
        with isolated_plugin_modules() as (events_module, _):
            manager = events_module.EventsManager()
            started = asyncio.Event()

            class Handler(BaseEventHandler):
                async def execute(self, _message):
                    started.set()
                    await asyncio.Event().wait()

            info = EventHandlerInfo(name=Handler.handler_name)
            self.assertTrue(manager.register_event_subscriber(info, Handler))
            handler = manager._events_subscribers[EventType.ON_MESSAGE][0]
            manager._dispatch_handler_task(handler, EventType.ON_MESSAGE)
            await started.wait()
            self.assertTrue(await manager.unregister_event_subscriber(Handler.handler_name))
            self.assertNotIn(Handler.handler_name, manager._handler_tasks)
            self.assertTrue(await manager.unregister_event_subscriber(Handler.handler_name))

    async def test_unregister_refuses_unload_while_interceptor_ignores_cancel(self) -> None:
        with isolated_plugin_modules() as (events_module, _):
            manager = events_module.EventsManager(handler_cancel_timeout=0.01)
            started = asyncio.Event()
            release = asyncio.Event()
            calls = []

            class Handler(BaseEventHandler):
                intercept_message = True

                async def execute(self, _message):
                    calls.append("handled")
                    started.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()
                    return True, True, "ok", None, None

            info = EventHandlerInfo(
                name=Handler.handler_name,
                intercept_message=True,
            )
            self.assertTrue(manager.register_event_subscriber(info, Handler))
            handler = manager._events_subscribers[EventType.ON_MESSAGE][0]
            dispatch = asyncio.create_task(
                manager._dispatch_intercepting_handler_task(
                    handler,
                    EventType.ON_MESSAGE,
                )
            )
            await started.wait()

            self.assertFalse(
                await manager.unregister_event_subscriber(Handler.handler_name)
            )
            self.assertIn(Handler.handler_name, manager._handler_tasks)
            self.assertNotIn(Handler.handler_name, manager._closing_handlers)
            self.assertTrue(manager.has_event_subscriber(Handler.handler_name))

            release.set()
            await dispatch
            await manager._dispatch_intercepting_handler_task(
                handler,
                EventType.ON_MESSAGE,
            )
            self.assertEqual(calls, ["handled", "handled"])
            self.assertTrue(
                await manager.unregister_event_subscriber(Handler.handler_name)
            )
            self.assertNotIn(Handler.handler_name, manager._handler_mapping)


class ComponentRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_command_pattern_leaves_no_partial_registration(self) -> None:
        with isolated_plugin_modules() as (_, registry_module):
            registry = registry_module.ComponentRegistry()

            class Command(BaseCommand):
                pass

            info = CommandInfo(name="broken", command_pattern="[")
            self.assertFalse(registry.register_component(info, Command))
            self.assertIsNone(registry.get_component_info("broken", ComponentType.COMMAND))
            self.assertNotIn("broken", registry.get_command_registry())

    async def test_event_subscription_failure_leaves_no_partial_registration(self) -> None:
        with isolated_plugin_modules() as (events_module, registry_module):
            registry = registry_module.ComponentRegistry()

            class Handler(BaseEventHandler):
                handler_name = "rejected"

            events_module.events_manager.register_event_subscriber = lambda *_args, **_kwargs: False
            info = EventHandlerInfo(name=Handler.handler_name)
            self.assertFalse(registry.register_component(info, Handler))
            self.assertIsNone(registry.get_component_info(Handler.handler_name, ComponentType.EVENT_HANDLER))
            self.assertNotIn(Handler.handler_name, registry.get_event_handler_registry())

    async def test_disabled_component_remove_and_disable_are_idempotent(self) -> None:
        with isolated_plugin_modules() as (_, registry_module):
            registry = registry_module.ComponentRegistry()

            class Action(BaseAction):
                pass

            info = ActionInfo(name="disabled", enabled=False)
            self.assertTrue(registry.register_component(info, Action))
            self.assertTrue(await registry.disable_component("disabled", ComponentType.ACTION))
            self.assertTrue(await registry.disable_component("disabled", ComponentType.ACTION))
            self.assertTrue(await registry.remove_component("disabled", ComponentType.ACTION, "test_plugin"))
            self.assertTrue(await registry.remove_component("disabled", ComponentType.ACTION, "test_plugin"))
            self.assertIsNone(registry.get_component_info("disabled", ComponentType.ACTION))

    async def test_plugin_registration_rolls_back_earlier_components(self) -> None:
        with isolated_plugin_modules() as (_, registry_module):
            registry = registry_module.ComponentRegistry()
            sys.modules["src.plugin_system.core.component_registry"].component_registry = registry

            class Action(BaseAction):
                pass

            class Command(BaseCommand):
                pass

            class Plugin(registry_module.BasePluginForTest):
                plugin_name = "atomic_plugin"

                def get_plugin_components(self):
                    return [
                        (ActionInfo(name="first"), Action),
                        (CommandInfo(name="broken", command_pattern="["), Command),
                    ]

            plugin = Plugin()
            plugin.log_prefix = "[Plugin:atomic_plugin]"
            plugin.plugin_info = PluginInfo(name="atomic_plugin")
            self.assertFalse(plugin.register_plugin())
            self.assertNotIn("first", registry.get_action_registry())
            self.assertIsNone(registry.get_plugin_info("atomic_plugin"))

    async def test_arbitrary_component_registration_exception_rolls_back_indexes(self) -> None:
        with isolated_plugin_modules() as (_, registry_module):
            registry = registry_module.ComponentRegistry()

            class Action(BaseAction):
                pass

            info = ActionInfo(name="explodes")

            def explode_after_mutation(_info, _class):
                registry._action_registry["explodes"] = Action
                registry._default_actions["explodes"] = info
                raise RuntimeError("arbitrary registration failure")

            registry._register_action_component = explode_after_mutation
            self.assertFalse(registry.register_component(info, Action))
            self.assertNotIn("explodes", registry.get_action_registry())
            self.assertNotIn("explodes", registry.get_default_actions())
            self.assertIsNone(registry.get_component_info("explodes", ComponentType.ACTION))


if __name__ == "__main__":
    unittest.main()
