from __future__ import annotations

import asyncio
import base64
import builtins
import json
import math
import os
import queue
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from PIL import Image

from desktop_pet_adapter.backend import BackendContext, create_backend
from desktop_pet_adapter.config import ConfigError, DesktopInteractionConfig, load_config
from desktop_pet_adapter.desktop_controller import (
    DesktopPetController,
    DesktopPetControllerHost,
    DesktopPetFrame,
)
from desktop_pet_adapter.desktop_interactions import DesktopInteractionSession
from desktop_pet_adapter.desktop_movement import MovementGeometry
from desktop_pet_adapter.desktop_scene import SceneSnapshot, SceneWindow
from desktop_pet_adapter.input_counter import InputCounter
from desktop_pet_adapter.launcher import LaunchSession, SessionStarter, SessionSupervisor, launch
from desktop_pet_adapter.protocol import AvatarCommand, AvatarEvent, ProtocolError
from desktop_pet_adapter.runtime import AvatarRuntime
from desktop_pet_adapter.sprite_backend import SpriteBackend
from desktop_pet_adapter.sprite_counter_display import CounterFeedbackState, draw_counter_overlay
from desktop_pet_adapter.sprite_movement import SpriteRoamingPolicy
from desktop_pet_adapter.sprite_renderer import SpriteAtlasManifest, SpriteRenderer
from desktop_pet_adapter.sprite_robot_expression import (
    draw_robot_expression,
    select_robot_expression,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "desktop_pet_adapter" / "assets" / "nachobot_chibi" / "manifest.json"


def _copy_builtin_sprite_assets(folder: Path) -> None:
    for name in ("sprites.png", "sprites-left.png", "sprites-held.png"):
        shutil.copyfile(DEFAULT_MANIFEST.parent / name, folder / name)


def _synthetic_direction_manifest(folder: Path, *, with_left_states: bool = True) -> Path:
    frame_width = frame_height = 32
    states = {}
    for index, state in enumerate(("idle", "walk", "climb", "fall", "drag", "react")):
        frames = [[column * frame_width, index * frame_height, frame_width, frame_height]
                  for column in range(2)]
        states[state] = {
            "frames": 2,
            "fps": 8,
            "frame_rects": frames,
            "face_rects": [[8, 7, 12, 9], [8, 7, 12, 9]],
        }
    for filename, base_color, left_facing in (
        ("right.png", (20, 70, 210, 255), False),
        ("left.png", (20, 180, 55, 255), True),
    ):
        image = Image.new("RGBA", (frame_width * 2, frame_height * 6), (0, 0, 0, 0))
        for state_index, state in enumerate(("idle", "walk", "climb", "fall", "drag", "react")):
            if filename == "left.png" and state not in {"walk", "climb"}:
                continue
            for frame_index in range(2):
                x_offset = frame_index * frame_width
                y_offset = state_index * frame_height
                for y in range(4, 28):
                    for x in range(6, 26):
                        image.putpixel((x_offset + x, y_offset + y), base_color)
                # The hair clip stays on the viewer's right in both assets.
                for y in range(5, 9):
                    for x in range(21, 25):
                        image.putpixel((x_offset + x, y_offset + y), (245, 20, 25, 255))
                eye_x = 9 if left_facing else 21
                image.putpixel((x_offset + eye_x, y_offset + 13), (255, 255, 255, 255))
        image.save(folder / filename)
    payload = {
        "version": 1,
        "atlas": "right.png",
        "left_atlas": "left.png",
        "columns": 2,
        "frame_width": frame_width,
        "frame_height": frame_height,
        "alpha_margin": 1,
        "states": states,
    }
    if with_left_states:
        payload["left_states"] = {
            state: {
                "face_rects": [[10, 7, 11, 9], [10, 7, 11, 9]],
            }
            for state in ("walk", "climb")
        }
    manifest_path = folder / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


def _config(*, desktop_pet_enabled: bool = True, size: int = 176):
    base = load_config(
        ROOT / "config.toml",
        runtime_mode_override="desktop_pet",
        renderer_backend_override="sprite",
    )
    interaction = replace(
        base.desktop_pet.interactions,
        enabled=False,
        window_approach=True,
        icon_approach=True,
        follow_foreground_window=True,
    )
    chat = replace(base.desktop_pet.chat, enabled=False, follow_pet=False)
    pet = replace(
        base.desktop_pet,
        enabled=desktop_pet_enabled,
        tray_icon=False,
        remember_position=False,
        state_path=None,
        idle_motion_groups=(),
        interactions=interaction,
        chat=chat,
    )
    return replace(
        base,
        renderer=replace(
            base.renderer,
            backend="sprite",
            model_path=ROOT / "does-not-exist.model3.json",
            width=size,
            height=size,
        ),
        sprite=replace(base.sprite, size_px=size, atlas_path=None),
        desktop_pet=pet,
        components=replace(base.components, core=False, tts=False, perception=False),
    )


def _context(config, *, command_queue=None, voice_stream_queue=None):
    return BackendContext(
        config=config,
        logger=Mock(),
        command_queue=command_queue or queue.Queue(),
        voice_stream_queue=voice_stream_queue or queue.Queue(),
        on_click=lambda _button: None,
        on_ready=lambda: None,
        on_exit=lambda: None,
    )


class SpriteManifestAndBackendTests(unittest.TestCase):
    def test_packaged_manifest_loads_zero_origin_rectangles_and_all_states(self) -> None:
        manifest = SpriteAtlasManifest.load(DEFAULT_MANIFEST)
        self.assertEqual(set(manifest.states), {"idle", "walk", "climb", "fall", "drag", "react"})
        self.assertEqual(manifest.states["idle"].frame_rects[0][:2], (0, 0))
        self.assertEqual(len(manifest.states["walk"].frame_rects), 4)
        self.assertEqual(len(manifest.states["idle"].face_rects), 4)
        self.assertEqual(manifest.states["drag"].frames, 8)
        self.assertEqual(manifest.states["drag"].fps, 6)
        self.assertEqual(manifest.states["drag"].atlas_path.name, "sprites-held.png")
        self.assertEqual(manifest.states["drag"].source_scale, 0.55)
        self.assertEqual(manifest.states["drag"].source_anchor, (215.0, 408.0))
        self.assertEqual(manifest.states["drag"].frame_rects[0], (0, 0, 444, 444))
        self.assertEqual(manifest.states["drag"].frame_rects[-1], (1331, 444, 443, 443))
        self.assertEqual(len(manifest.states["drag"].face_rects), 8)
        self.assertEqual(len(manifest.states["drag"].robot_rects), 8)
        self.assertEqual(
            {state: row.frames for state, row in manifest.states.items()},
            {"idle": 4, "walk": 4, "climb": 4, "fall": 4, "drag": 8, "react": 4},
        )
        self.assertEqual(sum(len(row.face_rects) for row in manifest.states.values()), 28)
        self.assertEqual(manifest.robot_scale, 1.9)
        self.assertTrue(manifest.atlas_path.is_file())

    def test_legacy_manifest_without_face_rectangles_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            _copy_builtin_sprite_assets(folder)
            payload = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
            payload.pop("left_atlas", None)
            payload.pop("left_states", None)
            for state in payload["states"].values():
                state.pop("face_rects", None)
                state.pop("robot_rects", None)
            manifest_path = folder / "manifest.json"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            manifest = SpriteAtlasManifest.load(manifest_path)
            self.assertTrue(all(row.face_rects is None for row in manifest.states.values()))

    def test_per_state_atlas_scale_and_pivot_are_optional_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            manifest_path = _synthetic_direction_manifest(folder, with_left_states=False)
            shutil.copyfile(folder / "right.png", folder / "held.png")
            legacy_manifest = SpriteAtlasManifest.load(manifest_path)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["states"]["drag"].update(
                atlas="held.png",
                source_scale=0.5,
                source_anchor=[16, 5],
            )
            payload["states"]["walk"].update(
                atlas="held.png",
                source_scale=0.75,
                source_anchor=[16, 5],
            )
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            manifest = SpriteAtlasManifest.load(manifest_path)
            self.assertEqual(manifest.states["drag"].atlas_path, (folder / "held.png").resolve())
            self.assertEqual(manifest.states["drag"].source_scale, 0.5)
            self.assertEqual(manifest.states["drag"].source_anchor, (16.0, 5.0))
            self.assertEqual(manifest.states["idle"], legacy_manifest.states["idle"])
            self.assertEqual(manifest.left_states["walk"].atlas_path, None)
            self.assertEqual(manifest.left_states["walk"].source_scale, 0.75)
            self.assertEqual(manifest.left_states["walk"].source_anchor, (16.0, 5.0))
            self.assertIsNone(legacy_manifest.states["drag"].atlas_path)
            self.assertEqual(legacy_manifest.states["drag"].source_scale, 1.0)
            self.assertIsNone(legacy_manifest.states["drag"].source_anchor)

            invalid_cases = (
                ({"atlas": "../held.png"}, "asset directory"),
                ({"atlas": "missing.png"}, "image not found"),
                ({"source_scale": 0.0}, "source_scale must be finite"),
                ({"source_scale": float("nan")}, "source_scale must be finite"),
                ({"source_anchor": [float("inf"), 5]}, "source_anchor must be a finite"),
                ({"source_anchor": [-1, 5]}, "source_anchor must be non-negative"),
                ({"source_anchor": [32, 5]}, "source_anchor must fit every source frame"),
            )
            for values, error in invalid_cases:
                with self.subTest(values=values):
                    invalid_payload = json.loads(json.dumps(payload))
                    invalid_payload["states"]["drag"].update(values)
                    manifest_path.write_text(json.dumps(invalid_payload), encoding="utf-8")
                    with self.assertRaisesRegex((ValueError, FileNotFoundError), error):
                        SpriteAtlasManifest.load(manifest_path)

            invalid_payload = json.loads(json.dumps(payload))
            invalid_payload["states"]["drag"]["frame_rects"][0] = [60, 128, 32, 32]
            manifest_path.write_text(json.dumps(invalid_payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frame 0 is outside the image"):
                SpriteAtlasManifest.load(manifest_path)

            Image.new("RGBA", (64, 192), (10, 20, 30, 255)).save(folder / "held.png")
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "has no transparent pixels"):
                SpriteAtlasManifest.load(manifest_path)

    def test_face_rect_metadata_rejects_frame_count_and_bounds_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            _copy_builtin_sprite_assets(folder)
            payload = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
            payload["states"]["idle"]["face_rects"] = [[1, 1, 10, 10]]
            manifest_path = folder / "manifest.json"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid row, frame count, or fps"):
                SpriteAtlasManifest.load(manifest_path)

            payload["states"]["idle"]["face_rects"] = [
                [255, 1, 2, 10],
                *json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))["states"]["idle"]["face_rects"][1:],
            ]
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "face rectangle 0 is outside its frame"):
                SpriteAtlasManifest.load(manifest_path)

    def test_manifest_rejects_non_finite_animation_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            _copy_builtin_sprite_assets(folder)
            payload = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
            payload["states"]["walk"]["fps"] = float("nan")
            manifest_path = folder / "manifest.json"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid row, frame count, or fps"):
                SpriteAtlasManifest.load(manifest_path)

    def test_left_atlas_defaults_to_walk_and_climb_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            manifest_path = _synthetic_direction_manifest(folder, with_left_states=False)
            with Image.open(folder / "left.png") as source:
                expanded = Image.new("RGBA", (source.width + 16, source.height), (0, 0, 0, 0))
                expanded.paste(source, (0, 0))
                expanded.save(folder / "left.png")
            manifest = SpriteAtlasManifest.load(manifest_path)
        self.assertEqual(manifest.left_atlas_path.name, "left.png")
        self.assertEqual(set(manifest.left_states), {"walk", "climb"})
        self.assertEqual(manifest.left_states["walk"], manifest.states["walk"])

    def test_left_atlas_rejects_traversal_and_invalid_rectangles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            manifest_path = _synthetic_direction_manifest(folder)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["left_atlas"] = "../outside.png"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "left_atlas must remain inside"):
                SpriteAtlasManifest.load(manifest_path)

            payload["left_atlas"] = "left.png"
            payload["left_states"]["walk"]["face_rects"] = [[25, 0, 10, 10]] * 2
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "face rectangle 0 is outside its frame"):
                SpriteAtlasManifest.load(manifest_path)

            payload["left_states"]["walk"]["face_rects"] = [[10, 7, 11, 9]] * 2
            payload["left_states"]["walk"]["frame_rects"] = [[60, 32, 32, 32], [32, 32, 32, 32]]
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frame 0 is outside the image"):
                SpriteAtlasManifest.load(manifest_path)

            payload["left_states"]["walk"]["frame_rects"] = [
                [32, 32, 32, 32], [0, 32, 32, 32]
            ]
            payload["left_states"]["walk"]["robot_rects"] = [[5, 4, 10, 12]] * 2
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "robot rectangle must fit its frame and contain its face"):
                SpriteAtlasManifest.load(manifest_path)

            payload["left_states"]["walk"].pop("robot_rects")
            Image.new("RGBA", (64, 192), (50, 50, 50, 255)).save(folder / "left.png")
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "left sprite atlas has no transparent pixels"):
                SpriteAtlasManifest.load(manifest_path)

    def test_left_atlas_rejects_inconsistent_frame_count_and_invalid_fps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            manifest_path = _synthetic_direction_manifest(folder)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["left_states"]["walk"]["frames"] = 1
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid row, frame count, or fps"):
                SpriteAtlasManifest.load(manifest_path)

            payload["left_states"]["walk"].pop("frames")
            payload["left_states"]["walk"]["fps"] = float("inf")
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid row, frame count, or fps"):
                SpriteAtlasManifest.load(manifest_path)

    def test_config_override_selects_sprite_without_rewriting_main_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            source = (
                '[runtime]\nmode = "desktop_pet"\n'
                '[renderer]\nbackend = "live2d"\nmodel_path = "missing.model3.json"\nwidth = 520\nheight = 760\n'
                '[sprite]\nsize_px = 168\n'
            )
            path.write_text(source, encoding="utf-8")
            live_config = load_config(path)
            sprite_config = load_config(path, renderer_backend_override="sprite")
            self.assertEqual(live_config.renderer.backend, "live2d")
            self.assertEqual((live_config.renderer.width, live_config.renderer.height), (520, 760))
            self.assertEqual(sprite_config.renderer.backend, "sprite")
            self.assertEqual((sprite_config.renderer.width, sprite_config.renderer.height), (168, 168))
            self.assertEqual(sprite_config.desktop_pet.character_name, "NachoBot")
            self.assertNotEqual(live_config.desktop_pet.state_path, sprite_config.desktop_pet.state_path)
            self.assertEqual(path.read_text(encoding="utf-8"), source)

    def test_sprite_ready_payload_uses_sprite_size_and_omits_model_path(self) -> None:
        config = _config(size=168)
        runtime = AvatarRuntime(config, Mock())
        runtime.backend = SpriteBackend(config, Mock())
        runtime.renderer = SimpleNamespace(
            pcm_audio_ready=True,
            audio_ready=False,
            _wav_channel=None,
        )
        payload = runtime.ready_payload()
        self.assertEqual((payload["width"], payload["height"]), (168, 168))
        self.assertEqual(payload["backend"], "sprite")
        self.assertIsNone(payload["model_path"])

    def test_config_rejects_non_finite_roaming_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[renderer]\nbackend = "sprite"\n'
                '[sprite]\nmovement_speed_px_per_second = nan\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "must be finite"):
                load_config(path)

    def test_sprite_backend_prepares_without_importing_live2d_or_resolving_model(self) -> None:
        config = _config()
        original_import = builtins.__import__

        def blocked_import(name, *args, **kwargs):
            if name == "live2d_adapter" or name.startswith("desktop_pet_adapter."):
                raise AssertionError(f"sprite import reached Live2D module: {name}")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=blocked_import):
            backend = create_backend("sprite", config, Mock())
            info = backend.prepare()
        self.assertIsNone(backend.model_adapter)
        self.assertEqual(info["atlas"], str(DEFAULT_MANIFEST.parent / "sprites.png"))
        self.assertFalse(config.renderer.model_path.is_file())

    def test_audio_capabilities_report_wav_and_pcm_independently(self) -> None:
        backend = SpriteBackend(_config(), Mock())
        capabilities = backend.capabilities(
            SimpleNamespace(audio_ready=True, _wav_channel=None, pcm_audio_ready=True)
        )
        self.assertFalse(capabilities["audio_playback"])
        self.assertTrue(capabilities["voice_stream_pcm"])
        self.assertIn("voice_stream", capabilities["commands"])
        self.assertNotIn("play_audio", capabilities["commands"])


