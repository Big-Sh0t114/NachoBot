from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from service.config import load_config
from service.guard_setup import artifact_guard, prepare_guard

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT.parent / "NachoBot/plugins/qqcall_plugin/config.example.toml"


def inventory():
    return [{"id": "capture-id", "name": "Playback (VB-Audio Virtual Cable)", "flow": "capture"},
            {"id": "render-id", "name": "Capture (VB-Audio Virtual Cable)", "flow": "render"},
            {"id": "playback-id", "name": "Playback (VB-Audio Virtual Cable)", "flow": "render"}]


class GuardSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "NachoBot/plugins/qqcall_plugin/config.toml"
        self.config.parent.mkdir(parents=True)
        text = EXAMPLE.read_text(encoding="utf-8").replace("enabled = false", "enabled = true", 1)
        text = text.replace('account_id = ""', 'account_id = "123456789"', 1)
        text = text.replace('url = ""', 'url = "http://127.0.0.1:49999"', 1)
        text = text.replace('qq_exe = ""', 'qq_exe = "C:/fixture/QQ.exe"', 1)
        self.config.write_text(text, encoding="utf-8")
        self.manifest = self.root / "artifacts/artifact-manifest.json"
        self.manifest.parent.mkdir()
        records = []
        for name in ("QQCallAudioRouteGuard-v2.node", "QQCallAudioRouteGuard-v2.dll"):
            value = name.encode()
            self.manifest.with_name(name).write_bytes(value)
            records.append({"name": name, "sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)})
        self.manifest.write_text(json.dumps({"schema": 1, "qqBuild": "9.9.26-44343", "artifacts": records}), encoding="utf-8")
        self.target = Path(load_config(self.config)["native"]["guard_config_file"])

    def prepare(self, **kwargs):
        return prepare_guard(self.config, self.manifest, inventory=kwargs.pop("inventory", inventory),
                             project_root=self.root / "NachoBot-QQCall", **kwargs)

    def test_default_path_dry_run_and_creation_preserve_canonical_config(self):
        original = self.config.read_bytes()
        self.assertTrue(load_config(self.config)["audio_guard_config_file_defaulted"])
        self.assertEqual(self.prepare()["phase"], "guard_prepared")
        self.assertFalse(self.target.parent.exists())
        self.assertEqual(self.prepare(apply=True)["phase"], "guard_created")
        self.assertEqual(self.config.read_bytes(), original)
        value = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(value["captureEndpointId"], "capture-id")
        self.assertEqual(value["renderEndpointId"], "render-id")
        self.assertEqual(set(value), {"version", "qqBuild", "hostGeneration", "nodeArtifact", "nodeSha256",
                                     "guardDllPath", "dllSha256", "captureEndpointId", "renderEndpointId"})

    def test_existing_guard_is_validated_and_retained_without_build_manifest(self):
        self.prepare(apply=True)
        original = self.target.read_bytes()
        self.manifest.unlink()
        result = self.prepare(apply=True)
        self.assertEqual(result, {"ok": True, "phase": "existing_guard_valid", "changed": False})
        self.assertEqual(self.target.read_bytes(), original)

    def test_invalid_existing_guard_is_not_overwritten(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"invalid")
        with self.assertRaisesRegex(RuntimeError, "audio_guard_config_unavailable"):
            self.prepare(apply=True)
        self.assertEqual(self.target.read_bytes(), b"invalid")

    def test_ambiguous_virtual_routes_require_public_overrides(self):
        records = inventory() + [{"id": "other-render", "name": "Other (VB-Audio Virtual Cable)", "flow": "render"}]
        with self.assertRaisesRegex(ValueError, "audio_route_selection_required"):
            self.prepare(apply=True, inventory=lambda: records)
        self.assertFalse(self.target.exists())
        text = self.config.read_text(encoding="utf-8").replace('capture_device = ""',
                                                              'capture_device = "Capture (VB-Audio Virtual Cable) (loopback)"')
        self.config.write_text(text, encoding="utf-8")
        self.assertEqual(self.prepare(apply=True, inventory=lambda: records)["phase"], "guard_created")

    def test_physical_routes_are_never_selected(self):
        records = [{**item, "name": "Physical audio"} for item in inventory()]
        with self.assertRaisesRegex(ValueError, "audio_route_selection_unavailable"):
            self.prepare(apply=True, inventory=lambda: records)
        self.assertFalse(self.target.exists())

    def test_hotplug_and_configuration_change_before_publish_leave_no_guard(self):
        count = 0

        def hotplug():
            nonlocal count
            count += 1
            records = inventory()
            if count > 1:
                records[-1]["id"] = "replacement-playback-id"
            return records

        with self.assertRaisesRegex(ValueError, "audio_guard_preparation_changed"):
            self.prepare(apply=True, inventory=hotplug)
        self.assertFalse(self.target.exists())

        def changed_config():
            self.config.write_text(self.config.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
            return inventory()

        with self.assertRaisesRegex(ValueError, "audio_guard_configuration_changed"):
            self.prepare(apply=True, inventory=changed_config)
        self.assertFalse(self.target.exists())

    def test_artifact_damage_is_rejected_without_publication(self):
        self.manifest.with_name("QQCallAudioRouteGuard-v2.node").write_bytes(b"damaged")
        with self.assertRaises(ValueError):
            artifact_guard(self.manifest)
        self.assertFalse(self.target.exists())

    def test_disabled_configuration_does_not_read_devices_or_artifacts(self):
        self.config.write_text(self.config.read_text(encoding="utf-8").replace("enabled = true", "enabled = false", 1), encoding="utf-8")
        self.manifest.unlink()

        def forbidden_inventory():
            self.fail("disabled preparation must not inspect endpoints")

        self.assertEqual(self.prepare(apply=True, inventory=forbidden_inventory)["phase"], "disabled")
        self.assertFalse(self.target.parent.exists())

    def test_concurrent_publication_keeps_the_winning_valid_guard(self):
        def publish_other(path, value):
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({**value, "hostGeneration": 9}), encoding="utf-8")
            return False

        with patch("service.guard_setup.atomic_private_json_if_absent", side_effect=publish_other):
            self.assertEqual(self.prepare(apply=True)["phase"], "existing_guard_valid")
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))["hostGeneration"], 9)

    def test_artifact_change_between_inventory_reads_prevents_publication(self):
        count = 0

        def changed_artifact():
            nonlocal count
            count += 1
            if count == 2:
                self.manifest.with_name("QQCallAudioRouteGuard-v2.node").write_bytes(b"damaged")
            return inventory()

        with self.assertRaisesRegex(ValueError, "audio_guard_artifact_invalid"):
            self.prepare(apply=True, inventory=changed_artifact)
        self.assertFalse(self.target.exists())

    def test_missing_explicit_output_outside_runtime_is_rejected(self):
        outside = self.root / "outside.private.json"
        text = self.config.read_text(encoding="utf-8").replace('guard_config_file = ""',
                                                              f"guard_config_file = {json.dumps(str(outside))}")
        self.config.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "audio_guard_output_outside_runtime"):
            self.prepare(apply=True)
        self.assertFalse(outside.exists())
