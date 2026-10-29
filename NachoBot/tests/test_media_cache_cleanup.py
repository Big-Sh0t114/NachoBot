from __future__ import annotations

import os
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from peewee import AutoField, BooleanField, FloatField, Model, SqliteDatabase, TextField


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_isolated_cleanup():
    """Load the cleaner directly, without importing the host src.chat package."""
    module_names = ["src", "src.common", "src.common.media_conversion_cache"]
    previous = {name: sys.modules.get(name) for name in module_names}

    def install(name: str):
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
        return module

    def load_module(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise AssertionError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    try:
        install("src")
        install("src.common")
        load_module(
            "src.common.media_conversion_cache",
            PROJECT_ROOT / "src" / "common" / "media_conversion_cache.py",
        )
        cleanup_module = load_module(
            "_nachobot_media_cache_cleanup_test",
            PROJECT_ROOT / "src" / "chat" / "emoji_system" / "media_cache_cleanup.py",
        )
        return cleanup_module.cleanup_media_cache
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


cleanup_media_cache = _load_isolated_cleanup()


class MediaCacheCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "data"
        self.root.mkdir()
        for directory in ("image", "images", "emoji", "video", "emoji_registed"):
            (self.root / directory).mkdir()
        self.now = 2_000_000_000.0
        self.db = SqliteDatabase(str(Path(self.temp_dir.name) / "cache.sqlite"))

        class BaseModel(Model):
            class Meta:
                database = self.db

        class Images(BaseModel):
            id = AutoField()
            image_id = TextField(default="")
            emoji_hash = TextField(default="")
            path = TextField()
            type = TextField(default="image")
            description = TextField(null=True)
            timestamp = FloatField(null=True)
            last_used_time = FloatField(null=True)
            no_file_flag = BooleanField(default=False)

        class ImageDescriptions(BaseModel):
            id = AutoField()
            image_description_hash = TextField()
            type = TextField()
            description = TextField()
            timestamp = FloatField(null=True)

        class Emoji(BaseModel):
            id = AutoField()
            full_path = TextField()
            is_registered = BooleanField(default=False)
            last_used_time = FloatField(null=True)

        self.Images = Images
        self.ImageDescriptions = ImageDescriptions
        self.Emoji = Emoji
        self.db.connect()
        self.db.create_tables([Images, ImageDescriptions, Emoji])
        self.addCleanup(self.db.close)

    def _write_old_file(self, relative_path: str, *, age_days: int = 60) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cache-file")
        old_time = self.now - age_days * 86_400
        os.utime(path, (old_time, old_time))
        return path

    def _image_record(self, path: Path, *, timestamp: float, last_used_time: float | None, media_type="image"):
        return self.Images.create(
            image_id=f"image-{path.name}-{self.Images.select().count()}",
            emoji_hash=f"hash-{path.name}-{self.Images.select().count()}",
            path=str(path),
            type=media_type,
            timestamp=timestamp,
            last_used_time=last_used_time,
            no_file_flag=False,
        )

    def _cleanup(self, **overrides):
        options = {
            "root": self.root,
            "images_model": self.Images,
            "descriptions_model": self.ImageDescriptions,
            "emoji_model": self.Emoji,
            "database": self.db,
            "image_file_retention_days": 14,
            "emoji_file_retention_days": 30,
            "description_retention_days": 30,
            "video_file_retention_days": 14,
            "batch_size": 100,
            "now": self.now,
        }
        options.update(overrides)
        return cleanup_media_cache(**options)

    def test_expired_file_is_removed_but_description_survives_until_its_own_expiry(self):
        path = self._write_old_file("images/cached.png", age_days=20)
        old_time = self.now - 20 * 86_400
        image_record = self._image_record(path, timestamp=old_time, last_used_time=old_time)
        description = self.ImageDescriptions.create(
            image_description_hash="cached-image",
            type="image",
            description="retained description",
            timestamp=self.now - 5 * 86_400,
        )

        result = self._cleanup()

        self.assertFalse(path.exists())
        self.assertTrue(self.Images.get_by_id(image_record.id).no_file_flag)
        self.assertEqual(result.files_deleted, 1)
        self.assertEqual(result.images_deleted, 0)
        self.assertIsNotNone(self.ImageDescriptions.get_by_id(description.id))

        image_record.last_used_time = self.now - 40 * 86_400
        image_record.save()
        result = self._cleanup()
        self.assertEqual(result.images_deleted, 1)
        self.assertIsNone(self.Images.get_or_none(self.Images.id == image_record.id))
        self.assertIsNotNone(self.ImageDescriptions.get_by_id(description.id))

        description.timestamp = self.now - 31 * 86_400
        description.save()
        result = self._cleanup()
        self.assertEqual(result.descriptions_deleted, 1)
        self.assertIsNone(self.ImageDescriptions.get_or_none(self.ImageDescriptions.id == description.id))

    def test_recently_used_record_protects_old_file_and_legacy_timestamp_is_fallback(self):
        protected = self._write_old_file("images/recent.png", age_days=90)
        self._image_record(
            protected,
            timestamp=self.now - 90 * 86_400,
            last_used_time=self.now - 1 * 86_400,
        )

        self._cleanup()

        self.assertTrue(protected.exists())

    def test_old_orphan_is_removed_but_unknown_extension_and_fresh_file_are_kept(self):
        orphan = self._write_old_file("image/orphan.jpg", age_days=40)
        unknown = self._write_old_file("image/notes.txt", age_days=400)
        fresh = self.root / "images" / "fresh.png"
        fresh.write_bytes(b"fresh")
        os.utime(fresh, (self.now, self.now))

        result = self._cleanup()

        self.assertFalse(orphan.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(result.files_deleted, 1)

    def test_registered_outside_scope_and_symlink_targets_are_protected(self):
        registered = self._write_old_file("emoji_registed/registered.png", age_days=400)
        self.Emoji.create(full_path=str(registered), is_registered=True, last_used_time=self.now - 400 * 86_400)
        outside = Path(self.temp_dir.name) / "outside.png"
        outside.write_bytes(b"outside")
        os.utime(outside, (self.now - 400 * 86_400, self.now - 400 * 86_400))
        self._image_record(outside, timestamp=self.now - 400 * 86_400, last_used_time=self.now - 400 * 86_400)

        target = Path(self.temp_dir.name) / "symlink-target.png"
        target.write_bytes(b"outside target")
        os.utime(target, (self.now - 400 * 86_400, self.now - 400 * 86_400))
        link = self.root / "images" / "link.png"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not available in this environment")

        self._cleanup()

        self.assertTrue(registered.exists())
        self.assertTrue(outside.exists())
        self.assertTrue(target.exists())
        self.assertTrue(link.is_symlink())

    def test_registered_hardlink_is_protected(self):
        registered = self._write_old_file("emoji_registed/registered.png", age_days=400)
        cache_link = self.root / "images" / "registered-copy.png"
        try:
            os.link(registered, cache_link)
        except OSError:
            self.skipTest("hard links are not available in this environment")
        self.Emoji.create(full_path=str(registered), is_registered=True, last_used_time=self.now - 400 * 86_400)

        self._cleanup()

        self.assertTrue(registered.exists())
        self.assertTrue(cache_link.exists())

    def test_an_old_hardlink_is_protected_when_another_record_used_the_same_file(self):
        old_link = self._write_old_file("images/old.png", age_days=60)
        recent_link = self.root / "image" / "recent.png"
        try:
            os.link(old_link, recent_link)
        except OSError:
            self.skipTest("hard links are not available in this environment")
        old_time = self.now - 60 * 86_400
        self._image_record(old_link, timestamp=old_time, last_used_time=old_time)
        self._image_record(recent_link, timestamp=old_time, last_used_time=self.now - 1 * 86_400)

        self._cleanup()

        self.assertTrue(old_link.exists())
        self.assertTrue(recent_link.exists())

    def test_cleanup_respects_delete_batch_size(self):
        files = [self._write_old_file(f"images/orphan-{index}.png") for index in range(3)]

        result = self._cleanup(batch_size=1)

        self.assertEqual(result.files_deleted, 1)
        self.assertEqual(sum(path.exists() for path in files), 2)

    def test_database_scan_failure_fails_closed_before_deleting_files(self):
        old_file = self._write_old_file("images/orphan.png", age_days=90)

        with mock.patch.object(self.Images, "select", side_effect=RuntimeError("db scan failed")):
            with self.assertRaisesRegex(RuntimeError, "db scan failed"):
                self._cleanup()

        self.assertTrue(old_file.exists())


if __name__ == "__main__":
    unittest.main()
