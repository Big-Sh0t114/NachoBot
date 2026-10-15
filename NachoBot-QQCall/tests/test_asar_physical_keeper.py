from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import ntpath
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "probe" / "asar-physical-keeper.py"
SPEC = importlib.util.spec_from_file_location("asar_physical_keeper_under_test", SOURCE_PATH)
assert SPEC is not None and SPEC.loader is not None
keeper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = keeper
SPEC.loader.exec_module(keeper)


DEFAULT_CONTENTS = {
    "application.asar": b"fake pinned archive bytes",
    "package.json": b"fake pinned package bytes",
    "wrapper.node": b"fake pinned wrapper bytes",
}
DEFAULT_HASHES = {name: hashlib.sha256(data).hexdigest().upper() for name, data in DEFAULT_CONTENTS.items()}
KEEPER_IDENTITY = keeper.ProcessIdentity(501, 123456, r"C:\runtime\python.exe")
KERNEL_IDENTITY = keeper.ProcessIdentity(702, 654321, keeper.KERNEL_IMAGE_PATH)


class FakeNative:
    def __init__(
        self,
        contents: dict[str, bytes] | None = None,
        *,
        reparse_path: str | None = None,
        fail_read_name: str | None = None,
        short_reads: int | None = None,
        changed_path: str | None = None,
        fail_close_handle: int | None = None,
    ) -> None:
        self.contents = dict(DEFAULT_CONTENTS if contents is None else contents)
        self.reparse_path = reparse_path
        self.fail_read_name = fail_read_name
        self.short_reads = short_reads
        self.changed_path = changed_path
        self.fail_close_handle = fail_close_handle
        self.paths: dict[int, str] = {10: "C:\\"}
        self.handles: dict[str, int] = {}
        self.open_calls: list[tuple[int, str, bool, int]] = []
        self.root_calls: list[str] = []
        self.seek_calls: list[int] = []
        self.read_calls: list[tuple[int, int, int]] = []
        self.close_calls: list[int] = []
        self.closed_handles: set[int] = set()
        self.offsets: dict[int, int] = {}
        self.metadata_calls: dict[int, int] = {}
        self._next_handle = 11

    def open_volume_root(self, drive: str) -> int:
        self.root_calls.append(drive)
        return 10

    def open_child(self, parent: int, component: str, *, directory: bool, share: int) -> int:
        if parent in self.closed_handles or parent not in self.paths:
            raise AssertionError("child open did not use a live parent handle")
        if not component or component in {".", ".."} or any(ch in component for ch in ("\\", "/", ":", "\x00")):
            raise AssertionError("child open was not a single relative component")
        if share != keeper.WindowsPhysicalNative.FILE_SHARE_READ:
            raise AssertionError("unexpected sharing mode")
        path = ntpath.join(self.paths[parent], component)
        self.open_calls.append((parent, component, directory, share))
        handle = self._next_handle
        self._next_handle += 1
        self.paths[handle] = path
        self.offsets[handle] = 0
        if not directory:
            self.handles[path] = handle
        return handle

    def metadata(self, handle: int) -> keeper.NativeMetadata:
        if handle in self.closed_handles or handle not in self.paths:
            raise AssertionError("metadata query did not use a live handle")
        path = self.paths[handle]
        self.metadata_calls[handle] = self.metadata_calls.get(handle, 0) + 1
        if path == self.changed_path and self.metadata_calls[handle] >= 3:
            mtime = 999
        else:
            mtime = 100
        is_directory = path not in {ntpath.join(keeper.APP_PATH, name) for name in keeper.PINNED_SHA256}
        attributes = keeper.WindowsPhysicalNative.FILE_ATTRIBUTE_DIRECTORY if is_directory else 0
        if path == self.reparse_path:
            attributes |= keeper.WindowsPhysicalNative.FILE_ATTRIBUTE_REPARSE_POINT
        if is_directory:
            size = 0
        else:
            name = ntpath.basename(path)
            size = len(self.contents[name])
        return keeper.NativeMetadata(
            attributes=attributes,
            is_directory=is_directory,
            volume_serial=1234,
            file_id=handle.to_bytes(16, "big"),
            final_path=path,
            size=size,
            last_write_time_100ns=mtime,
        )

    def seek_to_start(self, handle: int) -> None:
        if handle in self.closed_handles:
            raise AssertionError("seek used a closed handle")
        self.seek_calls.append(handle)
        self.offsets[handle] = 0

    def read_chunk(self, handle: int, count: int) -> bytes:
        if handle in self.closed_handles:
            raise AssertionError("read used a closed handle")
        path = self.paths[handle]
        name = ntpath.basename(path)
        if name == self.fail_read_name:
            raise OSError("fake private path and exception must not escape")
        data = self.contents[name]
        start = self.offsets[handle]
        limit = count if self.short_reads is None else min(count, self.short_reads)
        chunk = data[start : start + limit]
        self.offsets[handle] += len(chunk)
        self.read_calls.append((handle, start, len(chunk)))
        return chunk

    def close(self, handle: int) -> None:
        self.close_calls.append(handle)
        self.closed_handles.add(handle)
        if handle == self.fail_close_handle:
            raise OSError("close failure detail must not escape")


