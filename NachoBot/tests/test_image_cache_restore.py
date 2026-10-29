from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import io
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock

from peewee import AutoField, BooleanField, FloatField, IntegerField, Model, SqliteDatabase, TextField
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def isolated_image_modules(database, images_model, descriptions_model):
    """Load the real image manager and file helpers with stubbed host modules."""
    module_names = [
        "src",
        "src.common",
        "src.common.logger",
        "src.common.media_conversion_cache",
        "src.common.database",
        "src.common.database.database",
        "src.common.database.database_model",
        "src.config",
        "src.config.config",
        "src.llm_models",
        "src.llm_models.utils_model",
        "src.chat",
        "src.chat.utils",
        "src.chat.utils.visual_policy",
        "src.chat.utils.utils_image",
        "src.chat.emoji_system",
        "src.chat.emoji_system.emoji_manager",
        "src.chat.emoji_system.media_cache_cleanup",
    ]
    previous = {name: sys.modules.get(name) for name in module_names}

    def install(name: str, **attributes):
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
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

    for package_name in (
        "src",
        "src.common",
        "src.common.database",
        "src.config",
        "src.llm_models",
        "src.chat",
        "src.chat.utils",
        "src.chat.emoji_system",
    ):
        install(package_name).__path__ = []

    class Logger:
        def __getattr__(self, _level):
            return lambda *_args, **_kwargs: None

    class VisualTaskPolicy:
        cache_type = "image"
        prompt = "describe image"
        gif_prompt = "describe gif"
        temperature = 0.4
        max_tokens = 300
        extra_params: dict = {}

    class EmojiManagerStub:
        async def get_emoji_tag_by_hash(self, _emoji_hash, image_bytes=None):
            return None

    install("src.common.logger", get_logger=lambda _name: Logger())
    install("src.common.database.database", db=database)
    install("src.common.database.database_model", Images=images_model, ImageDescriptions=descriptions_model)
    install(
        "src.config.config",
        model_config=types.SimpleNamespace(model_task_config=types.SimpleNamespace(utils_small="test-model")),
    )
    install("src.llm_models.utils_model", LLMRequest=object)
    install(
        "src.chat.utils.visual_policy",
        CORE_GENERIC_EMOJI_PROMPT="emoji",
        CORE_GENERIC_GIF_EMOJI_PROMPT="gif",
        CORE_GENERIC_IMAGE_PROMPT="image",
        VisualTaskPolicy=VisualTaskPolicy,
        resolve_visual_task_policy=lambda *_args, **_kwargs: VisualTaskPolicy(),
        scoped_media_hash=lambda image_hash, _policy: image_hash,
    )
    install("src.chat.emoji_system.emoji_manager", get_emoji_manager=lambda: EmojiManagerStub())
    load_module(
        "src.common.media_conversion_cache",
        PROJECT_ROOT / "src" / "common" / "media_conversion_cache.py",
    )
    load_module(
        "src.chat.emoji_system.media_cache_cleanup",
        PROJECT_ROOT / "src" / "chat" / "emoji_system" / "media_cache_cleanup.py",
    )
    image_module = load_module("src.chat.utils.utils_image", PROJECT_ROOT / "src" / "chat" / "utils" / "utils_image.py")
    try:
        yield image_module
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


