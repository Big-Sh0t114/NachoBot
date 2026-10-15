from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.tts_runtime_manager as manager


class TtsDependencySecurityTests(unittest.TestCase):
    def _metadata_probe(self, profile: str, *, triton: bool = False, versions=None, events=None):
        installed = dict(versions or manager._tts_torch_versions(profile))
        if triton:
            installed["triton-windows"] = manager.TTS_TRITON_WINDOWS_VERSION

        def fake_run(command, **kwargs):
            script = command[-1]
            if "sys.version_info" in script:
                return types.SimpleNamespace(returncode=0, stdout="3.12\n", stderr="")
            self.assertIn("importlib.metadata", script)
            if events is not None:
                events.append("metadata-probe")

            class PackageNotFoundError(Exception):
                pass

            fake_metadata = types.ModuleType("importlib.metadata")
            fake_metadata.PackageNotFoundError = PackageNotFoundError

            def version(package):
                if package not in installed:
                    raise PackageNotFoundError(package)
                return installed[package]

            fake_metadata.version = version
            importlib_package = importlib
            prior_metadata = getattr(importlib_package, "metadata", None)
            importlib_package.metadata = fake_metadata
            try:
                with mock.patch.dict(sys.modules, {"importlib.metadata": fake_metadata}):
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        exec(compile(script, "<tts-metadata-probe>", "exec"), {})
            finally:
                if prior_metadata is None:
                    delattr(importlib_package, "metadata")
                else:
                    importlib_package.metadata = prior_metadata
            return types.SimpleNamespace(returncode=0, stdout=stdout.getvalue(), stderr="")

        return fake_run

    def test_default_profiles_and_legacy_cuda_index_are_explicit(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            manager.shutil, "which", return_value="nvidia-smi"
        ):
            self.assertEqual(manager.torch_index_url(), manager.PYTORCH_CUDA_INDEX)

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            manager.shutil, "which", return_value=None
        ):
            self.assertEqual(manager.torch_index_url(), manager.PYTORCH_CPU_INDEX)

        with mock.patch.dict(
            os.environ,
            {
                "NACHOBOT_TTS_RUNTIME_PROFILE": "cpu",
                "NACHOBOT_TTS_TORCH_INDEX": manager.PYTORCH_CPU_INDEX,
            },
            clear=True,
        ), mock.patch.object(manager.shutil, "which", return_value="nvidia-smi"):
            self.assertEqual(manager.torch_index_url(), manager.PYTORCH_CPU_INDEX)

        with mock.patch.dict(
            os.environ,
            {
                "NACHOBOT_TTS_RUNTIME_PROFILE": "gpu",
                "NACHOBOT_TTS_TORCH_INDEX": "https://download.pytorch.org/whl/cu128",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "cu126"):
                manager.torch_index_url()

    def test_custom_index_credentials_are_not_logged_or_saved(self):
        secret_index = "https://user:secret@example.invalid/simple?token=hidden"
        self.assertNotIn("secret", manager._safe_torch_index_label(secret_index))
        with mock.patch.object(manager.subprocess, "run") as subprocess_run:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                manager.run(["uv", "pip", "install", "--index-url", secret_index])
        self.assertNotIn("secret", stdout.getvalue())
        self.assertNotIn("hidden", stdout.getvalue())
        subprocess_run.assert_called_once()

    def test_voxcpm_old_marker_cannot_bypass_exact_secure_recipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            runtime_dir = runtime_root / "voxcpm"
            runtime_dir.mkdir(parents=True)
            old_marker = runtime_dir / ".deps-voxcpm-2.0.3-py3.12-torch211-triton36.ready"
            old_marker.write_text("stale", encoding="utf-8")
            python = root / "managed" / "python.exe"
            commands = []
            env = {
                "NACHOBOT_TTS_RUNTIME_PROFILE": "gpu",
                "NACHOBOT_TTS_TORCH_INDEX": manager.PYTORCH_CUDA_INDEX,
            }
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                manager, "RUNTIME_ROOT", runtime_root
            ), mock.patch.object(manager, "prepare_emotion_classifier"), mock.patch.object(
                manager, "ensure_venv", return_value=python
            ), mock.patch.object(manager, "require_uv", return_value="uv"), mock.patch.object(
                manager, "run", side_effect=lambda command, **kwargs: commands.append(command)
            ), mock.patch.object(
                manager.subprocess, "run", side_effect=self._metadata_probe("gpu", triton=True)
            ), mock.patch.object(
                manager,
                "_tts_triton_required",
                side_effect=lambda profile, backend: profile == "gpu" and backend == "voxcpm-2.0.3",
            ):
                result = manager.prepare_voxcpm()

            self.assertEqual(result, python)
            self.assertFalse(old_marker.exists())
            marker = manager._tts_marker_path(runtime_dir, "voxcpm-2.0.3", "3.12", "gpu")
            self.assertTrue(marker.is_file())
            marker_text = marker.read_text(encoding="utf-8")
            self.assertIn("torch=2.13.0+cu126", marker_text)
            self.assertIn("torchaudio=2.11.0+cu126", marker_text)
            self.assertIn("triton-windows=3.7.1.post27", marker_text)
            self.assertNotIn(manager.PYTORCH_CUDA_INDEX, marker_text)

            rendered = [" ".join(command) for command in commands]
            self.assertEqual(len(rendered), 3)
            self.assertTrue(all("--constraint" in command for command in commands))
            self.assertTrue(any("torch==2.13.0+cu126" in command for command in rendered))
            self.assertTrue(any("torchaudio==2.11.0+cu126" in command for command in rendered))
            self.assertTrue(any("triton-windows==3.7.1.post27" in command for command in rendered))
            self.assertTrue(any("voxcpm==2.0.3" in command for command in rendered))
            self.assertFalse(any("torchvision==" in command for command in rendered))

            constraints = runtime_dir / "constraints-torch213-audio211-triton371-v3-gpu.txt"
            self.assertIn("torchvision==0.28.0+cu126", constraints.read_text(encoding="utf-8"))

    def test_gpt_requirements_keep_torch_floor_and_marker_follows_final_install(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            source_dir = root / "gpt-source"
            source_dir.mkdir()
            extra_requirements = source_dir / "extra-req.txt"
            extra_requirements.write_text("some-extra==1.0\n", encoding="utf-8")
            requirements = source_dir / "requirements.txt"
            requirements.write_text("torchaudio\nsome-package\n", encoding="utf-8")
            python = root / "managed" / "python.exe"
            events = []
            commands = []
            env = {
                "NACHOBOT_TTS_RUNTIME_PROFILE": "cpu",
                "NACHOBOT_TTS_TORCH_INDEX": manager.PYTORCH_CPU_INDEX,
            }
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                manager, "RUNTIME_ROOT", runtime_root
            ), mock.patch.object(
                manager, "ensure_gpt_source", return_value=source_dir
            ), mock.patch.object(
                manager, "ensure_venv", return_value=python
            ), mock.patch.object(
                manager, "require_uv", return_value="uv"
            ), mock.patch.object(
                manager,
                "run",
                side_effect=lambda command, **kwargs: (commands.append(command), events.append("install")),
            ), mock.patch.object(
                manager.subprocess, "run", side_effect=self._metadata_probe("cpu", events=events)
            ), mock.patch.object(
                manager,
                "patch_gpt_runtime_compat",
                return_value=requirements,
            ), mock.patch.object(manager, "ensure_gpt_assets"):
                _resolved_python, resolved_source = manager.prepare_gpt_sovits()

            self.assertEqual(resolved_source, source_dir)
            self.assertEqual(len(commands), 3)
            self.assertTrue(all("--constraint" in command for command in commands))
            self.assertTrue(any("torch==2.13.0+cpu" in command for command in commands[0]))
            self.assertTrue(any("torchaudio==2.11.0+cpu" in command for command in commands[0]))
            self.assertEqual(commands[-1][commands[-1].index("-r") + 1], str(requirements))
            self.assertLess(max(i for i, event in enumerate(events) if event == "install"), events.index("metadata-probe"))

            constraints = runtime_root / "gpt-sovits" / "constraints-torch213-audio211-triton371-v3-cpu.txt"
            constraints_text = constraints.read_text(encoding="utf-8")
            self.assertIn("torchvision==0.28.0+cpu", constraints_text)
            self.assertNotIn("triton-windows", constraints_text)
            marker = manager._tts_marker_path(runtime_root / "gpt-sovits", manager.GPT_REF, "3.12", "cpu")
            self.assertTrue(marker.is_file())
            self.assertIn("torch=2.13.0+cpu", marker.read_text(encoding="utf-8"))

    def test_gpt_downgrade_after_requirements_never_writes_ready_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            source_dir = root / "gpt-source"
            source_dir.mkdir()
            (source_dir / "extra-req.txt").write_text("", encoding="utf-8")
            requirements = source_dir / "requirements.txt"
            requirements.write_text("torchaudio\n", encoding="utf-8")
            python = root / "managed" / "python.exe"
            commands = []
            env = {
                "NACHOBOT_TTS_RUNTIME_PROFILE": "cpu",
                "NACHOBOT_TTS_TORCH_INDEX": manager.PYTORCH_CPU_INDEX,
            }
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                manager, "RUNTIME_ROOT", runtime_root
            ), mock.patch.object(
                manager, "ensure_gpt_source", return_value=source_dir
            ), mock.patch.object(
                manager, "ensure_venv", return_value=python
            ), mock.patch.object(
                manager, "require_uv", return_value="uv"
            ), mock.patch.object(
                manager, "run", side_effect=lambda command, **kwargs: commands.append(command)
            ), mock.patch.object(
                manager.subprocess,
                "run",
                side_effect=self._metadata_probe(
                    "cpu", versions={"torch": "2.12.1+cpu", "torchaudio": "2.11.0+cpu"}
                ),
            ), mock.patch.object(
                manager, "patch_gpt_runtime_compat", return_value=requirements
            ), mock.patch.object(manager, "ensure_gpt_assets"):
                with self.assertRaisesRegex(RuntimeError, "拒绝写入就绪标记"):
                    manager.prepare_gpt_sovits()

            self.assertEqual(len(commands), 3)
            self.assertTrue(all("--constraint" in command for command in commands))
            marker = manager._tts_marker_path(runtime_root / "gpt-sovits", manager.GPT_REF, "3.12", "cpu")
            self.assertFalse(marker.exists())

    def test_marker_reuse_requires_actual_patched_versions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_dir = root / "vox"
            runtime_dir.mkdir()
            marker = manager._tts_marker_path(runtime_dir, "voxcpm-2.0.3", "3.12", "cpu")
            marker.write_text(
                "".join(
                    f"{key}={value}\n"
                    for key, value in manager._tts_marker_values(
                        manager.PYTORCH_CPU_INDEX, "3.12", "cpu", "voxcpm-2.0.3"
                    ).items()
                ),
                encoding="utf-8",
            )
            python = root / "python.exe"
            with mock.patch.object(
                manager.subprocess,
                "run",
                side_effect=self._metadata_probe("cpu"),
            ):
                self.assertTrue(
                    manager._tts_marker_is_valid(
                        marker,
                        python,
                        manager.PYTORCH_CPU_INDEX,
                        "3.12",
                        "cpu",
                        "voxcpm-2.0.3",
                    )
                )
            with mock.patch.object(
                manager.subprocess,
                "run",
                side_effect=self._metadata_probe(
                    "cpu", versions={"torch": "2.12.1+cpu", "torchaudio": "2.11.0+cpu"}
                ),
            ):
                self.assertFalse(
                    manager._tts_marker_is_valid(
                        marker,
                        python,
                        manager.PYTORCH_CPU_INDEX,
                        "3.12",
                        "cpu",
                        "voxcpm-2.0.3",
                    )
                )


if __name__ == "__main__":
    unittest.main()
