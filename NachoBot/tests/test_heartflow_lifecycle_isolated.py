"""Heartflow TTL/LRU 与关闭顺序的隔离单元测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class FakeRuntime:
    def __init__(self, _chat_id=None):
        self.stop_count = 0
        self.running = True
        self._planner_interrupt_flag = None
        self._in_flight_operations = 0

    async def start(self):
        self.running = True

    async def stop(self):
        self.stop_count += 1
        self.running = False


class FocusCoordinator:
    def __init__(self):
        self.managed: set[str] = set()
        self.callback = None

    def is_managed(self, chat_id: str) -> bool:
        return chat_id in self.managed

    def set_ensure_runtime_callback(self, callback) -> None:
        self.callback = callback


class AppointmentScheduler:
    def __init__(self):
        self.pending: set[str] = set()

    def get_pending(self, chat_id: str):
        return [{}] if chat_id in self.pending else []


@contextmanager
def isolated_heartflow_module():
    module_name = "_nachobot_heartflow_test"
    names = {
        module_name,
        "src",
        "src.chat",
        "src.chat.heart_flow",
        "src.chat.heart_flow.heartFC_chat",
        "src.chat.brain_chat",
        "src.chat.brain_chat.brain_chat",
        "src.chat.heart_flow.appointment_scheduler",
        "src.chat.message_receive",
        "src.chat.message_receive.chat_stream",
        "src.chat.focus",
        "src.chat.focus.coordinator",
        "src.common",
        "src.common.logger",
    }
    previous = {name: sys.modules.get(name) for name in names}

    def install(name: str, **attributes):
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        sys.modules[name] = module
        return module

    for package_name in (
        "src",
        "src.chat",
        "src.chat.heart_flow",
        "src.chat.brain_chat",
        "src.chat.message_receive",
        "src.chat.focus",
        "src.common",
    ):
        package = install(package_name)
        package.__path__ = []

    focus = FocusCoordinator()
    appointments = AppointmentScheduler()
    stream_manager = types.SimpleNamespace(get_stream=lambda _chat_id: None)
    install(
        "src.chat.message_receive.chat_stream",
        ChatStream=object,
        get_chat_manager=lambda: stream_manager,
    )
    install("src.common.logger", get_logger=lambda _name: _Logger())
    install("src.chat.heart_flow.heartFC_chat", HeartFChatting=FakeRuntime)
    install("src.chat.brain_chat.brain_chat", BrainChatting=FakeRuntime)
    install("src.chat.focus.coordinator", focus_coordinator=focus)
    install(
        "src.chat.heart_flow.appointment_scheduler",
        appointment_scheduler=appointments,
    )

    spec = importlib.util.spec_from_file_location(
        module_name,
        PROJECT_ROOT / "src/chat/heart_flow/heartflow.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load heartflow")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module, focus, appointments
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class HeartflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_chat_cancels_an_unpublished_creation(self) -> None:
        with isolated_heartflow_module() as (module, _, __):
            coordinator = module.Heartflow(cleanup_interval_seconds=0)
            started = asyncio.Event()

            async def creating():
                started.set()
                await asyncio.Event().wait()

            creation_task = asyncio.create_task(creating())
            coordinator._creation_tasks["chat"] = creation_task
            await started.wait()
            await coordinator.stop_chat("chat")

            self.assertTrue(creation_task.cancelled())
            self.assertNotIn("chat", coordinator._creation_tasks)
            self.assertNotIn("chat", coordinator.heartflow_chat_list)

    async def test_ttl_skips_focus_appointment_and_in_flight_runtime(self) -> None:
        with isolated_heartflow_module() as (module, focus, appointments):
            coordinator = module.Heartflow(idle_ttl_seconds=1, cleanup_interval_seconds=0)
            runtimes = {name: FakeRuntime() for name in ("idle", "focus", "appointment", "in-flight")}
            runtimes["in-flight"]._in_flight_operations = 1
            focus.managed.add("focus")
            appointments.pending.add("appointment")
            coordinator.heartflow_chat_list.update(runtimes)
            coordinator._last_accessed.update({name: time.monotonic() - 5 for name in runtimes})

            self.assertEqual(await coordinator.cleanup_idle_chats(), 1)
            self.assertEqual(runtimes["idle"].stop_count, 1)
            self.assertEqual(set(coordinator.heartflow_chat_list), {"focus", "appointment", "in-flight"})
            await coordinator.stop_all()

    async def test_stop_all_cancels_creation_and_drains_stop_tasks(self) -> None:
        with isolated_heartflow_module() as (module, _, __):
            coordinator = module.Heartflow(cleanup_interval_seconds=0)
            runtime = FakeRuntime()
            coordinator.heartflow_chat_list["chat"] = runtime
            coordinator._last_accessed["chat"] = time.monotonic()

            started = asyncio.Event()

            async def creating():
                started.set()
                await asyncio.Event().wait()

            creation_task = asyncio.create_task(creating())
            coordinator._creation_tasks["creating"] = creation_task
            await started.wait()
            await coordinator.stop_all()

            self.assertTrue(creation_task.cancelled())
            self.assertEqual(runtime.stop_count, 1)
            self.assertFalse(coordinator.heartflow_chat_list)
            self.assertFalse(coordinator._creation_tasks)
            self.assertFalse(coordinator._stopping_tasks)


if __name__ == "__main__":
    unittest.main()
