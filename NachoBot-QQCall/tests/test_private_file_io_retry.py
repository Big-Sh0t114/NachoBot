import errno
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from service import private_files, manager


@unittest.skipUnless(os.name == 'nt', 'Windows atomic replacement behavior')
class PrivateFileIoRetryTests(unittest.TestCase):
    def test_recovered_read_keeps_size_and_json_validation(self):
        payloads = (
            (b'{"x":1,"x":2}', 'private_json_duplicate_key', 1024),
            (b'{"x":NaN}', 'private_json_nonfinite', 1024),
            (b'not-json', 'private_json_invalid', 1024),
            (b'123456', 'private_file_too_large', 4),
        )
        for data, code, limit in payloads:
            with (
                self.subTest(code=code),
                patch.object(Path, 'open', side_effect=[PermissionError(errno.EACCES, 'private data'), io.BytesIO(data)]) as opened,
                patch.object(private_files.time, 'sleep') as delay,
            ):
                with self.assertRaises(private_files.PrivateFileError) as caught:
                    private_files.read_private_json('manifest.json', max_bytes=limit)
                self.assertEqual(str(caught.exception), code)
                self.assertEqual(opened.call_count, 2)
                delay.assert_called_once_with(.02)

    def test_missing_and_unknown_io_fail_immediately_but_permanent_access_stays_closed(self):
        for error, attempts in ((FileNotFoundError(errno.ENOENT, 'missing'), 1),
                                (OSError(errno.EIO, 'private data'), 1),
                                (PermissionError(errno.EACCES, 'private data'), 5)):
            with (
                self.subTest(attempts=attempts),
                patch.object(Path, 'open', side_effect=error) as opened,
                patch.object(private_files.time, 'sleep') as delay,
            ):
                with self.assertRaisesRegex(private_files.PrivateFileError, '^private_file_unavailable$'):
                    private_files.read_private_json('manifest.json')
                self.assertEqual(opened.call_count, attempts)
                self.assertEqual(delay.call_count, attempts-1)

    def test_generation_changed_during_read_retry_is_rejected_without_native_rebind(self):
        original = dict(schema=1, adapter='snowluma', accountId='2955610491', connected=True,
                        adapterPid=1, adapterCreationTime100ns='2', connectionGeneration=1, updatedAtMs=1)
        replacement = {**original, 'connectionGeneration':2}
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)/'platform-snowluma.private.json'
            destination.write_text(json.dumps(original), encoding='utf-8')
            supervisor = object.__new__(manager.Supervisor)
            supervisor.config = {'runtime_dir':folder, 'adapter':'snowluma'}
            supervisor._read_manifest = lambda: manager._read_manifest(supervisor.config)
            native = Mock(return_value=True)
            supervisor.system = SimpleNamespace(revalidate_binding_with_error=native)
            with (
                patch.object(Path, 'open', side_effect=[PermissionError(errno.EACCES, 'private data'),
                                                       io.BytesIO(json.dumps(replacement).encode())]),
                patch.object(private_files.time, 'sleep'),
            ):
                self.assertFalse(supervisor._binding_still_current(original, object()))
            native.assert_not_called()

    def test_replace_recovers_and_permanent_failure_preserves_old_snapshot_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)/'manifest.json'
            real_replace = os.replace
            errors = iter([PermissionError(errno.EACCES, 'private data'), None])

            def replace(source, target):
                error = next(errors)
                if error:
                    raise error
                return real_replace(source, target)

            with (
                patch.object(private_files, 'secure_directory', side_effect=lambda path: Path(path)),
                patch.object(private_files, 'secure_file'),
                patch.object(private_files.time, 'sleep'),
                patch.object(private_files.os, 'replace', side_effect=replace) as moved,
            ):
                private_files.atomic_private_json(destination, {'generation':1})
                self.assertEqual(moved.call_count, 2)
            self.assertEqual(json.loads(destination.read_text()), {'generation':1})
            with (
                patch.object(private_files, 'secure_directory', side_effect=lambda path: Path(path)),
                patch.object(private_files, 'secure_file'),
                patch.object(private_files.time, 'sleep'),
                patch.object(private_files.os, 'replace', side_effect=PermissionError(errno.EACCES, 'private data')) as moved,
            ):
                with self.assertRaisesRegex(private_files.PrivateFileError, '^private_write_failed$'):
                    private_files.atomic_private_json(destination, {'generation':2})
                self.assertEqual(moved.call_count, 5)
            self.assertEqual(json.loads(destination.read_text()), {'generation':1})
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])
