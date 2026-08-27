import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MANAGER_PATH = PROJECT_ROOT / "scripts" / "tts_runtime_manager.py"

spec = importlib.util.spec_from_file_location(
    "tts_runtime_manager_under_test",
    RUNTIME_MANAGER_PATH,
)
tts_runtime_manager = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(tts_runtime_manager)


class TTSRuntimeManagerTests(unittest.TestCase):
    def test_ensure_venv_reuses_matching_python_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            python = tts_runtime_manager.runtime_python(runtime_dir / ".venv")
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"")

            probe = subprocess.CompletedProcess(
                [str(python)], 0, stdout="3.12\n", stderr=""
            )
            with (
                mock.patch.object(
                    tts_runtime_manager.subprocess,
                    "run",
                    return_value=probe,
                ),
                mock.patch.object(tts_runtime_manager, "run") as uv_run,
            ):
                result = tts_runtime_manager.ensure_venv(runtime_dir, "3.12")

            self.assertEqual(result, python)
            uv_run.assert_not_called()

    def test_ensure_venv_rebuilds_mismatched_python_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            venv_dir = runtime_dir / ".venv"
            python = tts_runtime_manager.runtime_python(venv_dir)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"")

            probes = [
                subprocess.CompletedProcess(
                    [str(python)], 0, stdout="3.10\n", stderr=""
                ),
                subprocess.CompletedProcess(
                    [str(python)], 0, stdout="3.12\n", stderr=""
                ),
            ]

            def create_venv(command: list[str], **_kwargs: object) -> None:
                self.assertEqual(
                    command,
                    ["uv", "venv", str(venv_dir), "--python", "3.12"],
                )
                python.parent.mkdir(parents=True, exist_ok=True)
                python.write_bytes(b"")

            with (
                mock.patch.object(
                    tts_runtime_manager.subprocess,
                    "run",
                    side_effect=probes,
                ),
                mock.patch.object(
                    tts_runtime_manager,
                    "require_uv",
                    return_value="uv",
                ),
                mock.patch.object(
                    tts_runtime_manager,
                    "run",
                    side_effect=create_venv,
                ) as uv_run,
            ):
                result = tts_runtime_manager.ensure_venv(runtime_dir, "3.12")

            self.assertEqual(result, python)
            uv_run.assert_called_once()

    def test_prepare_gpt_sovits_uses_python_312_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            runtime_dir = runtime_root / "gpt-sovits"
            source_dir = runtime_dir / "source"
            source_dir.mkdir(parents=True)
            python = runtime_dir / ".venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")

            marker = runtime_dir / (
                f".deps-{tts_runtime_manager.GPT_REF}-py3.12-v2.ready"
            )
            marker.write_text("ready\n", encoding="utf-8")

            with (
                mock.patch.object(
                    tts_runtime_manager,
                    "RUNTIME_ROOT",
                    runtime_root,
                ),
                mock.patch.object(
                    tts_runtime_manager,
                    "ensure_gpt_source",
                    return_value=source_dir,
                ),
                mock.patch.object(
                    tts_runtime_manager,
                    "ensure_venv",
                    return_value=python,
                ) as ensure_venv,
                mock.patch.object(
                    tts_runtime_manager,
                    "patch_gpt_runtime_compat",
                ),
                mock.patch.object(
                    tts_runtime_manager,
                    "ensure_gpt_assets",
                ),
                mock.patch.object(tts_runtime_manager, "run") as install_run,
            ):
                result_python, result_source = (
                    tts_runtime_manager.prepare_gpt_sovits()
                )

            ensure_venv.assert_called_once_with(runtime_dir, "3.12")
            install_run.assert_not_called()
            self.assertEqual(result_python, python)
            self.assertEqual(result_source, source_dir)


if __name__ == "__main__":
    unittest.main()
