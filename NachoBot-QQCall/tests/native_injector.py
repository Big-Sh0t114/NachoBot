from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
INJECTOR_PATH = ROOT / "scripts" / "qqcall_injector.py"
SPEC = importlib.util.spec_from_file_location("qqcall_injector", INJECTOR_PATH)
assert SPEC and SPEC.loader
injector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = injector
SPEC.loader.exec_module(injector)


def write_config(folder: Path, pid: int, *, overrides: dict[str, object] | None = None) -> Path:
    bootstrap = folder / "bootstrap.cjs"
    bootstrap.write_text("exports.bootstrap = () => {};\n", encoding="utf-8")
    config: dict[str, object] = {
        "modulePath": str(bootstrap),
        "appPath": str(folder),
        "reportPath": str(folder / "report"),
        "pipeName": rf"\\.\pipe\nachobot-qqcall-{pid}-0123456789abcdef0123456789abcdef",
        "sessionName": "native_test",
        "connectionGeneration": 7,
        "ipcToken": "A" * 43,
    }
    if overrides:
        config.update(overrides)
    path = folder / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class FakeKernel32:
    def __init__(self, wait_result: int):
        self.wait_result = wait_result
        self.closed: list[object] = []

    def CreateRemoteThread(self, *_args: object) -> int:
        return 91

    def WaitForSingleObject(self, *_args: object) -> int:
        return self.wait_result

    def GetExitCodeThread(self, _thread: object, output: object) -> int:
        output._obj.value = 0
        return 1

    def CloseHandle(self, handle: object) -> int:
        self.closed.append(handle)
        return 1


class FakeApi:
    def __init__(self, wait_result: int, shim_path: str):
        self.k32 = FakeKernel32(wait_result)
        self.shim_path = shim_path
        self.freed: list[int] = []
        self.allocations: list[tuple[int, int]] = []

    def alloc(self, _handle: object, size: int) -> int:
        address = 0x700000
        self.allocations.append((address, size))
        return address

    def write_memory(self, *_args: object) -> None:
        return None

    def remote_thread(self, *args: object) -> int:
        return injector.WinApi.remote_thread(self, *args)

    def free(self, _handle: object, address: int) -> None:
        self.freed.append(address)

    def modules(self, _pid: int) -> list[object]:
        return [injector.ModuleInfo("QQCallShim.dll", self.shim_path, 0x180000000, 0x30000)]


