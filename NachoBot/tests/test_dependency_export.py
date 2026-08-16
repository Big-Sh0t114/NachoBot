import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DependencyExportTests(unittest.TestCase):
    def test_requirements_is_utf8_export_of_lock(self) -> None:
        uv = shutil.which("uv")
        if uv is None:
            self.skipTest("uv is not installed")

        requirements = PROJECT_ROOT / "requirements.txt"
        current = requirements.read_bytes()
        self.assertNotIn(b"\x00", current)
        current.decode("utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            generated = Path(temp_dir) / "requirements.txt"
            subprocess.run(
                [
                    uv,
                    "export",
                    "--locked",
                    "--no-dev",
                    "--no-hashes",
                    "--output-file",
                    str(generated),
                ],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            generated_lines = generated.read_text(encoding="utf-8").splitlines()
            current_lines = current.decode("utf-8").splitlines()

        # uv 在第二行记录 --output-file 参数，比较时忽略该路径。
        self.assertEqual(current_lines[:1], generated_lines[:1])
        self.assertEqual(current_lines[2:], generated_lines[2:])


if __name__ == "__main__":
    unittest.main()
