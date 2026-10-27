from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from multimodal_runtime import MultimodalRuntimeManager as Manager  # noqa: E402


class RuntimeDependencyIntegrityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dependency integrity ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in (("ADAPTER_DIR", self.root), ("RUNTIME_DIR", self.root / ".runtime"),
                            ("_installing", set())):
            patcher = patch.object(Manager, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for profile in ("gpu", "cpu"):
            python = Manager.python_path(profile)
            python.parent.mkdir(parents=True)
            python.touch()
            site = Manager.env_dir(profile) / "Lib" / "site-packages"
            for package in ("torch", "transformers", "timm", "sherpa_onnx"):
                (site / package).mkdir(parents=True)
            Manager._marker_path(profile).write_text(json.dumps({"schema": 2, "profile": profile}))
        (self.root / "pyproject.toml").write_text('[project]\nname="test"\nversion="1"\n')
        (self.root / "uv.lock").touch()

    def test_existing_python_and_four_package_folders_are_not_proof(self):
        with patch("multimodal_runtime.subprocess.run", return_value=SimpleNamespace(returncode=1)) as run:
            status = Manager.get_status("gpu")
            self.assertFalse(status["installed"])
            with self.assertRaises(RuntimeError):
                Manager.require_python("gpu")
        command = run.call_args.args[0]
        for flag in ("--check", "--locked", "--offline", "--no-python-downloads"):
            self.assertIn(flag, command)
        self.assertEqual(run.call_args.kwargs["env"]["UV_PROJECT_ENVIRONMENT"], str(Manager.env_dir("gpu")))

    def test_complete_locked_environment_is_ready(self):
        with patch("multimodal_runtime.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            self.assertTrue(Manager.get_status("gpu")["installed"])

    def test_legacy_environment_also_requires_full_check(self):
        Manager._marker_path("gpu").unlink()
        with patch("multimodal_runtime.subprocess.run", return_value=SimpleNamespace(returncode=1)) as run:
            self.assertFalse(Manager.get_status("gpu")["installed"])
            run.assert_called_once()

    def test_failed_and_installing_environments_are_not_ready(self):
        for state in ("failed", "installing"):
            Manager._marker_path("gpu").write_text(json.dumps({"schema": 2, "profile": "gpu", "state": state}))
            with patch.object(Manager, "_check_dependencies") as check:
                self.assertFalse(Manager.get_status("gpu")["installed"])
                check.assert_not_called()

    def test_cpu_uses_its_own_manifest_lock_and_environment(self):
        project = self.root / ".runtime" / "webui-cpu"
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text("cpu-project")
        (project / "uv.lock").touch()
        (self.root / "pyproject.toml.cpu").write_text("cpu-project")
        with patch("multimodal_runtime.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(Manager.get_status("cpu")["installed"])
            self.assertIn("--no-install-project", run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["cwd"], project)
            self.assertEqual(run.call_args.kwargs["env"]["UV_PROJECT_ENVIRONMENT"], str(Manager.env_dir("cpu")))
        (self.root / "pyproject.toml.cpu").write_text("new-spec")
        with patch("multimodal_runtime.subprocess.run") as run:
            self.assertFalse(Manager.get_status("cpu")["installed"])
            run.assert_not_called()

    def test_check_timeout_is_not_ready(self):
        with patch("multimodal_runtime.subprocess.run", side_effect=subprocess.TimeoutExpired("uv", 15)):
            self.assertFalse(Manager.get_status("gpu")["installed"])

    def test_first_import_uses_cold_start_budget_and_isolated_python(self):
        result = SimpleNamespace(returncode=0, stdout="runtime-check: torch\nruntime-ok torch=CUDA\n")
        with patch("multimodal_runtime.subprocess.run", return_value=result) as run:
            ok, message = Manager._validate_runtime("gpu")
        self.assertTrue(ok)
        self.assertEqual(message, "runtime-ok torch=CUDA")
        self.assertEqual(run.call_args.kwargs["timeout"], 300)
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.root))
        self.assertIn("-I", run.call_args.args[0])

    def test_import_timeout_names_last_stage_and_is_not_success(self):
        error = subprocess.TimeoutExpired("python", 300, output=b"runtime-check: torch\nruntime-check: timm\n")
        with patch("multimodal_runtime.subprocess.run", side_effect=error):
            ok, message = Manager._validate_runtime("gpu")
        self.assertFalse(ok)
        self.assertIn("300", message)
        self.assertIn("timm", message)

    def test_real_import_error_is_returned_without_retry(self):
        result = SimpleNamespace(returncode=1, stdout="ImportError: DLL load failed")
        with patch("multimodal_runtime.subprocess.run", return_value=result) as run:
            ok, message = Manager._validate_runtime("gpu")
        self.assertFalse(ok)
        self.assertIn("DLL load failed", message)
        self.assertEqual(run.call_count, 1)

    async def test_cold_validation_waits_and_reports_progress_before_marking_ready(self):
        frames = []

        async def callback(line):
            frames.append(line)

        def cold_import(profile):
            time.sleep(.08)
            return True, "runtime-ok"

        with (
            patch.object(Manager, "VALIDATION_PROGRESS_INTERVAL", .01),
            patch.object(Manager, "_prepare_project", return_value=self.root),
            patch.object(Manager, "_validate_runtime", side_effect=cold_import),
            patch("multimodal_runtime.run_install_command", new_callable=AsyncMock, return_value=0),
        ):
            result = await Manager.install("gpu", callback)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(any("验证仍在进行" in line for line in frames))
        self.assertTrue(Manager._marker_valid("gpu"))

    async def test_validation_timeout_never_marks_environment_ready(self):
        with (
            patch.object(Manager, "_prepare_project", return_value=self.root),
            patch.object(Manager, "_validate_runtime", return_value=(False, "依赖导入验证超时")),
            patch("multimodal_runtime.run_install_command", new_callable=AsyncMock, return_value=0),
        ):
            result = await Manager.install("gpu")
        self.assertEqual(result["status"], "error")
        self.assertFalse(Manager._marker_valid("gpu"))

    async def test_failed_install_can_retry_sync_despite_existing_environment(self):
        frames = []

        async def on_frame(frame):
            frames.append(frame)

        with (
            patch.object(Manager, "_prepare_project", return_value=self.root),
            patch.object(Manager, "_validate_runtime", return_value=(True, "runtime-ok")),
            patch("multimodal_runtime.run_install_command", new_callable=AsyncMock, side_effect=[9, 0]) as run,
        ):
            failed = await Manager.install("gpu", on_frame)
            self.assertEqual(failed["status"], "error")
            self.assertEqual(json.loads(Manager._marker_path("gpu").read_text())["state"], "failed")
            self.assertFalse(Manager.get_status("gpu")["installed"])
            succeeded = await Manager.install("gpu", on_frame)
            self.assertEqual(succeeded["status"], "ok")
            self.assertEqual(run.await_count, 2)
            self.assertTrue(Manager._marker_valid("gpu"))
        self.assertTrue(any("uv sync" in line for line in frames))

    @unittest.skipUnless(shutil.which("uv"), "uv is required")
    def test_real_uv_check_detects_missing_dependency_without_mutation(self):
        # Use a tiny project and a real venv; no downloads, shared cache, or live
        # GPU/CPU environments are modified by this verification.
        project = self.root / "check-project"
        project.mkdir()
        manifest = project / "pyproject.toml"
        wheel = project / "integrity_dep-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("integrity_dep.py", "VALUE = 1\n")
            archive.writestr("integrity_dep-1.0.dist-info/METADATA",
                             "Metadata-Version: 2.1\nName: integrity-dep\nVersion: 1.0\n")
            archive.writestr("integrity_dep-1.0.dist-info/WHEEL",
                             "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr("integrity_dep-1.0.dist-info/RECORD", "")
        manifest.write_text(
            '[project]\nname="integrity-fixture"\nversion="1.0"\nrequires-python=">=3.11"\n'
            'dependencies=["integrity-dep==1.0"]\n'
            '[tool.uv.sources]\nintegrity-dep={path="integrity_dep-1.0-py3-none-any.whl"}\n'
        )
        env_dir = project / ".venv"
        env = {key: value for key, value in os.environ.items() if key not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}}
        for command in (
            ["uv", "venv", str(env_dir), "--python", sys.executable],
            ["uv", "sync", "--offline", "--python", sys.executable],
        ):
            subprocess.run(command, cwd=project, env=env, capture_output=True, check=True)
        with patch.object(Manager, "ADAPTER_DIR", project):
            before = {p.relative_to(project): (p.stat().st_size, p.stat().st_mtime_ns)
                      for p in project.rglob("*") if p.is_file() and p.name != ".lock"}
            self.assertTrue(Manager._check_dependencies("gpu")[0])
            after = {p.relative_to(project): (p.stat().st_size, p.stat().st_mtime_ns)
                     for p in project.rglob("*") if p.is_file() and p.name != ".lock"}
            self.assertEqual(before, after)
            subprocess.run(["uv", "pip", "uninstall", "--python", str(Manager.python_path("gpu")), "integrity-dep"],
                           cwd=project, env=env, capture_output=True, check=True)
            self.assertFalse(Manager._check_dependencies("gpu")[0])


if __name__ == "__main__":
    unittest.main()
