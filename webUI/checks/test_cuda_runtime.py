"""Regression checks for WebUI's shared CPU/cu130 PyTorch index choice."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from multimodal_runtime import MultimodalRuntimeManager  # noqa: E402
from process_manager import ProcessManager  # noqa: E402


class WebUICudaRuntimeTests(unittest.TestCase):
    def test_gpu_and_cpu_profiles_keep_their_shared_torch_index(self):
        self.assertEqual(
            MultimodalRuntimeManager.torch_index_url("gpu"),
            "https://download.pytorch.org/whl/cu130",
        )
        self.assertEqual(
            MultimodalRuntimeManager.torch_index_url("cpu"),
            "https://download.pytorch.org/whl/cpu",
        )

    def test_tts_process_manager_passes_the_profile_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = root / "NachoBot-Multimodal-Adapter"
            entrypoint = adapter / "scripts" / "container_tts_entrypoint.py"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.touch()

            process_manager = ProcessManager.__new__(ProcessManager)
            process_manager.root = root
            for profile, expected_index in (
                ("gpu", "https://download.pytorch.org/whl/cu130"),
                ("cpu", "https://download.pytorch.org/whl/cpu"),
            ):
                process_manager._launch_runtime = profile
                with mock.patch.object(
                    MultimodalRuntimeManager,
                    "require_python",
                    return_value=root / profile / "python.exe",
                ), mock.patch(
                    "process_manager._read_tts_runtime_settings",
                    return_value=("127.0.0.1", 9880, 9881),
                ):
                    command, cwd, environ = process_manager._resolve_tts_runtime_cmd()
                self.assertEqual(command[0], str(root / profile / "python.exe"))
                self.assertEqual(cwd, str(adapter))
                self.assertEqual(environ["NACHOBOT_TTS_TORCH_INDEX"], expected_index)
                self.assertEqual(environ["NACHOBOT_TTS_RUNTIME_PROFILE"], profile)

    def test_old_gpu_marker_still_runs_locked_check_and_rejects_cu126_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            patches = (
                mock.patch.object(MultimodalRuntimeManager, "ADAPTER_DIR", root),
                mock.patch.object(MultimodalRuntimeManager, "RUNTIME_DIR", root / ".runtime"),
                mock.patch.object(MultimodalRuntimeManager, "_installing", set()),
            )
            with patches[0], patches[1], patches[2]:
                python = MultimodalRuntimeManager.python_path("gpu")
                python.parent.mkdir(parents=True)
                python.touch()
                site = MultimodalRuntimeManager.env_dir("gpu") / "Lib" / "site-packages"
                for package in ("torch", "transformers", "timm", "sherpa_onnx"):
                    (site / package).mkdir(parents=True)
                root.joinpath("pyproject.toml").write_text(
                    'dependencies = ["torch==2.13.0+cu130"]\n', encoding="utf-8"
                )
                root.joinpath("uv.lock").write_text("version = 1\n", encoding="utf-8")
                marker = MultimodalRuntimeManager._marker_path("gpu")
                marker.write_text(json.dumps({"schema": 2, "profile": "gpu"}), encoding="utf-8")

                with mock.patch(
                    "multimodal_runtime.subprocess.run",
                    return_value=SimpleNamespace(returncode=1),
                ) as run:
                    status = MultimodalRuntimeManager.get_status("gpu")

                self.assertFalse(status["installed"])
                command = run.call_args.args[0]
                self.assertIn("--check", command)
                self.assertIn("--locked", command)
                self.assertIn("--offline", command)
                self.assertEqual(run.call_args.kwargs["cwd"], root)


if __name__ == "__main__":
    unittest.main()