class InjectorRegressionTests(unittest.TestCase):
    def test_recovery_flag_selects_exact_module_without_changing_default(self) -> None:
        production_path = r"C:\app\native\build\x64\QQCallShim.dll"
        recovery_path_1 = r"C:\app\native\build\x64\QQCallRecoveryShim.dll"
        recovery_path_2 = r"C:\app\native\build\x64\QQCallRecoveryShim2.dll"
        recovery_1 = injector.ModuleInfo("QQCallRecoveryShim.dll", recovery_path_1, 0x180000000, 0x30000)
        recovery_2 = injector.ModuleInfo("QQCallRecoveryShim2.dll", recovery_path_2, 0x188000000, 0x30000)
        production = injector.ModuleInfo("QQCallShim.dll", production_path, 0x190000000, 0x30000)

        class StatusApi:
            def modules(self, _pid: int) -> list[object]:
                # Put both recovery instances first so selection cannot pass by order.
                return [recovery_2, recovery_1, production]

            def alloc(self, _handle: object, _size: int) -> int:
                return 0x710000

            def write_memory(self, *_args: object) -> None:
                return None

            def remote_thread(self, *_args: object) -> int:
                return 0

            def read_memory(self, _handle: object, _address: int, size: int) -> bytes:
                self.assert_status_size(size)
                return struct.pack("<6IQ", 32, 1, 0, 0, 2468, 0, 7)

            @staticmethod
            def assert_status_size(size: int) -> None:
                if size != 32:
                    raise AssertionError(f"unexpected read size: {size}")

            def free(self, _handle: object, _address: int) -> None:
                return None

        api = StatusApi()
        resolved: list[tuple[object, str, str]] = []

        def resolve(_reader: object, modules: list[object], module_name: str, symbol: str, **_kwargs: object) -> int:
            resolved.append((modules[0], module_name, symbol))
            return 0x180001000

        with mock.patch.object(injector, "resolve_remote_export", side_effect=resolve):
            recovery_1_status = injector.query_status(api, ctypes.c_void_p(1), 2468, 1000, recovery_path_1)
            recovery_2_status = injector.query_status(api, ctypes.c_void_p(1), 2468, 1000, recovery_path_2)
            production_status = injector.query_status(api, ctypes.c_void_p(1), 2468, 1000)

        self.assertTrue(recovery_1_status["loaded"])
        self.assertTrue(recovery_2_status["loaded"])
        self.assertTrue(production_status["loaded"])
        self.assertEqual(resolved, [
            (recovery_1, "QQCallRecoveryShim.dll", "QQCallShimGetStatus"),
            (recovery_2, "QQCallRecoveryShim2.dll", "QQCallShimGetStatus"),
            (production, "QQCallShim.dll", "QQCallShimGetStatus"),
        ])
        self.assertEqual(injector._selected_shim_path(True, 1).name, "QQCallRecoveryShim.dll")
        self.assertEqual(injector._selected_shim_path(True, 2).name, "QQCallRecoveryShim2.dll")
        self.assertEqual(injector._selected_shim_path(False).name, "QQCallShim.dll")
        with self.assertRaises(injector.ProbeError):
            injector._selected_shim_path(False, 2)

    def test_remote_kernel32_reader_binds_process_handle(self) -> None:
        handle = ctypes.c_void_p(0x1234)
        expected = b"PE-read"

        class ReaderApi:
            def __init__(self) -> None:
                self.calls: list[tuple[object, int, int]] = []

            def read_memory(self, actual_handle: object, address: int, size: int) -> bytes:
                self.calls.append((actual_handle, address, size))
                return expected

        api = ReaderApi()

        def inspect_reader(read: object, _modules: object, _module: str, _symbol: str, **_kwargs: object) -> bytes:
            return read(0x180001000, 7)

        with mock.patch.object(injector, "resolve_remote_export", side_effect=inspect_reader):
            result = injector._resolve_kernel32_loadlibrary(api, handle, [])
        self.assertEqual(result, expected)
        self.assertEqual(api.calls, [(handle, 0x180001000, 7)])

    def test_config_preflight_matches_agent_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            valid = write_config(folder, 2468)
            parsed = injector.validate_config(valid, 2468)
            self.assertEqual(parsed["sessionName"], "native_test")
            self.assertEqual(len(parsed["ipcToken"]), 43)

            invalid = (
                {"ipcToken": "A" * 24},
                {"pipeName": rf"\\.\pipe\nachobot-qqcall-2468-{'z' * 32}"},
                {"sessionName": "bad session"},
                {"sessionName": "s" * 81},
            )
            for changed in invalid:
                with self.subTest(changed=list(changed)):
                    config_path = write_config(folder, 2468, overrides=changed)
                    with self.assertRaises(injector.ProbeError):
                        injector.validate_config(config_path, 2468)

    def test_remote_timeout_and_wait_failed_retain_buffer_and_ini(self) -> None:
        if os.name != "nt":
            self.skipTest("the injector is Windows-only")

        shim_path = str(ROOT / "native" / "build" / "x64" / "QQCallShim.dll")
        self.assertTrue(Path(shim_path).is_file(), "build native/QQCallShim.dll before running launcher tests")
        target = injector.TargetIdentity(2468, r"C:\Program Files\Tencent\QQNT\QQ.exe", 123456789)

        for action, wait_result in (("inject", injector.WAIT_TIMEOUT), ("cancel", injector.WAIT_FAILED)):
            with self.subTest(action=action, wait_result=wait_result), tempfile.TemporaryDirectory() as temporary:
                config_path = write_config(Path(temporary), target.pid)
                api = FakeApi(wait_result, shim_path)
                handle = ctypes.c_void_p(0x55)
                with (
                    mock.patch.object(injector, "_load_winapi", return_value=api),
                    mock.patch.object(injector, "_require_qq_identity", return_value=(handle, target)),
                    mock.patch.object(injector, "_verify_handle_identity"),
                    mock.patch.object(injector, "resolve_remote_export", return_value=0x180001000),
                ):
                    with self.assertRaises(injector.RemoteTimeout) as caught:
                        if action == "inject":
                            injector.inject(target.pid, str(config_path), target.created_utc, 1000)
                        else:
                            injector.cancel(target.pid, str(config_path), target.created_utc, 1000)

                retained = caught.exception.retained_path
                self.assertIsNotNone(retained)
                self.assertTrue(Path(retained).is_file(), "timeout must retain the remote thread's config file")
                self.assertEqual(api.freed, [], "timeout or WAIT_FAILED must retain remote parameter memory")
                self.assertTrue(api.allocations)
                Path(retained).unlink(missing_ok=True)

    def test_confirmed_thread_exit_allows_buffer_cleanup(self) -> None:
        class CompletedApi:
            def __init__(self) -> None:
                self.freed: list[int] = []

            def alloc(self, _handle: object, _size: int) -> int:
                return 0x710000

            def write_memory(self, *_args: object) -> None:
                return None

            def remote_thread(self, *_args: object) -> int:
                return 0

            def free(self, _handle: object, address: int) -> None:
                self.freed.append(address)

        api = CompletedApi()
        result = injector._remote_call_with_buffer(api, ctypes.c_void_p(1), 0x180001000, b"x\0", 1000, "test")
        self.assertEqual(result, 0)
        self.assertEqual(api.freed, [0x710000])


if __name__ == "__main__":
    unittest.main()