class FakeProcess:
    def __init__(self, identities: list[keeper.ProcessIdentity]) -> None:
        self.identities = identities
        self.calls = 0
        self.closed = False

    def identity(self) -> keeper.ProcessIdentity:
        index = min(self.calls, len(self.identities) - 1)
        self.calls += 1
        return self.identities[index]

    def close(self) -> None:
        self.closed = True


class FakeProcessFactory:
    def __init__(self, profiles: dict[int, list[keeper.ProcessIdentity]] | None = None) -> None:
        self.profiles = profiles or {
            KEEPER_IDENTITY.pid: [KEEPER_IDENTITY],
            KERNEL_IDENTITY.pid: [KERNEL_IDENTITY],
        }
        self.instances: dict[int, FakeProcess] = {}

    def __call__(self, pid: int) -> FakeProcess:
        profile = self.profiles.get(pid, [])
        if not profile:
            raise OSError("unexpected process lookup")
        process = FakeProcess(profile)
        self.instances[pid] = process
        return process


class FakeApiFunction:
    def __init__(self, callback) -> None:
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class FakeExitedKernel32:
    def __init__(self) -> None:
        self.query_image_calls = 0
        self.GetProcessId = FakeApiFunction(lambda _handle: KERNEL_IDENTITY.pid)
        self.GetProcessTimes = FakeApiFunction(self._get_process_times)
        self.QueryFullProcessImageNameW = FakeApiFunction(self._query_image)

    @staticmethod
    def _set_filetime(pointer, value: int) -> None:
        target = keeper.ctypes.cast(pointer, keeper.ctypes.POINTER(keeper._FileTime)).contents
        target.dwLowDateTime = value & 0xFFFFFFFF
        target.dwHighDateTime = value >> 32

    def _get_process_times(self, _handle, created, exited, kernel, user) -> int:
        self._set_filetime(created, KERNEL_IDENTITY.creation_time_100ns)
        self._set_filetime(exited, 1)
        self._set_filetime(kernel, 0)
        self._set_filetime(user, 0)
        return 1

    def _query_image(self, *_args) -> int:
        self.query_image_calls += 1
        return 0


class TrackingWriter(io.BytesIO):
    def __init__(self, on_final=None, on_ready=None) -> None:
        super().__init__()
        self.on_final = on_final
        self.on_ready = on_ready

    def write(self, value: bytes) -> int:
        if self.on_final is not None:
            try:
                frame = json.loads(value)
            except Exception:
                frame = None
            if isinstance(frame, dict) and frame.get("state") == "ready" and self.on_ready is not None:
                self.on_ready()
            if isinstance(frame, dict) and frame.get("state") == "final":
                self.on_final()
        return super().write(value)


def _request_lines(*, nonce: str = "A" * 64, finish_nonce: str | None = None, extra: dict | None = None) -> bytes:
    initial = {
        "schema": keeper.SCHEMA,
        "nonce": nonce,
        "kernelPid": KERNEL_IDENTITY.pid,
        "kernelCreationTime100ns": str(KERNEL_IDENTITY.creation_time_100ns),
        "connectionGeneration": 17,
        "appPath": keeper.APP_PATH,
    }
    if extra:
        initial.update(extra)
    finish = {"op": "finish", "nonce": nonce if finish_nonce is None else finish_nonce}
    return (json.dumps(initial, separators=(",", ":")) + "\n" + json.dumps(finish, separators=(",", ":")) + "\n").encode()


