import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

from scripts.bootstrap_compose import (
    FILE_SOURCES,
    GENERATED_FILES,
    MULTIMODAL_CONFIG_SOURCES,
    bootstrap,
    validate,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ComposeBootstrapTests(unittest.TestCase):
    def test_bootstrap_is_safe_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for source_relative in (*FILE_SOURCES, *MULTIMODAL_CONFIG_SOURCES):
                source = root / source_relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(f"fixture:{source_relative}\n", encoding="utf-8")

            first_created = bootstrap(root)
            validate(root)
            self.assertTrue(first_created)

            protected_file = root / "docker-config/mmc/.env"
            protected_file.write_text("USER_VALUE=preserve\n", encoding="utf-8")
            second_created = bootstrap(root)
            validate(root)

            self.assertEqual(second_created, [])
            self.assertEqual(
                protected_file.read_text(encoding="utf-8"),
                "USER_VALUE=preserve\n",
            )
            statistics_file = root / next(iter(GENERATED_FILES))
            self.assertTrue(statistics_file.is_file())
            self.assertIn("<!doctype html>", statistics_file.read_text(encoding="utf-8"))

    def test_check_reports_missing_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "bootstrap_compose.py"):
                validate(Path(temp_dir))

    def test_all_copy_sources_are_tracked_by_git(self) -> None:
        tracked = set(
            subprocess.run(
                ["git", "ls-files"],
                cwd=PROJECT_ROOT.parent,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
        )
        copy_sources = {*FILE_SOURCES, *MULTIMODAL_CONFIG_SOURCES}
        normalized = {
            str((PROJECT_ROOT / source).resolve().relative_to(PROJECT_ROOT.parent)).replace("\\", "/")
            for source in copy_sources
        }
        self.assertEqual(normalized.difference(tracked), set())

    def test_cli_reports_sibling_multimodal_paths_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "NachoBot"
            root.mkdir()
            for source_relative in FILE_SOURCES:
                source = root / source_relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("fixture\n", encoding="utf-8")
            for source_relative in MULTIMODAL_CONFIG_SOURCES:
                source = root / source_relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("fixture\n", encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "bootstrap_compose.py"),
                    "--root",
                    str(root),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("NachoBot-Multimodal-Adapter", completed.stdout)
            self.assertIn("Compose bind-source preflight passed.", completed.stdout)


if __name__ == "__main__":
    unittest.main()
