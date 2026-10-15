from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
PACKAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_DIR))
import loader_probe as probe


def _pe_image(machine: int = probe.EXPECTED_MACHINE) -> bytes:
    data = bytearray(64 + 24 + 240)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 64)
    struct.pack_into("<4sHHIIIHH", data, 64, b"PE\0\0", machine, 1, 0, 0, 0, 240, 0x2022)
    optional_offset = 64 + 24
    struct.pack_into("<H", data, optional_offset, 0x20B)
    struct.pack_into("<I", data, optional_offset + 56, 0x400000)
    return bytes(data)


class _FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class _FakeKernel32:
    base = 0x18000000

    def __init__(self, *, load_result: int | None = None, missing: set[str] | None = None):
        self.loads = []
        self.lookups = []
        self.missing = missing or set()
        self.load_result = self.base if load_result is None else load_result
        self.LoadLibraryExW = _FakeFunction(self._load)
        self.GetProcAddress = _FakeFunction(self._get_proc)

    def _load(self, path, _reserved, flags):
        self.loads.append((path, flags))
        return self.load_result

    def _get_proc(self, module, name):
        export = name.decode("ascii")
        self.lookups.append((module, export))
        if export in self.missing:
            return 0
        rva = probe.EXPECTED_EXPORT_RVAS.get(export, 0x10000 + len(self.lookups) * 16)
        return self.base + rva


class _FakeProcess:
    def __init__(self, *, mode: str = "exit", exit_code: int = 0):
        self.mode = mode
        self.exit_code = exit_code
        self.wait_calls = 0
        self.killed = False

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.mode == "timeout" and self.wait_calls == 1:
            raise subprocess.TimeoutExpired("fake-child", timeout)
        if self.mode == "wait_oserror" and self.wait_calls == 1:
            raise OSError("synthetic wait failure")
        if self.mode == "timeout" and not self.killed:
            raise subprocess.TimeoutExpired("fake-child", timeout)
        return -9 if self.killed else self.exit_code

    def kill(self):
        self.killed = True


class LoaderProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dll = self.root / "avsdk-test.dll"
        self.dll.write_bytes(_pe_image())
        self.digest = hashlib.sha256(self.dll.read_bytes()).hexdigest()
        self.expected_sha_patch = patch.object(probe, "EXPECTED_DLL_SHA256", self.digest)
        self.expected_sha_patch.start()
        self.addCleanup(self.expected_sha_patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.popen_calls = []
        self.last_kwargs = None

    def _factory(self, *, result_builder=None, process=None, mutate=None):
        process = process or _FakeProcess()

        def popen(command, **kwargs):
            self.popen_calls.append(command)
            self.last_kwargs = kwargs
            if mutate is not None:
                mutate()
            result = result_builder() if result_builder is not None else probe._probe_child(
                command[-2], kernel32=_FakeKernel32()
            )
            probe._write_json_atomic(Path(command[-1]) / probe.CHILD_RESULT_NAME, result)
            return process

        return popen, process

    def _run(self, output_name="out", **options):
        factory, _process = self._factory(**options)
        return probe.run_probe(
            self.dll,
            self.root / output_name,
            _popen_factory=factory,
            _is_windows=True,
            _source_environment={"SystemRoot": "C:\\Windows", "SECRET_TOKEN": "must-not-pass"},
        )

    def test_pin_mismatch_and_wrong_arch_are_rejected_before_child(self):
        with patch.object(
            probe, "EXPECTED_DLL_SHA256", "757c48ddc0b5fdff101c36bf309bf9f79fe8618a64fbf0d059347ad3834a9dbf"
        ):
            pin_mismatch = probe.run_probe(
                self.dll,
                self.root / "pin-mismatch",
                _popen_factory=lambda *_args, **_kwargs: self.fail("must not start child"),
                _is_windows=True,
                _source_environment={"SystemRoot": "C:\\Windows"},
            )
        self.assertEqual(pin_mismatch["failure_code"], "dll_sha256_mismatch")

        wrong_arch = self.root / "x86.dll"
        wrong_arch.write_bytes(_pe_image(0x14C))
        rejected = probe.run_probe(
            wrong_arch,
            self.root / "wrong-arch",
            _popen_factory=lambda *_args, **_kwargs: self.fail("must not start child"),
            _is_windows=True,
            _source_environment={"SystemRoot": "C:\\Windows"},
        )
        self.assertEqual(rejected["failure_code"], "unsupported_pe_machine")

    def test_child_loads_with_safe_flags_and_resolves_exact_exports_only(self):
        native = _FakeKernel32()
        result = probe._probe_child(self.dll, kernel32=native)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["stages"], list(probe.CHILD_STAGES))
        self.assertEqual(native.loads, [(str(self.dll.resolve()), probe.LOAD_FLAGS)])
        self.assertEqual(probe.LOAD_FLAGS, 0x100 | 0x800)
        self.assertEqual([name for _module, name in native.lookups], list(probe.EXPORT_NAMES))
        self.assertFalse(result["pcm_verified"])
        self.assertTrue(result["loader_metadata_resolved"])

    def test_success_report_uses_private_minimal_child_environment(self):
        report = self._run()
        self.assertEqual(report["status"], "pcm_unverified")
        self.assertTrue(report["loader_metadata_resolved"])
        self.assertEqual(report["expected_resolved"], {name: True for name in probe.EXPECTED_EXPORT_RVAS})
        self.assertEqual(self.last_kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(self.last_kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(self.last_kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(self.last_kwargs["env"]["PATH"], "C:\\Windows\\System32")
        self.assertNotIn("SECRET_TOKEN", self.last_kwargs["env"])
        self.assertNotIn("HOME", self.last_kwargs["env"])
        self.assertNotIn("CODEX_HOME", self.last_kwargs["env"])
        self.assertTrue(Path(self.last_kwargs["cwd"]).is_relative_to(self.root / "out" / "runtime"))
        self.assertTrue(Path(self.last_kwargs["env"]["TEMP"]).is_relative_to(self.root / "out" / "runtime"))
        rendered = json.dumps(report)
        self.assertNotIn(str(self.dll), rendered)
        self.assertNotIn("SECRET_TOKEN", rendered)

    def test_kernel32_failure_prefix_preserves_typed_code(self):
        def build_result():
            with patch.object(
                probe, "_kernel32_for_child", side_effect=probe.ProbeFailure("kernel32_unavailable")
            ):
                return probe._probe_child(self.dll)

        report = self._run("kernel-fail", result_builder=build_result)
        self.assertEqual(report["failure_code"], "kernel32_unavailable")
        self.assertEqual(report["status"], "probe_failed")
        self.assertEqual(report["stages"], [])
        self.assertFalse(report["loader_metadata_resolved"])

    def test_load_failure_preserves_numeric_win32_code_only(self):
        native = _FakeKernel32(load_result=0)
        with patch.object(probe.ctypes, "get_last_error", return_value=126, create=True):
            report = self._run("load-fail", result_builder=lambda: probe._probe_child(self.dll, kernel32=native))
        self.assertEqual(report["failure_code"], "dll_load_failed")
        self.assertEqual(report["win32_error_code"], 126)
        self.assertEqual(native.lookups, [])
        self.assertNotIn("message", report)

    def test_child_recheck_hash_mismatch_is_not_swallowed(self):
        changed = _pe_image() + b"changed after parent check"
        report = self._run(
            "changed-hash",
            mutate=lambda: self.dll.write_bytes(changed),
            result_builder=lambda: probe._probe_child(self.dll, kernel32=_FakeKernel32()),
        )
        self.assertEqual(report["failure_code"], "dll_sha256_mismatch")
        self.assertEqual(report["status"], "probe_failed")
        self.assertFalse(report["loader_metadata_resolved"])
        self.assertEqual(report["stages"], [])

    def test_child_recheck_architecture_mismatch_is_not_swallowed(self):
        report = self._run(
            "changed-arch",
            mutate=lambda: self.dll.write_bytes(_pe_image(0x14C)),
            result_builder=lambda: probe._probe_child(self.dll, kernel32=_FakeKernel32()),
        )
        self.assertEqual(report["failure_code"], "unsupported_pe_machine")
        self.assertEqual(report["machine"], "unsupported")
        self.assertEqual(report["stages"], [])
        self.assertFalse(report["loader_metadata_resolved"])

    def test_timeout_kills_and_reaps_without_accepting_partial_success(self):
        partial = probe._child_result(
            status="ok",
            dll_sha256=self.digest,
            machine="AMD64",
            stages=list(probe.CHILD_STAGES),
            exports={
                name: {
                    "resolved": True,
                    "rva": probe.EXPECTED_EXPORT_RVAS.get(name, 0x10000 + index * 16),
                }
                for index, name in enumerate(probe.EXPORT_NAMES, 1)
            },
        )
        process = _FakeProcess(mode="timeout")
        report = self._run("timeout", result_builder=lambda: partial, process=process)
        self.assertEqual(report["status"], "child_timed_out")
        self.assertTrue(report["child_timed_out"])
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_calls, 2)
        self.assertFalse(report["loader_metadata_resolved"])
        self.assertEqual(report["stages"], [])

    def test_wait_oserror_kills_and_reaps_child(self):
        process = _FakeProcess(mode="wait_oserror")
        report = self._run("wait-error", process=process)
        self.assertEqual(report["failure_code"], "child_wait_failed")
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_calls, 2)
        self.assertFalse(report["loader_metadata_resolved"])

    def test_crash_and_invalid_child_report_never_claim_success(self):
        process = _FakeProcess(exit_code=17)
        crashed = self._run("crash", process=process)
        self.assertEqual(crashed["failure_code"], "child_exit_nonzero")
        self.assertFalse(crashed["loader_metadata_resolved"])

        def invalid_result():
            result = probe._probe_child(self.dll, kernel32=_FakeKernel32())
            result["stages"] = []
            return result

        rejected = self._run("invalid-report", result_builder=invalid_result)
        self.assertEqual(rejected["failure_code"], "child_report_invalid")
        self.assertFalse(rejected["loader_metadata_resolved"])

    def test_missing_export_has_a_typed_failure(self):
        report = self._run(
            "missing-export",
            result_builder=lambda: probe._probe_child(
                self.dll, kernel32=_FakeKernel32(missing={"PPP_GetInterface"})
            ),
        )
        self.assertEqual(report["failure_code"], "required_export_missing")
        self.assertFalse(report["loader_metadata_resolved"])
        self.assertTrue(report["expected_resolved"]["StartToDumpAudio"])

    def test_help_is_read_only_and_exposes_no_invoke_option(self):
        with patch.object(sys, "stdout") as stdout:
            self.assertEqual(probe.main(["--help"]), 0)
        help_text = "".join(str(call.args[0]) for call in stdout.write.call_args_list)
        self.assertIn("never invokes exports", help_text)
        self.assertNotIn("--invoke", help_text)


if __name__ == "__main__":
    unittest.main()