def _run(
    *,
    contents: dict[str, bytes] | None = None,
    input_bytes: bytes | None = None,
    native: FakeNative | None = None,
    process_factory: FakeProcessFactory | None = None,
    reader=None,
    writer: TrackingWriter | None = None,
    clock=None,
) -> tuple[int, list[dict], FakeNative | None, FakeProcessFactory, TrackingWriter]:
    native = native if native is not None else FakeNative(contents)
    process_factory = process_factory if process_factory is not None else FakeProcessFactory()
    writer = writer if writer is not None else TrackingWriter()
    args = {
        "native_factory": lambda: native,
        "process_factory": process_factory,
        "current_pid": lambda: KEEPER_IDENTITY.pid,
    }
    if reader is not None:
        args["reader"] = reader
    if clock is not None:
        args["clock"] = clock
    with patch.object(keeper, "PINNED_SHA256", DEFAULT_HASHES):
        status = keeper.run_stdio(io.BytesIO(_request_lines() if input_bytes is None else input_bytes), writer, **args)
    frames = [json.loads(line) for line in writer.getvalue().splitlines()]
    return status, frames, native, process_factory, writer


class PhysicalKeeperTests(unittest.TestCase):
    def test_pins_match_existing_source_probe_and_fixed_wrapper_relative_path(self) -> None:
        source = (ROOT / "agent" / "asar-source-probe.cjs").read_text(encoding="utf-8")
        expected = {
            "application.asar": "9C9959CFA351E8597C008C944DD482F826A24967ABA607F56350B06AC56200E2",
            "package.json": "15557B5A5A49A9C866D21048316DB50723642E13067CA12AFB36AF35ED3ECDC9",
            "wrapper.node": "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2",
        }
        self.assertEqual(keeper.PINNED_SHA256, expected)
        for name, digest in expected.items():
            self.assertIn(digest, source)
            self.assertIn(name, source)
        self.assertRegex(source, r'pathModule\.join\(appPath,\s*["\']wrapper\.node["\']\)')

    def test_ready_then_final_hashes_all_three_files_through_retained_handles(self) -> None:
        status, frames, native, processes, _ = _run()
        self.assertEqual(status, 0)
        self.assertEqual([frame["state"] for frame in frames], ["ready", "final"])
        self.assertEqual(set(frames[0]), {
            "schema", "nonce", "state", "keeperPid", "keeperCreationTime100ns", "kernelPid",
            "kernelCreationTime100ns", "connectionGeneration", "appPath", "files",
        })
        self.assertEqual(frames[0]["files"], frames[1]["files"])
        self.assertEqual([record["name"] for record in frames[0]["files"]], list(keeper.PINNED_SHA256))
        for record in frames[0]["files"]:
            self.assertEqual(set(record), {
                "name", "nativeVolumeSerial", "nativeFileId", "canonicalFinalPath", "size",
                "lastWriteTime100ns", "sha256",
            })
            self.assertEqual(record["sha256"], DEFAULT_HASHES[record["name"]])
            self.assertRegex(record["nativeFileId"], r"^[0-9A-F]{32}$")
            self.assertEqual(record["nativeVolumeSerial"], "1234")
        assert native is not None
        self.assertEqual(native.root_calls, ["C:"])
        for name in keeper.PINNED_SHA256:
            handle = native.handles[ntpath.join(keeper.APP_PATH, name)]
            self.assertEqual(native.seek_calls.count(handle), 2)
            self.assertGreaterEqual(sum(1 for read in native.read_calls if read[0] == handle), 2)
            self.assertEqual(sum(1 for call in native.open_calls if call[1] == name), 1)
        self.assertEqual(len(native.close_calls), len(set(native.close_calls)))
        self.assertEqual(native.closed_handles, set(native.paths))
        self.assertTrue(all(process.closed for process in processes.instances.values()))

    def test_component_opens_are_relative_and_reparse_components_reject_before_descendants(self) -> None:
        reparse_path = ntpath.join(r"C:\Program Files", "Tencent")
        native = FakeNative(reparse_path=reparse_path)
        status, frames, native, _, _ = _run(native=native)
        self.assertEqual(status, 1)
        self.assertEqual(frames, [{"schema": keeper.SCHEMA, "state": "rejected", "code": "component_reparse_rejected"}])
        self.assertFalse(any(call[1] == "QQNT" for call in native.open_calls))
        self.assertTrue(native.closed_handles)
        self.assertEqual(native.closed_handles, set(native.paths))
        self.assertTrue(all("\\" not in call[1] and "/" not in call[1] for call in native.open_calls))
        self.assertTrue(keeper.WindowsPhysicalNative.OBJ_DONT_REPARSE)
        self.assertTrue(keeper.WindowsPhysicalNative.FILE_OPEN_REPARSE_POINT)

    def test_only_bounded_pinned_names_are_opened_and_read_handles_are_readonly_share_read(self) -> None:
        status, _, native, _, _ = _run()
        self.assertEqual(status, 0)
        assert native is not None
        self.assertEqual([call[1] for call in native.open_calls[-3:]], list(keeper.PINNED_SHA256))
        self.assertTrue(all(call[3] == keeper.WindowsPhysicalNative.FILE_SHARE_READ for call in native.open_calls))
        self.assertEqual(keeper.FILE_SPECS, (
            ("application.asar", 64 * 1024 * 1024),
            ("package.json", 64 * 1024),
            ("wrapper.node", 128 * 1024 * 1024),
        ))

    def test_short_reads_are_bounded_and_complete_hash_still_matches(self) -> None:
        native = FakeNative(short_reads=3)
        status, frames, _, _, _ = _run(native=native)
        self.assertEqual(status, 0)
        self.assertEqual(frames[-1]["state"], "final")
        self.assertTrue(any(read[2] == 3 for read in native.read_calls))

    def test_hash_mismatch_rejects_and_does_not_emit_candidate_records(self) -> None:
        contents = dict(DEFAULT_CONTENTS)
        contents["application.asar"] = b"changed archive contents"
        status, frames, native, processes, _ = _run(contents=contents)
        self.assertEqual(status, 1)
        self.assertEqual(frames, [{"schema": keeper.SCHEMA, "state": "rejected", "code": "file_hash_mismatch"}])
        assert native is not None
        self.assertEqual(native.closed_handles, set(native.paths))
        self.assertTrue(all(process.closed for process in processes.instances.values()))

    def test_package_size_bound_is_enforced_before_read(self) -> None:
        contents = dict(DEFAULT_CONTENTS)
        contents["package.json"] = b"x" * (keeper.MAX_PACKAGE_BYTES + 1)
        native = FakeNative(contents)
        status, frames, native, _, _ = _run(native=native)
        self.assertEqual(status, 1)
        self.assertEqual(frames[-1]["code"], "file_size_exceeded")
        package_handle = native.handles[ntpath.join(keeper.APP_PATH, "package.json")]
        self.assertFalse(any(call[0] == package_handle for call in native.read_calls))
        self.assertEqual(native.closed_handles, set(native.paths))

    def test_native_read_exception_is_reduced_to_fixed_code(self) -> None:
        native = FakeNative(fail_read_name="application.asar")
        status, frames, native, _, _ = _run(native=native)
        self.assertEqual(status, 1)
        self.assertEqual(frames, [{"schema": keeper.SCHEMA, "state": "rejected", "code": "file_hash_unavailable"}])
        self.assertNotIn("fake private path", json.dumps(frames))
        self.assertEqual(native.closed_handles, set(native.paths))

    def test_held_file_metadata_change_rejects_on_finish(self) -> None:
        native = FakeNative(changed_path=ntpath.join(keeper.APP_PATH, "application.asar"))
        status, frames, native, _, _ = _run(native=native)
        self.assertEqual(status, 1)
        self.assertEqual([frame["state"] for frame in frames], ["ready", "rejected"])
        self.assertEqual(frames[-1]["code"], "file_metadata_changed")
        self.assertEqual(native.closed_handles, set(native.paths))

    def test_finish_nonce_mismatch_rejects_and_closes_every_handle(self) -> None:
        native = FakeNative()
        status, frames, native, _, _ = _run(native=native, input_bytes=_request_lines(finish_nonce="B" * 64))
        self.assertEqual(status, 1)
        self.assertEqual([frame["state"] for frame in frames], ["ready", "rejected"])
        self.assertEqual(frames[-1], {"schema": keeper.SCHEMA, "state": "rejected", "code": "nonce_mismatch"})
        self.assertEqual(native.closed_handles, set(native.paths))

    def test_kernel_pid_or_creation_change_rejects_after_ready(self) -> None:
        changed = keeper.ProcessIdentity(KERNEL_IDENTITY.pid, KERNEL_IDENTITY.creation_time_100ns + 1, KERNEL_IDENTITY.image_path)
        factory = FakeProcessFactory({
            KEEPER_IDENTITY.pid: [KEEPER_IDENTITY],
            KERNEL_IDENTITY.pid: [KERNEL_IDENTITY, KERNEL_IDENTITY, changed],
        })
        native = FakeNative()
        status, frames, native, factory, _ = _run(native=native, process_factory=factory)
        self.assertEqual(status, 1)
        self.assertEqual([frame["state"] for frame in frames], ["ready", "rejected"])
        self.assertEqual(frames[-1]["code"], "process_identity_changed")
        self.assertEqual(native.closed_handles, set(native.paths))
        self.assertTrue(all(process.closed for process in factory.instances.values()))

    def test_kernel_pid_change_rejects_after_ready(self) -> None:
        changed = keeper.ProcessIdentity(KERNEL_IDENTITY.pid + 1, KERNEL_IDENTITY.creation_time_100ns, KERNEL_IDENTITY.image_path)
        factory = FakeProcessFactory({
            KEEPER_IDENTITY.pid: [KEEPER_IDENTITY],
            KERNEL_IDENTITY.pid: [KERNEL_IDENTITY, KERNEL_IDENTITY, changed],
        })
        native = FakeNative()
        status, frames, native, factory, _ = _run(native=native, process_factory=factory)
        self.assertEqual(status, 1)
        self.assertEqual([frame["state"] for frame in frames], ["ready", "rejected"])
        self.assertEqual(frames[-1]["code"], "process_identity_changed")
        self.assertEqual(native.closed_handles, set(native.paths))
        self.assertTrue(all(process.closed for process in factory.instances.values()))

    def test_kernel_image_change_rejects_after_ready(self) -> None:
        changed = keeper.ProcessIdentity(KERNEL_IDENTITY.pid, KERNEL_IDENTITY.creation_time_100ns, r"C:\other\python.exe")
        factory = FakeProcessFactory({
            KEEPER_IDENTITY.pid: [KEEPER_IDENTITY],
            KERNEL_IDENTITY.pid: [KERNEL_IDENTITY, KERNEL_IDENTITY, changed],
        })
        status, frames, native, _, _ = _run(process_factory=factory)
        self.assertEqual(status, 1)
        self.assertEqual(frames[-1]["code"], "process_identity_changed")
        self.assertEqual(native.closed_handles, set(native.paths))

    def test_non_qq_kernel_image_is_rejected_before_opening_archive(self) -> None:
        wrong = keeper.ProcessIdentity(KERNEL_IDENTITY.pid, KERNEL_IDENTITY.creation_time_100ns, r"C:\other\QQ.exe")
        factory = FakeProcessFactory({
            KEEPER_IDENTITY.pid: [KEEPER_IDENTITY],
            KERNEL_IDENTITY.pid: [wrong],
        })
        native = FakeNative()
        status, frames, native, factory, _ = _run(native=native, process_factory=factory)
        self.assertEqual(status, 1)
        self.assertEqual(frames, [{"schema": keeper.SCHEMA, "state": "rejected", "code": "process_identity_mismatch"}])
        self.assertFalse(native.open_calls)
        self.assertTrue(all(process.closed for process in factory.instances.values()))

    def test_exited_kernel_process_handle_is_rejected(self) -> None:
        process = keeper.WindowsProcessHandle.__new__(keeper.WindowsProcessHandle)
        process.handle = 99
        process.kernel32 = FakeExitedKernel32()
        with self.assertRaises(keeper.KeeperReject) as caught:
            process.identity()
        self.assertEqual(caught.exception.code, "process_identity_changed")
        self.assertEqual(process.kernel32.query_image_calls, 0)

    def test_input_eof_timeout_unknown_schema_and_oversize_use_exact_rejection_frame(self) -> None:
        cases = [
            (b"", None, "input_eof"),
            (_request_lines().splitlines()[0] + b"\n", lambda *_: (_ for _ in ()).throw(keeper.KeeperReject("input_timeout")), "input_timeout"),
            (json.dumps({"schema": "wrong"}).encode() + b"\n", None, "input_schema_rejected"),
            (b" " * (keeper.MAX_INPUT_LINE_BYTES + 1) + b"\n", None, "input_too_large"),
        ]
        for payload, reader, code in cases:
            with self.subTest(code=code):
                status, frames, _, processes, _ = _run(input_bytes=payload, reader=reader)
                self.assertEqual(status, 1)
                self.assertEqual(frames, [{"schema": keeper.SCHEMA, "state": "rejected", "code": code}])
                self.assertEqual(set(frames[0]), {"schema", "state", "code"})
                self.assertFalse(processes.instances)

    def test_initial_binding_mismatch_and_unknown_finish_op_do_not_leak_values(self) -> None:
        initial = json.loads(_request_lines().splitlines()[0])
        initial["appPath"] = r"C:\not-the-pinned-app"
        status, frames, _, _, _ = _run(input_bytes=json.dumps(initial).encode() + b"\n")
        self.assertEqual(status, 1)
        self.assertEqual(frames[0]["code"], "app_path_rejected")

        initial_line = _request_lines().splitlines()[0]
        finish_line = json.dumps({"op": "other", "nonce": "A" * 64}).encode()
        native = FakeNative()
        status, frames, native, _, _ = _run(input_bytes=initial_line + b"\n" + finish_line + b"\n", native=native)
        self.assertEqual(status, 1)
        self.assertEqual([frame["state"] for frame in frames], ["ready", "rejected"])
        self.assertEqual(frames[-1]["code"], "unknown_operation")
        self.assertEqual(native.closed_handles, set(native.paths))

    def test_all_handles_close_before_final_and_close_failure_prevents_success(self) -> None:
        native = FakeNative()
        factory = FakeProcessFactory()

        def assert_open_at_ready() -> None:
            self.assertFalse(native.close_calls)
            self.assertTrue(all(not process.closed for process in factory.instances.values()))

        def assert_closed_before_final() -> None:
            self.assertEqual(native.closed_handles, set(native.paths))
            self.assertTrue(all(process.closed for process in factory.instances.values()))

        writer = TrackingWriter(on_final=assert_closed_before_final, on_ready=assert_open_at_ready)
        status, frames, native, factory, _ = _run(native=native, process_factory=factory, writer=writer)
        self.assertEqual(status, 0)
        self.assertEqual(frames[-1]["state"], "final")
        self.assertTrue(all(process.closed for process in factory.instances.values()))

        native = FakeNative(fail_close_handle=11)
        status, frames, native, _, _ = _run(native=native)
        self.assertEqual(status, 1)
        self.assertEqual(frames[-1], {"schema": keeper.SCHEMA, "state": "rejected", "code": "resource_close_failed"})
        self.assertEqual(native.closed_handles, set(native.paths))

    def test_native_relative_open_rejects_non_component_inputs_and_other_shares(self) -> None:
        native = keeper.WindowsPhysicalNative.__new__(keeper.WindowsPhysicalNative)
        native.calls = []
        native._open = lambda *args, **kwargs: native.calls.append((args, kwargs)) or 88
        self.assertEqual(native.open_child(77, "leaf", directory=False, share=keeper.WindowsPhysicalNative.FILE_SHARE_READ), 88)
        self.assertEqual(native.calls[0][0], ("leaf", 77))
        for bad in ("..", ".", r"a\b", "a/b", "C:", ""):
            with self.subTest(component=bad):
                with self.assertRaises(keeper.KeeperReject):
                    native.open_child(77, bad, directory=False, share=keeper.WindowsPhysicalNative.FILE_SHARE_READ)
        with self.assertRaises(keeper.KeeperReject):
            native.open_child(77, "leaf", directory=False, share=0x7)


if __name__ == "__main__":
    unittest.main()
