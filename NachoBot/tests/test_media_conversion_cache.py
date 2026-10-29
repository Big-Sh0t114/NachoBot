from __future__ import annotations

import threading
import time
import unittest
import base64
import importlib.util
import io
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from PIL import Image
from src.common.media_conversion_cache import MediaConversionCache


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def isolated_compress_messages():
    """Load compress_messages with fake database/config dependencies only."""
    module_names = [
        "src",
        "src.common",
        "src.common.logger",
        "src.common.media_conversion_cache",
        "src.common.database",
        "src.common.database.database",
        "src.common.database.database_model",
        "src.config",
        "src.config.api_ada_configs",
        "src.llm_models",
        "src.llm_models.payload_content",
        "src.llm_models.payload_content.message",
        "src.llm_models.model_client",
        "src.llm_models.model_client.base_client",
        "src.llm_models.utils",
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
        "src.llm_models.payload_content",
        "src.llm_models.model_client",
    ):
        install(package_name).__path__ = []

    class _Logger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    install("src.common.logger", get_logger=lambda _name: _Logger())
    install("src.common.database.database", db=types.SimpleNamespace(create_tables=lambda *_args, **_kwargs: None))
    install("src.common.database.database_model", LLMUsage=types.SimpleNamespace())

    class ModelInfo:
        pass

    install("src.config.api_ada_configs", ModelInfo=ModelInfo)
    install("src.llm_models.model_client.base_client", UsageRecord=object)
    load_module(
        "src.common.media_conversion_cache",
        PROJECT_ROOT / "src" / "common" / "media_conversion_cache.py",
    )
    load_module(
        "src.llm_models.payload_content.message",
        PROJECT_ROOT / "src" / "llm_models" / "payload_content" / "message.py",
    )
    utils_module = load_module("src.llm_models.utils", PROJECT_ROOT / "src" / "llm_models" / "utils.py")
    try:
        yield utils_module
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


def _encode_image(image: Image.Image, image_format: str, **options) -> bytes:
    output = io.BytesIO()
    image.save(output, format=image_format, **options)
    return output.getvalue()


class MediaConversionCacheTests(unittest.TestCase):
    def test_content_and_options_are_part_of_key(self):
        cache = MediaConversionCache()
        calls = 0

        def convert():
            nonlocal calls
            calls += 1
            return b"converted"

        self.assertEqual(cache.get_or_compute(b"same", "jpeg", {"quality": 80}, convert), b"converted")
        self.assertEqual(cache.get_or_compute(b"same", "jpeg", {"quality": 80}, convert), b"converted")
        cache.get_or_compute(b"same", "jpeg", {"quality": 90}, convert)
        cache.get_or_compute(b"replacement", "jpeg", {"quality": 80}, convert)

        self.assertEqual(calls, 3)
        self.assertEqual(cache.entry_count, 3)

    def test_lru_entry_and_memory_bounds_and_oversized_bypass(self):
        cache = MediaConversionCache(max_entries=2, max_bytes=8, max_concurrent=2)
        cache.get_or_compute(b"1", "x", {}, lambda: b"1234")
        cache.get_or_compute(b"2", "x", {}, lambda: b"5678")
        cache.get_or_compute(b"1", "x", {}, lambda: b"miss")
        cache.get_or_compute(b"3", "x", {}, lambda: b"abcd")

        self.assertEqual(cache.entry_count, 2)
        self.assertLessEqual(cache.stored_bytes, 8)

        calls = 0

        def oversized():
            nonlocal calls
            calls += 1
            return b"123456789"

        cache.get_or_compute(b"big", "x", {}, oversized)
        cache.get_or_compute(b"big", "x", {}, oversized)
        self.assertEqual(calls, 2)
        self.assertEqual(cache.entry_count, 2)
        self.assertLessEqual(cache.stored_bytes, 8)

    def test_cached_output_is_immutable_copy_of_mutable_converter_result(self):
        cache = MediaConversionCache()
        mutable_output = bytearray(b"jpeg-bytes")
        result = cache.get_or_compute(b"source", "jpeg", {}, lambda: mutable_output)
        mutable_output[:] = b"x" * len(mutable_output)

        self.assertIsInstance(result, bytes)
        self.assertEqual(result, b"jpeg-bytes")
        self.assertEqual(cache.get_or_compute(b"source", "jpeg", {}, lambda: b"wrong"), b"jpeg-bytes")

    def test_same_key_is_singleflight(self):
        cache = MediaConversionCache()
        barrier = threading.Barrier(8)
        call_lock = threading.Lock()
        calls = 0

        def convert():
            nonlocal calls
            with call_lock:
                calls += 1
            time.sleep(0.05)
            return b"shared"

        def request():
            barrier.wait(timeout=2)
            return cache.get_or_compute(b"content", "jpeg", {"v": 1}, convert)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _index: request(), range(8)))

        self.assertEqual(results, [b"shared"] * 8)
        self.assertEqual(calls, 1)
        self.assertEqual(cache.pending_count, 0)

    def test_conversion_concurrency_is_bounded_to_two(self):
        cache = MediaConversionCache()
        lock = threading.Lock()
        active = 0
        maximum_active = 0

        def request(index: int):
            def convert():
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.04)
                with lock:
                    active -= 1
                return str(index).encode()

            return cache.get_or_compute(str(index).encode(), "convert", {}, convert)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(request, range(8)))

        self.assertEqual(maximum_active, 2)

    def test_failed_flight_releases_waiters_and_can_retry(self):
        cache = MediaConversionCache()
        barrier = threading.Barrier(6)

        def fail():
            time.sleep(0.08)
            raise ValueError("broken conversion")

        def request():
            barrier.wait(timeout=2)
            return cache.get_or_compute(b"same", "x", {}, fail)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(request) for _ in range(6)]
            for future in futures:
                with self.assertRaisesRegex(ValueError, "broken conversion"):
                    future.result(timeout=3)

        self.assertEqual(cache.pending_count, 0)
        self.assertEqual(cache.get_or_compute(b"same", "x", {}, lambda: b"recovered"), b"recovered")

    def test_nested_cache_miss_fails_fast_without_deadlocking(self):
        cache = MediaConversionCache(max_concurrent=1)

        def outer():
            return b"outer:" + cache.get_or_compute(b"inner", "x", {}, lambda: b"inner")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(cache.get_or_compute, b"outer", "x", {}, outer)
            with self.assertRaisesRegex(RuntimeError, "nested media conversion"):
                future.result(timeout=2)
        self.assertEqual(cache.pending_count, 0)

    def test_same_key_recursion_fails_without_leaking_pending_state(self):
        cache = MediaConversionCache(max_concurrent=1)

        def recursive():
            return cache.get_or_compute(b"same", "x", {}, lambda: b"never")

        with self.assertRaisesRegex(RuntimeError, "reentrant"):
            cache.get_or_compute(b"same", "x", {}, recursive)
        self.assertEqual(cache.pending_count, 0)

class CompressMessagesIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.modules_context = isolated_compress_messages()
        self.utils = self.modules_context.__enter__()
        self.addCleanup(self.modules_context.__exit__, None, None, None)

        from src.llm_models.payload_content.message import MessageBuilder

        self.message_builder = MessageBuilder

    def _compress(self, image_format: str, image_bytes: bytes, target_size: int = 1024 * 1024):
        message = self.message_builder().add_image_content(
            image_format,
            base64.b64encode(image_bytes).decode("ascii"),
        ).build()
        compressed = self.utils.compress_messages([message], img_target_size=target_size)[0]
        self.assertIsInstance(compressed.content, list)
        media_format, encoded = compressed.content[0]
        result_bytes = base64.b64decode(encoded)
        with Image.open(io.BytesIO(result_bytes)) as result_image:
            self.assertEqual(media_format, (result_image.format or "").lower())
        return media_format, result_bytes

    def test_opaque_png_and_webp_convert_to_valid_jpeg(self):
        opaque = Image.new("RGB", (24, 18), (20, 100, 180))
        self.assertEqual(self._compress("png", _encode_image(opaque, "PNG"))[0], "jpeg")

        webp_bytes = _encode_image(opaque, "WEBP")
        self.assertEqual(self._compress("webp", webp_bytes)[0], "jpeg")

    def test_transparent_png_stays_png_and_keeps_alpha(self):
        image = Image.new("RGBA", (20, 20), (220, 30, 50, 0))
        image.putpixel((10, 10), (10, 90, 170, 255))
        media_format, result_bytes = self._compress("png", _encode_image(image, "PNG"))

        self.assertEqual(media_format, "png")
        with Image.open(io.BytesIO(result_bytes)) as result_image:
            self.assertEqual(result_image.getpixel((0, 0))[3], 0)
            self.assertEqual(result_image.getpixel((10, 10))[3], 255)

    def test_animated_gif_format_and_frames_are_preserved(self):
        frames = [Image.new("RGB", (12, 10), color) for color in ((255, 0, 0), (0, 0, 255))]
        input_bytes = _encode_image(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
        media_format, result_bytes = self._compress("gif", input_bytes)

        self.assertEqual(media_format, "gif")
        with Image.open(io.BytesIO(result_bytes)) as result_image:
            self.assertEqual(getattr(result_image, "n_frames", 1), 2)

    def test_compressed_conversion_is_reused_for_identical_content(self):
        image = Image.new("RGB", (24, 18), (20, 100, 180))
        image_bytes = _encode_image(image, "PNG")
        self._compress("png", image_bytes)
        cache = self.utils.media_conversion_cache
        first_entry_count = cache.entry_count
        self._compress("png", image_bytes)
        self.assertEqual(cache.entry_count, first_entry_count)


if __name__ == "__main__":
    unittest.main()
