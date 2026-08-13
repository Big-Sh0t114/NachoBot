from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import process_manager as process_module
from process_manager import ProcessManager, ServiceDef, ServiceStatus


class FakeProcess:
    def __init__(self):
        self.pid = None
        self.returncode = None
        self.stdout = None
        self.stdin = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class FakePsutilProcess:
    def __init__(self, pid: int, *, running: bool = True):
        self.pid = pid
        self.running = running
        self.terminated = False
        self.killed = False

    def children(self, recursive=True):
        return []

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.running = False

    def is_running(self):
        return self.running

    def wait(self, timeout=None):
        if self.running:
            raise process_module.psutil.TimeoutExpired(timeout)
        return 0


class FakeWindowsJobFacade:
    def __init__(self, *, active=0, fail_create=False):
        self.active = active
        self.fail_create = fail_create
        self.events = []
        self.close_count = 0
        self.job = process_module._WindowsJobCapability(99)

    def create_assign_resume(self, pid):
        self.events.append(("create", pid))
        if self.fail_create:
            self.events.append(("cleanup", pid))
            self.close(self.job)
            raise RuntimeError("resume failed")
        self.events.extend((("assign", pid), ("resume", pid)))
        return self.job

    def terminate(self, job):
        self.events.append(("terminate", job.handle))
        if self.active:
            self.active = 0

    def active_processes(self, job):
        self.events.append(("active", job.handle))
        return self.active

    def close(self, job):
        self.events.append(("close", job.handle))
        self.close_count += 1
        job.closed = True


class _FakeWin32Function:
    def __init__(self, result=1):
        self.result = result
        self.side_effect = None
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if self.side_effect is not None:
            return self.side_effect(*args)
        return self.result


class _FakeWin32Library:
    def __init__(self):
        self.CreateJobObjectW = _FakeWin32Function(0x100001234)
        self.SetInformationJobObject = _FakeWin32Function(1)
        self.OpenProcess = _FakeWin32Function(0x100005678)
        self.AssignProcessToJobObject = _FakeWin32Function(1)
        self.CloseHandle = _FakeWin32Function(1)
        self.TerminateProcess = _FakeWin32Function(1)
        self.TerminateJobObject = _FakeWin32Function(1)
        self.QueryInformationJobObject = _FakeWin32Function(1)
        self.NtResumeProcess = _FakeWin32Function(0)


async def hold_output(*_args) -> None:
    await asyncio.Event().wait()


class ProcessManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = ProcessManager(Path.cwd())
        self.service = ServiceDef(
            "svc",
            "Service",
            "test",
            ".",
            ["fake"],
            port=18999,
            wait_port=True,
        )
        self.registry = patch.object(process_module, "_register_services", return_value=None)
        self.registry.start()
        process_module.SERVICE_DEFS = {"svc": self.service}
        process_module.GROUP_DEFS = {}

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.registry.stop()

    async def test_service_stays_starting_until_readiness_succeeds(self) -> None:
        process = FakeProcess()
        ready = asyncio.Event()

        async def wait_for_ready(*_args, **_kwargs) -> bool:
            await ready.wait()
            return True

        self.manager._read_output = hold_output
        self.manager._wait_for_port = wait_for_ready
        self.manager._port_is_open = lambda _port: False
        with patch.object(process_module.os, "name", "posix"), patch.object(
            asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            start = asyncio.create_task(self.manager.start_service("svc"))
            for _ in range(10):
                await asyncio.sleep(0)
                if self.manager.states.get("svc", None) and self.manager.states["svc"].process:
                    break
            self.assertEqual(self.manager.states["svc"].status, ServiceStatus.STARTING)
            ready.set()
            await start

        self.assertEqual(self.manager.states["svc"].status, ServiceStatus.RUNNING)
        await self.manager.stop_service("svc")
        self.assertEqual(self.manager.states["svc"].status, ServiceStatus.STOPPED)

    async def test_windows_job_setup_without_pid_is_error_and_reaps_leader(self) -> None:
        process = FakeProcess()
        facade = FakeWindowsJobFacade()
        self.manager._windows_job_facade = facade
        self.service.wait_port = False
        self.service.port = None
        with patch.object(process_module.os, "name", "nt"), patch.object(
            asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)
        ):
            await self.manager.start_service("svc")
        state = self.manager.states["svc"]
        self.assertEqual(state.status, ServiceStatus.ERROR)
        self.assertTrue(process.terminated)
        self.assertIsNone(state.process)
        self.assertIsNone(state.pid)
        self.assertFalse(any(event[0] == "create" for event in facade.events))

    async def test_webui_control_token_is_not_inherited_by_child_services(self) -> None:
        process = FakeProcess()
        self.manager._read_output = hold_output
        self.manager._wait_for_port = AsyncMock(return_value=True)
        self.manager._port_is_open = lambda _port: False
        captured = {}

        async def spawn(*_args, **kwargs):
            captured.update(kwargs["env"])
            return process

        with (
            patch.dict("os.environ", {"NACHOBOT_WEBUI_TOKEN": "control-secret"}),
            patch.object(asyncio, "create_subprocess_exec", side_effect=spawn),
        ):
            await self.manager.start_service("svc")

        self.assertNotIn("NACHOBOT_WEBUI_TOKEN", captured)

    async def test_failed_readiness_terminates_process_and_sets_error(self) -> None:
        process = FakeProcess()
        self.manager._read_output = hold_output
        self.manager._wait_for_port = AsyncMock(return_value=False)
        self.manager._port_is_open = lambda _port: False
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            await self.manager.start_service("svc")

        state = self.manager.states["svc"]
        self.assertEqual(state.status, ServiceStatus.ERROR)
        self.assertTrue(process.terminated)
        self.assertIsNone(state.process)

    async def test_managed_start_exception_is_observable(self) -> None:
        self.manager.start_service = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(process_module.logger, "error") as log_error:
            self.manager.request_start_service("svc")
            operation = self.manager._operation_tasks["service:svc"]
            await asyncio.gather(operation, return_exceptions=True)
            await asyncio.sleep(0)
        log_error.assert_called_once()
        self.assertEqual(self.manager.states["svc"].status, ServiceStatus.ERROR)

    async def test_unknown_service_is_rejected_before_scheduling(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.request_start_service("missing")
        self.assertFalse(self.manager._operation_tasks)

    async def test_failed_stop_is_retryable_and_visible(self) -> None:
        state = process_module.ServiceState(
            status=ServiceStatus.RUNNING,
            process=FakeProcess(),
            pid=123,
        )
        self.manager.states["svc"] = state
        self.manager._terminate_state_process = AsyncMock(side_effect=RuntimeError("denied"))

        with self.assertRaisesRegex(RuntimeError, "denied"):
            await self.manager.stop_service("svc")
        self.assertEqual(state.status, ServiceStatus.ERROR)

        self.manager._terminate_state_process = AsyncMock(return_value=None)
        await self.manager.stop_service("svc")
        self.assertEqual(state.status, ServiceStatus.STOPPED)

    async def test_group_rolls_back_if_unprobed_service_exits_during_grace(self) -> None:
        first = ServiceDef("first", "First", "test", ".", ["fake"])
        second = ServiceDef("second", "Second", "test", ".", ["fake"])
        group = process_module.GroupDef("test", "Test", "x", ["first", "second"])
        process_module.SERVICE_DEFS = {"first": first, "second": second}
        process_module.GROUP_DEFS = {"test": group}
        calls = []

        async def start(service_id, **_kwargs):
            calls.append(("start", service_id))
            self.manager.states[service_id] = process_module.ServiceState(
                status=ServiceStatus.RUNNING
            )

        async def stop(service_id, **_kwargs):
            calls.append(("stop", service_id))
            self.manager.states[service_id].status = ServiceStatus.STOPPED

        async def crash_during_grace(*_args, **_kwargs):
            self.manager.states["first"].status = ServiceStatus.ERROR

        self.manager.start_service = start
        self.manager.stop_service = stop
        with patch.object(asyncio, "sleep", side_effect=crash_during_grace):
            await self.manager.start_group("test")

        self.assertEqual(calls, [("start", "first"), ("stop", "first")])
        self.assertEqual(self.manager.states["first"].status, ServiceStatus.STOPPED)

    async def test_immediate_group_stop_replaces_an_unstarted_group_start(self) -> None:
        group = process_module.GroupDef("test", "Test", "x", ["svc"])
        process_module.GROUP_DEFS = {"test": group}
        self.manager.start_group = AsyncMock()
        self.manager.stop_group = AsyncMock()

        self.manager.request_start_group("test")
        start_task = self.manager._operation_tasks["group:test"]
        self.manager.request_stop_group("test")
        stop_task = self.manager._operation_tasks["group:test"]
        await asyncio.gather(start_task, stop_task, return_exceptions=True)

        self.assertTrue(start_task.cancelled())
        self.manager.start_group.assert_not_awaited()
        self.manager.stop_group.assert_awaited_once_with("test")

    async def test_posix_group_kills_descendants_after_leader_exits(self) -> None:
        process = FakeProcess()
        process.pid = 321
        process.returncode = -15
        state = process_module.ServiceState(
            status=ServiceStatus.STOPPED,
            process=process,
            pid=321,
            process_group_id=4321,
        )
        signals = []
        group_alive = True
        kill_signal = 9

        def killpg(pgid, sig):
            nonlocal group_alive
            signals.append((pgid, sig))
            if sig == 0:
                if group_alive:
                    return
                raise ProcessLookupError
            if sig == kill_signal:
                group_alive = False

        with (
            patch.object(process_module.os, "name", "posix"),
            patch.object(process_module.os, "killpg", side_effect=killpg, create=True),
            patch.object(process_module.signal, "SIGKILL", 9, create=True),
            patch.object(process_module, "_PROCESS_GROUP_TERM_TIMEOUT", 0.0),
            patch.object(process_module, "_PROCESS_GROUP_KILL_TIMEOUT", 0.0),
        ):
            await self.manager._terminate_state_process(state)

        self.assertEqual(
            signals,
            [
                (4321, process_module.signal.SIGTERM),
                (4321, 0),
                (4321, kill_signal),
                (4321, 0),
            ],
        )
        self.assertIsNone(state.process_group_id)

    async def test_stopped_state_with_owned_group_is_cleaned_by_shutdown(self) -> None:
        process = FakeProcess()
        process.pid = 654
        process.returncode = -15
        state = process_module.ServiceState(
            status=ServiceStatus.STOPPED,
            process=process,
            pid=654,
            process_group_id=7654,
        )
        self.manager.states["svc"] = state
        with patch.object(process_module.os, "name", "posix"), patch.object(
            process_module.os,
            "killpg",
            side_effect=ProcessLookupError,
            create=True,
        ) as killpg:
            await self.manager.shutdown()

        killpg.assert_called_once_with(7654, process_module.signal.SIGTERM)
        self.assertEqual(state.status, ServiceStatus.STOPPED)
        self.assertIsNone(state.process_group_id)

    async def test_leader_exit_retains_live_posix_group_for_later_stop(self) -> None:
        process = FakeProcess()
        process.pid = 987
        process.returncode = 0
        state = process_module.ServiceState(
            status=ServiceStatus.RUNNING,
            process=process,
            pid=987,
            process_group_id=9987,
        )
        self.manager.states["svc"] = state
        self.manager._posix_process_group_exists = lambda _pgid: True
        process.stdout = type("Output", (), {"readline": _readline_eof})()

        with patch.object(process_module.os, "name", "posix"):
            await self.manager._read_output("svc", process)

        self.assertEqual(state.status, ServiceStatus.ERROR)
        self.assertIsNone(state.process)
        self.assertEqual(state.process_group_id, 9987)

    async def test_start_refuses_error_state_with_retained_process_group(self) -> None:
        state = process_module.ServiceState(
            status=ServiceStatus.ERROR,
            process=None,
            pid=None,
            process_group_id=11001,
        )
        self.manager.states["svc"] = state
        with patch.object(asyncio, "create_subprocess_exec", new=AsyncMock()) as spawn:
            await self.manager.start_service("svc")

        spawn.assert_not_awaited()
        self.assertEqual(state.status, ServiceStatus.ERROR)
        self.assertEqual(state.process_group_id, 11001)
        self.assertIsNone(state.process)

    async def test_windows_output_eof_retains_live_process_for_stop(self) -> None:
        process = FakeProcess()
        process.pid = 12001
        state = process_module.ServiceState(
            status=ServiceStatus.RUNNING,
            process=process,
            pid=12001,
        )
        self.manager.states["svc"] = state
        process.stdout = type("Output", (), {"readline": _readline_eof})()
        parent = type(
            "Parent",
            (),
            {
                "children": lambda self, recursive=True: [],
                "terminate": lambda self: None,
                "is_running": lambda self: False,
            },
        )()
        with (
            patch.object(process_module.os, "name", "nt"),
            patch.object(process_module.psutil, "Process", return_value=parent) as process_ctor,
            patch.object(process_module.psutil, "wait_procs", return_value=([], [])) as wait_procs,
        ):
            await self.manager._read_output("svc", process)
            self.assertEqual(state.status, ServiceStatus.ERROR)
            self.assertIs(state.process, process)
            self.assertEqual(state.pid, 12001)
            await self.manager.stop_service("svc")

        process_ctor.assert_called_once_with(12001)
        wait_procs.assert_called_once()
        self.assertEqual(state.status, ServiceStatus.STOPPED)
        self.assertIsNone(state.process)
        self.assertIsNone(state.pid)

    async def test_start_does_not_overwrite_live_eof_error_during_spawn_broadcast(self) -> None:
        process = FakeProcess()
        process.pid = 13001
        self.service.wait_port = False
        self.service.port = None
        process.stdout = type("Output", (), {"readline": _readline_eof})()

        async def broadcast(_service_id, line):
            # The spawned broadcast is the deterministic scheduling seam: the
            # reader runs while start() is awaiting it and reports EOF with a
            # live (returncode=None) process.
            if "spawned" in line:
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        self.manager._broadcast = broadcast
        self.manager._windows_job_facade = FakeWindowsJobFacade()
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as spawn:
            await self.manager.start_service("svc")

        state = self.manager.states["svc"]
        spawn.assert_awaited_once()
        self.assertEqual(state.status, ServiceStatus.ERROR)
        self.assertIs(state.process, process)
        self.assertEqual(state.pid, 13001)

        self.manager._terminate_state_process = AsyncMock(return_value=None)
        await self.manager.stop_service("svc")
        self.assertEqual(state.status, ServiceStatus.STOPPED)
        self.assertIsNone(state.process)
        self.assertIsNone(state.pid)

    async def test_windows_captures_descendants_before_leader_exit_and_reaps_after(self) -> None:
        process = FakeProcess()
        process.pid = 5001
        parent = FakePsutilProcess(5001)
        child = FakePsutilProcess(5002)
        parent.children = lambda recursive=True: [child]
        wait_calls = []

        def wait_procs(procs, timeout):
            wait_calls.append(tuple(proc.pid for proc in procs))
            for proc in procs:
                if proc.killed:
                    proc.running = False
            survivors = [proc for proc in procs if proc.running]
            return [proc for proc in procs if not proc.running], survivors

        with (
            patch.object(process_module.os, "name", "nt"),
            patch.object(process_module.psutil, "Process", return_value=parent),
            patch.object(process_module.psutil, "wait_procs", side_effect=wait_procs),
        ):
            state = process_module.ServiceState(status=ServiceStatus.RUNNING, process=process, pid=5001)
            await self.manager._terminate_state_process(state)

        self.assertEqual(wait_calls, [(5001, 5002), (5001, 5002)])
        self.assertTrue(parent.terminated and child.terminated)
        self.assertTrue(parent.killed and child.killed)

    async def test_windows_survivor_keeps_error_and_owned_handle_for_retry(self) -> None:
        process = FakeProcess()
        process.pid = 6001
        parent = FakePsutilProcess(6001)
        with (
            patch.object(process_module.os, "name", "nt"),
            patch.object(process_module.psutil, "Process", return_value=parent),
            patch.object(process_module.psutil, "wait_procs", return_value=([ ], [parent])),
        ):
            state = process_module.ServiceState(status=ServiceStatus.RUNNING, process=process, pid=6001)
            self.manager.states["svc"] = state
            with self.assertRaises(RuntimeError):
                await self.manager.stop_service("svc")

        self.assertEqual(state.status, ServiceStatus.ERROR)
        self.assertIs(state.process, process)
        self.assertEqual(state.pid, 6001)
        self.assertTrue(state.windows_owned_processes)

    async def test_windows_job_start_assigns_before_resume_and_stores_capability(self) -> None:
        process = FakeProcess()
        process.pid = 9001
        facade = FakeWindowsJobFacade()
        self.manager._windows_job_facade = facade
        self.service.wait_port = False
        self.service.port = None
        self.manager._read_output = hold_output
        with (
            patch.object(process_module.os, "name", "nt"),
            patch.object(asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)),
            patch.object(process_module.psutil, "Process", return_value=FakePsutilProcess(9001)),
        ):
            await self.manager.start_service("svc")

        state = self.manager.states["svc"]
        self.assertEqual(state.status, ServiceStatus.RUNNING)
        self.assertIs(state.windows_job, facade.job)
        self.assertLess(facade.events.index(("assign", 9001)), facade.events.index(("resume", 9001)))
        self.manager._terminate_state_process = AsyncMock(return_value=None)
        state.status = ServiceStatus.STOPPED

    async def test_windows_job_survives_leader_exit_and_terminates_late_descendant(self) -> None:
        process = FakeProcess()
        process.pid = 9101
        process.returncode = 0
        process.stdout = type("Output", (), {"readline": _readline_eof})()
        facade = FakeWindowsJobFacade(active=1)
        state = process_module.ServiceState(
            status=ServiceStatus.RUNNING,
            process=process,
            pid=9101,
            windows_job=facade.job,
        )
        self.manager.states["svc"] = state
        self.manager._windows_job_facade = facade
        with patch.object(process_module.os, "name", "nt"):
            await self.manager._read_output("svc", process)
            self.assertEqual(state.status, ServiceStatus.ERROR)
            self.assertIs(state.windows_job, facade.job)
            await self.manager.stop_service("svc")

        self.assertEqual(state.status, ServiceStatus.STOPPED)
        self.assertEqual(facade.close_count, 1)
        self.assertIn(("terminate", 99), facade.events)

    async def test_windows_job_nonzero_active_is_retryable_and_closes_on_later_zero(self) -> None:
        process = FakeProcess()
        process.pid = 9201
        facade = FakeWindowsJobFacade(active=1)
        state = process_module.ServiceState(
            status=ServiceStatus.RUNNING,
            process=process,
            pid=9201,
            windows_job=facade.job,
        )
        self.manager.states["svc"] = state
        self.manager._windows_job_facade = facade
        original_terminate = facade.terminate
        facade.terminate = lambda job: self._keep_job_active(job, facade, original_terminate)
        with patch.object(process_module.os, "name", "nt"):
            with self.assertRaises(RuntimeError):
                await self.manager.stop_service("svc")
        self.assertEqual(state.status, ServiceStatus.ERROR)
        self.assertIs(state.windows_job, facade.job)
        self.assertEqual(facade.close_count, 0)

        facade.terminate = original_terminate
        state.windows_owned_processes = []
        state.process = None
        state.pid = None
        with patch.object(process_module.os, "name", "nt"):
            await self.manager.stop_service("svc")
        self.assertEqual(state.status, ServiceStatus.STOPPED)
        self.assertEqual(facade.close_count, 1)

    @staticmethod
    def _keep_job_active(job, facade, original_terminate):
        facade.events.append(("terminate", job.handle))

    async def test_windows_job_start_failure_reaps_leader_without_running(self) -> None:
        process = FakeProcess()
        process.pid = 9301
        facade = FakeWindowsJobFacade(fail_create=True)
        self.manager._windows_job_facade = facade
        self.service.wait_port = False
        self.service.port = None
        with (
            patch.object(process_module.os, "name", "nt"),
            patch.object(asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)),
        ):
            await self.manager.start_service("svc")

        state = self.manager.states["svc"]
        self.assertEqual(state.status, ServiceStatus.ERROR)
        self.assertTrue(process.terminated)
        self.assertIsNone(state.windows_job)
        self.assertEqual(facade.close_count, 1)

    async def test_windows_job_facade_binds_win64_prototypes_and_preserves_handles(self) -> None:
        kernel = _FakeWin32Library()
        ntdll = _FakeWin32Library()
        with patch.object(process_module.os, "name", "nt"), patch.object(
            process_module.ctypes, "WinDLL", side_effect=[kernel, ntdll], create=True
        ):
            facade = process_module._WindowsJobFacade()
            capability = facade.create_assign_resume(9401)
            self.assertEqual(capability.handle, 0x100001234)
            critical = (
                "CreateJobObjectW",
                "SetInformationJobObject",
                "OpenProcess",
                "AssignProcessToJobObject",
                "CloseHandle",
                "TerminateProcess",
                "TerminateJobObject",
                "QueryInformationJobObject",
            )
            for name in critical:
                function = getattr(kernel, name)
                self.assertIsNotNone(function.restype, name)
                self.assertIsNotNone(function.argtypes, name)
            self.assertEqual(kernel.CreateJobObjectW.restype, process_module.wintypes.HANDLE)
            self.assertEqual(kernel.OpenProcess.restype, process_module.wintypes.HANDLE)
            self.assertEqual(ntdll.NtResumeProcess.restype, process_module.ctypes.c_long)
            self.assertIsNotNone(ntdll.NtResumeProcess.argtypes)
            self.assertEqual(kernel.AssignProcessToJobObject.calls[0][0].value, capability.handle)
            self.assertGreater(kernel.AssignProcessToJobObject.calls[0][1].value, 0xFFFFFFFF)
            facade.close(capability)
            self.assertTrue(capability.closed)
            self.assertEqual(kernel.CloseHandle.calls[-1][0].value, capability.handle)

    async def test_windows_job_setup_does_not_require_private_transport(self) -> None:
        process = FakeProcess()
        process.pid = 9501
        facade = FakeWindowsJobFacade()
        self.manager._windows_job_facade = facade
        self.service.wait_port = False
        self.service.port = None
        self.manager._read_output = hold_output
        with patch.object(process_module.os, "name", "nt"), patch.object(
            asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)
        ):
            await self.manager.start_service("svc")
        self.assertEqual(self.manager.states["svc"].status, ServiceStatus.RUNNING)
        self.assertIn(("resume", 9501), facade.events)

    async def test_windows_job_setup_failure_retains_unreaped_leader(self) -> None:
        class TimeoutProcess(FakeProcess):
            async def wait(self):
                raise asyncio.TimeoutError

            def kill(self):
                self.terminated = True

        process = TimeoutProcess()
        process.pid = 9601
        facade = FakeWindowsJobFacade(fail_create=True)
        self.manager._windows_job_facade = facade
        self.service.wait_port = False
        self.service.port = None
        with patch.object(process_module.os, "name", "nt"), patch.object(
            asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)
        ):
            await self.manager.start_service("svc")
        state = self.manager.states["svc"]
        self.assertEqual(state.status, ServiceStatus.ERROR)
        self.assertIs(state.process, process)
        self.assertEqual(state.pid, 9601)

    async def test_windows_job_setup_failure_reap_clears_after_confirmed_wait(self) -> None:
        process = FakeProcess()
        process.pid = 9701
        facade = FakeWindowsJobFacade(fail_create=True)
        self.manager._windows_job_facade = facade
        self.service.wait_port = False
        self.service.port = None
        with patch.object(process_module.os, "name", "nt"), patch.object(
            asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)
        ):
            await self.manager.start_service("svc")
        state = self.manager.states["svc"]
        self.assertEqual(state.status, ServiceStatus.ERROR)
        self.assertIsNone(state.process)
        self.assertIsNone(state.pid)

    async def test_windows_job_close_failure_is_surfaced_without_marking_closed(self) -> None:
        kernel = _FakeWin32Library()
        kernel.CloseHandle.result = 0
        ntdll = _FakeWin32Library()
        with patch.object(process_module.os, "name", "nt"), patch.object(
            process_module.ctypes, "WinDLL", side_effect=[kernel, ntdll], create=True
        ), patch.object(
            process_module.ctypes, "get_last_error", return_value=5, create=True
        ):
            facade = process_module._WindowsJobFacade()
            capability = process_module._WindowsJobCapability(0x100001234)
            with self.assertRaises(OSError) as raised:
                facade.close(capability)
            self.assertEqual(raised.exception.errno, 5)
            self.assertFalse(capability.closed)

    async def test_windows_job_temporary_handle_close_failure_keeps_job_cleanup_ownership(self) -> None:
        kernel = _FakeWin32Library()
        close_results = iter((0, 0))
        kernel.CloseHandle.side_effect = lambda *_args: next(close_results)
        ntdll = _FakeWin32Library()
        with patch.object(process_module.os, "name", "nt"), patch.object(
            process_module.ctypes, "WinDLL", side_effect=[kernel, ntdll], create=True
        ), patch.object(
            process_module.ctypes, "get_last_error", return_value=5, create=True
        ):
            facade = process_module._WindowsJobFacade()
            with self.assertRaises(OSError) as raised:
                facade.create_assign_resume(9451)

        error = raised.exception
        capability = getattr(error, "windows_job", None)
        self.assertIsNotNone(capability)
        self.assertFalse(capability.closed)
        self.assertGreaterEqual(len(kernel.TerminateJobObject.calls), 1)
        self.assertEqual(len(kernel.CloseHandle.calls), 2)

    async def test_windows_second_wait_confirms_delayed_exit(self) -> None:
        process = FakeProcess()
        process.pid = 7001
        parent = FakePsutilProcess(7001)
        calls = 0

        def wait_procs(procs, timeout):
            nonlocal calls
            calls += 1
            if calls == 2:
                parent.running = False
            return ([], [parent] if parent.running else [])

        with (
            patch.object(process_module.os, "name", "nt"),
            patch.object(process_module.psutil, "Process", return_value=parent),
            patch.object(process_module.psutil, "wait_procs", side_effect=wait_procs),
        ):
            state = process_module.ServiceState(status=ServiceStatus.RUNNING, process=process, pid=7001)
            self.manager.states["svc"] = state
            await self.manager.stop_service("svc")

        self.assertEqual(state.status, ServiceStatus.STOPPED)
        self.assertIsNone(state.process)
        self.assertIsNone(state.pid)

    async def test_windows_access_denied_verification_is_error_not_stopped(self) -> None:
        process = FakeProcess()
        process.pid = 8001
        parent = FakePsutilProcess(8001)
        parent.is_running = lambda: (_ for _ in ()).throw(process_module.psutil.AccessDenied(8001))
        with (
            patch.object(process_module.os, "name", "nt"),
            patch.object(process_module.psutil, "Process", return_value=parent),
            patch.object(process_module.psutil, "wait_procs", return_value=([], [])),
        ):
            state = process_module.ServiceState(status=ServiceStatus.RUNNING, process=process, pid=8001)
            self.manager.states["svc"] = state
            with self.assertRaises(RuntimeError):
                await self.manager.stop_service("svc")

        self.assertEqual(state.status, ServiceStatus.ERROR)
        self.assertIs(state.process, process)


async def _readline_eof(self):
    return b""


if __name__ == "__main__":
    unittest.main()
