from __future__ import annotations

import asyncio
import base64
import importlib.util
import os
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __init__(self):
        self.records: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __getattr__(self, level):
        def record(message, *_args, **_kwargs):
            with self._lock:
                self.records.append((level, str(message)))

        return record


class _BaseEventHandler:
    def __init__(self):
        self.config = {}
        self.commands = []
        self.texts = []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    async def send_command(self, **kwargs):
        self.commands.append(kwargs)

    async def send_text(self, *args, **kwargs):
        self.texts.append((args, kwargs))


@contextmanager
def isolated_moderation_module():
    """Load the real handler while replacing only host plugin imports."""
    module_name = "_nachobot_moderation_refresh_test"
    names = {
        module_name,
        "src",
        "src.plugin_system",
        "src.plugin_system.base",
        "src.common",
        "src.common.logger",
    }
    previous = {name: sys.modules.get(name) for name in names}

    def install(name, **attributes):
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        sys.modules[name] = module
        return module

    for name in ("src", "src.plugin_system", "src.common"):
        install(name).__path__ = []
    log = _Logger()
    install(
        "src.plugin_system.base",
        BaseEventHandler=_BaseEventHandler,
        EventType=types.SimpleNamespace(ON_MESSAGE="on_message"),
        NachoMessages=object,
        CustomEventHandlerResult=object,
    )
    install("src.common.logger", get_logger=lambda _name: log)

    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / "plugins/moderation_plugin/event_handler.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load moderation event handler")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module, log
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


