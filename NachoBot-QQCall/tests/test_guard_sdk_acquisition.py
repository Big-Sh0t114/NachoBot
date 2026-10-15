from __future__ import annotations

import base64
import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("qqcall_guard_sdk_acquire", ROOT / "scripts/acquire-guard-sdk.py")
sdk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sdk)


class SDKAcquisitionTests(unittest.TestCase):
    def fixture(self, root, entries):
        archive = root / "fixture.sdk.1.nupkg"
        with ZipFile(archive, "w") as zipped:
            for name, value in entries.items():
                zipped.writestr(name, value)
        data = archive.read_bytes()
        return {"id": "fixture.sdk", "size": len(data), "sha512": base64.b64encode(hashlib.sha512(data).digest()).decode()}

    def test_verified_cached_package_extracts_only_build_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self.fixture(root, {"c/Include/shared/fixture.h": "header", "c/um/x64/fixture.lib": "library",
                                          "tools/not-needed.exe": "tool"})
            report = sdk.acquire(package, "1", root)
            self.assertEqual(report["extractedFiles"], 2)
            self.assertTrue((root / "fixture.sdk/c/Include/shared/fixture.h").is_file())
            self.assertFalse((root / "fixture.sdk/tools/not-needed.exe").exists())

    def test_traversal_is_rejected_even_in_a_hash_verified_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self.fixture(root, {"../outside": "bad"})
            with self.assertRaisesRegex(ValueError, "sdk_archive_path_invalid"):
                sdk.acquire(package, "1", root)
            self.assertFalse((root.parent / "outside").exists())