@contextmanager
def isolated_emoji_manager_module(database, emoji_model):
    """Load the production EmojiManager class with stubbed host packages."""
    module_names = [
        "src",
        "src.common",
        "src.common.logger",
        "src.common.media_conversion_cache",
        "src.common.database",
        "src.common.database.database",
        "src.common.database.database_model",
        "src.config",
        "src.config.config",
        "src.llm_models",
        "src.llm_models.utils_model",
        "src.chat",
        "src.chat.utils",
        "src.chat.utils.utils_image",
        "src.chat.emoji_system",
        "src.chat.emoji_system.media_cache_cleanup",
        "src.chat.emoji_system.emoji_manager",
    ]
    previous = {name: sys.modules.get(name) for name in module_names}

    def install(name: str, **attributes):
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
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

    for package_name in (
        "src",
        "src.common",
        "src.common.database",
        "src.config",
        "src.llm_models",
        "src.chat",
        "src.chat.utils",
        "src.chat.emoji_system",
    ):
        install(package_name).__path__ = []

    class Logger:
        def __getattr__(self, _level):
            return lambda *_args, **_kwargs: None

    class LLMRequestStub:
        def __init__(self, *_args, **_kwargs):
            pass

    install("src.common.logger", get_logger=lambda _name: Logger())
    install("src.common.database.database", db=database)
    install("src.common.database.database_model", Emoji=emoji_model)
    install(
        "src.config.config",
        global_config=types.SimpleNamespace(
            emoji=types.SimpleNamespace(max_reg_num=100, do_replace=False)
        ),
        model_config=types.SimpleNamespace(
            model_task_config=types.SimpleNamespace(utils_small="test-model")
        ),
    )
    install("src.llm_models.utils_model", LLMRequest=LLMRequestStub)
    install(
        "src.chat.utils.utils_image",
        image_path_to_base64=lambda _path: None,
        get_image_manager=lambda: None,
    )
    load_module(
        "src.common.media_conversion_cache",
        PROJECT_ROOT / "src" / "common" / "media_conversion_cache.py",
    )
    load_module(
        "src.chat.emoji_system.media_cache_cleanup",
        PROJECT_ROOT / "src" / "chat" / "emoji_system" / "media_cache_cleanup.py",
    )
    emoji_module = load_module(
        "src.chat.emoji_system.emoji_manager",
        PROJECT_ROOT / "src" / "chat" / "emoji_system" / "emoji_manager.py",
    )
    try:
        yield emoji_module
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


def _png_bytes(color=(60, 100, 150, 255)) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (14, 12), color).save(output, format="PNG")
    return output.getvalue()


class ImageCacheRestoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.data_root = self.root / "data"
        for name in ("image", "images", "emoji", "emoji_registed"):
            (self.data_root / name).mkdir(parents=True, exist_ok=True)
        self.database = SqliteDatabase(str(self.root / "cache.sqlite"))

        class BaseModel(Model):
            class Meta:
                database = self.database

        class Images(BaseModel):
            id = AutoField()
            image_id = TextField(default="")
            emoji_hash = TextField(index=True)
            description = TextField(null=True)
            path = TextField()
            count = IntegerField(default=1)
            timestamp = FloatField()
            type = TextField()
            vlm_processed = BooleanField(default=False)
            last_used_time = FloatField(null=True)
            no_file_flag = BooleanField(default=False)

        class ImageDescriptions(BaseModel):
            id = AutoField()
            image_description_hash = TextField(index=True)
            type = TextField()
            description = TextField()
            timestamp = FloatField()

        self.Images = Images
        self.ImageDescriptions = ImageDescriptions
        self.database.connect()
        self.database.create_tables([Images, ImageDescriptions])
        self.addCleanup(self.database.close)
        self.modules_context = isolated_image_modules(self.database, Images, ImageDescriptions)
        self.module = self.modules_context.__enter__()
        self.addCleanup(self.modules_context.__exit__, None, None, None)
        self.manager = self.module.ImageManager.__new__(self.module.ImageManager)
        self.manager.IMAGE_DIR = str(self.data_root)

    async def test_process_image_restores_picid_and_skips_vlm_for_cached_description(self):
        image_bytes = _png_bytes()
        image_hash = hashlib.md5(image_bytes, usedforsecurity=False).hexdigest()
        image_path = self.data_root / "images" / "cached-pic.png"
        record = self.Images.create(
            image_id="cached-pic",
            emoji_hash=image_hash,
            description="already described",
            path=str(image_path),
            count=1,
            timestamp=time.time() - 80 * 86_400,
            type="image",
            vlm_processed=True,
            last_used_time=None,
            no_file_flag=True,
        )
        self.manager._process_image_with_vlm = AsyncMock()

        image_id, marker = await self.manager.process_image(base64.b64encode(image_bytes).decode("ascii"))

        self.assertEqual((image_id, marker), ("cached-pic", "[picid:cached-pic]"))
        self.assertEqual(image_path.read_bytes(), image_bytes)
        refreshed = self.Images.get_by_id(record.id)
        self.assertFalse(refreshed.no_file_flag)
        self.assertGreater(refreshed.last_used_time, time.time() - 10)
        self.assertEqual(refreshed.count, 2)
        self.manager._process_image_with_vlm.assert_not_awaited()

    async def test_process_image_does_not_return_picid_when_safe_restore_fails(self):
        image_bytes = _png_bytes()
        image_hash = hashlib.md5(image_bytes, usedforsecurity=False).hexdigest()
        outside_path = self.root / "outside" / "cached-pic.png"
        record = self.Images.create(
            image_id="outside-pic",
            emoji_hash=image_hash,
            description="already described",
            path=str(outside_path),
            count=1,
            timestamp=time.time() - 80 * 86_400,
            type="image",
            vlm_processed=True,
            last_used_time=None,
            no_file_flag=True,
        )
        self.manager._process_image_with_vlm = AsyncMock()

        image_id, marker = await self.manager.process_image(base64.b64encode(image_bytes).decode("ascii"))

        self.assertEqual((image_id, marker), ("", "[图片]"))
        self.assertFalse(outside_path.exists())
        self.assertTrue(self.Images.get_by_id(record.id).no_file_flag)
        self.manager._process_image_with_vlm.assert_not_awaited()

    async def test_cached_restore_does_not_block_event_loop(self):
        image_bytes = _png_bytes()
        image_hash = hashlib.md5(image_bytes, usedforsecurity=False).hexdigest()
        self.Images.create(
            image_id="responsive-pic",
            emoji_hash=image_hash,
            description="cached",
            path=str(self.data_root / "images" / "responsive-pic.png"),
            count=1,
            timestamp=time.time(),
            type="image",
            vlm_processed=True,
            last_used_time=None,
            no_file_flag=True,
        )
        restore_started = threading.Event()
        allow_restore_to_finish = threading.Event()
        loop_progressed = asyncio.Event()

        def blocking_restore(_record, _image, *, preferred_directory):
            self.assertEqual(preferred_directory, "images")
            restore_started.set()
            allow_restore_to_finish.wait(timeout=0.2)
            return True

        async def ticker():
            await asyncio.to_thread(restore_started.wait, 1)
            loop_progressed.set()
            allow_restore_to_finish.set()

        self.manager._restore_and_touch_cached_image = blocking_restore
        self.manager._process_image_with_vlm = AsyncMock()
        tick_task = asyncio.create_task(ticker())
        try:
            process_task = asyncio.create_task(
                self.manager.process_image(base64.b64encode(image_bytes).decode("ascii"))
            )
            await asyncio.wait_for(loop_progressed.wait(), timeout=0.1)
            result = await process_task
            self.assertEqual(result, ("responsive-pic", "[picid:responsive-pic]"))
        finally:
            allow_restore_to_finish.set()
            await tick_task

    async def test_image_description_hit_restores_file_and_touches_description_timestamp(self):
        image_bytes = _png_bytes((10, 80, 170, 255))
        image_hash = hashlib.md5(image_bytes, usedforsecurity=False).hexdigest()
        image_path = self.data_root / "image" / "image-cache.png"
        image_record = self.Images.create(
            image_id="image-cache",
            emoji_hash=image_hash,
            description="cached image text",
            path=str(image_path),
            count=1,
            timestamp=time.time() - 60 * 86_400,
            type="image",
            vlm_processed=True,
            last_used_time=None,
            no_file_flag=True,
        )

        result = await self.manager.get_image_description(base64.b64encode(image_bytes).decode("ascii"))

        self.assertEqual(result, "[图片：cached image text]")
        self.assertEqual(image_path.read_bytes(), image_bytes)
        refreshed = self.Images.get_by_id(image_record.id)
        self.assertFalse(refreshed.no_file_flag)
        self.assertGreater(refreshed.last_used_time, time.time() - 10)

    async def test_emoji_description_cache_hit_restores_file(self):
        image_bytes = _png_bytes((150, 50, 10, 255))
        image_hash = hashlib.md5(image_bytes, usedforsecurity=False).hexdigest()
        image_path = self.data_root / "emoji" / "emoji-cache.png"
        image_record = self.Images.create(
            image_id="emoji-cache",
            emoji_hash=image_hash,
            description="visual description",
            path=str(image_path),
            count=1,
            timestamp=time.time() - 60 * 86_400,
            type="emoji",
            vlm_processed=True,
            last_used_time=None,
            no_file_flag=True,
        )
        description_record = self.ImageDescriptions.create(
            image_description_hash=image_hash,
            type="image",
            description="cached emotion",
            timestamp=time.time() - 60 * 86_400,
        )

        result = await self.manager.get_emoji_description(base64.b64encode(image_bytes).decode("ascii"))

        self.assertEqual(result, "[表情包：cached emotion]")
        self.assertEqual(image_path.read_bytes(), image_bytes)
        self.assertFalse(self.Images.get_by_id(image_record.id).no_file_flag)
        self.assertGreater(self.Images.get_by_id(image_record.id).last_used_time, time.time() - 10)
        self.assertGreater(self.ImageDescriptions.get_by_id(description_record.id).timestamp, time.time() - 10)


