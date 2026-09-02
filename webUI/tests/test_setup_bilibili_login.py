from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.responses import JSONResponse

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import server

from setup_bilibili_login import (
    BilibiliLoginCleanupError,
    BilibiliLoginManager,
    BilibiliLoginProcessError,
)


PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
)


class _FakeProcess:
    def __init__(self, return_code: int | None = None) -> None:
        self.returncode = None
        self._return_code = return_code
        self._finished = asyncio.Event()
        self.terminated = False
        if return_code is not None:
            self._finished.set()

    def terminate(self) -> None:
        self.terminated = True
        self._return_code = self._return_code if self._return_code is not None else -15
        self._finished.set()

    def kill(self) -> None:
        self.terminated = True
        self._return_code = self._return_code if self._return_code is not None else -9
        self._finished.set()

    async def wait(self) -> int:
        await self._finished.wait()
        self.returncode = self._return_code if self._return_code is not None else 0
        return self.returncode


class _NeverExitsProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> None:
        await asyncio.Future()


class _RouteManager:
    def __init__(self, blocked_error=None) -> None:
        self.blocked_error = blocked_error
        self.events: list[str] = []
        self.status_result = {"status": "waiting", "job_id": "current"}

    @asynccontextmanager
    async def config_generation(self):
        self.events.append("enter")
        if self.blocked_error is not None:
            raise self.blocked_error("lifecycle blocked")
        try:
            yield
        finally:
            self.events.append("exit")

    def mark_config_generation(self, result) -> None:
        self.events.append("mark")

    async def status(self, job_id: str):
        self.events.append(f"status:{job_id}")
        return self.status_result


class BilibiliLoginManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        adapter = self.root / "NachoBot-Bilibili-Adapter"
        adapter.mkdir()
        python_dir = adapter / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        python_dir.mkdir(parents=True)
        python_name = "python.exe" if os.name == "nt" else "python"
        (python_dir / python_name).write_bytes(b"test interpreter")
        (adapter / "config.toml").write_text(
            "[bilibili]\nsessdata = \" \"\nbili_jct = \" \"\ndede_user_id = \" \"\n",
            encoding="utf-8",
        )
        (adapter / "qr_login.py").write_text("# fixed test helper\n", encoding="utf-8")
        self.processes: list[_FakeProcess] = []

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def manager(self, *return_codes: int | None) -> BilibiliLoginManager:
        codes = list(return_codes)

        async def factory(*args, **kwargs):
            process = _FakeProcess(codes.pop(0) if codes else None)
            self.processes.append(process)
            self.factory_args = (args, kwargs)
            return process

        manager = BilibiliLoginManager(
            self.root,
            process_factory=factory,
            poll_interval=0.02,
            stop_timeout=0.2,
        )
        manager.mark_config_generation(True)
        manager.mark_dependency_task("bilibili", "ok")
        return manager

    async def wait_for_status(self, manager, job_id: str, expected: str) -> dict:
        for _ in range(100):
            status = await manager.status(job_id)
            if status and status["status"] == expected:
                return status
            await asyncio.sleep(0.005)
        self.fail(f"status did not become {expected}")

    def test_retry_replaces_job_and_cleans_stale_image(self) -> None:
        async def scenario() -> None:
            manager = self.manager()
            first = await manager.start()
            await asyncio.sleep(0.01)
            manager.qr_path.write_bytes(PNG)
            self.assertEqual(await manager.read_qr(first["job_id"]), PNG)

            second = await manager.start()
            self.assertNotEqual(first["job_id"], second["job_id"])
            self.assertIsNone(await manager.status(first["job_id"]))
            self.assertIsNone(await manager.read_qr(first["job_id"]))
            self.assertFalse(manager.qr_path.exists())
            self.assertTrue(self.processes[0].terminated)
            await manager.shutdown()

        asyncio.run(scenario())

    def test_command_uses_fixed_adapter_helper_and_discards_child_output(self) -> None:
        async def scenario() -> None:
            manager = self.manager()
            await manager.start()
            await asyncio.sleep(0.01)
            args, kwargs = self.factory_args
            self.assertEqual(
                args,
                (
                    str(manager.python_path),
                    str(manager.script_path),
                    "--config",
                    str(manager.config_path),
                    "--qr-output",
                    str(manager.qr_path),
                ),
            )
            self.assertEqual(kwargs["cwd"], str(manager.adapter_dir))
            self.assertEqual(kwargs["stdout"], asyncio.subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], asyncio.subprocess.DEVNULL)
            self.assertNotIn("shell", kwargs)
            await manager.shutdown()

        asyncio.run(scenario())

    def test_missing_adapter_interpreter_fails_closed_before_spawning(self) -> None:
        async def scenario() -> None:
            manager = self.manager()
            manager.python_path.unlink()
            with self.assertRaises(RuntimeError):
                await manager.start()
            self.assertEqual(self.processes, [])
            self.assertIsNone(await manager.status("not-started"))
            await manager.shutdown()

        asyncio.run(scenario())

    def test_cleanup_failure_keeps_stale_qr_unserved_and_blocks_retry(self) -> None:
        async def scenario() -> None:
            manager = self.manager()
            first = await manager.start()
            await asyncio.sleep(0.01)
            manager.qr_path.write_bytes(PNG)

            real_unlink = Path.unlink

            def fail_qr_unlink(path, *args, **kwargs):
                if path == manager.qr_path:
                    raise OSError("simulated cleanup refusal")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(
                Path, "unlink", autospec=True, side_effect=fail_qr_unlink
            ):
                with self.assertRaises(BilibiliLoginCleanupError):
                    await manager.start()

            self.assertEqual(len(self.processes), 1)
            self.assertTrue(self.processes[0].terminated)
            self.assertTrue(manager.qr_path.exists())
            self.assertEqual(manager.qr_path.read_bytes(), PNG)
            self.assertIsNone(await manager.status(first["job_id"]))
            self.assertIsNone(await manager.read_qr("replacement-job"))
            self.assertTrue(manager.cleanup_failed)
            with mock.patch.object(
                Path, "unlink", autospec=True, side_effect=fail_qr_unlink
            ):
                await manager.shutdown()
            self.assertTrue(manager.cleanup_failed)

        asyncio.run(scenario())

    def test_config_generation_refuses_write_when_qr_cleanup_fails(self) -> None:
        async def scenario() -> None:
            manager = self.manager()
            first = await manager.start()
            await asyncio.sleep(0.01)
            manager.qr_path.write_bytes(PNG)

            real_unlink = Path.unlink

            def fail_qr_unlink(path, *args, **kwargs):
                if path == manager.qr_path:
                    raise OSError("simulated cleanup refusal")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(
                Path, "unlink", autospec=True, side_effect=fail_qr_unlink
            ):
                with self.assertRaises(BilibiliLoginCleanupError):
                    async with manager.config_generation():
                        self.fail("config generation entered after cleanup failure")

            self.assertEqual(len(self.processes), 1)
            self.assertTrue(self.processes[0].terminated)
            self.assertTrue(manager.qr_path.exists())
            self.assertEqual(manager.qr_path.read_bytes(), PNG)
            self.assertIsNone(await manager.status(first["job_id"]))
            self.assertIsNone(await manager.read_qr("replacement-job"))
            await manager.shutdown()

        asyncio.run(scenario())

    def test_unproven_process_exit_blocks_retry_and_config_generation(self) -> None:
        async def scenario() -> None:
            stubborn = _NeverExitsProcess()
            spawn_count = 0

            async def factory(*args, **kwargs):
                nonlocal spawn_count
                spawn_count += 1
                return stubborn

            manager = BilibiliLoginManager(
                self.root,
                process_factory=factory,
                poll_interval=0.02,
                stop_timeout=0.1,
            )
            manager.mark_config_generation(True)
            manager.mark_dependency_task("bilibili", "ok")
            first = await manager.start()
            await self.wait_for_status(manager, first["job_id"], "waiting")
            manager.qr_path.write_bytes(PNG)
            original_config = manager.config_path.read_bytes()

            with self.assertRaises(BilibiliLoginProcessError):
                await manager.start()
            self.assertEqual(spawn_count, 1)
            self.assertGreater(stubborn.terminate_calls, 0)
            self.assertGreater(stubborn.kill_calls, 0)
            failed = await manager.status(first["job_id"])
            self.assertIsNotNone(failed)
            self.assertEqual(failed["status"], "error")
            self.assertTrue(failed["lifecycle_failed"])
            self.assertIsNone(await manager.read_qr(first["job_id"]))
            self.assertEqual(manager.qr_path.read_bytes(), PNG)

            with self.assertRaises(BilibiliLoginProcessError):
                async with manager.config_generation():
                    self.fail("config generation entered after unproven process exit")
            self.assertEqual(spawn_count, 1)
            self.assertEqual(manager.config_path.read_bytes(), original_config)
            await manager.shutdown()
            self.assertTrue(manager.lifecycle_failed)

        asyncio.run(scenario())

    def test_nonzero_exit_and_zero_exit_without_credentials_are_failures(self) -> None:
        async def scenario() -> None:
            failed_manager = self.manager(2)
            failed = await failed_manager.start()
            status = await self.wait_for_status(failed_manager, failed["job_id"], "error")
            self.assertTrue(status["retryable"])
            await failed_manager.shutdown()

            missing_manager = self.manager(0)
            missing = await missing_manager.start()
            status = await self.wait_for_status(missing_manager, missing["job_id"], "error")
            self.assertTrue(status["retryable"])
            await missing_manager.shutdown()

        asyncio.run(scenario())

    def test_success_requires_all_credentials_and_does_not_return_values(self) -> None:
        async def scenario() -> None:
            config = self.root / "NachoBot-Bilibili-Adapter/config.toml"
            config.write_text(
                "[bilibili]\nsessdata = \"fixture-a\"\nbili_jct = \"fixture-b\"\ndede_user_id = \"fixture-c\"\n",
                encoding="utf-8",
            )
            manager = self.manager(0)
            job = await manager.start()
            status = await self.wait_for_status(manager, job["job_id"], "success")
            self.assertNotIn("fixture-a", str(status))
            self.assertNotIn("fixture-b", str(status))
            self.assertNotIn("fixture-c", str(status))
            await manager.shutdown()

        asyncio.run(scenario())

    def test_shutdown_cancels_pending_child_and_cleans_qr(self) -> None:
        async def scenario() -> None:
            manager = self.manager()
            await manager.start()
            await asyncio.sleep(0.01)
            manager.qr_path.write_bytes(PNG)
            await asyncio.wait_for(manager.shutdown(), timeout=1)
            self.assertTrue(self.processes[0].terminated)
            self.assertFalse(manager.qr_path.exists())

        asyncio.run(scenario())

    def test_config_generation_context_stops_active_job_before_write_window(self) -> None:
        async def scenario() -> None:
            manager = self.manager()
            job = await manager.start()
            await asyncio.sleep(0.01)
            manager.qr_path.write_bytes(PNG)
            async with manager.config_generation():
                self.assertIsNone(await manager.status(job["job_id"]))
                self.assertTrue(self.processes[0].terminated)
                self.assertFalse(manager.qr_path.exists())
                # The caller owns the actual config write while this short
                # lifecycle lock is held.
                manager.config_path.write_text(
                    "[bilibili]\nsessdata = \" \"\nbili_jct = \" \"\ndede_user_id = \" \"\n",
                    encoding="utf-8",
                )
            await manager.shutdown()

        asyncio.run(scenario())


