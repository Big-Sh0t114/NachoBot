"""Release pin consistency and real-DLL metadata-only build verification."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("qqcall_release_normalizer", ROOT / "native/normalize_recovery_shim.py")
normalizer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(normalizer)
RELEASE = json.loads((ROOT / "native/recovery-shim-release.json").read_text(encoding="utf-8"))


class ReleaseProfileTests(unittest.TestCase):
    def test_release_digest_is_the_existing_supervisor_pin(self):
        tree = ast.parse((ROOT / "service/manager.py").read_text(encoding="utf-8"))
        pins = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "_RECOVERY_SHIM_SHA256" for target in node.targets)]
        self.assertEqual(pins, [RELEASE["sha256"]])
        normalizer.validate_release(RELEASE)

    def test_invalid_profiles_fail_before_file_processing(self):
        for changed in ({"size": 1024**4}, {"schema": True}, {"timestamp": -1}, {"timestamp_offsets": [208, 209]},
                        {"timestamp_offsets": [208.0, 14612.0]},
                        {"sha256": "bad"}, {"unexpected": True}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                normalizer.normalize(b"", {**RELEASE, **changed})


class RealReleaseBinaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        artifact = ROOT / "native/build/x64/QQCallRecoveryShim2.dll"
        if not artifact.is_file():
            raise unittest.SkipTest("build the pinned Recovery2 release before real-DLL verification")
        cls.original = artifact.read_bytes()
        if hashlib.sha256(cls.original).hexdigest().upper() != RELEASE["sha256"]:
            raise AssertionError("existing Recovery2 artifact differs from the supervisor pin")

    def test_only_release_timestamps_can_be_normalized(self):
        self.assertEqual(normalizer.normalize(self.original, RELEASE), self.original)
        candidate = bytearray(self.original)
        for offset in RELEASE["timestamp_offsets"]:
            struct.pack_into("<I", candidate, offset, RELEASE["timestamp"] + 1000)
        self.assertEqual(normalizer.normalize(bytes(candidate), RELEASE), self.original)
        candidate[1024] ^= 1
        with self.assertRaisesRegex(ValueError, "recovery_build_hash_mismatch"):
            normalizer.normalize(bytes(candidate), RELEASE)

    def test_format_changes_are_rejected(self):
        for offset in (0, RELEASE["pe_offset"] + 4, RELEASE["pe_offset"] + 24,
                       RELEASE["timestamp_offsets"][1] + 8):
            candidate = bytearray(self.original)
            candidate[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                normalizer.normalize(bytes(candidate), RELEASE)

    def test_failed_cli_verification_does_not_write_the_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / RELEASE["artifact"]
            candidate = bytearray(self.original)
            candidate[1024] ^= 1
            path.write_bytes(candidate)
            result = subprocess.run([sys.executable, str(ROOT / "native/normalize_recovery_shim.py"), str(path)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(json.loads(result.stdout)["verified"])
            self.assertEqual(path.read_bytes(), bytes(candidate))
