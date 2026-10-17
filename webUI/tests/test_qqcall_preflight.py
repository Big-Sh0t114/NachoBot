import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from webUI import qqcall_preflight as preflight
from webUI.process_manager import ProcessManager


class DeploymentPreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def installed_files(self):
        for name in (
            "NachoBot-UniversalVC-Adapter/.venv/Scripts/python.exe",
            "NachoBot-UniversalVC-Adapter/check_deployment.py",
            "NachoBot-UniversalVC-Adapter/service_main.py",
            "NachoBot/.venv/Scripts/python.exe",
            "NachoBot-QQCall/scripts/prepare-audio-route.py",
        ):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()

    async def test_missing_environments_block_without_launch(self):
        with patch.object(preflight, "_probe", new_callable=AsyncMock) as probe:
            issues = await preflight.check_qqcall_deployment(self.root)
        self.assertEqual(len(issues), 2)
        probe.assert_not_awaited()
        self.assertIn("部署向导", preflight.deployment_error(issues))

    async def test_valid_environment_and_route_pass(self):
        self.installed_files()
        with patch.object(preflight, "_probe", new_callable=AsyncMock) as probe:
            probe.side_effect = [{"ok": True, "issues": []},
                                 {"ok": True, "phase": "existing_guard_valid"}]
            self.assertEqual(await preflight.check_qqcall_deployment(self.root), [])
        self.assertIn("UniversalVC", str(probe.call_args_list[0].args[0]))
        self.assertIn("NachoBot", str(probe.call_args_list[1].args[0]))

    async def test_multiple_missing_components_are_reported_together(self):
        self.installed_files()
        with patch.object(preflight, "_probe", new_callable=AsyncMock) as probe:
            probe.side_effect = [{"ok": False, "issues": ["vad_model_missing", "speaker_model_missing"]},
                                 {"ok": False, "code": "audio_route_selection_required"}]
            issues = await preflight.check_qqcall_deployment(self.root)
        self.assertEqual(len(issues), 3)
        self.assertIn("VAD", issues[0])
        self.assertIn("虚拟声卡", issues[2])

    async def test_missing_guard_and_invalid_models_block(self):
        self.installed_files()
        with patch.object(preflight, "_probe", new_callable=AsyncMock) as probe:
            probe.side_effect = [{"ok": False, "issues": ["vad_model_invalid"]},
                                 {"ok": True, "phase": "guard_prepared"}]
            issues = await preflight.check_qqcall_deployment(self.root)
        self.assertEqual(len(issues), 2)
        self.assertIn("--apply", issues[1])

    async def test_probe_failures_and_untrusted_output_fail_closed(self):
        self.installed_files()
        for result in (asyncio.TimeoutError(), {"ok": True, "issues": ["secret-token"]},
                       {"ok": False, "issues": []}):
            with self.subTest(result=result), patch.object(preflight, "_probe", new_callable=AsyncMock) as probe:
                probe.side_effect = [result, {"ok": True, "phase": "existing_guard_valid"}]
                issues = await preflight.check_qqcall_deployment(self.root)
                self.assertEqual(len(issues), 1)
                self.assertNotIn("secret-token", issues[0])

    async def test_timeout_kills_only_probe(self):
        child = AsyncMock()
        child.returncode = None
        async def hang():
            await asyncio.sleep(60)
        child.communicate.side_effect = hang
        from unittest.mock import Mock
        child.kill = Mock()
        with patch.object(asyncio, "create_subprocess_exec", return_value=child):
            with self.assertRaises(asyncio.TimeoutError):
                await preflight._probe(Path("python"), Path("check.py"), timeout=0.01)
        child.kill.assert_called_once()
        child.wait.assert_awaited_once()

    async def test_injection_does_not_launch_bat_on_failure(self):
        manager = ProcessManager.__new__(ProcessManager)
        manager.root = self.root
        manager._broadcast = AsyncMock()
        with patch.object(preflight, "check_qqcall_deployment", new_callable=AsyncMock) as check, \
                patch.object(asyncio, "create_subprocess_exec", new_callable=AsyncMock) as launch:
            check.return_value = ["缺少 VAD 模型"]
            with self.assertRaisesRegex(RuntimeError, "部署或修复 Universal"):
                await manager._run_qqcall_injection(self.root / "start-qqcall.bat")
        launch.assert_not_awaited()
        self.assertIn("缺少 VAD", manager._qqcall_preflight_error)
        self.assertIn("ERROR", manager._broadcast.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