class BilibiliSetupRouteTests(unittest.TestCase):
    def test_non_bilibili_generation_is_serialized_before_writes(self) -> None:
        async def scenario() -> None:
            manager = _RouteManager()
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "generated.marker"

                def generate(data):
                    manager.events.append("write")
                    target.write_text("written", encoding="utf-8")
                    return {"generated": [], "errors": []}

                body = server.SetupWizardData(components=["core"])
                with mock.patch.object(
                    server, "bilibili_login_manager", manager
                ), mock.patch.object(
                    server.ConfigInitializer,
                    "generate_configs",
                    side_effect=generate,
                ):
                    result = await server.setup_generate_configs(body)

                self.assertEqual(result["errors"], [])
                self.assertEqual(manager.events, ["enter", "write", "exit", "mark"])
                self.assertEqual(target.read_text(encoding="utf-8"), "written")

        asyncio.run(scenario())

    def test_lifecycle_failure_blocks_non_bilibili_generation(self) -> None:
        async def scenario() -> None:
            for blocked_error in (BilibiliLoginCleanupError, BilibiliLoginProcessError):
                manager = _RouteManager(blocked_error)
                generate = mock.Mock(
                    return_value={"generated": [], "errors": []}
                )
                body = server.SetupWizardData(components=["core"])
                with mock.patch.object(
                    server, "bilibili_login_manager", manager
                ), mock.patch.object(
                    server.ConfigInitializer,
                    "generate_configs",
                    generate,
                ):
                    with self.assertRaises(HTTPException) as raised:
                        await server.setup_generate_configs(body)

                self.assertEqual(raised.exception.status_code, 500)
                generate.assert_not_called()
                self.assertEqual(manager.events, ["enter"])

        asyncio.run(scenario())

    def test_bilibili_status_routes_are_no_store_for_current_and_stale_jobs(self) -> None:
        async def scenario() -> None:
            manager = _RouteManager()
            endpoints = (
                lambda: server.setup_bilibili_login_status("current"),
                lambda: server.setup_bilibili_login_status_query(job_id="current"),
            )
            with mock.patch.object(server, "bilibili_login_manager", manager):
                for endpoint in endpoints:
                    manager.status_result = {"status": "waiting", "job_id": "current"}
                    response = await endpoint()
                    self.assertIsInstance(response, JSONResponse)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["cache-control"], "no-store")

                    manager.status_result = None
                    with self.assertRaises(HTTPException) as raised:
                        await endpoint()
                    self.assertEqual(raised.exception.status_code, 404)
                    self.assertEqual(
                        raised.exception.headers["Cache-Control"], "no-store"
                    )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
