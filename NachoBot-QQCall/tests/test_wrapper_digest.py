import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service.windows import WindowsServiceError, WindowsSystem


@unittest.skipUnless(os.name == "nt", "Windows file identity contract")
class WrapperDigestTests(unittest.TestCase):
    def metadata(self, **changes):
        values = dict(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4,
                      st_ctime_ns=1234567890123456789, st_mode=stat.S_IFREG,
                      st_file_attributes=0)
        values.update(changes)
        return SimpleNamespace(**values)

    def digest(self, metadata, *, version=(3, 11), final=None):
        system = WindowsSystem()
        path = MagicMock(spec=Path)
        path.__str__.return_value = r"C:\QQ\wrapper.node"
        path.is_symlink.return_value = False
        path.lstat.side_effect = [metadata, final or metadata]
        stream = path.open.return_value.__enter__.return_value
        stream.read.side_effect = [b"abc", b""]
        with patch("service.windows.sys.version_info", version), patch(
            "service.windows.os.fstat", return_value=metadata
        ):
            value = system._snowluma_wrapper_digest(path)
        return value, system._snowluma_wrapper_digest_cache

    def test_python311_uses_exact_windows_creation_time(self):
        value, cache = self.digest(self.metadata())
        self.assertEqual(value, hashlib.sha256(b"abc").hexdigest().upper())
        self.assertEqual(cache[1][4], 1234567890123456789)

    def test_python312_prefers_birthtime_over_ctime(self):
        _, cache = self.digest(self.metadata(st_birthtime_ns=789), version=(3, 12))
        self.assertEqual(cache[1][4], 789)

    def test_missing_birthtime_on_new_python_still_rejected(self):
        with self.assertRaises(WindowsServiceError):
            self.digest(self.metadata(), version=(3, 12))

    def test_invalid_legacy_creation_time_rejected(self):
        for value in (None, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(WindowsServiceError):
                self.digest(self.metadata(st_ctime_ns=value))

    def test_file_replacement_during_read_rejected(self):
        with self.assertRaises(WindowsServiceError):
            self.digest(self.metadata(), final=self.metadata(st_ino=99))

    def test_creation_time_change_during_read_rejected(self):
        with self.assertRaises(WindowsServiceError):
            self.digest(self.metadata(), final=self.metadata(st_ctime_ns=99))

    def test_real_file_hash_cache_and_content_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrapper.node"
            system = WindowsSystem()
            path.write_bytes(b"first")
            expected = hashlib.sha256(b"first").hexdigest().upper()
            self.assertEqual(system._snowluma_wrapper_digest(path), expected)
            self.assertEqual(system._snowluma_wrapper_digest(path), expected)
            path.write_bytes(b"second content")
            self.assertEqual(system._snowluma_wrapper_digest(path),
                             hashlib.sha256(b"second content").hexdigest().upper())


if __name__ == "__main__":
    unittest.main()