class _TrackedSqliteDatabase(SqliteDatabase):
    def __init__(self, database_path: str):
        super().__init__(database_path)
        self.opened_threads: set[int] = set()
        self.closed_threads: set[int] = set()
        self._tracking_lock = threading.Lock()

    def connect(self, reuse_if_open: bool = False):
        was_closed = self.is_closed()
        result = super().connect(reuse_if_open=reuse_if_open)
        if was_closed and not self.is_closed():
            with self._tracking_lock:
                self.opened_threads.add(threading.get_ident())
        return result

    def close(self):
        was_open = not self.is_closed()
        try:
            return super().close()
        finally:
            if was_open:
                with self._tracking_lock:
                    self.closed_threads.add(threading.get_ident())


class RegisteredEmojiCacheHitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.data_root = self.root / "data"
        self.registered_dir = self.data_root / "emoji_registed"
        self.registered_dir.mkdir(parents=True)
        self.database = _TrackedSqliteDatabase(str(self.root / "emoji-cache.sqlite"))

        class BaseModel(Model):
            class Meta:
                database = self.database

        class Emoji(BaseModel):
            id = AutoField()
            full_path = TextField(unique=True)
            format = TextField()
            emoji_hash = TextField(index=True)
            description = TextField()
            query_count = IntegerField(default=0)
            is_registered = BooleanField(default=True)
            is_banned = BooleanField(default=False)
            emotion = TextField(null=True)
            record_time = FloatField()
            register_time = FloatField(null=True)
            usage_count = IntegerField(default=0)
            last_used_time = FloatField(null=True)

        self.Emoji = Emoji
        self.database.connect()
        self.database.create_tables([Emoji])
        self.addCleanup(self.database.close)
        self.modules_context = isolated_emoji_manager_module(self.database, Emoji)
        self.module = self.modules_context.__enter__()
        self.addCleanup(self.modules_context.__exit__, None, None, None)
        self.module.BASE_DIR = str(self.data_root)
        self.module.EMOJI_REGISTERED_DIR = str(self.registered_dir)
        self.manager = self.module.EmojiManager.__new__(self.module.EmojiManager)
        self.manager._initialized = True
        self.manager.emoji_objects = []
        self.main_thread_id = threading.get_ident()
        self.image_bytes = _png_bytes((42, 86, 120, 255))
        self.emoji_hash = hashlib.md5(self.image_bytes, usedforsecurity=False).hexdigest()
        self.registered_path = self.registered_dir / "cache-hit.png"
        self.old_time = time.time() - 1_000

    def _create_record(self, full_path: Path | None = None):
        return self.Emoji.create(
            full_path=str(full_path or self.registered_path),
            format="png",
            emoji_hash=self.emoji_hash,
            description="database description",
            emotion="joy,fun",
            record_time=self.old_time,
            register_time=self.old_time,
            usage_count=1,
            last_used_time=self.old_time,
        )

    def _memory_emoji(self, full_path: Path | None = None):
        emoji = self.module.NcnkEmoji(str(full_path or self.registered_path))
        emoji.hash = self.emoji_hash
        emoji.emotion = ["joy", "fun"]
        emoji.description = "memory description"
        emoji.last_used_time = self.old_time
        return emoji

    def _assert_worker_connections_closed(self):
        worker_threads = self.database.opened_threads - {self.main_thread_id}
        self.assertTrue(worker_threads, "cache touch should use a worker-thread database connection")
        self.assertLessEqual(worker_threads, self.database.closed_threads)

    async def test_memory_tag_hit_restores_bytes_and_touches_memory_and_database(self):
        record = self._create_record()
        emoji = self._memory_emoji()
        self.manager.emoji_objects = [emoji]

        result = await self.manager.get_emoji_tag_by_hash(self.emoji_hash, image_bytes=self.image_bytes)

        self.assertEqual(result, ["joy", "fun"])
        self.assertEqual(self.registered_path.read_bytes(), self.image_bytes)
        self.assertGreater(emoji.last_used_time, self.old_time)
        self.assertGreater(self.Emoji.get_by_id(record.id).last_used_time, self.old_time)
        self._assert_worker_connections_closed()

    async def test_database_tag_hit_restores_bytes_and_touches_database(self):
        record = self._create_record()

        result = await self.manager.get_emoji_tag_by_hash(self.emoji_hash, image_bytes=self.image_bytes)

        self.assertEqual(result, ["joy", "fun"])
        self.assertEqual(self.registered_path.read_bytes(), self.image_bytes)
        self.assertGreater(self.Emoji.get_by_id(record.id).last_used_time, self.old_time)
        self._assert_worker_connections_closed()

    async def test_memory_description_hit_restores_bytes_and_touches_memory_and_database(self):
        record = self._create_record()
        emoji = self._memory_emoji()
        self.manager.emoji_objects = [emoji]

        result = await self.manager.get_emoji_description_by_hash(self.emoji_hash, image_bytes=self.image_bytes)

        self.assertEqual(result, "memory description")
        self.assertEqual(self.registered_path.read_bytes(), self.image_bytes)
        self.assertGreater(emoji.last_used_time, self.old_time)
        self.assertGreater(self.Emoji.get_by_id(record.id).last_used_time, self.old_time)
        self._assert_worker_connections_closed()

    async def test_database_description_hit_restores_bytes_and_touches_database(self):
        record = self._create_record()

        result = await self.manager.get_emoji_description_by_hash(self.emoji_hash, image_bytes=self.image_bytes)

        self.assertEqual(result, "database description")
        self.assertEqual(self.registered_path.read_bytes(), self.image_bytes)
        self.assertGreater(self.Emoji.get_by_id(record.id).last_used_time, self.old_time)
        self._assert_worker_connections_closed()

    async def test_outside_path_is_not_written_and_cached_text_is_preserved(self):
        outside_directory = self.root / "outside"
        outside_directory.mkdir()
        outside_path = outside_directory / "outside.png"
        self._create_record(outside_path)
        emoji = self._memory_emoji(outside_path)
        self.manager.emoji_objects = [emoji]

        tag = await self.manager.get_emoji_tag_by_hash(self.emoji_hash, image_bytes=self.image_bytes)
        description = await self.manager.get_emoji_description_by_hash(
            self.emoji_hash,
            image_bytes=self.image_bytes,
        )

        self.assertEqual(tag, ["joy", "fun"])
        self.assertEqual(description, "memory description")
        self.assertFalse(outside_path.exists())
        self._assert_worker_connections_closed()


if __name__ == "__main__":
    unittest.main()
