"""Focused regressions for the unified CUDA 13 PyTorch runtime."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ADAPTER_ROOT.parent
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))

from scripts import tts_runtime_manager  # noqa: E402


CUDA_INDEX = "https://download.pytorch.org/whl/cu130"
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
GPU_VERSIONS = {
    "torch": "2.13.0+cu130",
    "torchaudio": "2.11.0+cu130",
    "torchvision": "0.28.0+cu130",
}


class UnifiedCudaRuntimeTests(unittest.TestCase):
    def test_root_manifest_and_generated_lock_use_official_cu130_trio(self):
        manifest = tomllib.loads((ADAPTER_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        lock = tomllib.loads((ADAPTER_ROOT / "uv.lock").read_text(encoding="utf-8"))
        project = manifest["project"]
        for package, version in GPU_VERSIONS.items():
            self.assertIn(f"{package}=={version}", project["dependencies"])

        index = manifest["tool"]["uv"]["index"][0]
        self.assertEqual(index["name"], "pytorch-cu130")
        self.assertEqual(index["url"], CUDA_INDEX)
        self.assertEqual(
            {name: source["index"] for name, source in manifest["tool"]["uv"]["sources"].items()},
            {name: "pytorch-cu130" for name in GPU_VERSIONS},
        )

        locked = {package["name"]: package for package in lock["package"]}
        root_package = locked[project["name"].lower()]
        locked_requirements = {
            entry["name"]: entry for entry in root_package["metadata"]["requires-dist"]
        }
        for package, version in GPU_VERSIONS.items():
            self.assertEqual(locked[package]["version"], version)
            self.assertEqual(locked[package]["source"]["registry"].rstrip("/"), CUDA_INDEX)
            self.assertEqual(locked_requirements[package]["specifier"], f"=={version}")
            self.assertEqual(locked_requirements[package]["index"], CUDA_INDEX)
            self.assertTrue(all("/cu130/" in wheel["url"] for wheel in locked[package]["wheels"]))

    def test_bat_gpu_indexes_match_manifest(self):
        for launcher in (REPO_ROOT / "launchbot.bat", REPO_ROOT / "launchbot_lite.bat"):
            source = launcher.read_text(encoding="utf-8")
            self.assertIn(CUDA_INDEX, source)
            self.assertNotIn("https://download.pytorch.org/whl/cu126", source)
            self.assertIn(CPU_INDEX, source)

    def test_managed_tts_cuda_flavor_constraints_and_markers_use_cu130(self):
        self.assertEqual(tts_runtime_manager.PYTORCH_CUDA_INDEX, CUDA_INDEX)
        self.assertEqual(
            tts_runtime_manager._tts_torch_versions("gpu"),
            {"torch": GPU_VERSIONS["torch"], "torchaudio": GPU_VERSIONS["torchaudio"]},
        )
        self.assertEqual(
            tts_runtime_manager._tts_torch_versions("cpu"),
            {"torch": "2.13.0+cpu", "torchaudio": "2.11.0+cpu"},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            constraints = tts_runtime_manager._tts_dependency_constraints(
                Path(temp_dir), "gpu", "voxcpm-2.0.3"
            )
            expected_constraints = {
                f"{package}=={version}" for package, version in GPU_VERSIONS.items()
            }
            if tts_runtime_manager._tts_triton_required("gpu", "voxcpm-2.0.3"):
                expected_constraints.add(
                    f"triton-windows=={tts_runtime_manager.TTS_TRITON_WINDOWS_VERSION}"
                )
            self.assertEqual(
                set(constraints.read_text(encoding="utf-8").splitlines()),
                expected_constraints,
            )
            marker_values = tts_runtime_manager._tts_marker_values(
                CUDA_INDEX, "3.12", "gpu", "voxcpm-2.0.3"
            )
            self.assertEqual(marker_values["torch"], GPU_VERSIONS["torch"])
            self.assertEqual(marker_values["torchaudio"], GPU_VERSIONS["torchaudio"])
            self.assertEqual(marker_values["index_sha256"], hashlib.sha256(CUDA_INDEX.encode()).hexdigest())

            stale_marker = Path(temp_dir) / "old-cu126.ready"
            stale_values = dict(marker_values)
            stale_values.update(
                {
                    "index_sha256": hashlib.sha256(
                        "https://download.pytorch.org/whl/cu126".encode()
                    ).hexdigest(),
                    "torch": "2.13.0+cu126",
                    "torchaudio": "2.11.0+cu126",
                }
            )
            stale_marker.write_text(
                "".join(f"{key}={value}\n" for key, value in stale_values.items()),
                encoding="utf-8",
            )
            self.assertFalse(
                tts_runtime_manager._tts_marker_is_valid(
                    stale_marker,
                    Path(temp_dir) / "python.exe",
                    CUDA_INDEX,
                    "3.12",
                    "gpu",
                    "voxcpm-2.0.3",
                )
            )

    def test_tts_index_policy_accepts_cu130_and_cpu_and_rejects_old_cuda_indexes(self):
        with mock.patch.dict(
            os.environ,
            {"NACHOBOT_TTS_RUNTIME_PROFILE": "gpu", "NACHOBOT_TTS_TORCH_INDEX": CUDA_INDEX},
            clear=True,
        ):
            self.assertEqual(tts_runtime_manager.torch_index_url(), CUDA_INDEX)

        for old_index in (
            "https://download.pytorch.org/whl/cu126",
            "https://download.pytorch.org/whl/cu128",
        ):
            with mock.patch.dict(
                os.environ,
                {"NACHOBOT_TTS_RUNTIME_PROFILE": "gpu", "NACHOBOT_TTS_TORCH_INDEX": old_index},
                clear=True,
            ), self.subTest(index=old_index):
                with self.assertRaisesRegex(RuntimeError, "cu130"):
                    tts_runtime_manager.torch_index_url()

        with mock.patch.dict(
            os.environ,
            {"NACHOBOT_TTS_RUNTIME_PROFILE": "cpu", "NACHOBOT_TTS_TORCH_INDEX": CPU_INDEX},
            clear=True,
        ):
            self.assertEqual(tts_runtime_manager.torch_index_url(), CPU_INDEX)

        with mock.patch.dict(
            os.environ,
            {"NACHOBOT_TTS_RUNTIME_PROFILE": "cpu", "NACHOBOT_TTS_TORCH_INDEX": CUDA_INDEX},
            clear=True,
        ), self.assertRaisesRegex(RuntimeError, "CPU"):
            tts_runtime_manager.torch_index_url()


if __name__ == "__main__":
    unittest.main()
