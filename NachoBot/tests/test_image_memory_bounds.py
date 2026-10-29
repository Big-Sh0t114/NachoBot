from __future__ import annotations

import base64
import gc
import importlib.util
import io
import sys
import tempfile
import threading
import types
import unittest
import weakref
from PIL import features
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests.test_image_cache_restore import isolated_image_modules
from tests.test_media_conversion_cache import isolated_compress_messages


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _animated_gif(width: int = 128, height: int = 128, frame_count: int = 180) -> bytes:
    frames = [
        Image.new("RGB", (width, height), (index % 8, (index // 8) % 8, 90))
        for index in range(frame_count)
    ]
    output = io.BytesIO()
    try:
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=30,
            loop=0,
            optimize=False,
        )
        return output.getvalue()
    finally:
        for frame in frames:
            frame.close()


class _PillowWorkingSet:
    """Tracks live Pillow image allocations, including native image cores."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live_count = 0
        self.live_bytes = 0
        self.peak_count = 0
        self.peak_bytes = 0

    def register(self, image: Image.Image) -> None:
        if getattr(image, "_memory_test_state", None) is not None:
            return
        size = image.width * image.height * max(1, len(image.getbands()))
        state = {"size": size, "active": True}
        image._memory_test_state = state
        with self.lock:
            self.live_count += 1
            self.live_bytes += size
            self.peak_count = max(self.peak_count, self.live_count)
            self.peak_bytes = max(self.peak_bytes, self.live_bytes)
        weakref.finalize(image, self._release, state)

    def release(self, image: Image.Image) -> None:
        state = getattr(image, "_memory_test_state", None)
        if state is not None:
            self._release(state)

    def _release(self, state: dict[str, int | bool]) -> None:
        with self.lock:
            if state["active"]:
                state["active"] = False
                self.live_count -= 1
                self.live_bytes -= int(state["size"])


@contextmanager
def _track_pillow_working_set():
    tracker = _PillowWorkingSet()
    original_new = Image.Image._new
    original_close = Image.Image.close
    original_image_new = Image.new
    original_open = Image.open

    def tracked_new(image, core):
        created = original_new(image, core)
        tracker.register(created)
        return created

    def tracked_close(image):
        tracker.release(image)
        return original_close(image)

    def tracked_image_new(*args, **kwargs):
        created = original_image_new(*args, **kwargs)
        tracker.register(created)
        return created

    def tracked_open(*args, **kwargs):
        opened = original_open(*args, **kwargs)
        tracker.register(opened)
        return opened

    with (
        patch.object(Image.Image, "_new", tracked_new),
        patch.object(Image.Image, "close", tracked_close),
        patch.object(Image, "new", tracked_image_new),
        patch.object(Image, "open", tracked_open),
    ):
        yield tracker


@contextmanager
def _isolated_collage_module():
    module_names = [
        "src",
        "src.common",
        "src.common.logger",
        "src.common.media_conversion_cache",
        "src.plugin_system",
        "src.plugin_system.apis",
        "src.plugin_system.apis.emoji_api",
        "src.chat",
        "src.chat.emoji_system",
        "src.chat.emoji_system.emoji_collage",
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
        "src.plugin_system",
        "src.plugin_system.apis",
        "src.chat",
        "src.chat.emoji_system",
    ):
        install(package_name).__path__ = []

    class Logger:
        def __getattr__(self, _level):
            return lambda *_args, **_kwargs: None

    install("src.common.logger", get_logger=lambda _name: Logger())
    install("src.plugin_system.apis.emoji_api", EmojiCandidate=type("EmojiCandidate", (), {}))
    load_module(
        "src.common.media_conversion_cache",
        PROJECT_ROOT / "src" / "common" / "media_conversion_cache.py",
    )
    collage = load_module(
        "src.chat.emoji_system.emoji_collage",
        PROJECT_ROOT / "src" / "chat" / "emoji_system" / "emoji_collage.py",
    )
    try:
        yield collage
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class _ExpandingImage:
    """Fake decoder whose frame header expands before any pixel access."""

    format = "GIF"

    def __init__(self, initial_size=(10, 10), expanded_at=None, expanded_size=(5_000, 4_000)):
        self._size = initial_size
        self.expanded_at = expanded_at
        self.expanded_size = expanded_size
        self.info = {"duration": 40, "loop": 0}
        self.seek_calls: list[int] = []
        self.convert_called = False
        self.load_called = False
        self.thumbnail_called = False

    @property
    def size(self):
        return self._size

    @property
    def width(self):
        return self._size[0]

    @property
    def height(self):
        return self._size[1]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def seek(self, frame_index):
        self.seek_calls.append(frame_index)
        if frame_index == self.expanded_at:
            self._size = self.expanded_size

    def convert(self, *_args, **_kwargs):
        self.convert_called = True
        raise AssertionError("oversized frame must be rejected before conversion")

    def load(self):
        self.load_called = True
        raise AssertionError("oversized frame must be rejected before load")

    def thumbnail(self, *_args, **_kwargs):
        self.thumbnail_called = True
        raise AssertionError("oversized frame must be rejected before thumbnail")


class _DimensionSequenceImage:
    def __init__(self, sizes):
        self.sizes = sizes
        self.size = sizes[0]
        self.info = {"duration": 40, "loop": 0}

    def seek(self, frame_index):
        if frame_index >= len(self.sizes):
            raise EOFError
        self.size = self.sizes[frame_index]


class ImageMemoryBoundsTests(unittest.TestCase):
    def test_gif_transform_streams_and_bounds_output_frames(self):
        with isolated_image_modules(None, object, object) as image_module:
            gif_bytes = _animated_gif()
            gif_base64 = base64.b64encode(gif_bytes).decode("ascii")
            image_module.media_conversion_cache.clear()

            with _track_pillow_working_set() as tracker:
                result = image_module.ImageManager.transform_gif(
                    gif_base64,
                    similarity_threshold=0,
                    max_frames=180,
                )
                cached_result = image_module.ImageManager.transform_gif(
                    gif_base64,
                    similarity_threshold=0,
                    max_frames=15,
                )
            gc.collect()

            self.assertIsNotNone(result)
            self.assertEqual(result, cached_result)
            self.assertEqual(image_module.media_conversion_cache.entry_count, 1)
            with Image.open(io.BytesIO(base64.b64decode(result))) as sheet:
                self.assertEqual(sheet.format, "JPEG")
                self.assertEqual(sheet.height, 200)
                self.assertEqual(sheet.width, 200 * 15)
            self.assertLessEqual(tracker.peak_count, 17)
            self.assertLessEqual(tracker.peak_bytes, 2 * 8_192 * 200 * 3)
            self.assertEqual(tracker.live_count, 0)
            print(
                "GIF transform bounded: 180 frames at 128x128; "
                f"peak Pillow allocations={tracker.peak_count} objects / {tracker.peak_bytes:,} bytes"
            )

    def test_gif_transform_rejects_declared_and_per_frame_oversize_before_decode(self):
        with isolated_image_modules(None, object, object) as image_module:
            encoded = base64.b64encode(b"synthetic decoder input").decode("ascii")
            declared = _ExpandingImage(initial_size=(5_000, 4_000))
            with patch.object(image_module.Image, "open", return_value=declared):
                self.assertIsNone(image_module.ImageManager._transform_gif_uncached(encoded))
            self.assertEqual(declared.seek_calls, [])
            self.assertFalse(declared.convert_called)

            expanded = _ExpandingImage(expanded_at=0)
            with patch.object(image_module.Image, "open", return_value=expanded):
                self.assertIsNone(image_module.ImageManager._transform_gif_uncached(encoded))
            self.assertEqual(expanded.seek_calls, [0])
            self.assertFalse(expanded.convert_called)

    def test_gif_similarity_uses_legacy_uint8_mse_math(self):
        with isolated_image_modules(None, object, object) as image_module:
            current = image_module.np.array([[[0, 255, 20], [8, 100, 240]]], dtype=image_module.np.uint8)
            previous = image_module.np.array([[[255, 0, 40], [0, 100, 0]]], dtype=image_module.np.uint8)
            expected = image_module.np.mean((current - previous) ** 2)
            actual = image_module._legacy_uint8_frame_mse(current, previous)
            self.assertEqual(actual, expected)

    def test_corrupt_gif_fails_without_populating_conversion_cache(self):
        with isolated_image_modules(None, object, object) as image_module:
            image_module.media_conversion_cache.clear()
            self.assertIsNone(image_module.ImageManager.transform_gif("bm90LWEtZ2lm"))
            self.assertEqual(image_module.media_conversion_cache.entry_count, 0)
            self.assertEqual(image_module.media_conversion_cache.pending_count, 0)

    def test_collage_downsamples_before_rgba_compositing_and_returns_copies(self):
        with _isolated_collage_module() as collage:
            with tempfile.TemporaryDirectory() as temporary_directory:
                source_path = Path(temporary_directory) / "oversized-source.png"
                with Image.new("RGBA", (2_400, 1_200), (20, 80, 140, 120)) as source:
                    source.save(source_path, format="PNG")
                candidate = types.SimpleNamespace(full_path=str(source_path))

                first = collage._prepare_image(candidate)
                second = collage._prepare_image(candidate)

                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
                self.assertEqual(first.image.size, (1_600, 800))
                self.assertIsNot(first.image, second.image)
                second_pixel = second.image.getpixel((0, 0))
                first.image.putpixel((0, 0), (0, 0, 0))
                self.assertEqual(second.image.getpixel((0, 0)), second_pixel)
                first.image.close()
                second.image.close()

    def test_collage_rejects_expanding_frame_before_thumbnail(self):
        with _isolated_collage_module() as collage:
            with tempfile.TemporaryDirectory() as temporary_directory:
                source_path = Path(temporary_directory) / "fake.gif"
                source_path.write_bytes(b"synthetic")
                candidate = types.SimpleNamespace(full_path=str(source_path))
                declared = _ExpandingImage(initial_size=(5_000, 4_000))
                with patch.object(collage.Image, "open", return_value=declared):
                    self.assertIsNone(collage._prepare_image(candidate))
                self.assertEqual(declared.seek_calls, [])
                self.assertFalse(declared.thumbnail_called)

                fake = _ExpandingImage(expanded_at=1)
                with patch.object(collage.Image, "open", return_value=fake):
                    self.assertIsNone(collage._prepare_image(candidate))
                self.assertEqual(fake.seek_calls, [0, 1])
                self.assertFalse(fake.thumbnail_called)

    def test_compression_bounds_animation_frames_and_preserves_prefix_duration(self):
        with isolated_compress_messages() as utils:
            message_module = sys.modules["src.llm_models.payload_content.message"]
            builder = message_module.MessageBuilder()
            gif_bytes = _animated_gif()
            gif_base64 = base64.b64encode(gif_bytes).decode("ascii")
            target_size = max(1, len(gif_base64) // 2)
            message = builder.add_image_content("gif", gif_base64).build()

            with _track_pillow_working_set() as tracker:
                compressed = utils.compress_messages([message], img_target_size=target_size)[0]
            gc.collect()

            image_format, result_base64 = compressed.content[0]
            self.assertEqual(image_format, "gif")
            with Image.open(io.BytesIO(base64.b64decode(result_base64))) as result:
                self.assertEqual(result.format, "GIF")
                self.assertGreater(result.n_frames, 1)
                self.assertLessEqual(result.n_frames, 32)
                total_duration = 0
                for frame_index in range(result.n_frames):
                    result.seek(frame_index)
                    total_duration += result.info.get("duration", 0)
                self.assertEqual(total_duration, 180 * 30)
            self.assertLessEqual(tracker.peak_count, 100)
            self.assertLessEqual(tracker.peak_bytes, 2_000_000)
            self.assertEqual(tracker.live_count, 0)
            print(
                "GIF compression bounded: 180 frames at 128x128; "
                f"peak Pillow allocations={tracker.peak_count} objects / {tracker.peak_bytes:,} bytes"
            )

            ordinary_message = message_module.MessageBuilder().add_image_content("gif", gif_base64).build()
            unchanged = utils.compress_messages(
                [ordinary_message],
                img_target_size=len(gif_base64) * 2,
            )[0]
            self.assertEqual(unchanged.content[0], ("gif", gif_base64))

    def test_compression_rejects_expanding_frame_before_pixel_access(self):
        with isolated_compress_messages() as utils:
            message_module = sys.modules["src.llm_models.payload_content.message"]
            message = message_module.MessageBuilder().add_image_content(
                "gif", base64.b64encode(b"synthetic").decode("ascii")
            ).build()
            declared = _ExpandingImage(initial_size=(5_000, 4_000))
            with patch.object(utils.Image, "open", return_value=declared):
                declared_result = utils.compress_messages([message])[0]
            self.assertEqual(declared_result.content[0], message.content[0])
            self.assertEqual(declared.seek_calls, [])

            fake = _ExpandingImage(expanded_at=1)
            with patch.object(utils.Image, "open", return_value=fake):
                result = utils.compress_messages([message])[0]
            self.assertEqual(result.content[0][0], "gif")
            self.assertEqual(result.content[0][1], base64.b64encode(b"synthetic").decode("ascii"))
            self.assertEqual(fake.seek_calls, [1])
            self.assertFalse(fake.convert_called)
            self.assertFalse(fake.load_called)

    def test_animation_scan_accounts_cumulative_pixels_from_each_frame_header(self):
        with isolated_compress_messages() as utils:
            sizes = [(2_000, 2_000), (4_000, 4_000), (1_000, 1_000), (4_000, 4_000), (4_000, 4_000), (4_000, 4_000)]
            image = _DimensionSequenceImage(sizes)
            selected, durations, loop_count, scanned_sizes = utils._bounded_animation_plan(image)

            self.assertEqual(scanned_sizes, sizes[:5])
            self.assertEqual(sum(width * height for width, height in scanned_sizes), 53_000_000)
            self.assertEqual(selected, [0, 1, 2, 3, 4])
            self.assertEqual(durations, [40] * 5)
            self.assertEqual(loop_count, 0)

    @unittest.skipUnless(features.check("webp"), "Pillow has no WebP encoder")
    def test_animated_webp_compression_reports_webp_mime(self):
        with isolated_compress_messages() as utils:
            message_module = sys.modules["src.llm_models.payload_content.message"]
            frames = [
                Image.new("RGBA", (64, 64), (value * 40, 80, 120, 180))
                for value in range(6)
            ]
            output = io.BytesIO()
            try:
                frames[0].save(
                    output,
                    format="WEBP",
                    save_all=True,
                    append_images=frames[1:],
                    duration=[30, 40, 50, 60, 70, 80],
                    loop=0,
                    quality=90,
                )
            finally:
                for frame in frames:
                    frame.close()

            encoded = base64.b64encode(output.getvalue()).decode("ascii")
            message = message_module.MessageBuilder().add_image_content("webp", encoded).build()
            result = utils.compress_messages([message], img_target_size=max(1, len(encoded) // 2))[0]
            image_format, compressed_base64 = result.content[0]
            self.assertEqual(image_format, "webp")
            with Image.open(io.BytesIO(base64.b64decode(compressed_base64))) as compressed:
                self.assertEqual(compressed.format, "WEBP")
                self.assertGreater(compressed.n_frames, 1)

    def test_corrupt_compression_input_is_retained_without_cache_failure(self):
        with isolated_compress_messages() as utils:
            message_module = sys.modules["src.llm_models.payload_content.message"]
            encoded = base64.b64encode(b"not-an-image").decode("ascii")
            message = message_module.MessageBuilder().add_image_content("png", encoded).build()
            result = utils.compress_messages([message])[0]
            self.assertEqual(result.content[0], ("png", encoded))


if __name__ == "__main__":
    unittest.main()
