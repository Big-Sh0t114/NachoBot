import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.listen_address import resolve_listen_address  # noqa: E402


class ContainerBindTests(unittest.TestCase):
    def test_listen_address_import_does_not_require_runtime_config(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(PROJECT_ROOT), env.get("PYTHONPATH", "")) if part
        )
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-c", "from src.listen_address import resolve_listen_address"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertFalse((Path(directory) / "config.toml").exists())

    def test_environment_overrides_local_config_for_container(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "NACHOBOT_NAPCAT_LISTEN_HOST": "0.0.0.0",
                "NACHOBOT_NAPCAT_LISTEN_PORT": "8095",
            },
        ):
            self.assertEqual(
                resolve_listen_address("127.0.0.1", 9000),
                ("0.0.0.0", 8095),
            )


if __name__ == "__main__":
    unittest.main()
