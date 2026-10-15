import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import tts_public_runtime as runtime


class PublicRuntimeTests(unittest.TestCase):
    def test_selects_cpu_only_for_windows_vox_cpu_classifier_using_selected_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory, "custom-base.toml")
            vox = config.with_name("vox.toml")
            with mock.patch.object(runtime, "prepare_cpu_public_runtime", return_value=Path(directory, "cpu-python.exe")) as prepare:
                for platform, engine, enabled, device, use_cpu in (
                    ("nt", "voxcpm", True, "cpu", True),
                    ("nt", "voxcpm", False, "cpu", False),
                    ("nt", "voxcpm", True, "cuda:0", False),
                    ("nt", "gpt-sovits", True, "cpu", False),
                    ("posix", "voxcpm", True, "cpu", False),
                ):
                    with self.subTest(platform=platform, engine=engine, enabled=enabled, device=device):
                        prepare.reset_mock()
                        vox.write_text(f'[emotion]\nenabled={str(enabled).lower()}\nclassifier_device="{device}"\n', encoding="utf-8")
                        result = runtime.resolve_public_python(engine, config, platform=platform)
                        self.assertEqual(result, str(prepare.return_value) if use_cpu else sys.executable)
                        self.assertEqual(prepare.call_count, int(use_cpu))

    def test_missing_live_vox_config_fails_before_preparing_environment(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(runtime, "prepare_cpu_public_runtime") as prepare:
            with self.assertRaises(FileNotFoundError):
                runtime.resolve_public_python("voxcpm", Path(directory, "base.toml"), platform="nt")
            prepare.assert_not_called()

    def test_invalid_cached_runtime_is_repaired_and_marker_requires_success(self):
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory, ".venv", "Scripts", "python.exe")
            python.parent.mkdir(parents=True)
            marker = python.parent.parent / ".public-cpu-v1.ready"
            marker.write_text("stale", encoding="utf-8")
            with mock.patch.object(runtime.manager, "ensure_venv", return_value=python) as ensure, mock.patch.object(runtime.manager, "require_uv", return_value="uv"), mock.patch.object(runtime.manager, "run") as run, mock.patch.object(runtime.subprocess, "run", side_effect=[mock.Mock(returncode=1), mock.Mock(returncode=0)]):
                self.assertEqual(runtime.prepare_cpu_public_runtime(), python)
                ensure.assert_called_once_with(runtime.manager.RUNTIME_ROOT / "public-cpu")
                self.assertEqual(run.call_count, 2)
                self.assertIn("https://download.pytorch.org/whl/cpu", run.call_args_list[0].args[0])
                self.assertIn(runtime._CPU_TORCH, run.call_args_list[0].args[0])
                self.assertIn("validated", marker.read_text())
            marker.unlink()
            with mock.patch.object(runtime.manager, "ensure_venv", return_value=python), mock.patch.object(runtime.manager, "require_uv", return_value="uv"), mock.patch.object(runtime.manager, "run"), mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=1)):
                with self.assertRaisesRegex(RuntimeError, "validation failed"):
                    runtime.prepare_cpu_public_runtime()
                self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
