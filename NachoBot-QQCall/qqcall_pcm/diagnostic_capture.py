"""Opt-in, bounded snapshots of accepted PCM from an existing group stream.

This never opens an audio endpoint. A local synthetic-test request must name
the exact live room and a fresh observed isolated caller input/output pair.
No request means no retained PCM. Snapshot failures never affect transport.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import wave
from pathlib import Path
from typing import Any

from .group_protocol import validate_group_scope

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_KEYS = {"schema", "captureId", "scope", "startAtMs", "endAtMs", "syntheticOnly",
         "callerPid", "callerCreationTime100ns", "settingsObservedAtMs"}
_MAX_BYTES = 45 * 16000 * 2


def _read_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("linked_request")
    with path.open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("oversized_request")
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_request_key")
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("invalid_request")
    return value


def _process_current(pid: int, creation_time: int) -> bool:
    # The installed PCM package does not include the supervisor's service package.
    # Query the pinned incarnation directly, with no extra package dependency.
    if os.name != "nt" or type(pid) is not int or pid <= 0 or type(creation_time) is not int:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] * 5
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x00100000 | 0x00001000, 0, pid)
        if not handle:
            return False
        current = False
        try:
            created, exited, kernel, user = (ctypes.c_uint64() for _ in range(4))
            current = bool(kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                                   ctypes.byref(kernel), ctypes.byref(user)))
            current = current and created.value == creation_time and kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            closed = bool(kernel32.CloseHandle(handle))
        return current and closed
    except (OSError, AttributeError, TypeError, ValueError, ctypes.ArgumentError):
        return False


class BoundedPcmSnapshot:
    def __init__(self, *, root: Path | None = None, process_current=None):
        self.directory = (root or Path(__file__).resolve().parents[1]) / ".runtime" / "pcm-diagnostics"
        self.request_path = self.directory / "request.private.json"
        self.settings_path = self.directory / "caller-settings.private.json"
        self.process_current = process_current or _process_current
        self.next_read_ms = 0
        self.request = None
        self.scope = None
        self.pcm = bytearray()
        self.frames = []
        self.next_sample = None
        self.seen = set()
        self.timer = None
        self.writes = set()

    def _arm(self, request, scope, at_ms):
        if set(request) != _KEYS or type(request["schema"]) is not int or request["schema"] != 1 or request["syntheticOnly"] is not True:
            return
        capture_id = request["captureId"]
        if not isinstance(capture_id, str) or not _UUID.fullmatch(capture_id) or capture_id in self.seen:
            return
        candidate_scope = validate_group_scope(request["scope"])
        if candidate_scope != scope:
            return
        start, end = request["startAtMs"], request["endAtMs"]
        if any(type(value) is not int for value in (start, end, request["callerPid"], request["settingsObservedAtMs"])):
            return
        if not 0 < end - start <= 45000 or not start - 2000 <= at_ms < end:
            return
        creation = request["callerCreationTime100ns"]
        if not isinstance(creation, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", creation):
            return
        settings = _read_object(self.settings_path)
        if (settings.get("schema") != 1 or settings.get("verifiedByVisibleQQSettings") is not True
                or settings.get("callerPid") != request["callerPid"]
                or settings.get("callerCreationTime100ns") != creation
                or settings.get("observedAtMs") != request["settingsObservedAtMs"]
                or not 0 <= at_ms - request["settingsObservedAtMs"] <= 300000
                or settings.get("inputName") != "麦克风 (Steam Streaming Microphone)"
                or settings.get("outputName") != "扬声器 (Steam Streaming Speakers)"
                or not self.process_current(request["callerPid"], int(creation))):
            return
        if not self.directory.is_dir() or self.directory.is_symlink():
            return
        if any((self.directory / f"{capture_id}.{suffix}").exists() for suffix in ("private.wav", "safe.json")):
            return
        self.request, self.scope = dict(request), dict(candidate_scope)
        self.seen.add(capture_id)
        if len(self.seen) > 256:
            self.seen = {capture_id}
        self.pcm.clear()
        self.frames.clear()
        self.next_sample = None
        self.timer = asyncio.get_running_loop().call_later(max(0, (end - at_ms) / 1000), self._finish, "completed")

    def observe(self, pcm: bytes, metadata: dict, at_ms: int):
        if not isinstance(metadata.get("scope"), dict) or metadata["scope"].get("kind") != "qqcall_group":
            return
        if self.request and metadata["scope"] != self.scope:
            self._finish("scope_changed")
            return
        if at_ms >= self.next_read_ms:
            self.next_read_ms = at_ms + 250
            try:
                request = _read_object(self.request_path)
                if self.request:
                    if request != self.request:
                        self._finish("request_revoked")
                        return
                else:
                    self._arm(request, metadata["scope"], at_ms)
            except Exception:
                if self.request:
                    self._finish("request_revoked")
                return
        if not self.request or at_ms < self.request["startAtMs"]:
            return
        if at_ms >= self.request["endAtMs"]:
            self._finish("completed")
            return
        sample = metadata.get("sample_counter")
        count = metadata.get("sample_count")
        if (type(pcm) is not bytes or type(sample) is not int or type(count) is not int
                or count <= 0 or count * 2 != len(pcm) or (self.next_sample is not None and sample != self.next_sample)):
            self._finish("sample_discontinuity")
            return
        if len(self.pcm) + len(pcm) > _MAX_BYTES or len(self.frames) >= 4096:
            self._finish("size_limit")
            return
        self.frames.append([at_ms, len(self.pcm) // 2, count, sample])
        self.pcm.extend(pcm)
        self.next_sample = sample + count

    def close(self):
        self._finish("capture_stopped")

    def _finish(self, outcome):
        if not self.request:
            return
        if self.timer:
            self.timer.cancel()
            self.timer = None
        request, frames, pcm = self.request, self.frames, bytes(self.pcm)
        self.request = self.scope = None
        self.frames, self.pcm = [], bytearray()
        self.next_sample = None
        report = {"schema": 1, "captureId": request["captureId"], "syntheticOnly": True,
                  "existingCaptureStreamOnly": True, "isPostQQTransportCapture": True,
                  "hostGeneration": request["scope"]["hostGeneration"],
                  "connectionGeneration": request["scope"]["connectionGeneration"],
                  "roomGeneration": request["scope"]["roomGeneration"],
                  "startAtMs": request["startAtMs"], "endAtMs": request["endAtMs"],
                  "outcome": outcome, "complete": outcome == "completed" and bool(pcm),
                  "sampleCount": len(pcm) // 2, "frames": frames}

        def write():
            try:
                if report["complete"]:
                    with (self.directory / f"{request['captureId']}.private.wav").open("xb") as stream:
                        with wave.open(stream, "wb") as wav:
                            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                            wav.writeframes(pcm)
                with (self.directory / f"{request['captureId']}.safe.json").open("x", encoding="utf-8") as stream:
                    json.dump(report, stream)
            except Exception:
                pass

        task = asyncio.create_task(asyncio.to_thread(write))
        self.writes.add(task)
        task.add_done_callback(self.writes.discard)
