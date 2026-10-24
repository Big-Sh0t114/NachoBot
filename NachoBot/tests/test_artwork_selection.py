from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTWORK_PLUGIN_DIR = PROJECT_ROOT / "plugins" / "artwork_plugin"


class _Logger:
    def __getattr__(self, _level):
        return lambda *_args, **_kwargs: None


@dataclass
class _EmojiCandidate:
    full_path: str
    emoji_hash: str
    description: str = ""


@contextmanager
def isolated_artwork_modules():
    """Load the real plugin/helpers while stubbing the host plugin runtime."""
    package_name = "_nachobot_artwork_plugin_test"
    module_names = [
        "src",
        "src.common",
        "src.common.logger",
        "src.chat",
        "src.chat.emoji_system",
        "src.chat.emoji_system.emoji_collage",
        "src.chat.emoji_system.image_selection",
        "src.multimodal",
        "src.plugin_system",
        "src.plugin_system.apis",
        "src.plugin_system.apis.emoji_api",
        "src.plugin_system.base",
        "src.plugin_system.base.base_action",
        "src.plugin_system.base.component_types",
        "src.plugin_system.base.config_types",
        package_name,
        f"{package_name}.description_cache",
        f"{package_name}.plugin",
    ]
    previous = {name: sys.modules.get(name) for name in module_names}

    def install(name: str, **attributes):
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        sys.modules[name] = module
        return module

    for name in (
        "src",
        "src.common",
        "src.chat",
        "src.chat.emoji_system",
        "src.plugin_system",
        "src.plugin_system.apis",
        "src.plugin_system.base",
    ):
        install(name).__path__ = []

    logger = _Logger()
    install("src.common.logger", get_logger=lambda _name: logger)
    install("src.multimodal")
    install("src.plugin_system.apis.emoji_api", EmojiCandidate=_EmojiCandidate)

    class BaseAction:
        def get_config(self, key, default=None):
            return self.config.get(key, default)

    class BasePlugin:
        pass

    install(
        "src.plugin_system",
        BasePlugin=BasePlugin,
        ComponentInfo=object,
        register_plugin=lambda cls: cls,
    )
    install("src.plugin_system.base.base_action", BaseAction=BaseAction)
    install(
        "src.plugin_system.base.component_types",
        ActionActivationType=types.SimpleNamespace(LLM_JUDGE="llm_judge"),
    )
    install("src.plugin_system.base.config_types", ConfigField=lambda **kwargs: kwargs)

    message_api = types.SimpleNamespace(
        get_recent_messages=lambda **_kwargs: ["history-item"],
        build_readable_messages=lambda **_kwargs: "recent-history-context",
    )
    llm_api = types.SimpleNamespace(
        get_available_models=lambda: {"utils_small": "test-utils-model"},
        generate_with_model=None,
    )
    install("src.plugin_system.apis", llm_api=llm_api, message_api=message_api)

    def load_module(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise AssertionError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    helper = load_module(
        "src.chat.emoji_system.image_selection",
        PROJECT_ROOT / "src" / "chat" / "emoji_system" / "image_selection.py",
    )
    collage_module = load_module(
        "src.chat.emoji_system.emoji_collage",
        PROJECT_ROOT / "src" / "chat" / "emoji_system" / "emoji_collage.py",
    )
    package = install(package_name)
    package.__path__ = [str(ARTWORK_PLUGIN_DIR)]
    cache_module = load_module(f"{package_name}.description_cache", ARTWORK_PLUGIN_DIR / "description_cache.py")
    plugin = load_module(f"{package_name}.plugin", ARTWORK_PLUGIN_DIR / "plugin.py")

    try:
        yield plugin, helper, collage_module, cache_module, llm_api, message_api
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


def _write_png(path: Path, *, size: tuple[int, int] = (48, 48), color=(40, 80, 120)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")


class ArtworkSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.modules_context = isolated_artwork_modules()
        (
            self.module,
            self.selection_helper,
            self.collage_module,
            self.cache_module,
            self.llm_api,
            self.message_api,
        ) = self.modules_context.__enter__()
        self.addCleanup(self.modules_context.__exit__, None, None, None)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.artwork = self.root / "artwork"
        self.artwork.mkdir()
        self.cache_root = self.root / "artwork_descriptions"

    def _action(self, *, platform="qq", is_group=False, user_id="user-1"):
        action = self.module.SendArtworkAction.__new__(self.module.SendArtworkAction)
        action.config = {
            "artwork.directory": str(self.artwork),
            "artwork.allowed_extensions": [".png", ".jpg", ".jpeg", ".gif", ".webp"],
            "access_control.discord_artwork_whitelist": ["allowed-user"],
        }
        action.platform = platform
        action.is_group = is_group
        action.user_id = user_id
        action.action_message = types.SimpleNamespace(
            processed_plain_text="发张蓝色的猫图看看",
            display_message="发张图看看",
        )
        action.action_data = {"reason": "回应用户看图请求"}
        action.reasoning = "planner-reason"
        action.chat_id = "chat-1"
        action.log_prefix = "test-artwork"
        action._description_cache_root = lambda: self.cache_root
        action.send_image = mock.AsyncMock(return_value=True)
        action.send_text = mock.AsyncMock(return_value=True)
        action.store_action_info = mock.AsyncMock()
        return action

    def _write_art(self, name="first.png", *, color=(40, 80, 120)) -> Path:
        path = self.artwork / name
        _write_png(path, color=color)
        return path

    def _make_collage(self, candidates, _limit=10):
        return types.SimpleNamespace(
            image_base64="numbered-collage",
            image_format="jpeg",
            candidates=list(reversed(candidates)),
        )

    async def test_strict_numbered_index_and_optional_description_parsing(self):
        parse = self.selection_helper.parse_numbered_selection
        valid = parse('{"index": 2, "descriptions": {"1": "蓝色猫咪", "9": "忽略"}}', 2)
        self.assertEqual(valid.index, 1)
        self.assertEqual(valid.descriptions, {0: "蓝色猫咪"})
        self.assertIsNone(parse('{"index": true}', 2).index)
        self.assertIsNone(parse('{"index": 3}', 2).index)
        self.assertIsNone(parse('{"index": "2"}', 2).index)

    async def test_shared_helper_keeps_emoji_route_and_degraded_invalid_behavior(self):
        calls = []

        class Router:
            async def describe_emoji(self, image, **kwargs):
                calls.append(("emoji", image, kwargs))
                return types.SimpleNamespace(text='{"index": true}', degraded=False)

            async def describe_image(self, *_args, **_kwargs):
                raise AssertionError("emoji selection must use describe_emoji")

        sys.modules["src.multimodal"].get_multimodal_router = lambda: Router()
        result = await self.selection_helper.request_numbered_image_selection(
            "collage",
            prompt="choose",
            candidate_count=2,
            media_format="jpeg",
            image_kind="emoji",
            metadata={"temperature": 0, "max_tokens": 50},
        )
        self.assertIsNone(result.index)
        self.assertEqual(calls[0][0], "emoji")
        self.assertEqual(calls[0][2]["metadata"], {"temperature": 0, "max_tokens": 50})

        class DegradedRouter(Router):
            async def describe_emoji(self, image, **kwargs):
                calls.append(("degraded", image, kwargs))
                return types.SimpleNamespace(text='{"index": 1}', degraded=True)

        sys.modules["src.multimodal"].get_multimodal_router = lambda: DegradedRouter()
        degraded = await self.selection_helper.request_numbered_image_selection(
            "collage",
            prompt="choose",
            candidate_count=2,
            media_format="jpeg",
            image_kind="emoji",
        )
        self.assertTrue(degraded.degraded)
        self.assertIsNone(degraded.index)

        class ArtworkRouter(Router):
            async def describe_image(self, image, **kwargs):
                calls.append(("image", image, kwargs))
                return types.SimpleNamespace(text='{"index": 2}', degraded=False)

        sys.modules["src.multimodal"].get_multimodal_router = lambda: ArtworkRouter()
        artwork = await self.selection_helper.request_numbered_image_selection(
            "artwork-collage",
            prompt="choose artwork",
            candidate_count=2,
            media_format="jpeg",
            image_kind="image",
        )
        self.assertEqual(artwork.index, 1)
        self.assertEqual(calls[-1][0], "image")

    async def test_actual_collage_order_is_authoritative_after_corrupt_candidate(self):
        valid_a = self._write_art("portrait.png", color=(200, 20, 20))
        valid_b = self._write_art("landscape.png", color=(20, 200, 20))
        corrupt = self.artwork / "corrupt.png"
        corrupt.write_bytes(b"not an image")
        inputs = [
            _EmojiCandidate(str(corrupt), "corrupt"),
            _EmojiCandidate(str(valid_a), "portrait"),
            _EmojiCandidate(str(valid_b), "landscape"),
        ]
        original_layout = self.collage_module._find_best_layout

        def reversed_layout(items):
            layout = original_layout(items)
            layout.rows = [list(reversed(row)) for row in layout.rows]
            return layout

        with mock.patch.object(self.collage_module, "_find_best_layout", side_effect=reversed_layout):
            collage = self.collage_module.build_emoji_collage(inputs, limit=10)
        self.assertIsNotNone(collage)
        self.assertEqual(len(collage.candidates), 2)
        self.assertEqual(set(candidate.emoji_hash for candidate in collage.candidates), {"portrait", "landscape"})
        selected_by_number = collage.candidates[0]
        self.assertIn(Path(selected_by_number.full_path), {valid_a, valid_b})

    async def test_vlm_selects_reordered_original_and_persists_semantic_descriptions(self):
        first = self._write_art("first.png", color=(20, 30, 40))
        second = self._write_art("second.png", color=(50, 60, 70))
        mapped_order = {}

        def builder(candidates, limit=10):
            collage = self._make_collage(candidates, limit)
            mapped_order["candidates"] = collage.candidates
            return collage

        async def visual(*_args, **kwargs):
            self.assertEqual(kwargs["candidate_count"], 2)
            self.assertIn("发张蓝色的猫图看看", kwargs["prompt"])
            self.assertIn("recent-history-context", kwargs["prompt"])
            return self.selection_helper.parse_numbered_selection(
                '{"index": 1, "descriptions": {"1": "蓝色猫咪坐在窗边", "2": "绿色森林小屋"}}',
                2,
            )

        action = self._action()
        with (
            mock.patch.object(self.module, "build_emoji_collage", side_effect=builder),
            mock.patch.object(self.module, "request_numbered_image_selection", side_effect=visual),
        ):
            success, result = await action.execute(trigger_evidence="发张图看看")

        selected = mapped_order["candidates"][0]
        expected_data = base64.b64encode(selected.source_path.read_bytes()).decode("ascii")
        self.assertTrue(success, result)
        self.assertEqual(result, f"已发送画作: {selected.filename}")
        self.assertEqual(action.send_image.await_args.args[0], expected_data)
        action.store_action_info.assert_awaited_once()
        info = action.store_action_info.await_args.kwargs
        self.assertTrue(info["action_build_into_prompt"])
        self.assertIn(selected.filename, info["action_prompt_display"])
        self.assertIn(selected.description, info["action_prompt_display"])
        self.assertIn(selected.source_path, (first, second))
        fresh_cache = self.cache_module.ArtworkDescriptionCache(self.cache_root)
        self.assertEqual(fresh_cache.get(selected.emoji_hash), selected.description)

    async def test_visual_degraded_exception_boolean_and_range_use_text_llm_cache(self):
        first = self._write_art("cat.png")
        second = self._write_art("forest.png", color=(0, 150, 40))
        cache = self.cache_module.ArtworkDescriptionCache(self.cache_root)
        cache.save(hashlib.sha256(first.read_bytes()).hexdigest(), "一只小猫躺在草地上", filename=first.name)
        cache.save(hashlib.sha256(second.read_bytes()).hexdigest(), "绿色森林和木屋", filename=second.name)

        outcomes = [
            self.selection_helper.NumberedImageSelection(degraded=True),
            self.selection_helper.NumberedImageSelection(error=RuntimeError("router error")),
            self.selection_helper.parse_numbered_selection('{"index": true}', 2),
            self.selection_helper.parse_numbered_selection('{"index": 8}', 2),
        ]
        prompts = []
        displayed_orders = []
        action = self._action()

        def builder(candidates, limit=10):
            collage = self._make_collage(candidates, limit)
            displayed_orders.append(collage.candidates)
            return collage

        async def visual(*_args, **kwargs):
            prompts.append(kwargs["prompt"])
            return outcomes.pop(0)

        async def text_model(prompt, **kwargs):
            prompts.append(prompt)
            self.assertEqual(kwargs["request_type"], "artwork.text_select")
            self.assertEqual(kwargs["temperature"], 0)
            self.assertEqual(kwargs["model_config"], "test-utils-model")
            return True, '{"index": 2}', "", "offline-test"

        with (
            mock.patch.object(self.module, "build_emoji_collage", side_effect=builder),
            mock.patch.object(self.module, "request_numbered_image_selection", side_effect=visual),
            mock.patch.object(self.llm_api, "generate_with_model", side_effect=text_model),
        ):
            for _ in range(4):
                action.send_image.reset_mock()
                action.store_action_info.reset_mock()
                success, _ = await action.execute()
                self.assertTrue(success)
                self.assertEqual(action.send_image.await_count, 1)
                self.assertIn("一只小猫躺在草地上", prompts[-1])
                self.assertIn("发张蓝色的猫图看看", prompts[-1])
                self.assertIn("recent-history-context", prompts[-1])
                selected_by_text_index = displayed_orders[-1][1]
                self.assertEqual(
                    action.send_image.await_args.args[0],
                    base64.b64encode(selected_by_text_index.source_path.read_bytes()).decode("ascii"),
                )

    async def test_cache_reuse_replacement_invalidation_and_concurrent_atomic_saves(self):
        image = self._write_art()
        old_hash = hashlib.sha256(image.read_bytes()).hexdigest()
        cache = self.cache_module.ArtworkDescriptionCache(self.cache_root)
        self.assertTrue(cache.save(old_hash, "窗边的蓝色猫咪", filename=image.name))
        self.assertEqual(self.cache_module.ArtworkDescriptionCache(self.cache_root).get(old_hash), "窗边的蓝色猫咪")

        _write_png(image, color=(200, 100, 50))
        new_hash = hashlib.sha256(image.read_bytes()).hexdigest()
        self.assertNotEqual(new_hash, old_hash)
        self.assertIsNone(cache.get(new_hash))
        self.assertEqual(cache.get(old_hash), "窗边的蓝色猫咪")
        self.assertFalse(cache.save(new_hash, "无法辨认", filename=image.name))

        descriptions = [f"并发描述 {index}" for index in range(12)]
        threads = [threading.Thread(target=cache.save, args=(new_hash, description)) for description in descriptions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        persisted = cache.get(new_hash)
        self.assertIn(persisted, descriptions)
        self.assertFalse(list(self.cache_root.glob("*.tmp")))

    async def test_cache_write_failure_does_not_discard_successful_selection(self):
        self._write_art()
        action = self._action()
        cache = self.cache_module.ArtworkDescriptionCache(self.cache_root)
        save = mock.Mock(return_value=False)
        cache.save = save
        original_builder = self._make_collage

        async def visual(*_args, **_kwargs):
            return self.selection_helper.parse_numbered_selection(
                '{"index": 1, "descriptions": {"1": "窗边的蓝色猫咪"}}',
                1,
            )

        with (
            mock.patch.object(self.module, "ArtworkDescriptionCache", return_value=cache),
            mock.patch.object(self.module, "build_emoji_collage", side_effect=original_builder),
            mock.patch.object(self.module, "request_numbered_image_selection", side_effect=visual),
        ):
            success, _ = await action.execute()
        self.assertTrue(success)
        self.assertEqual(save.call_count, 1)
        action.send_image.assert_awaited_once()
        action.store_action_info.assert_awaited_once()

    async def test_both_model_paths_fail_randomly_within_valid_collage_pool(self):
        self._write_art("first.png")
        self._write_art("second.png", color=(10, 200, 30))
        action = self._action()
        selected = {}

        def choose_first(candidates):
            selected["candidate"] = candidates[0]
            return candidates[0]

        async def failed_text(*_args, **_kwargs):
            return False, "", "", ""

        with (
            mock.patch.object(self.module, "build_emoji_collage", side_effect=self._make_collage),
            mock.patch.object(
                self.module,
                "request_numbered_image_selection",
                return_value=self.selection_helper.NumberedImageSelection(degraded=True),
            ),
            mock.patch.object(self.llm_api, "generate_with_model", side_effect=failed_text),
            mock.patch.object(self.module.random, "choice", side_effect=choose_first) as choose,
        ):
            success, _ = await action.execute()
        self.assertTrue(success)
        self.assertEqual(choose.call_count, 1)
        self.assertEqual(
            action.send_image.await_args.args[0],
            base64.b64encode(selected["candidate"].source_path.read_bytes()).decode("ascii"),
        )
        self.assertEqual(action.send_text.await_count, 0)
        action.store_action_info.assert_awaited_once()
        self.assertIn("发送了画作", action.store_action_info.await_args.kwargs["action_prompt_display"])

    async def test_image_read_or_send_failure_silently_skips_success_record(self):
        for failure in ("read", "send", "send_exception"):
            image = self._write_art(f"{failure}.png")
            action = self._action()
            candidates_by_name = {}

            def builder(candidates, limit=10, candidates_by_name=candidates_by_name):
                collage = self._make_collage(candidates, limit)
                candidates_by_name.update({candidate.filename: candidate for candidate in collage.candidates})
                return collage

            async def visual(*_args, failure=failure, image=image, candidates_by_name=candidates_by_name, **kwargs):
                candidate = candidates_by_name[image.name]
                if failure == "read":
                    candidate.source_path.unlink()
                return self.selection_helper.parse_numbered_selection('{"index": 1}', 1)

            with (
                mock.patch.object(self.module, "build_emoji_collage", side_effect=builder),
                mock.patch.object(self.module, "request_numbered_image_selection", side_effect=visual),
            ):
                if failure == "send":
                    action.send_image = mock.AsyncMock(return_value=False)
                elif failure == "send_exception":
                    action.send_image = mock.AsyncMock(side_effect=RuntimeError("send unavailable"))
                success, _ = await action.execute()
            self.assertFalse(success)
            action.send_text.assert_not_awaited()
            action.store_action_info.assert_not_awaited()
            if failure == "read":
                action.send_image.assert_not_awaited()

    async def test_replacement_during_visual_wait_is_not_cached_or_sent(self):
        image = self._write_art("replaced.png", color=(10, 20, 30))
        original_hash = hashlib.sha256(image.read_bytes()).hexdigest()
        action = self._action()
        candidates_by_name = {}

        def builder(candidates, limit=10):
            collage = self._make_collage(candidates, limit)
            candidates_by_name.update({candidate.filename: candidate for candidate in collage.candidates})
            return collage

        async def visual(*_args, **_kwargs):
            _write_png(image, color=(200, 210, 220))
            return self.selection_helper.parse_numbered_selection(
                '{"index": 1, "descriptions": {"1": "旧图片的描述"}}',
                1,
            )

        with (
            mock.patch.object(self.module, "build_emoji_collage", side_effect=builder),
            mock.patch.object(self.module, "request_numbered_image_selection", side_effect=visual),
        ):
            success, _ = await action.execute()

        new_hash = hashlib.sha256(image.read_bytes()).hexdigest()
        cache = self.cache_module.ArtworkDescriptionCache(self.cache_root)
        self.assertNotEqual(new_hash, original_hash)
        self.assertIsNone(cache.get(original_hash))
        self.assertIsNone(cache.get(new_hash))
        self.assertFalse(success)
        action.send_image.assert_not_awaited()
        action.send_text.assert_not_awaited()
        action.store_action_info.assert_not_awaited()

    async def test_discord_denial_performs_no_model_cache_or_send_work(self):
        self._write_art()
        group_action = self._action(platform="discord", is_group=True)
        with (
            mock.patch.object(self.module, "ArtworkDescriptionCache", side_effect=AssertionError("cache touched")),
            mock.patch.object(self.module, "build_emoji_collage", side_effect=AssertionError("collage touched")),
            mock.patch.object(
                self.module, "request_numbered_image_selection", side_effect=AssertionError("VLM touched")
            ),
            mock.patch.object(self.llm_api, "generate_with_model", side_effect=AssertionError("LLM touched")),
        ):
            success, result = await group_action.execute()
        self.assertFalse(success)
        self.assertEqual(result, "Discord群聊禁用发送画作")
        group_action.send_image.assert_not_awaited()
        group_action.send_text.assert_not_awaited()

        unlisted = self._action(platform="discord", user_id="other-user")
        with mock.patch.object(self.module, "ArtworkDescriptionCache", side_effect=AssertionError("cache touched")):
            success, result = await unlisted.execute()
        self.assertFalse(success)
        self.assertEqual(result, "用户不在Discord画作白名单中")
        unlisted.send_image.assert_not_awaited()

    async def test_semantic_subject_single_character_matches_long_cjk_description(self):
        path = self._write_art()
        candidate = self.module._ArtworkCandidate(
            full_path=str(path),
            emoji_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            description="一只小猫躺在草地上",
            source_path=path,
            filename=path.name,
        )
        self.assertGreater(self.module.SendArtworkAction._description_match_score("发张猫图", candidate), 0)


if __name__ == "__main__":
    unittest.main()
