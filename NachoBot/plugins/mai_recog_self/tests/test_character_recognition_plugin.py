"""Multi-character behavior for the native MaiSelf plugin integration."""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_self_recognition_plugin import (
    _FakeMessage,
    _InferenceService,
    _image,
    _png,
    isolated_plugin,
)


def _multiclass_result(
    predicted: str = "nachobot",
    character_id: str | None = "nachobot",
    recognized: bool = True,
    is_other: bool = False,
    scores: dict[str, float] | None = None,
) -> dict:
    class_names = ["nachobot", "shizukubot", "moli", "other"]
    scores = scores or {"nachobot": 0.82, "shizukubot": 0.08, "moli": 0.06, "other": 0.04}
    margin = sorted(scores.values(), reverse=True)[0] - sorted(scores.values(), reverse=True)[1]
    return {
        "status": "ok",
        "task": "multiclass",
        "format_version": 2,
        "class_names": class_names,
        "scores": scores,
        "score": scores[predicted],
        "predicted_class": predicted,
        "character_id": character_id,
        "recognized": recognized,
        "is_other": is_other,
        "judgment": character_id or "不确定",
        "margin": margin,
        "score_is_calibrated_probability": False,
        "version": "v2",
    }


def _service(result: dict) -> _InferenceService:
    service = _InferenceService(Path("unused"))
    service.state = {"desired_version": "v2", "loaded_version": "v2"}
    service.result = result
    return service


class CharacterRecognitionPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_known_class_label_is_used_in_the_hint(self):
        for label in ("nachobot", "shizukubot", "moli"):
            with self.subTest(character_id=label), isolated_plugin() as (module, manager, _holder):
                plugin = module.MaiRecogSelfPlugin("unused")
                manager.instance = plugin
                scores = {name: 0.05 for name in ("nachobot", "shizukubot", "moli", "other")}
                scores[label] = 0.85
                plugin._service = _service(
                    _multiclass_result(
                        predicted=label,
                        character_id=label,
                        recognized=True,
                        scores=scores,
                    )
                )
                handler = module.SelfRecognitionPreProcess()
                handler.plugin_config = {
                    "plugin": {"enabled": True},
                    "recognition": {"auto_recognize": True, "budget_seconds": 1.0},
                }
                message = _FakeMessage([_image(_png(label.encode("ascii")))])
                outcome = await handler.execute(message)
                self.assertIsNotNone(outcome[4])
                hint = outcome[4].message_segments[1].data
                self.assertIn(f"角色「{label}」", hint)
                self.assertNotIn("Nacho", hint)
                await plugin.on_unload()

    async def test_backend_decision_controls_hint_and_other_or_uncertain_adds_none(self):
        with isolated_plugin() as (module, manager, _holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            plugin.config["recognition"]["self_character_id"] = "nachobot"
            service = _service(_multiclass_result(
                predicted="moli",
                character_id="moli",
                recognized=True,
                scores={"nachobot": 0.15, "shizukubot": 0.10, "moli": 0.55, "other": 0.20},
            ))
            plugin._service = service
            handler = module.SelfRecognitionPreProcess()
            handler.plugin_config = {
                "plugin": {"enabled": True},
                "recognition": {"auto_recognize": True, "budget_seconds": 1.0},
            }
            image = _image(_png(b"accepted-low-top1"))
            message = _FakeMessage([image])
            result = await handler.execute(message)
            self.assertIsNotNone(result[4])
            self.assertIn("角色「moli」", result[4].message_segments[1].data)
            self.assertNotIn("Nacho", result[4].message_segments[1].data)
            self.assertEqual(message.message_segments, [image])
            self.assertEqual(service.calls, 1)
            await plugin.on_unload()

            for suffix, result_fields in (
                (
                    b"accepted-other",
                    _multiclass_result(
                        predicted="other",
                        character_id="other",
                        recognized=False,
                        is_other=True,
                        scores={"nachobot": 0.04, "shizukubot": 0.06, "moli": 0.08, "other": 0.82},
                    ),
                ),
                (
                    b"uncertain",
                    _multiclass_result(
                        predicted="moli",
                        character_id=None,
                        recognized=False,
                        is_other=False,
                        scores={"nachobot": 0.15, "shizukubot": 0.10, "moli": 0.55, "other": 0.20},
                    ),
                ),
            ):
                with isolated_plugin() as (module, manager, _holder):
                    plugin = module.MaiRecogSelfPlugin("unused")
                    manager.instance = plugin
                    plugin._service = _service(result_fields)
                    handler = module.SelfRecognitionPreProcess()
                    handler.plugin_config = {
                        "plugin": {"enabled": True},
                        "recognition": {"auto_recognize": True, "budget_seconds": 1.0},
                    }
                    message = _FakeMessage([_image(_png(suffix))])
                    outcome = await handler.execute(message)
                    self.assertIsNone(outcome[4])
                    self.assertEqual(len(message.message_segments), 1)
                    self.assertEqual(plugin._service.calls, 1)
                    await plugin.on_unload()

    async def test_is_self_is_configured_separately_and_cache_tracks_config(self):
        with isolated_plugin() as (module, manager, _holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            service = _service(_multiclass_result())
            plugin._service = service
            raw = _png(b"same-image")

            plugin.config["recognition"]["self_character_id"] = "nachobot"
            own = await plugin.classify_image(raw, deadline=time.monotonic() + 1)
            self.assertEqual(own["character_id"], "nachobot")
            self.assertTrue(own["recognized"])
            self.assertTrue(own["is_self"])
            self.assertEqual(own["self_status"], "match")

            plugin.config["recognition"]["self_character_id"] = "moli"
            known_other = await plugin.classify_image(raw, deadline=time.monotonic() + 1)
            self.assertEqual(known_other["character_id"], "nachobot")
            self.assertTrue(known_other["recognized"])
            self.assertFalse(known_other["is_self"])
            self.assertEqual(known_other["self_status"], "different")
            self.assertEqual(service.calls, 2)

            plugin.config["recognition"]["self_character_id"] = ""
            unconfigured = await plugin.classify_image(raw, deadline=time.monotonic() + 1)
            self.assertTrue(unconfigured["recognized"])
            self.assertIsNone(unconfigured["is_self"])
            self.assertEqual(unconfigured["self_status"], "unconfigured")
            self.assertEqual(service.calls, 3)

            service.result = _multiclass_result(
                predicted="other",
                character_id="other",
                recognized=False,
                is_other=True,
                scores={"nachobot": 0.04, "shizukubot": 0.06, "moli": 0.08, "other": 0.82},
            )
            plugin.config["recognition"]["self_character_id"] = "nachobot"
            other = await plugin.classify_image(_png(b"other-image"), deadline=time.monotonic() + 1)
            self.assertFalse(other["recognized"])
            self.assertTrue(other["is_other"])
            self.assertFalse(other["is_self"])
            self.assertEqual(other["self_status"], "other")
            await plugin.on_unload()

    async def test_malformed_or_unknown_multiclass_schema_fails_closed(self):
        with isolated_plugin() as (module, manager, _holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            malformed = _multiclass_result(
                predicted="nachobot",
                character_id="moli",
                recognized=True,
            )
            service = _service(malformed)
            plugin._service = service
            mismatched = await plugin.classify_image(_png(b"mismatched-id"), deadline=time.monotonic() + 1)
            self.assertEqual(mismatched["status"], "model_error")

            unknown = _multiclass_result()
            unknown["format_version"] = 3
            service.result = unknown
            unsupported = await plugin.classify_image(_png(b"unknown-schema"), deadline=time.monotonic() + 1)
            self.assertEqual(unsupported["status"], "model_error")
            await plugin.on_unload()

    async def test_tools_use_their_own_scoped_context_and_return_multiclass_fields(self):
        with isolated_plugin() as (module, manager, _holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            plugin.config["recognition"]["self_character_id"] = "nachobot"
            service = _service(_multiclass_result())
            service.state = {"desired_version": "v3", "loaded_version": "v2"}
            plugin._service = service
            message = _FakeMessage(
                [_image(_png(b"tool-image")), _image(_png(b"second-image"))],
                stream_id="chat-a",
                message_id="canonical-7",
                plain_text="two pictures",
            )
            self.assertEqual(plugin.record_message_media(message), 2)
            generic = module.RecognizeCharacterImageTool()
            legacy = module.RecognizeSelfImageTool()

            preparation = generic.prepare_execution_context(SimpleNamespace(stream_id="chat-a"))
            self.assertIn("recognize_character_image", preparation.prompt_supplement)
            self.assertNotIn("recognize_self_image", preparation.prompt_supplement)
            self.assertTrue(preparation.cache_sensitive)
            result = await generic.execute_with_context({"message_id": "canonical-7"}, preparation.context)
            payload = json.loads(result["content"])
            self.assertEqual(result["name"], "recognize_character_image")
            self.assertEqual(payload["character_id"], "nachobot")
            self.assertEqual(payload["character"], "nachobot")
            self.assertEqual(payload["judgment"], "nachobot")
            self.assertEqual(payload["identity_judgment"], "nachobot")
            self.assertEqual(payload["predicted_class"], "nachobot")
            self.assertTrue(payload["recognized"])
            self.assertTrue(payload["is_self"])
            self.assertEqual(payload["image_index"], 0)
            self.assertEqual(payload["version"], "v2")
            self.assertEqual(payload["loaded_version"], "v2")
            self.assertEqual(payload["desired_version"], "v3")
            self.assertTrue(payload["fallback"])

            wrong_tool_context = await legacy.execute_with_context(
                {"message_id": "canonical-7"}, preparation.context
            )
            self.assertEqual(json.loads(wrong_tool_context["content"])["status"], "context_required")
            legacy_prep = legacy.prepare_execution_context(SimpleNamespace(stream_id="chat-a"))
            self.assertIn("recognize_self_image", legacy_prep.prompt_supplement)
            selected = await legacy.execute_with_context(
                {"message_id": "canonical-7", "image_index": 1}, legacy_prep.context
            )
            selected_payload = json.loads(selected["content"])
            self.assertEqual(selected["name"], "recognize_self_image")
            self.assertEqual(selected_payload["image_index"], 1)

            empty = generic.prepare_execution_context(SimpleNamespace(stream_id="chat-empty"))
            self.assertFalse(empty.cache_sensitive)
            cross_chat = generic.prepare_execution_context(SimpleNamespace(stream_id="chat-b"))
            missing = await generic.execute_with_context(
                {"message_id": "canonical-7"}, cross_chat.context
            )
            self.assertEqual(json.loads(missing["content"])["status"], "not_found")
            await plugin.on_unload()

    async def test_legacy_uncertainty_and_legacy_wrapper_fields_remain(self):
        with isolated_plugin() as (module, manager, _holder):
            plugin = module.MaiRecogSelfPlugin("unused")
            manager.instance = plugin
            service = _InferenceService(Path("unused"))
            service.state = {"desired_version": "v1", "loaded_version": "v1"}
            service.result = {"status": "ok", "version": "v1", "score": 0.5, "judgment": "不确定", "character": "Nacho"}
            plugin._service = service
            legacy = await plugin.classify_image(_png(b"legacy-mid-score"), deadline=time.monotonic() + 1)
            self.assertEqual(legacy["character"], "Nacho")
            self.assertEqual(legacy["judgment"], "不确定")
            self.assertIsNone(legacy["is_self"])
            self.assertEqual(legacy["self_status"], "unknown")
            await plugin.on_unload()