class SpriteRoamingTests(unittest.TestCase):
    def test_low_speed_and_delta_time_are_not_quantized(self) -> None:
        low_speed = SpriteRoamingPolicy(
            speed_px_per_second=1,
            idle_pause_min_seconds=0,
            idle_pause_max_seconds=0,
        )
        start = (100.0, 724.0)
        position = start
        for _ in range(60):
            position = low_speed.advance(
                position,
                viewport=(176, 176),
                work_area=(0, 0, 2000, 900),
                delta_time=1 / 60,
                allowed=True,
            ).position
        self.assertAlmostEqual(position[0] - start[0], 1.0, places=6)

        fast = SpriteRoamingPolicy(
            speed_px_per_second=44,
            idle_pause_min_seconds=0,
            idle_pause_max_seconds=0,
        )
        slow = SpriteRoamingPolicy(
            speed_px_per_second=44,
            idle_pause_min_seconds=0,
            idle_pause_max_seconds=0,
        )
        fast_position = start
        slow_position = start
        for _ in range(30):
            fast_position = fast.advance(
                fast_position,
                viewport=(176, 176),
                work_area=(0, 0, 2000, 900),
                delta_time=1 / 30,
                allowed=True,
            ).position
        for _ in range(60):
            slow_position = slow.advance(
                slow_position,
                viewport=(176, 176),
                work_area=(0, 0, 2000, 900),
                delta_time=1 / 60,
                allowed=True,
            ).position
        self.assertAlmostEqual(fast_position[0], slow_position[0], places=6)

    def test_hidden_drag_pauses_fall_and_screen_edges_are_bounded(self) -> None:
        policy = SpriteRoamingPolicy(
            speed_px_per_second=40,
            idle_pause_min_seconds=0,
            idle_pause_max_seconds=0,
            margin=10,
        )
        initial = (100.0, 500.0)
        paused = policy.advance(
            initial,
            viewport=(176, 176),
            work_area=(0, 0, 800, 600),
            delta_time=0.2,
            allowed=False,
        )
        self.assertEqual(paused.position, initial)
        self.assertEqual(paused.state, "idle")
        falling = policy.advance(
            (100.0, 100.0),
            viewport=(176, 176),
            work_area=(0, 0, 800, 600),
            delta_time=0.2,
            allowed=True,
        )
        self.assertEqual(falling.state, "fall")
        self.assertAlmostEqual(falling.position[1], 108.0)
        at_edge = policy.advance(
            (614.0, 414.0),
            viewport=(176, 176),
            work_area=(0, 0, 800, 600),
            delta_time=0.25,
            allowed=True,
        )
        self.assertLessEqual(at_edge.position[0], 614.0)
        self.assertEqual(at_edge.direction, -1)
        self.assertEqual(at_edge.state, "idle")

    def test_active_walk_continues_during_input_then_stops_at_the_turn(self) -> None:
        policy = SpriteRoamingPolicy(
            speed_px_per_second=40,
            idle_pause_min_seconds=0.2,
            idle_pause_max_seconds=0.2,
        )
        position = (616.0, 424.0)
        paused = policy.advance(
            position,
            viewport=(176, 176),
            work_area=(0, 0, 800, 600),
            delta_time=0.25,
            allowed=True,
        )
        self.assertEqual(paused.state, "idle")
        started = policy.advance(
            paused.position,
            viewport=(176, 176),
            work_area=(0, 0, 800, 600),
            delta_time=0.1,
            allowed=True,
        )
        self.assertEqual(started.state, "walk")
        policy.note_user_input()

        position = started.position
        states = []
        for _ in range(4):
            step = policy.advance(
                position,
                viewport=(176, 176),
                work_area=(0, 0, 800, 600),
                delta_time=0.1,
                allowed=False,
                continue_allowed=True,
            )
            position = step.position
            states.append(step.state)

        self.assertEqual(position, (624.0, 424.0))
        self.assertEqual(states[-1], "idle")
        self.assertEqual(policy.direction, -1)
        blocked = policy.advance(
            position,
            viewport=(176, 176),
            work_area=(0, 0, 800, 600),
            delta_time=0.1,
            allowed=False,
            continue_allowed=True,
        )
        self.assertEqual(blocked.position, position)
        self.assertEqual(blocked.state, "idle")

    def test_fall_continues_during_input_and_cannot_start_a_new_walk(self) -> None:
        policy = SpriteRoamingPolicy(
            speed_px_per_second=40,
            idle_pause_min_seconds=0,
            idle_pause_max_seconds=0,
        )
        position = (100.0, 100.0)
        step = policy.advance(
            position,
            viewport=(176, 176),
            work_area=(0, 0, 800, 600),
            delta_time=0.25,
            allowed=True,
        )
        self.assertEqual(step.state, "fall")
        policy.note_user_input()
        position = step.position
        while position[1] < 424:
            step = policy.advance(
                position,
                viewport=(176, 176),
                work_area=(0, 0, 800, 600),
                delta_time=0.25,
                allowed=False,
                continue_allowed=True,
            )
            self.assertEqual(step.state, "fall")
            position = step.position
        self.assertEqual(position[1], 424.0)

        blocked = policy.advance(
            position,
            viewport=(176, 176),
            work_area=(0, 0, 800, 600),
            delta_time=0.25,
            allowed=False,
            continue_allowed=True,
        )
        self.assertEqual(blocked.position, position)
        self.assertEqual(blocked.state, "idle")

    def test_user_input_restarts_pause(self) -> None:
        class FixedRandom:
            def uniform(self, _a, _b):
                return 0.5

        policy = SpriteRoamingPolicy(
            speed_px_per_second=40,
            idle_pause_min_seconds=0.5,
            idle_pause_max_seconds=0.5,
            random_source=FixedRandom(),
        )
        position = (100.0, 424.0)
        policy.advance(position, viewport=(176, 176), work_area=(0, 0, 800, 600), delta_time=0.2, allowed=True)
        policy.note_user_input()
        after_input = policy.advance(
            position,
            viewport=(176, 176),
            work_area=(0, 0, 800, 600),
            delta_time=0.2,
            allowed=True,
        )
        self.assertEqual(after_input.position, position)
        self.assertEqual(after_input.state, "idle")


class SpriteRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import pygame
        except ImportError:
            cls.pygame = None
            return
        cls.pygame = pygame
        cls._previous_sdl_video_driver = os.environ.get("SDL_VIDEODRIVER")
        os.environ["SDL_VIDEODRIVER"] = "dummy"

    @classmethod
    def tearDownClass(cls):
        if cls.pygame is None:
            return
        if cls._previous_sdl_video_driver is None:
            os.environ.pop("SDL_VIDEODRIVER", None)
        else:
            os.environ["SDL_VIDEODRIVER"] = cls._previous_sdl_video_driver

    def setUp(self):
        if self.pygame is None:
            self.skipTest("pygame is not installed")
        self.pygame.display.init()
        self.pygame.display.set_mode((1, 1))
        self.renderer = SpriteRenderer(
            _context(_config()),
            SpriteAtlasManifest.load(DEFAULT_MANIFEST),
            enable_desktop_controller=False,
        )
        self.renderer._pygame = self.pygame
        self.renderer._surface = self.pygame.Surface(
            (self.renderer.width, self.renderer.height), self.pygame.SRCALPHA, 32
        )
        self.renderer._load_frames()

    def _make_renderer(self, manifest_path: Path) -> SpriteRenderer:
        renderer = SpriteRenderer(
            _context(_config()),
            SpriteAtlasManifest.load(manifest_path),
            enable_desktop_controller=False,
        )
        renderer._pygame = self.pygame
        renderer._surface = self.pygame.Surface(
            (renderer.width, renderer.height), self.pygame.SRCALPHA, 32
        )
        renderer._load_frames()
        return renderer

    def test_builtin_drag_cycles_distinct_held_poses_through_hover_and_release(self) -> None:
        renderer = self.renderer
        counts = SimpleNamespace(total=420, keyboard_presses=310, mouse_clicks=110)
        renderer._input_counter = SimpleNamespace(snapshot=lambda: counts)
        renderer._input_feedback = CounterFeedbackState()
        renderer._input_feedback.prime(counts.total)
        renderer._last_observed_counter_total = counts.total
        renderer._last_counter_input_at = None
        renderer._cursor_over_robot_face = Mock(return_value=True)
        renderer._dragging = True
        renderer._set_animation("drag", 1)

        row = renderer._selected_state_row()
        self.assertEqual(row.frames, 8)
        self.assertEqual(row.fps, 6)
        self.assertEqual(len(renderer._frames["drag"]), 8)
        self.assertEqual(len(renderer._face_bounds["drag"]), 8)
        self.assertTrue(all(bounds is not None for bounds in renderer._face_bounds["drag"]))

        # Exclude the robot and its counter by comparing a common body-only viewport.
        body_right = min(bounds[0] for bounds in renderer._face_bounds["drag"]) - 20
        self.assertGreater(body_right, 80)
        body_images = [
            self.pygame.image.tostring(
                frame.subsurface(self.pygame.Rect(0, 0, body_right, renderer.height)), "RGBA"
            )
            for frame in renderer._frames["drag"]
        ]
        self.assertEqual(len(set(body_images)), 8)

        frame_bounds = renderer._frame_bounds["drag"]
        self.assertLessEqual(
            max(bounds[1] for bounds in frame_bounds) - min(bounds[1] for bounds in frame_bounds),
            3,
        )
        self.assertLessEqual(
            max(bounds[3] for bounds in frame_bounds) - min(bounds[3] for bounds in frame_bounds),
            3,
        )

        with patch("desktop_pet_adapter.sprite_renderer.time.monotonic", return_value=200.0):
            rendered = []
            for index in range(row.frames):
                renderer._frame_index = index
                frame = renderer._frames["drag"][index]
                face_rect = renderer._face_bounds["drag"][index]
                expected = frame.copy()
                draw_counter_overlay(
                    expected,
                    face_rect,
                    counts.total,
                    keyboard_presses=counts.keyboard_presses,
                    mouse_clicks=counts.mouse_clicks,
                    hovered=True,
                )
                renderer._draw_frame()
                rendered.append(
                    (self.pygame.image.tostring(expected, "RGBA"),
                     self.pygame.image.tostring(renderer._surface, "RGBA"))
                )
            renderer._frame_elapsed = 0.0
            for index in range(1, row.frames + 1):
                renderer._advance_animation(1 / row.fps + 0.001)
                self.assertEqual(renderer._frame_index, index % row.frames)

        self.assertTrue(all(expected == actual for expected, actual in rendered))
        self.assertEqual(renderer._last_counter_input_at, None)
        self.assertEqual(renderer._input_feedback.burst_count, 0)
        self.assertEqual(
            renderer._cursor_over_robot_face.call_args_list,
            [call(bounds) for bounds in renderer._face_bounds["drag"]],
        )

        renderer._finish_drag()
        self.assertFalse(renderer._dragging)
        self.assertEqual(renderer._state, "idle")
        self.assertEqual(renderer._selected_state_row().frames, 4)
        for state, delta_time in (("fall", 0.25), ("walk", 0.25)):
            renderer._set_animation(state, 1)
            renderer._advance_animation(delta_time)
            self.assertEqual(renderer._selected_state_row().frames, 4)
            self.assertNotEqual(renderer._frame_index, 0)

    def test_local_keyboard_and_mouse_events_do_not_interrupt_an_active_walk(self) -> None:
        renderer = self.renderer
        renderer._work_area = (0, 0, 800, 600)
        floor_y = 600 - renderer.height - renderer.config.desktop_pet.margin
        renderer._position = (100.0, float(floor_y))
        renderer._visible = True
        renderer._dragging = False
        renderer._state = "idle"
        renderer._command_state_until = 0.0
        renderer._roaming = SpriteRoamingPolicy(
            speed_px_per_second=40,
            idle_pause_min_seconds=0.2,
            idle_pause_max_seconds=0.2,
            margin=renderer.config.desktop_pet.margin,
        )
        work_area = (0, 0, 800, 600)
        with patch.object(renderer, "_get_work_area", return_value=work_area), patch.object(
            renderer, "_system_idle_seconds", side_effect=(100.0, 100.0, 0.0, 0.0)
        ):
            renderer._advance_roaming(0.25)
            renderer._advance_roaming(0.1)
            started_at = renderer._position
            renderer._handle_events(
                [self.pygame.event.Event(self.pygame.KEYDOWN, {"key": self.pygame.K_a})]
            )
            renderer._handle_events(
                [
                    self.pygame.event.Event(
                        self.pygame.MOUSEMOTION,
                        {"pos": (10, 10), "rel": (1, 0), "buttons": (0, 0, 0)},
                    )
                ]
            )
            renderer._advance_roaming(0.1)
            after_input = renderer._position
            renderer._advance_roaming(0.1)

        self.assertGreater(started_at[0], 100.0)
        self.assertGreater(after_input[0], started_at[0])
        self.assertGreater(renderer._position[0], after_input[0])
        self.assertEqual(renderer._state, "walk")

    def test_global_activity_does_not_start_a_new_floor_journey(self) -> None:
        renderer = self.renderer
        renderer._work_area = (0, 0, 800, 600)
        floor_y = 600 - renderer.height - renderer.config.desktop_pet.margin
        renderer._position = (100.0, float(floor_y))
        renderer._visible = True
        renderer._dragging = False
        renderer._state = "idle"
        renderer._command_state_until = 0.0
        renderer._roaming = SpriteRoamingPolicy(
            speed_px_per_second=40,
            idle_pause_min_seconds=0,
            idle_pause_max_seconds=0,
            margin=renderer.config.desktop_pet.margin,
        )
        with patch.object(renderer, "_get_work_area", return_value=(0, 0, 800, 600)), patch.object(
            renderer, "_system_idle_seconds", return_value=0.0
        ):
            renderer._advance_roaming(0.1)
        self.assertEqual(renderer._position, (100.0, float(floor_y)))
        self.assertEqual(renderer._state, "idle")

    def _expected_expression_frame(self, frame, face_rect, state: str, elapsed: float):
        expected = frame.copy()
        expression = select_robot_expression(state, elapsed)
        draw_robot_expression(expected, face_rect, expression)
        return expected

    def test_renderer_construction_and_preview_do_not_start_input_hooks(self) -> None:
        self.assertIsNone(self.renderer._input_counter)
        with tempfile.TemporaryDirectory() as directory:
            self.renderer.run_preview_smoke(Path(directory) / "preview.png", frame_count=1)
        self.assertIsNone(self.renderer._input_counter)

    def test_game_input_flags_and_command_are_sprite_only_and_intercepted(self) -> None:
        class FakeCounter:
            game_input_available = True
            game_mode_state = "pending"

            def __init__(self):
                self.enabled = 0
                self.disabled = 0

            def enable_game_mode(self):
                self.enabled += 1
                return True

            def disable_game_mode(self):
                self.disabled += 1

        renderer = self.renderer
        counter = FakeCounter()
        renderer._input_counter = counter
        controller = Mock()
        renderer._desktop_controller = controller
        self.assertTrue(renderer._read_window_flag("game_input_available"))
        self.assertTrue(renderer._read_window_flag("game_input_pending"))
        self.assertFalse(renderer._read_window_flag("game_input_active"))
        self.assertFalse(renderer._read_window_flag("game_input_stopping"))
        self.assertFalse(renderer._read_window_flag("game_input_failed"))

        renderer.command_queue.put(("desktop_toggle_game_input", None))
        renderer._process_commands()
        self.assertEqual(counter.disabled, 1)
        controller.handle_command.assert_not_called()

        counter.game_mode_state = "failed"
        renderer.command_queue.put(("desktop_toggle_game_input", None))
        renderer._process_commands()
        self.assertEqual(counter.enabled, 1)
        controller.handle_command.assert_not_called()

        renderer._input_counter = None
        for flag in (
            "game_input_available",
            "game_input_pending",
            "game_input_active",
            "game_input_stopping",
            "game_input_failed",
        ):
            self.assertFalse(renderer._read_window_flag(flag))

    def test_drag_uses_cursor_monitor_and_preserves_fractional_window_position(self) -> None:
        renderer = self.renderer
        renderer._dragging = True
        renderer._hwnd = 123
        renderer._position = (100.5, 200.6)
        renderer._drag_last_global = (100, 200)
        calls = []
        win32api = SimpleNamespace(
            GetCursorPos=lambda: (115, 220),
            GetAsyncKeyState=lambda _key: 0x8000,
            MonitorFromPoint=lambda point, flag: calls.append((point, flag)) or 7,
            MonitorFromWindow=Mock(side_effect=AssertionError("drag must use cursor monitor")),
            GetMonitorInfo=lambda _monitor: {"Work": (0, 0, 3000, 2000)},
        )
        win32con = SimpleNamespace(
            MONITOR_DEFAULTTONEAREST=2,
            SWP_NOSIZE=1,
            SWP_NOZORDER=4,
            SWP_NOACTIVATE=16,
        )
        positioned = []
        win32gui = SimpleNamespace(SetWindowPos=lambda *args: positioned.append(args))
        with patch("desktop_pet_adapter.sprite_renderer.os.name", "nt"):
            with patch.dict(
                "sys.modules",
                {"win32api": win32api, "win32con": win32con, "win32gui": win32gui},
            ):
                renderer._poll_drag()

        self.assertEqual(renderer._work_area, (0, 0, 3000, 2000))
        self.assertEqual(renderer._position, (115.5, 220.6))
        self.assertEqual(calls, [((115, 220), 2)])
        self.assertEqual(positioned[0][2:4], (116, 221))

    def test_desktop_frame_refreshes_work_area_cache(self) -> None:
        renderer = self.renderer
        renderer._hwnd = 123
        renderer._work_area = (0, 0, 800, 600)
        win32api = SimpleNamespace(
            MonitorFromWindow=lambda _hwnd, _flag: 7,
            GetMonitorInfo=lambda _monitor: {"Work": (-100, 0, 2500, 1500)},
        )
        win32con = SimpleNamespace(MONITOR_DEFAULTTONEAREST=2)
        win32gui = SimpleNamespace(
            GetWindowRect=lambda _hwnd: (10, 20, 186, 196),
            IsWindowVisible=lambda _hwnd: True,
        )
        with patch("desktop_pet_adapter.sprite_renderer.os.name", "nt"):
            with patch.dict(
                "sys.modules",
                {"win32api": win32api, "win32con": win32con, "win32gui": win32gui},
            ):
                frame = renderer._desktop_frame()

        self.assertIsNotNone(frame)
        self.assertEqual(frame.work_area, (-100, 0, 2500, 1500))

    def test_counter_overlay_uses_directional_face_bounds_without_mirroring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            renderer = self._make_renderer(_synthetic_direction_manifest(Path(directory)))
            counts = SimpleNamespace(total=1, keyboard_presses=1, mouse_clicks=0)
            renderer._input_counter = SimpleNamespace(snapshot=lambda: counts)
            renderer._last_observed_counter_total = counts.total
            renderer._cursor_over_robot_face = Mock(return_value=False)
            for state in ("walk", "climb"):
                for direction in (1, -1):
                    with self.subTest(state=state, direction=direction):
                        renderer._state = state
                        renderer._frame_index = 0
                        renderer._direction = direction
                        frames, _bounds, faces = renderer._selected_frame_caches()
                        expected = frames[state][0].copy()
                        face_rect = faces[state][0]
                        self.assertIsNotNone(face_rect)
                        draw_counter_overlay(expected, face_rect, 1)
                        with patch(
                            "desktop_pet_adapter.sprite_renderer.time.monotonic",
                            return_value=100.0,
                        ):
                            renderer._last_counter_input_at = 100.0
                            renderer._draw_frame()
                        self.assertEqual(
                            self.pygame.image.tostring(renderer._surface, "RGBA"),
                            self.pygame.image.tostring(expected, "RGBA"),
                        )
                        renderer._cursor_over_robot_face.assert_called_with(face_rect)
                        renderer._cursor_over_robot_face.reset_mock()

    def test_hover_reveals_persisted_counter_details_without_startup_burst(self) -> None:
        renderer = self.renderer
        renderer._state = "idle"
        renderer._ordinary_face_epoch = 90.0
        counts = SimpleNamespace(total=420, keyboard_presses=310, mouse_clicks=110)
        renderer._input_counter = SimpleNamespace(snapshot=lambda: counts)
        renderer._input_feedback = CounterFeedbackState()
        renderer._last_observed_counter_total = None
        renderer._last_counter_input_at = None
        renderer._cursor_over_robot_face = Mock(return_value=True)
        face_rect = renderer._face_bounds["idle"][0]

        with patch(
            "desktop_pet_adapter.sprite_renderer.time.monotonic",
            side_effect=(100.0, 101.0),
        ):
            renderer._draw_frame()
            hovered = self.pygame.image.tostring(renderer._surface, "RGBA")
            renderer._cursor_over_robot_face.return_value = False
            renderer._draw_frame()
            left = self.pygame.image.tostring(renderer._surface, "RGBA")

        expected_hover = renderer._frames["idle"][0].copy()
        draw_counter_overlay(
            expected_hover,
            face_rect,
            counts.total,
            keyboard_presses=counts.keyboard_presses,
            mouse_clicks=counts.mouse_clicks,
            hovered=True,
        )
        expected_left = self._expected_expression_frame(
            renderer._frames["idle"][0], face_rect, "idle", 11.0
        )
        self.assertEqual(hovered, self.pygame.image.tostring(expected_hover, "RGBA"))
        self.assertEqual(left, self.pygame.image.tostring(expected_left, "RGBA"))
        self.assertIsNone(renderer._last_counter_input_at)
        self.assertEqual(renderer._last_observed_counter_total, counts.total)
        self.assertEqual(renderer._input_feedback.last_total, counts.total)
        self.assertEqual(renderer._input_feedback.burst_count, 0)
        self.assertEqual(renderer._ordinary_face_epoch, 90.0)

    def test_hover_keeps_counter_visible_past_idle_deadline_then_leaves_expression(self) -> None:
        renderer = self.renderer
        renderer._state = "walk"
        renderer._direction = 1
        counts = SimpleNamespace(total=12, keyboard_presses=8, mouse_clicks=4)
        renderer._input_counter = SimpleNamespace(snapshot=lambda: counts)
        renderer._input_feedback = CounterFeedbackState()
        renderer._input_feedback.prime(counts.total)
        renderer._input_feedback.burst_count = 4
        renderer._input_feedback.burst_until = 100.3
        renderer._input_feedback.pulse_until = 100.15
        renderer._last_observed_counter_total = counts.total
        renderer._last_counter_input_at = 100.0
        renderer._cursor_over_robot_face = Mock(side_effect=(True, True, True, False))
        face_rect = renderer._face_bounds["walk"][0]

        with patch(
            "desktop_pet_adapter.sprite_renderer.time.monotonic",
            side_effect=(104.0, 105.0, 111.0, 111.1),
        ):
            rendered = []
            for _ in range(4):
                renderer._draw_frame()
                rendered.append(self.pygame.image.tostring(renderer._surface, "RGBA"))

        expected_hover = renderer._frames["walk"][0].copy()
        draw_counter_overlay(
            expected_hover,
            face_rect,
            counts.total,
            keyboard_presses=counts.keyboard_presses,
            mouse_clicks=counts.mouse_clicks,
            hovered=True,
        )
        expected_left = self._expected_expression_frame(
            renderer._frames["walk"][0], face_rect, "walk", 0.0
        )
        self.assertEqual(rendered[:3], [self.pygame.image.tostring(expected_hover, "RGBA")] * 3)
        self.assertEqual(rendered[3], self.pygame.image.tostring(expected_left, "RGBA"))
        self.assertEqual(counts.total, 12)
        self.assertEqual(renderer._last_observed_counter_total, 12)
        self.assertEqual(renderer._last_counter_input_at, 100.0)
        self.assertEqual(renderer._input_feedback.last_total, 12)
        self.assertEqual(renderer._input_feedback.burst_count, 4)
        self.assertEqual(renderer._input_feedback.burst_until, 100.3)
        self.assertEqual(renderer._input_feedback.pulse_until, 100.15)

    def test_hovered_count_delta_triggers_one_existing_pulse_and_burst(self) -> None:
        renderer = self.renderer
        renderer._state = "climb"
        counts = SimpleNamespace(total=20, keyboard_presses=15, mouse_clicks=5)
        renderer._input_counter = SimpleNamespace(snapshot=lambda: counts)
        renderer._input_feedback = CounterFeedbackState()
        renderer._input_feedback.prime(counts.total)
        renderer._last_observed_counter_total = counts.total
        renderer._last_counter_input_at = 100.0
        renderer._cursor_over_robot_face = Mock(return_value=True)
        counts.total = 21
        counts.keyboard_presses = 16
        face_rect = renderer._face_bounds["climb"][0]

        with patch(
            "desktop_pet_adapter.sprite_renderer.time.monotonic",
            side_effect=(105.0, 105.05),
        ):
            renderer._draw_frame()
            after_delta = self.pygame.image.tostring(renderer._surface, "RGBA")
            renderer._draw_frame()
            repeated = self.pygame.image.tostring(renderer._surface, "RGBA")

        expected = renderer._frames["climb"][0].copy()
        draw_counter_overlay(
            expected,
            face_rect,
            counts.total,
            keyboard_presses=counts.keyboard_presses,
            mouse_clicks=counts.mouse_clicks,
            hovered=True,
            pulse=True,
            burst_count=1,
        )
        expected_bytes = self.pygame.image.tostring(expected, "RGBA")
        self.assertEqual(after_delta, expected_bytes)
        self.assertEqual(repeated, expected_bytes)
        self.assertEqual(renderer._last_counter_input_at, 105.0)
        self.assertEqual(renderer._last_observed_counter_total, 21)
        self.assertEqual(renderer._input_feedback.last_total, 21)
        self.assertEqual(renderer._input_feedback.burst_count, 1)
        self.assertEqual(renderer._input_feedback.burst_until, 105.4)
        self.assertEqual(renderer._input_feedback.pulse_until, 105.15)

    def test_hover_uses_selected_walk_and_climb_face_for_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            renderer = self._make_renderer(_synthetic_direction_manifest(Path(directory)))
            counts = SimpleNamespace(total=7, keyboard_presses=5, mouse_clicks=2)
            renderer._input_counter = SimpleNamespace(snapshot=lambda: counts)
            renderer._input_feedback = CounterFeedbackState()
            renderer._input_feedback.prime(counts.total)
            renderer._last_observed_counter_total = counts.total
            renderer._last_counter_input_at = None
            renderer._cursor_over_robot_face = Mock(return_value=True)
            for state in ("walk", "climb"):
                for direction in (1, -1):
                    with self.subTest(state=state, direction=direction):
                        renderer._state = state
                        renderer._direction = direction
                        frames, _bounds, faces = renderer._selected_frame_caches()
                        base = frames[state][0]
                        face_rect = faces[state][0]
                        expected = base.copy()
                        draw_counter_overlay(
                            expected,
                            face_rect,
                            counts.total,
                            keyboard_presses=counts.keyboard_presses,
                            mouse_clicks=counts.mouse_clicks,
                            hovered=True,
                        )
                        with patch(
                            "desktop_pet_adapter.sprite_renderer.time.monotonic",
                            return_value=100.0,
                        ):
                            renderer._draw_frame()
                        self.assertEqual(
                            self.pygame.image.tostring(renderer._surface, "RGBA"),
                            self.pygame.image.tostring(expected, "RGBA"),
                        )
                        renderer._cursor_over_robot_face.assert_called_with(face_rect)
                        renderer._cursor_over_robot_face.reset_mock()

    def test_counter_overlay_idle_boundary_is_four_point_nine_nine_nine_visible(self) -> None:
        renderer = self.renderer
        renderer._state = "walk"
        renderer._direction = 1
        counts = SimpleNamespace(total=7, keyboard_presses=5, mouse_clicks=2)
        renderer._input_counter = SimpleNamespace(snapshot=lambda: counts)
        renderer._input_feedback = CounterFeedbackState()
        renderer._input_feedback.prime(counts.total)
        renderer._last_observed_counter_total = counts.total
        renderer._last_counter_input_at = 100.0
        base = renderer._frames["walk"][0]
        with patch(
            "desktop_pet_adapter.sprite_renderer.time.monotonic",
            side_effect=(104.999, 105.0),
        ):
            renderer._draw_frame()
            visible = self.pygame.image.tostring(renderer._surface, "RGBA")
            renderer._draw_frame()
            hidden = self.pygame.image.tostring(renderer._surface, "RGBA")
        normal = self._expected_expression_frame(
            base,
            renderer._face_bounds["walk"][0],
            "walk",
            0.0,
        )
        self.assertNotEqual(visible, self.pygame.image.tostring(base, "RGBA"))
        self.assertEqual(hidden, self.pygame.image.tostring(normal, "RGBA"))
        self.assertEqual(renderer._ordinary_face_epoch, 105.0)
        self.assertEqual(renderer._input_feedback.last_total, counts.total)

    def test_global_mouse_movement_does_not_wake_or_reset_counter(self) -> None:
        renderer = self.renderer
        renderer._state = "walk"
        renderer._direction = 1
        counts = SimpleNamespace(total=12, keyboard_presses=8, mouse_clicks=4)
        renderer._input_counter = SimpleNamespace(snapshot=lambda: counts)
        renderer._input_feedback = CounterFeedbackState()
        renderer._input_feedback.prime(counts.total)
        renderer._last_observed_counter_total = counts.total
        renderer._last_counter_input_at = 100.0
        renderer._ordinary_face_epoch = 90.5
        base = self.pygame.image.tostring(renderer._frames["walk"][0], "RGBA")
        with patch(
            "desktop_pet_adapter.desktop_interactions._system_idle_seconds",
            return_value=0.0,
        ) as global_idle, patch(
            "desktop_pet_adapter.sprite_renderer.time.monotonic",
            side_effect=(105.0, 105.1),
        ):
            renderer._draw_frame()
            hidden = self.pygame.image.tostring(renderer._surface, "RGBA")
            renderer._draw_frame()
            still_hidden = self.pygame.image.tostring(renderer._surface, "RGBA")
        normal = self._expected_expression_frame(
            renderer._frames["walk"][0],
            renderer._face_bounds["walk"][0],
            "walk",
            14.5,
        )
        normal_bytes = self.pygame.image.tostring(normal, "RGBA")
        self.assertEqual(hidden, normal_bytes)
        self.assertEqual(still_hidden, normal_bytes)
        self.assertEqual(counts.total, 12)
        self.assertEqual(renderer._last_observed_counter_total, 12)
        self.assertEqual(renderer._last_counter_input_at, 100.0)
        global_idle.assert_not_called()

    def test_feedback_observes_new_counts_while_overlay_is_hidden(self) -> None:
        renderer = self.renderer
        renderer._state = "walk"
        renderer._direction = 1
        counts = SimpleNamespace(total=20, keyboard_presses=15, mouse_clicks=5)
        renderer._input_counter = SimpleNamespace(snapshot=lambda: counts)
        renderer._input_feedback = CounterFeedbackState()
        renderer._input_feedback.prime(counts.total)
        renderer._last_observed_counter_total = counts.total
        renderer._last_counter_input_at = 100.0
        renderer._ordinary_face_epoch = 90.5
        renderer._input_feedback.last_total = 19
        with patch(
            "desktop_pet_adapter.sprite_renderer.time.monotonic",
            return_value=105.0,
        ):
            renderer._draw_frame()
        self.assertEqual(renderer._input_feedback.last_total, 20)
        self.assertEqual(renderer._input_feedback.burst_count, 1)
        normal = self._expected_expression_frame(
            renderer._frames["walk"][0],
            renderer._face_bounds["walk"][0],
            "walk",
            14.5,
        )
        self.assertEqual(self.pygame.image.tostring(renderer._surface, "RGBA"),
                         self.pygame.image.tostring(normal, "RGBA"))

    def test_ordinary_expression_draws_without_input_counter_in_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            renderer = self._make_renderer(_synthetic_direction_manifest(Path(directory)))
            renderer._state = "walk"
            renderer._direction = -1
            renderer._ordinary_face_epoch = 100.0
            frame = renderer._left_frames["walk"][0]
            face_rect = renderer._left_face_bounds["walk"][0]
            expected = self._expected_expression_frame(frame, face_rect, "walk", 2.4)
            with patch(
                "desktop_pet_adapter.sprite_renderer.time.monotonic",
                return_value=102.4,
            ):
                renderer._draw_frame()
            self.assertIsNone(renderer._input_counter)
            self.assertEqual(
                self.pygame.image.tostring(renderer._surface, "RGBA"),
                self.pygame.image.tostring(expected, "RGBA"),
            )
            self.assertEqual(renderer._ordinary_face_epoch, 100.0)

    def test_count_delta_uses_recent_monotonic_window_without_startup_burst(self) -> None:
        renderer = self.renderer
        renderer._state = "walk"
        renderer._direction = 1
        counts = SimpleNamespace(total=40, keyboard_presses=32, mouse_clicks=8)
        renderer._input_counter = SimpleNamespace(snapshot=lambda: counts)
        renderer._input_feedback = CounterFeedbackState()
        renderer._last_observed_counter_total = None
        base = renderer._frames["walk"][0]
        with patch(
            "desktop_pet_adapter.sprite_renderer.time.monotonic",
            side_effect=(100.0, 100.1, 105.1),
        ):
            renderer._draw_frame()
            startup = self.pygame.image.tostring(renderer._surface, "RGBA")
            startup_normal = self._expected_expression_frame(
                base, renderer._face_bounds["walk"][0], "walk", 0.0
            )
            self.assertIsNone(renderer._last_counter_input_at)
            self.assertEqual(renderer._input_feedback.burst_count, 0)
            counts.total = 41
            counts.keyboard_presses = 33
            renderer._draw_frame()
            recent = self.pygame.image.tostring(renderer._surface, "RGBA")
            self.assertEqual(renderer._last_counter_input_at, 100.1)
            renderer._draw_frame()
            expired = self.pygame.image.tostring(renderer._surface, "RGBA")
            expired_normal = self._expected_expression_frame(
                base, renderer._face_bounds["walk"][0], "walk", 0.0
            )
        self.assertEqual(startup, self.pygame.image.tostring(startup_normal, "RGBA"))
        self.assertNotEqual(recent, self.pygame.image.tostring(base, "RGBA"))
        self.assertEqual(expired, self.pygame.image.tostring(expired_normal, "RGBA"))
        self.assertEqual(renderer._input_feedback.burst_count, 1)

    def test_robot_hover_bounds_include_face_rim_and_allow_click_through(self) -> None:
        renderer = self.renderer
        renderer._hwnd = 123
        renderer._visible = True
        renderer._click_through = True
        renderer._position = (100.0, 200.0)
        win32api = SimpleNamespace(GetCursorPos=lambda: (117, 232))
        with patch.dict("sys.modules", {"win32api": win32api}):
            self.assertTrue(renderer._cursor_over_robot_face((20, 30, 40, 50)))
        win32api.GetCursorPos = lambda: (145, 255)
        with patch.dict("sys.modules", {"win32api": win32api}):
            self.assertFalse(renderer._cursor_over_robot_face((20, 30, 40, 50)))

    def test_custom_manifest_without_face_metadata_keeps_original_expression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            _copy_builtin_sprite_assets(folder)
            payload = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
            payload.pop("left_atlas", None)
            payload.pop("left_states", None)
            for state in payload["states"].values():
                state.pop("face_rects", None)
                state.pop("robot_rects", None)
            manifest_path = folder / "manifest.json"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            renderer = SpriteRenderer(
                _context(_config()),
                SpriteAtlasManifest.load(manifest_path),
                enable_desktop_controller=False,
            )
            renderer._pygame = self.pygame
            renderer._surface = self.pygame.Surface(
                (renderer.width, renderer.height),
                self.pygame.SRCALPHA,
                32,
            )
            renderer._load_frames()
            renderer._input_counter = InputCounter(None, Mock())
            renderer._state = "idle"
            renderer._frame_index = 0
            renderer._draw_frame()
            self.assertEqual(renderer._face_bounds["idle"], [None] * 4)
            self.assertEqual(
                self.pygame.image.tostring(renderer._surface, "RGBA"),
                self.pygame.image.tostring(renderer._frames["idle"][0], "RGBA"),
            )
            renderer._state = "walk"
            renderer._direction = -1
            with patch.object(
                self.pygame.transform,
                "flip",
                side_effect=AssertionError("legacy art must stay unmirrored"),
            ):
                renderer._draw_frame()
            self.assertEqual(renderer._left_frames, {})
            self.assertEqual(
                self.pygame.image.tostring(renderer._surface, "RGBA"),
                self.pygame.image.tostring(renderer._frames["walk"][0], "RGBA"),
            )

    def tearDown(self):
        if self.pygame is not None:
            self.pygame.display.quit()

    def test_frames_advance_and_left_walking_uses_independent_art(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = _synthetic_direction_manifest(Path(directory))
            renderer = self._make_renderer(manifest_path)
            renderer._state = "walk"
            renderer._frame_elapsed = 0
            seen = []
            for _ in range(4):
                renderer._advance_animation(0.125)
                seen.append(renderer._frame_index)
            self.assertEqual(seen, [1, 0, 1, 0])

            expected = renderer._left_frames["walk"][0]
            right = renderer._frames["walk"][0]
            self.assertNotEqual(
                self.pygame.image.tostring(right, "RGBA"),
                self.pygame.image.tostring(expected, "RGBA"),
            )
            renderer._state = "walk"
            renderer._frame_index = 0
            renderer._direction = -1
            face_rect = renderer._left_face_bounds["walk"][0]
            expected = self._expected_expression_frame(expected, face_rect, "walk", 0.0)
            renderer._ordinary_face_epoch = 100.0
            with patch.object(
                self.pygame.transform,
                "flip",
                side_effect=AssertionError("directional sprite drawing must not transform.flip"),
            ), patch(
                "desktop_pet_adapter.sprite_renderer.time.monotonic",
                return_value=100.0,
            ):
                renderer._draw_frame()
            self.assertEqual(
                self.pygame.image.tostring(renderer._surface, "RGBA"),
                self.pygame.image.tostring(expected, "RGBA"),
            )
            red_pixels = [
                (x, y)
                for x in range(renderer.width)
                for y in range(renderer.height)
                if (lambda pixel: pixel.r > 220 and pixel.g < 30 and pixel.b < 40 and pixel.a > 200)(
                    renderer._surface.get_at((x, y))
                )
            ]
            self.assertTrue(red_pixels)
            self.assertGreater(min(x for x, _y in red_pixels), renderer.width // 2)
            right_feet = {bounds[3] for bounds in renderer._frame_bounds["walk"]}
            left_feet = {bounds[3] for bounds in renderer._left_frame_bounds["walk"]}
            self.assertEqual(right_feet, left_feet)
            renderer._direction = 1
            with patch.object(renderer, "_refresh_work_area"):
                right_anchor = renderer._desktop_frame().geometry.foot_anchor
                renderer._direction = -1
                left_anchor = renderer._desktop_frame().geometry.foot_anchor
            self.assertEqual(right_anchor, left_anchor)

    def test_scaled_visible_bounds_stay_inside_sprite_viewport(self) -> None:
        for bounds in self.renderer._frame_bounds.values():
            for left, top, right, bottom in bounds:
                self.assertGreaterEqual(left, 0)
                self.assertGreaterEqual(top, 0)
                self.assertLessEqual(right, self.renderer.width)
                self.assertLessEqual(bottom, self.renderer.height)
        idle_feet = {bounds[3] for bounds in self.renderer._frame_bounds["idle"]}
        walk_feet = {bounds[3] for bounds in self.renderer._frame_bounds["walk"]}
        self.assertEqual(idle_feet, {round(self.renderer.height * 0.9)})
        self.assertEqual(walk_feet, {round(self.renderer.height * 0.9)})

    def test_controller_anchor_sync_uses_shared_chat_signature_when_not_following(self) -> None:
        renderer = self.renderer
        renderer._position = (21.5, 33.25)
        renderer._work_area = (0, 0, 1920, 1040)
        renderer._visible = True
        renderer._always_on_top = True
        renderer._click_through = False

        class StrictChat:
            def __init__(self):
                self.anchor = None
                self.output_speaking = None

            def sync_anchor(
                self,
                pet_rect,
                work_area,
                *,
                visible,
                topmost,
                click_through,
                hovered=False,
                model_scale=1.0,
            ):
                self.anchor = (pet_rect, work_area, visible, topmost, click_through, hovered, model_scale)

            def set_output_speaking(self, speaking):
                self.output_speaking = speaking

        chat = StrictChat()
        renderer._sync_chat_anchor(chat, False)
        self.assertEqual(chat.anchor[0], (22, 33, 22 + renderer.width, 33 + renderer.height))
        self.assertTrue(chat.anchor[2])
        self.assertFalse(chat.anchor[4])
        self.assertFalse(chat.output_speaking)

    def test_local_chat_and_runtime_pcm_streams_reach_player_and_bad_events_do_not_crash(self) -> None:
        class FakePCMPlayer:
            ready = True

            def __init__(self):
                self.events = []
                self.active = False
                self.pumps = 0

            def handle_event(self, event):
                self.events.append(event)
                if event.get("event") == "start":
                    self.active = True
                elif event.get("event") in {"end", "abort"}:
                    self.active = False

            def pump(self):
                self.pumps += 1

            def stop(self):
                self.active = False

            def close(self):
                self.active = False

        player = FakePCMPlayer()
        self.renderer._pcm_player = player
        self.renderer.pcm_audio_ready = True
        local_event = {"event": "start", "stream_id": "local-chat", "seq": 0}
        runtime_event = {"event": "chunk", "stream_id": "runtime", "seq": 1, "pcm": b"\0\0"}
        self.renderer.command_queue.put(("voice_stream", local_event))
        self.renderer._process_commands()
        self.renderer.context.voice_stream_queue.put(runtime_event)
        self.renderer._pump_voice_streams()
        self.assertEqual(player.events, [local_event, runtime_event])
        self.assertGreaterEqual(player.pumps, 1)
        self.assertTrue(self.renderer._speaking)

        class MalformedPlayer(FakePCMPlayer):
            def handle_event(self, _event):
                raise BufferError("fixture stream exceeded bounds")

        self.renderer._pcm_player = MalformedPlayer()
        with patch.object(self.renderer.logger, "warning") as warning:
            self.renderer._handle_voice_stream(runtime_event)
        warning.assert_called_once()

    def test_roaming_stops_for_interaction_movement(self) -> None:
        class ActiveInteractions:
            interaction_movement_active = True

        self.renderer._work_area = (0, 0, 1600, 900)
        self.renderer._position = (100.0, 700.0)
        self.renderer._desktop_controller = ActiveInteractions()
        self.renderer._roaming = SpriteRoamingPolicy(
            speed_px_per_second=44,
            idle_pause_min_seconds=0,
            idle_pause_max_seconds=0,
        )
        self.renderer._advance_roaming(0.1)
        self.assertEqual(self.renderer._position, (100.0, 700.0))

    def test_hidden_frames_skip_layered_upload_until_visible(self) -> None:
        native = SimpleNamespace(present=Mock())
        self.renderer._native_window = native
        self.renderer._visible = False
        self.renderer._present_frame()
        native.present.assert_not_called()
        self.renderer._visible = True
        self.renderer._present_frame()
        native.present.assert_called_once()

    def test_speech_hold_releases_after_interaction_and_nonreact_motion(self) -> None:
        class InteractionController:
            def __init__(self, status):
                self.interaction_movement_active = True
                self._movement_status = status

        class ActivePCMPlayer:
            def __init__(self):
                self.active = True

            def handle_event(self, event):
                if event.get("event") == "abort":
                    self.active = False

        cases = (
            ("approaching", "climb", "speaking"),
            ("climbing", "walk", "stop_audio"),
            ("approaching", "walk", "abort"),
        )
        for status, commanded_state, stop_method in cases:
            with self.subTest(status=status, stop_method=stop_method):
                renderer = self.renderer
                controller = InteractionController(status)
                renderer._desktop_controller = controller
                renderer._position = (100.0, 600.0)
                renderer._work_area = (0, 0, 1200, 800)
                renderer._visible = True
                renderer._dragging = False
                renderer._state = "idle"
                renderer._direction = 1
                renderer._command_state_until = 0.0
                renderer._speech_reaction_active = False
                renderer._external_speaking = False
                renderer._speaking = False
                renderer._pcm_player = None
                renderer._roaming = SpriteRoamingPolicy(
                    speed_px_per_second=44,
                    idle_pause_min_seconds=0,
                    idle_pause_max_seconds=0,
                    margin=renderer.config.desktop_pet.margin,
                )
                renderer._last_position_before_update = renderer._position

                with patch("desktop_pet_adapter.sprite_renderer.time.monotonic", return_value=10.0):
                    renderer.command_queue.put(("speaking", True))
                    renderer._process_commands()
                    self.assertTrue(renderer._speech_reaction_active)
                    self.assertTrue(math.isfinite(renderer._command_state_until))

                    renderer._update_interaction_animation()
                    self.assertEqual(renderer._state, "walk" if status == "approaching" else "climb")

                    renderer.command_queue.put(("motion", commanded_state))
                    renderer._process_commands()
                    self.assertEqual(renderer._state, commanded_state)
                    self.assertTrue(math.isfinite(renderer._command_state_until))

                    if stop_method == "speaking":
                        renderer.command_queue.put(("speaking", False))
                        renderer._process_commands()
                    elif stop_method == "stop_audio":
                        renderer.command_queue.put(("stop_audio", None))
                        renderer._process_commands()
                    else:
                        renderer._pcm_player = ActivePCMPlayer()
                        renderer._external_speaking = False
                        renderer._sync_audio_speaking()
                        renderer._handle_voice_stream({"event": "abort"})

                    self.assertFalse(renderer._speaking)
                    self.assertFalse(renderer._speech_reaction_active)
                    self.assertTrue(math.isfinite(renderer._command_state_until))
                    held_position = renderer._position
                    with patch.object(
                        renderer,
                        "_get_work_area",
                        return_value=(0, 0, 1200, 800),
                    ):
                        renderer._advance_roaming(0.1)
                    self.assertEqual(renderer._position, held_position)

                    controller.interaction_movement_active = False
                    with patch("desktop_pet_adapter.sprite_renderer.time.monotonic", return_value=12.0), patch.object(
                        renderer, "_system_idle_seconds", return_value=100.0
                    ), patch.object(
                        renderer,
                        "_get_work_area",
                        return_value=(0, 0, 1200, 800),
                    ):
                        renderer._advance_roaming(0.1)
                    self.assertGreater(renderer._position[0], held_position[0])


class RuntimeAudioCapabilityTests(unittest.TestCase):
    def test_live2d_backend_without_model_info_accepts_remote_wav(self) -> None:
        from desktop_pet_adapter.live2d_backend import Live2DBackend

        config = _config()
        config = replace(
            config,
            renderer=replace(config.renderer, backend="live2d"),
        )
        runtime = AvatarRuntime(config, Mock())
        runtime.backend = Live2DBackend(
            config,
            Mock(),
            runtime_factory=Mock,
            runtime_options=None,
            runtime_frame_factory=lambda **values: SimpleNamespace(**values),
            model_adapter_factory=Mock(),
        )
        runtime.renderer = SimpleNamespace(pcm_audio_ready=True)
        runtime._started = True
        self.assertEqual(runtime.backend.capabilities(runtime.renderer), {})

        wav_bytes = b"RIFF\x04\x00\x00\x00WAVE"
        audio_base64 = base64.b64encode(wav_bytes).decode("ascii")
        commands = (
            AvatarCommand(
                AvatarEvent.PLAY_AUDIO,
                {"format": "wav", "audio_base64": audio_base64},
            ),
            AvatarCommand(
                AvatarEvent.QUEUE_AUDIO,
                {"format": "wav", "audio_base64": audio_base64, "reset": True},
            ),
        )
        for command, expected_kind in zip(commands, ("play_audio", "queue_audio")):
            with self.subTest(event=command.event.value):
                self.assertIsNone(asyncio.run(runtime.dispatch(command)))
                queued_kind, value = runtime.command_queue.get_nowait()
                self.assertEqual(queued_kind, expected_kind)
                if expected_kind == "play_audio":
                    self.assertEqual(value, wav_bytes)
                else:
                    self.assertEqual(value, {"audio": wav_bytes, "reset": True})

    def test_sprite_explicit_false_capability_rejects_remote_wav(self) -> None:
        config = _config()
        runtime = AvatarRuntime(config, Mock())
        runtime.backend = SpriteBackend(config, Mock())
        runtime.renderer = SimpleNamespace(
            audio_ready=False,
            _wav_channel=None,
            pcm_audio_ready=False,
        )
        runtime._started = True
        audio_base64 = base64.b64encode(b"RIFF\x04\x00\x00\x00WAVE").decode("ascii")
        for event in (AvatarEvent.PLAY_AUDIO, AvatarEvent.QUEUE_AUDIO):
            with self.subTest(event=event.value):
                command = AvatarCommand(
                    event,
                    {"format": "wav", "audio_base64": audio_base64},
                )
                with self.assertRaisesRegex(ProtocolError, "does not support WAV audio playback"):
                    asyncio.run(runtime.dispatch(command))
        self.assertTrue(runtime.command_queue.empty())


class _MemoryWorker:
    generation = 0
    alive = False

    def start(self):
        return None

    def invalidate(self):
        self.generation += 1
        return self.generation

    def submit(self, *_args, **_kwargs):
        return "job"

    def poll(self, **_kwargs):
        return []

    def stop(self, **_kwargs):
        return None


class DisplacementCapabilityTests(unittest.TestCase):
    def test_live2d_session_never_plans_pet_movement_from_flags_or_commands(self) -> None:
        config = DesktopInteractionConfig(
            enabled=True,
            idle_interval_seconds=1,
            scene_interval_seconds=0.5,
            window_approach=True,
            icon_approach=True,
            follow_foreground_window=True,
            move_windows=False,
            move_icons=False,
            gui_inspection=False,
            gui_actions=False,
        )
        clock = lambda: 100.0
        session = DesktopInteractionSession(
            config,
            own_hwnd=None,
            own_pid=1,
            logger=Mock(),
            worker=_MemoryWorker(),
            clock=clock,
            allow_pet_displacement=False,
        )
        target = SceneWindow(10, 20, "fixture.exe", "Fixture", (600, 500, 1200, 900), 0, True)
        session._scene = SceneSnapshot(
            captured_at=100.0,
            windows=(target,),
            icons=(),
            foreground_hwnd=target.hwnd,
            cursor=(0, 0),
            work_area=(0, 0, 1920, 1080),
            system_idle_seconds=100,
            fullscreen=False,
        )
        session._last_input_at = 0
        session._next_decision_at = 0
        with patch("desktop_pet_adapter.desktop_interactions._system_idle_seconds", return_value=100):
            update = session.update(
                position=(100.0, 100.0),
                viewport=(176, 176),
                work_area=(0, 0, 1920, 1080),
                delta_time=0.1,
                visible=True,
                pet_dragging=False,
            )
        self.assertIsNone(session._plan)
        self.assertEqual(update.position, (100.0, 100.0))
        self.assertIsNone(update.presentation_intent)
        self.assertTrue(session.handle_command("desktop_toggle_window_approach"))
        self.assertTrue(session.window_approach)
        self.assertFalse(session.get_state("window_approach"))
        self.assertTrue(session.get_state("interaction_enabled"))

    def test_live2d_controller_gates_returned_positions_but_keeps_native_ui_actions(self) -> None:
        events = []

        class MovementSession:
            allow_pet_displacement = True
            movement_active = True

            def update(self, **_kwargs):
                return SimpleNamespace(
                    position=(999.0, 999.0),
                    presentation_intent="approach_window",
                    status="approaching",
                )

            def get_state(self, name):
                return name == "interaction_enabled"

            def handle_command(self, command, value=None):
                events.append(f"session:{command}")
                return command == "desktop_toggle_window_approach"

            def consume_menu_changed(self):
                return False

            def shutdown(self):
                return None

        config = _config().desktop_pet
        frame = DesktopPetFrame((10.0, 20.0), MovementGeometry((176, 176)), (0, 0, 1000, 800), True, False)
        host = DesktopPetControllerHost(
            enqueue_command=lambda *_args: None,
            sample_frame=lambda: frame,
            move_window=lambda x, y: events.append(f"move:{x},{y}"),
            present_movement=lambda intent: events.append(f"present:{intent}") or True,
            start_motion=lambda *_args: True,
            read_window_flag=lambda _name: False,
            sync_chat_anchor=lambda *_args: None,
            open_parameters=lambda: None,
            set_parameter=lambda _value: None,
            reset_parameters=lambda _value: None,
            toggle_visibility=lambda: events.append("visibility"),
            toggle_click_through=lambda: None,
            toggle_topmost=lambda: None,
            reset_position=lambda: None,
            quit=lambda: None,
            report_click=lambda _button: None,
        )
        controller = DesktopPetController(
            config,
            Mock(),
            host,
            interactions=MovementSession(),
            autonomous_displacement=False,
        )
        controller.tick(0.1)
        self.assertEqual(events, [])
        self.assertEqual(controller.menu_flag("window_approach"), False)
        self.assertTrue(controller.menu_flag("interaction_enabled"))
        self.assertTrue(controller.handle_command("desktop_toggle_window_approach"))
        self.assertEqual(events, [])
        self.assertTrue(controller.handle_command("desktop_toggle_visibility"))
        self.assertEqual(events[-1], "visibility")
        self.assertEqual(events[-2], "session:desktop_toggle_visibility")


class LauncherBackendSwitchTests(unittest.TestCase):
    def test_existing_supervisor_switches_live2d_sprite_live2d(self) -> None:
        class PetProcess:
            next_pid = 1000

            def __init__(self):
                PetProcess.next_pid += 1
                self.pid = PetProcess.next_pid

            def poll(self):
                return None

        class Services:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        class FixtureStarter(SessionStarter):
            def __init__(self, adapter_dir):
                super().__init__(adapter_dir)
                self.backends = []
                self.sessions = []

            def start(self, config_path, mode, renderer_backend=None):
                config = load_config(
                    config_path,
                    runtime_mode_override=mode,
                    renderer_backend_override=renderer_backend,
                )
                services = Services()
                session = LaunchSession(
                    mode=mode,
                    config=config,
                    pet=PetProcess(),
                    services=services,
                    config_path=config_path,
                    pet_created=PetProcess.next_pid + 5000,
                )
                self.backends.append(config.renderer.backend)
                self.sessions.append(session)
                return session

        with tempfile.TemporaryDirectory() as directory:
            adapter_dir = Path(directory)
            config_path = adapter_dir / "config.toml"
            config_path.write_text(
                '[runtime]\nmode = "desktop_pet"\n'
                '[renderer]\nbackend = "live2d"\nmodel_path = "unused.model3.json"\n'
                '[sprite]\nsize_px = 176\n',
                encoding="utf-8",
            )
            supervisor = SessionSupervisor(adapter_dir)
            starter = FixtureStarter(adapter_dir)
            supervisor.starter = starter
            requests = supervisor.requests_dir
            requests.mkdir(parents=True)

            def send(request_id, backend):
                request_path = requests / f"{request_id}.request.json"
                request_path.write_text(
                    json.dumps({
                        "id": request_id,
                        "config": str(config_path),
                        "mode": "desktop_pet",
                        "renderer_backend": backend,
                    }),
                    encoding="utf-8",
                )
                with patch.object(supervisor, "_open_configured_log_windows", return_value=[]):
                    supervisor._handle_request(request_path)
                return json.loads((requests / f"{request_id}.result.json").read_text(encoding="utf-8"))

            first = send("live-one", "live2d")
            first_session = supervisor.session
            second = send("sprite", "sprite")
            self.assertTrue(first_session.services.stopped)
            third = send("live-two", "live2d")
            self.assertEqual(starter.backends, ["live2d", "sprite", "live2d"])
            self.assertEqual([first["status"], second["status"], third["status"]], ["started"] * 3)
            self.assertEqual(
                [first["renderer_backend"], second["renderer_backend"], third["renderer_backend"]],
                ["live2d", "sprite", "live2d"],
            )

    def test_default_client_sends_live2d_override_to_sprite_environment_supervisor(self) -> None:
        class PetProcess:
            pid = 4321

            def poll(self):
                return None

        class Services:
            def stop(self):
                return None

        class FixtureStarter(SessionStarter):
            def __init__(self, adapter_dir):
                super().__init__(adapter_dir)
                self.backends = []

            def start(self, config_path, mode, renderer_backend=None):
                config = load_config(
                    config_path,
                    runtime_mode_override=mode,
                    renderer_backend_override=renderer_backend,
                )
                self.backends.append(config.renderer.backend)
                return LaunchSession(mode, config, PetProcess(), Services(), config_path, pet_created=5432)

        with tempfile.TemporaryDirectory() as directory:
            adapter_dir = Path(directory)
            config_path = adapter_dir / "config.toml"
            config_path.write_text(
                '[runtime]\nmode = "desktop_pet"\n'
                '[renderer]\nbackend = "live2d"\nmodel_path = "unused.model3.json"\n'
                '[sprite]\nsize_px = 176\n',
                encoding="utf-8",
            )
            supervisor = SessionSupervisor(adapter_dir)
            starter = FixtureStarter(adapter_dir)
            supervisor.starter = starter

            def wait_for_result(request_id, requests_dir, **_kwargs):
                request_path = requests_dir / f"{request_id}.request.json"
                with patch.dict(os.environ, {"NACHOBOT_RENDERER_BACKEND": "sprite"}):
                    with patch.object(supervisor, "_open_configured_log_windows", return_value=[]):
                        supervisor._handle_request(request_path)
                return json.loads(
                    (requests_dir / f"{request_id}.result.json").read_text(encoding="utf-8")
                )

            with (
                patch.dict(os.environ, {"NACHOBOT_RENDERER_BACKEND": ""}),
                patch("desktop_pet_adapter.launcher._wait_for_result", side_effect=wait_for_result),
            ):
                result = launch(
                    config_path,
                    "desktop_pet",
                    adapter_dir=adapter_dir,
                )
            self.assertEqual(starter.backends, ["live2d"])
            self.assertEqual(result.identity.mode, "desktop_pet")


if __name__ == "__main__":
    unittest.main()
