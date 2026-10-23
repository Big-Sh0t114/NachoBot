from __future__ import annotations

import contextlib
import importlib
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import tts_public_runtime as public_runtime


class TtsPublicRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)

    @contextlib.contextmanager
    def _patched_runtime(self, probe_codes, *, install_side_effect=None):
        root = Path(self._temp_dir.name)
        runtime_root = root / "runtime"
        python = runtime_root / "public-cpu" / ".venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True, exist_ok=True)
        codes = iter(probe_codes)

        def run_probe(*args, **kwargs):
            return types.SimpleNamespace(returncode=next(codes))

        probe = mock.Mock(side_effect=run_probe)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(public_runtime.manager, "RUNTIME_ROOT", runtime_root))
            stack.enter_context(mock.patch.object(public_runtime.manager, "ensure_venv", return_value=python))
            stack.enter_context(mock.patch.object(public_runtime.manager, "require_uv", return_value="uv"))
            install = stack.enter_context(
                mock.patch.object(public_runtime.manager, "run", side_effect=install_side_effect)
            )
            stack.enter_context(mock.patch.object(public_runtime.subprocess, "run", probe))
            yield python, public_runtime._ready_marker(python), install, probe

    @staticmethod
    def _execute_probe(versions, *, cuda=None):
        metadata_module = types.ModuleType("importlib.metadata")

        def metadata_version(name):
            return versions[name]

        metadata_module.version = metadata_version
        modules = {"importlib.metadata": metadata_module}
        for name in ("aiohttp", "fastapi", "loguru", "numpy", "scipy", "toml", "uvicorn"):
            modules[name] = types.ModuleType(name)
        cryptography = types.ModuleType("cryptography")
        cryptography.__path__ = []
        cryptography_fernet = types.ModuleType("cryptography.fernet")
        cryptography_fernet.Fernet = object
        modules["cryptography"] = cryptography
        modules["cryptography.fernet"] = cryptography_fernet
        pillow = types.ModuleType("PIL")
        pillow.__path__ = []
        pillow_image = types.ModuleType("PIL.Image")
        pillow.Image = pillow_image
        modules["PIL"] = pillow
        modules["PIL.Image"] = pillow_image
        transformers = types.ModuleType("transformers")
        transformers.AutoTokenizer = object
        transformers.AutoModelForSequenceClassification = object
        modules["transformers"] = transformers
        torch = types.ModuleType("torch")
        torch.__version__ = versions["torch"]
        torch.version = types.SimpleNamespace(cuda=cuda)
        modules["torch"] = torch

        previous_metadata = getattr(importlib, "metadata", None)
        importlib.metadata = metadata_module
        try:
            with mock.patch.dict(sys.modules, modules):
                exec(compile(public_runtime._PROBE, "<public-cpu-probe>", "exec"), {})
        finally:
            if previous_metadata is None:
                delattr(importlib, "metadata")
            else:
                importlib.metadata = previous_metadata

    def test_cpu_security_floors_match_gpu_manifest_and_keep_cpu_torch_stack(self):
        import tomllib

        adapter_root = Path(__file__).resolve().parents[1]
        cpu = tomllib.loads((adapter_root / "pyproject.toml.cpu").read_text(encoding="utf-8"))
        gpu = tomllib.loads((adapter_root / "pyproject.toml").read_text(encoding="utf-8"))

        def requirements(project):
            return {item.split(">=", maxsplit=1)[0].split("==", maxsplit=1)[0]: item for item in project["project"]["dependencies"]}

        cpu_dependencies = requirements(cpu)
        gpu_dependencies = requirements(gpu)
        for package in ("aiohttp", "cryptography", "pillow", "uvicorn"):
            self.assertEqual(cpu_dependencies[package], gpu_dependencies[package])

        self.assertEqual(cpu_dependencies["fastapi"], "fastapi>=0.135.1,<0.141.0")
        self.assertEqual(cpu_dependencies["openai"], "openai>=2.50.0,<3.0.0")
        self.assertEqual(cpu_dependencies["websockets"], "websockets>=12.0,<18.0")
        self.assertEqual(cpu_dependencies["torch"], "torch==2.13.0+cpu")
        self.assertEqual(cpu_dependencies["torchaudio"], "torchaudio==2.11.0+cpu")
        self.assertEqual(cpu_dependencies["torchvision"], "torchvision==0.28.0+cpu")
        self.assertEqual(cpu_dependencies["transformers"], "transformers==5.14.0")
        self.assertEqual(gpu_dependencies["torch"], "torch==2.13.0+cu126")
        self.assertEqual(gpu_dependencies["torchaudio"], "torchaudio==2.11.0+cu126")
        self.assertEqual(gpu_dependencies["torchvision"], "torchvision==0.28.0+cu126")
        self.assertEqual(cpu_dependencies["transformers"], gpu_dependencies["transformers"])
        self.assertEqual(cpu["tool"]["uv"]["index"][0]["url"], "https://download.pytorch.org/whl/cpu")
        self.assertEqual(gpu["tool"]["uv"]["index"][0]["url"], "https://download.pytorch.org/whl/cu126")

    def test_probe_checks_all_exact_metadata_and_cpu_only_torch_without_real_torch(self):
        versions = dict(public_runtime._EXPECTED_VERSIONS)
        self._execute_probe(versions)

        downgraded = {**versions, "aiohttp": "3.14.3"}
        with self.assertRaises(AssertionError):
            self._execute_probe(downgraded)

        with self.assertRaises(AssertionError):
            self._execute_probe(versions, cuda="12.6")

    def test_legacy_v1_marker_cannot_bypass_secure_cpu_recipe(self):
        with self._patched_runtime([0]) as (python, marker, install, _probe):
            legacy_marker = python.parent.parent / ".public-cpu-v1.ready"
            legacy_marker.write_text("old runtime is ready\n", encoding="utf-8")

            self.assertEqual(public_runtime.prepare_cpu_public_runtime(), python)

            self.assertEqual(install.call_count, 2)
            self.assertFalse(legacy_marker.exists())
            self.assertEqual(marker.read_text(encoding="utf-8"), public_runtime._ready_marker_content())
            torch_install = install.call_args_list[0].args[0]
            self.assertIn(public_runtime._CPU_TORCH, torch_install)
            self.assertEqual(torch_install[torch_install.index("--index-url") + 1], "https://download.pytorch.org/whl/cpu")
            public_install = install.call_args_list[1].args[0]
            self.assertIn("aiohttp==3.14.5", public_install)
            self.assertIn("cryptography==50.0.2", public_install)
            self.assertIn("pillow==13.0.0", public_install)
            self.assertIn("uvicorn==0.54.0", public_install)

    def test_downgraded_packages_repair_even_when_current_recipe_marker_exists(self):
        with self._patched_runtime([1, 0]) as (_python, marker, install, probe):
            marker.write_text(public_runtime._ready_marker_content(), encoding="utf-8")

            public_runtime.prepare_cpu_public_runtime()

            self.assertEqual(probe.call_count, 2)
            self.assertEqual(install.call_count, 2)
            self.assertEqual(marker.read_text(encoding="utf-8"), public_runtime._ready_marker_content())

    def test_current_recipe_and_exact_runtime_reuse_without_install(self):
        with self._patched_runtime([0]) as (_python, marker, install, probe):
            marker.write_text(public_runtime._ready_marker_content(), encoding="utf-8")

            public_runtime.prepare_cpu_public_runtime()

            self.assertEqual(probe.call_count, 1)
            install.assert_not_called()

    def test_recipe_change_invalidates_previous_marker(self):
        with self._patched_runtime([0]) as (_python, marker, install, _probe):
            old_recipe = public_runtime._ready_marker_content()
            marker.write_text(old_recipe, encoding="utf-8")
            changed_dependencies = (*public_runtime._PUBLIC_DEPENDENCIES, "recipe-sentinel==1.0")

            with mock.patch.object(public_runtime, "_PUBLIC_DEPENDENCIES", changed_dependencies):
                public_runtime.prepare_cpu_public_runtime()
                self.assertEqual(install.call_count, 2)
                self.assertNotEqual(marker.read_text(encoding="utf-8"), old_recipe)

    def test_failed_install_or_final_probe_leaves_no_ready_marker(self):
        with self.subTest("install failure"), self._patched_runtime(
            [1], install_side_effect=RuntimeError("install failed")
        ) as (_python, marker, _install, _probe):
            marker.write_text(public_runtime._ready_marker_content(), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "install failed"):
                public_runtime.prepare_cpu_public_runtime()
            self.assertFalse(marker.exists())

        with self.subTest("post-install probe failure"), self._patched_runtime([1, 1]) as (
            _python, marker, _install, _probe
        ):
            marker.write_text(public_runtime._ready_marker_content(), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "validation failed"):
                public_runtime.prepare_cpu_public_runtime()
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