def _image_bytes(kind: str) -> bytes:
    image = Image.new("L", (64, 64))
    if kind == "ramp":
        image.putdata([x * 4 for _y in range(64) for x in range(64)])
    else:
        image.putdata([128] * (64 * 64))
    from io import BytesIO

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _write_image(path: Path, kind: str, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _image_bytes(kind)
    if replace:
        temp_path = path.with_name(f"{path.name}.replacement")
        temp_path.write_bytes(payload)
        os.replace(temp_path, path)
    else:
        path.write_bytes(payload)


def _segment(kind: str, payload: bytes | None = None):
    return types.SimpleNamespace(
        type=kind,
        data=base64.b64encode(payload or _image_bytes("solid")).decode("ascii"),
    )


def _message(*, segments=(), text="", user_id="user-1"):
    return types.SimpleNamespace(
        plain_text=text,
        raw_message=text,
        message_segments=list(segments),
        message_base_info={"user_id": user_id, "message_id": "message-1"},
        stream_id="stream-1",
    )


class ModerationImageRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module_context = isolated_moderation_module()
        self.module, self.log = self.module_context.__enter__()
        self.addCleanup(self.module_context.__exit__, None, None, None)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.rules = self.root / "rules"
        self.rules.mkdir()

        with mock.patch.object(self.module.ModerationEventHandler, "_load_stats"):
            self.handler = self.module.ModerationEventHandler()
        self.handler.stats_file = str(self.root / "stats" / "moderation_stats.json")
        self.handler.config = {
            "moderation.banned_images_dir": str(self.rules),
            "moderation.whitelist_qq": [],
            "moderation.ban_regex_list": [],
            "moderation.recall_message": "",
        }

    async def _allowed(self, *segments, text="", user_id="user-1") -> bool:
        result = await self.handler.execute(_message(segments=segments, text=text, user_id=user_id))
        return result[1]

    async def test_next_image_message_adds_and_deletes_rule_without_reload(self):
        solid = _segment("image", _image_bytes("solid"))
        self.assertTrue(await self._allowed(solid))

        _write_image(self.rules / "bad.png", "solid")
        self.assertFalse(await self._allowed(solid))
        self.assertEqual(self.handler._stats["images"]["bad.png"]["count"], 1)

        (self.rules / "bad.png").unlink()
        self.assertTrue(await self._allowed(solid))

    async def test_nested_rules_refresh_and_stats_keep_filename_attribution(self):
        nested = self.rules / "nested" / "deeper" / "bad-nested.png"
        _write_image(nested, "ramp")
        self.assertFalse(await self._allowed(_segment("emoji", _image_bytes("ramp"))))
        self.assertEqual(self.handler._stats["images"]["bad-nested.png"]["count"], 1)

        nested.unlink()
        self.assertTrue(await self._allowed(_segment("image", _image_bytes("ramp"))))

    async def test_missing_root_can_be_created_and_deleted_after_first_message(self):
        self.rules.rmdir()
        image = _segment("image", _image_bytes("solid"))
        self.assertTrue(await self._allowed(image))

        self.rules.mkdir()
        _write_image(self.rules / "created.png", "solid")
        self.assertFalse(await self._allowed(image))

        (self.rules / "created.png").unlink()
        self.rules.rmdir()
        self.assertTrue(await self._allowed(image))

    async def test_duplicate_hash_remains_after_one_file_is_deleted(self):
        first = self.rules / "a.png"
        second = self.rules / "nested" / "b.png"
        _write_image(first, "solid")
        _write_image(second, "solid")
        image = _segment("image", _image_bytes("solid"))

        self.assertFalse(await self._allowed(image))
        first.unlink()
        self.assertFalse(await self._allowed(image))
        self.assertEqual(self.handler._stats["images"]["b.png"]["count"], 1)
        self.assertEqual(len(self.handler._image_cache.files), 1)

    async def test_same_path_replacement_uses_new_fingerprint(self):
        rule = self.rules / "replace.png"
        _write_image(rule, "solid")
        solid = _segment("image", _image_bytes("solid"))
        ramp = _segment("image", _image_bytes("ramp"))
        self.assertFalse(await self._allowed(solid))

        _write_image(rule, "ramp", replace=True)
        self.assertTrue(await self._allowed(solid))
        self.assertFalse(await self._allowed(ramp))

    async def test_symlinked_root_and_file_rules_are_supported(self):
        actual_root = self.root / "actual-rules"
        actual_root.mkdir()
        root_link = self.root / "rules-link"
        external_image = self.root / "external.png"
        _write_image(external_image, "ramp")
        try:
            root_link.symlink_to(actual_root, target_is_directory=True)
            (actual_root / "linked.png").symlink_to(external_image)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        _write_image(actual_root / "direct.png", "solid")
        self.handler.config["moderation.banned_images_dir"] = str(root_link)

        self.assertFalse(await self._allowed(_segment("image", _image_bytes("solid"))))
        self.assertFalse(await self._allowed(_segment("image", _image_bytes("ramp"))))

    async def test_dangling_file_symlink_does_not_block_other_adds_or_deletes(self):
        target = self.root / "external.png"
        _write_image(target, "solid")
        link = self.rules / "dangling.png"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        solid = _segment("image", _image_bytes("solid"))
        ramp = _segment("image", _image_bytes("ramp"))
        self.assertFalse(await self._allowed(solid))

        target.unlink()
        added = self.rules / "added.png"
        _write_image(added, "ramp")
        self.assertFalse(await self._allowed(ramp))
        added.unlink()
        self.assertTrue(await self._allowed(ramp))

        link.unlink()
        self.assertTrue(await self._allowed(solid))

    async def test_unchanged_file_reuses_hash(self):
        _write_image(self.rules / "same.png", "solid")
        image = _segment("image", _image_bytes("solid"))
        original = self.module._read_stable_image_hash
        with mock.patch.object(self.module, "_read_stable_image_hash", wraps=original) as read_hash:
            self.assertFalse(await self._allowed(image))
            self.assertFalse(await self._allowed(image))
        self.assertEqual(read_hash.call_count, 1)

    async def test_damaged_new_file_is_retried_after_repair(self):
        damaged = self.rules / "damaged.png"
        damaged.write_bytes(b"not an image")
        ramp = _segment("image", _image_bytes("ramp"))
        self.assertTrue(await self._allowed(ramp))
        self.assertTrue(any("damaged.png" in message for _level, message in self.log.records))

        _write_image(damaged, "ramp", replace=True)
        self.assertFalse(await self._allowed(ramp))

    async def test_unreadable_changed_file_keeps_old_rule_and_retries(self):
        rule = self.rules / "changed.png"
        _write_image(rule, "solid")
        solid = _segment("image", _image_bytes("solid"))
        ramp = _segment("image", _image_bytes("ramp"))
        self.assertFalse(await self._allowed(solid))
        _write_image(rule, "ramp", replace=True)

        with mock.patch.object(self.module, "_read_stable_image_hash", side_effect=PermissionError("locked")):
            self.assertFalse(await self._allowed(solid))
            self.assertTrue(await self._allowed(ramp))

        self.assertFalse(await self._allowed(ramp))
        self.assertTrue(await self._allowed(solid))

    async def test_scan_failure_preserves_cache_and_retries_deletions(self):
        rule = self.rules / "known.png"
        _write_image(rule, "solid")
        solid = _segment("image", _image_bytes("solid"))
        self.assertFalse(await self._allowed(solid))

        with mock.patch.object(self.module, "_scan_banned_image_files", side_effect=PermissionError("denied")):
            self.assertFalse(await self._allowed(solid))
        rule.unlink()
        self.assertTrue(await self._allowed(solid))

    async def test_changed_root_failure_never_uses_previous_roots_rules(self):
        _write_image(self.rules / "old.png", "solid")
        solid = _segment("image", _image_bytes("solid"))
        self.assertFalse(await self._allowed(solid))
        new_root = self.root / "new-rules"
        self.handler.config["moderation.banned_images_dir"] = str(new_root)

        with mock.patch.object(self.module, "_scan_banned_image_files", side_effect=PermissionError("denied")):
            self.assertTrue(await self._allowed(solid))

        new_root.mkdir()
        _write_image(new_root / "new.png", "ramp")
        self.assertFalse(await self._allowed(_segment("image", _image_bytes("ramp"))))

    async def test_concurrent_image_messages_serialize_refreshes(self):
        _write_image(self.rules / "concurrent.png", "solid")
        image = _segment("image", _image_bytes("solid"))
        original = self.module._scan_banned_image_files
        entered = threading.Event()
        release = threading.Event()
        counter_lock = threading.Lock()
        active = 0
        peak_active = 0

        def slow_scan(directory, previous_files):
            nonlocal active, peak_active
            with counter_lock:
                active += 1
                peak_active = max(peak_active, active)
            entered.set()
            try:
                if not release.wait(5):
                    raise TimeoutError("test scan was not released")
                return original(directory, previous_files)
            finally:
                with counter_lock:
                    active -= 1

        with mock.patch.object(self.module, "_scan_banned_image_files", side_effect=slow_scan):
            first = asyncio.create_task(self._allowed(image))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            second = asyncio.create_task(self._allowed(image))
            release.set()
            self.assertEqual(await asyncio.gather(first, second), [False, False])

        self.assertEqual(peak_active, 1)
        self.assertEqual(len(self.handler._image_cache.files), 1)

    async def test_cancelled_scan_cannot_publish_after_replacement_scan(self):
        rule = self.rules / "changing.png"
        _write_image(rule, "solid")
        original = self.module._scan_banned_image_files
        old_snapshot_ready = threading.Event()
        release_old_worker = threading.Event()
        old_worker_finished = threading.Event()
        call_lock = threading.Lock()
        calls = 0

        def delayed_first_scan(directory, previous_files):
            nonlocal calls
            with call_lock:
                calls += 1
                call_number = calls
            snapshot = original(directory, previous_files)
            if call_number == 1:
                old_snapshot_ready.set()
                try:
                    if not release_old_worker.wait(5):
                        raise TimeoutError("old scan was not released")
                finally:
                    old_worker_finished.set()
            return snapshot

        with mock.patch.object(self.module, "_scan_banned_image_files", side_effect=delayed_first_scan):
            cancelled = asyncio.create_task(self._allowed(_segment("image", _image_bytes("solid"))))
            self.assertTrue(await asyncio.to_thread(old_snapshot_ready.wait, 5))
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            self.assertIsNone(self.handler._image_cache.directory)

            _write_image(rule, "ramp", replace=True)
            ramp_result = await self._allowed(_segment("image", _image_bytes("ramp")))
            self.assertFalse(ramp_result)
            published_hashes = dict(self.handler._image_cache.hashes)

            release_old_worker.set()
            self.assertTrue(await asyncio.to_thread(old_worker_finished.wait, 5))
            self.assertEqual(dict(self.handler._image_cache.hashes), published_hashes)
            self.assertEqual(len(self.handler._image_cache.files), 1)

    async def test_whitelist_and_text_regex_do_not_scan_or_change_behavior(self):
        self.handler.config["moderation.whitelist_qq"] = ["42"]
        self.handler.config["moderation.ban_regex_list"] = ["forbidden"]
        image = _segment("image")
        with mock.patch.object(self.module, "_scan_banned_image_files", side_effect=AssertionError("unexpected scan")):
            self.assertTrue(await self._allowed(image, text="forbidden", user_id="42"))
            self.assertFalse(await self._allowed(text="forbidden", user_id="7"))
            self.assertTrue(await self._allowed(text="ordinary", user_id="7"))
        self.assertFalse(self.handler._image_cache.files)
        self.assertEqual(self.handler._stats["text_patterns"]["forbidden"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
