from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live2d_web  # noqa: E402


def _write_model(directory: Path, *, name: str = "avatar", basename: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = basename or name
    (directory / f"{stem}.moc3").write_bytes(b"moc")
    (directory / "texture.png").write_bytes(b"texture")
    (directory / "shy.exp3.json").write_text('{"Parameters":[]}', encoding="utf-8")
    (directory / "angry.exp3.json").write_text('{"Parameters":[]}', encoding="utf-8")
    (directory / "nod.motion3.json").write_text('{"Meta":{}}', encoding="utf-8")
    descriptor = directory / f"{stem}.model3.json"
    descriptor.write_text(
        json.dumps(
            {
                "Version": 3,
                "FileReferences": {
                    "Moc": f"{stem}.moc3",
                    "Textures": ["texture.png"],
                    "Expressions": [
                        {"Name": "shy", "File": "shy.exp3.json"},
                        {"Name": "angry", "File": "angry.exp3.json"},
                    ],
                    "Motions": {"Nod": [{"File": "nod.motion3.json"}]},
                },
                "Groups": [
                    {"Target": "Parameter", "Name": "LipSync", "Ids": ["ParamMouthOpenY"]}
                ],
            }
        ),
        encoding="utf-8",
    )
    return descriptor


class Live2DWebManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.core = self.root / "core.js"
        self.core.write_text("core", encoding="utf-8")

    def manager(self, adapter_dir: Path | None = None) -> live2d_web.Live2DWebManager:
        return live2d_web.Live2DWebManager(adapter_dir=adapter_dir or self.root, core_file=self.core)

    def test_discovers_nested_duplicate_basenames_with_stable_relative_ids(self):
        first = _write_model(self.root / "resources" / "sample" / "runtime", basename="hiyori")
        second = _write_model(self.root / "models" / "copy" / "runtime", basename="hiyori")
        manager = self.manager()

        listing = manager.list_models()
        entries = listing["models"]
        self.assertTrue(listing["available"])
        self.assertEqual(listing["reason"], "ready")
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(entries[0]["id"], entries[1]["id"])
        self.assertNotEqual(entries[0]["name"], entries[1]["name"])
        self.assertTrue(all("model_url" in entry for entry in entries))
        self.assertTrue(all("ParamMouthOpenY" in entry["lip_sync_parameters"] for entry in entries))
        serialized = json.dumps(entries)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("auth_token", serialized)
        self.assertEqual(manager.get_model(entries[0]["id"])["id"], entries[0]["id"])
        self.assertTrue(first.is_file() and second.is_file())

    def test_missing_resources_invalid_json_and_external_references_are_skipped(self):
        good = _write_model(self.root / "good")
        missing = _write_model(self.root / "missing")
        (missing.parent / "texture.png").unlink()
        external = _write_model(self.root / "external")
        payload = json.loads(external.read_text(encoding="utf-8"))
        payload["FileReferences"]["Moc"] = "https://example.invalid/avatar.moc3"
        external.write_text(json.dumps(payload), encoding="utf-8")
        invalid_dir = self.root / "invalid"
        invalid_dir.mkdir()
        (invalid_dir / "broken.model3.json").write_text("{no json", encoding="utf-8")

        entries = self.manager().list_models()["models"]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["model_url"].endswith(good.name))

    def test_declared_assets_only_and_encoded_traversal_are_rejected(self):
        _write_model(self.root / "bundle")
        (self.root / "bundle" / "secret.txt").write_text("not declared", encoding="utf-8")
        manager = self.manager()
        entry = manager.list_models()["models"][0]
        descriptor_path = Path(entry["model_url"].rsplit("/", 1)[-1])
        self.assertEqual(manager.resolve_asset(entry["id"], descriptor_path.name)[1], "application/json")
        self.assertEqual(manager.resolve_asset(entry["id"], "texture.png")[0].name, "texture.png")
        for path in ("secret.txt", "../secret.txt", "%2e%2e/secret.txt", "https://evil.invalid/file"):
            with self.subTest(path=path), self.assertRaises(live2d_web.Live2DModelError):
                manager.resolve_asset(entry["id"], path)

    def test_symlink_escape_is_not_discovered_or_served(self):
        bundle = self.root / "bundle"
        descriptor = _write_model(bundle)
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside")
        texture = bundle / "texture.png"
        texture.unlink()
        try:
            os.symlink(outside, texture)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this Windows host")
        manager = self.manager()
        self.assertEqual(manager.list_models()["models"], [])
        with self.assertRaises(live2d_web.Live2DModelError):
            manager.resolve_asset("l2d-unknown", "texture.png")
        self.assertTrue(descriptor.is_file())

    def test_invalid_reply_normalizes_as_plain_text_without_controls(self):
        _write_model(self.root / "bundle")
        manager = self.manager()
        model_id = manager.list_models()["models"][0]["id"]
        prepared = manager.prepare_reply("call-a", model_id, "{not valid json")
        self.assertEqual(prepared["reply"], "{not valid json")
        self.assertEqual(manager.apply_control("call-a", prepared["control_id"]), [])

    def test_call_pipelines_are_isolated_and_controls_apply_once(self):
        _write_model(self.root / "bundle")
        manager = self.manager()
        model_id = manager.list_models()["models"][0]["id"]
        first = manager.prepare_reply(
            "call-a",
            model_id,
            json.dumps({"reply": "one", "emotion": "shy", "action": "点头/同意"}, ensure_ascii=False),
        )
        second = manager.prepare_reply(
            "call-b",
            model_id,
            json.dumps({"reply": "two", "emotion": "angry", "action": "点头/同意"}, ensure_ascii=False),
        )
        self.assertEqual(first["reply"], "one")
        self.assertEqual(second["reply"], "two")
        self.assertEqual(
            manager.apply_control("call-a", first["control_id"]),
            [
                {"type": "expression", "name": "shy"},
                {"type": "motion", "group": "Nod"},
            ],
        )
        self.assertEqual(manager.apply_control("call-a", first["control_id"]), [])
        self.assertEqual(
            manager.apply_control("call-b", second["control_id"]),
            [
                {"type": "expression", "name": "angry"},
                {"type": "motion", "group": "Nod"},
            ],
        )
        manager.discard("call-a")
        manager.discard("call-b")
        self.assertEqual(manager.apply_control("call-a", first["control_id"]), [])

    def test_new_model_is_found_after_scan_interval(self):
        manager = self.manager()
        self.assertEqual(manager.list_models()["models"], [])
        _write_model(self.root / "added-later")
        manager.scan_interval_seconds = 0
        self.assertEqual(len(manager.list_models()["models"]), 1)

    def test_missing_core_is_reported_without_hiding_scanned_models(self):
        _write_model(self.root / "bundle")
        manager = live2d_web.Live2DWebManager(
            adapter_dir=self.root,
            core_file=self.root / "missing-core.js",
        )
        listing = manager.list_models()
        self.assertFalse(listing["available"])
        self.assertEqual(listing["reason"], "missing_core")
        self.assertEqual(len(listing["models"]), 1)


if __name__ == "__main__":
    unittest.main()
