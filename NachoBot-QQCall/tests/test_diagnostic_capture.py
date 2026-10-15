from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path

from qqcall_pcm.diagnostic_capture import BoundedPcmSnapshot, _process_current

SCOPE = {"kind": "qqcall_group", "serviceInstance": "123e4567-e89b-42d3-a456-426614174000",
         "hostGeneration": 7, "connectionGeneration": 8, "groupId": "12345", "roomId": "12345", "roomGeneration": 1}
CAPTURE_ID = "723e4567-e89b-42d3-a456-426614174000"


class SnapshotTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "native Windows process identity")
    def test_installed_pcm_identity_check_has_no_supervisor_package_dependency(self):
        from service.windows import WindowsSystem
        from unittest.mock import patch
        identity = WindowsSystem().process_identity(os.getpid())
        self.assertIsNotNone(identity)
        with patch.dict("sys.modules", {"service": None, "service.windows": None}):
            self.assertTrue(_process_current(os.getpid(), identity[0]))
            self.assertFalse(_process_current(os.getpid(), identity[0] + 1))
            self.assertFalse(_process_current(0, identity[0]))

    def rig(self, root):
        at = 1_800_000_000_000
        snapshot = BoundedPcmSnapshot(root=root, process_current=lambda _pid, _ft: True)
        snapshot.directory.mkdir(parents=True)
        snapshot.settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = {"schema": 1, "callerPid": 22, "callerCreationTime100ns": "123456789",
                    "verifiedByVisibleQQSettings": True, "observedAtMs": at,
                    "inputName": "麦克风 (Steam Streaming Microphone)", "outputName": "扬声器 (Steam Streaming Speakers)"}
        snapshot.settings_path.write_text(json.dumps(settings), encoding="utf-8")
        request = {"schema": 1, "captureId": CAPTURE_ID, "scope": dict(SCOPE),
                   "startAtMs": at + 1000, "endAtMs": at + 2000, "syntheticOnly": True,
                   "callerPid": 22, "callerCreationTime100ns": "123456789", "settingsObservedAtMs": at}
        snapshot.request_path.write_text(json.dumps(request), encoding="utf-8")
        return snapshot, request, at

    async def finish(self, snapshot):
        if snapshot.writes:
            await asyncio.gather(*snapshot.writes)

    def test_exact_scope_window_retains_only_accepted_canonical_frames_and_never_overwrites(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                snapshot, request, at = self.rig(Path(directory))
                metadata = {"scope": dict(SCOPE), "sample_counter": 100, "sample_count": 320}
                snapshot.observe(b"\x01\x00" * 320, metadata, at)
                self.assertFalse(snapshot.pcm)
                snapshot.observe(b"\x01\x00" * 320, metadata, at + 1000)
                metadata["sample_counter"] = 420
                snapshot.observe(b"\x02\x00" * 320, metadata, at + 1020)
                snapshot.observe(b"\x03\x00" * 320, {**metadata, "sample_counter": 740}, at + 2000)
                await self.finish(snapshot)
                path = snapshot.directory / f"{CAPTURE_ID}.private.wav"
                with wave.open(str(path), "rb") as wav:
                    self.assertEqual(wav.getnframes(), 640)
                    self.assertEqual(wav.readframes(640), b"\x01\x00" * 320 + b"\x02\x00" * 320)
                saved = path.read_bytes()
                snapshot.observe(b"\x04\x00" * 320, metadata, at + 1500)
                self.assertEqual(path.read_bytes(), saved)
                report = json.loads((snapshot.directory / f"{CAPTURE_ID}.safe.json").read_text())
                self.assertTrue(report["complete"])
                self.assertEqual(report["frames"][0][0], at + 1000)
        asyncio.run(run())

    def test_missing_stale_or_nonisolated_settings_and_forged_scope_never_retain_audio(self):
        async def run():
            for case in ("missing", "stale", "input", "scope", "principal", "oversized_window", "real_audio"):
                with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                    snapshot, request, at = self.rig(Path(directory))
                    settings = json.loads(snapshot.settings_path.read_text())
                    if case == "missing": snapshot.settings_path.unlink()
                    elif case == "stale": settings["observedAtMs"] = at - 400000
                    elif case == "input": settings["inputName"] = "system microphone"
                    elif case == "scope": request["scope"]["roomGeneration"] = 2
                    elif case == "principal": snapshot.process_current = lambda _pid, _ft: False
                    elif case == "oversized_window": request["endAtMs"] = at + 60000
                    elif case == "real_audio": request["syntheticOnly"] = False
                    if case != "missing": snapshot.settings_path.write_text(json.dumps(settings))
                    snapshot.request_path.write_text(json.dumps(request))
                    snapshot.observe(b"\x01\x00" * 320, {"scope": dict(SCOPE), "sample_counter": 0, "sample_count": 320}, at + 1000)
                    self.assertIsNone(snapshot.request)
                    self.assertFalse(snapshot.pcm)
        asyncio.run(run())

    def test_scope_change_sample_gap_and_request_revocation_abort_without_wav(self):
        async def run():
            for cause in ("scope_changed", "sample_discontinuity", "request_revoked", "capture_stopped"):
                with self.subTest(cause=cause), tempfile.TemporaryDirectory() as directory:
                    snapshot, request, at = self.rig(Path(directory))
                    metadata = {"scope": dict(SCOPE), "sample_counter": 0, "sample_count": 320}
                    snapshot.observe(b"\x01\x00" * 320, metadata, at + 1000)
                    next_metadata = copy.deepcopy(metadata)
                    next_metadata["sample_counter"] = 320
                    if cause == "scope_changed": next_metadata["scope"]["roomGeneration"] = 2
                    elif cause == "sample_discontinuity": next_metadata["sample_counter"] = 321
                    elif cause == "request_revoked": snapshot.request_path.unlink()
                    else: snapshot.close()
                    snapshot.observe(b"\x01\x00" * 320, next_metadata, at + 1300)
                    await self.finish(snapshot)
                    self.assertFalse((snapshot.directory / f"{CAPTURE_ID}.private.wav").exists())
                    report = json.loads((snapshot.directory / f"{CAPTURE_ID}.safe.json").read_text())
                    self.assertFalse(report["complete"])
                    self.assertEqual(report["outcome"], cause)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
