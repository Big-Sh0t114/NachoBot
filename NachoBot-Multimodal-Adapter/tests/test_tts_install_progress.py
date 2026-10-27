from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import tts_runtime_manager as manager  # noqa: E402


class TtsInstallProgressTests(unittest.TestCase):
    def test_webui_installer_uses_injected_bridge_and_preserves_arguments(self):
        with tempfile.TemporaryDirectory(prefix="TTS progress ") as temp:
            helper = Path(temp) / "helper.py"
            helper.touch()
            env = {"NACHOBOT_INSTALL_PROGRESS_PYTHON": sys.executable,
                   "NACHOBOT_INSTALL_PROGRESS_HELPER": str(helper)}
            command = ["uv", "pip", "install", "--python", "path with spaces/python.exe", "torch"]
            with patch.object(manager.subprocess, "run") as run:
                manager.run(command, cwd=Path(temp), env=env)
            self.assertEqual(run.call_args.args[0], [sys.executable, "-I", str(helper), "--", *command])
            self.assertIs(run.call_args.kwargs["env"], env)
            self.assertTrue(run.call_args.kwargs["check"])

    def test_terminal_launch_without_bridge_keeps_original_command(self):
        with patch.object(manager.subprocess, "run") as run:
            manager.run(["uv", "pip", "install", "torch"], env={})
        self.assertEqual(run.call_args.args[0], ["uv", "pip", "install", "torch"])

    def test_install_failure_remains_an_error(self):
        with patch.object(manager.subprocess, "run", side_effect=subprocess.CalledProcessError(9, ["uv"])):
            with self.assertRaises(subprocess.CalledProcessError):
                manager.run(["uv", "pip", "install", "torch"], env={})


if __name__ == "__main__":
    unittest.main()
