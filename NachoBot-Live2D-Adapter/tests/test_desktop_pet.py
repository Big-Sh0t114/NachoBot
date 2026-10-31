from __future__ import annotations

import json
import queue
import sys
import tempfile
import unittest
import weakref
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from live2d_adapter.config import ConfigError, DesktopChatConfig, DesktopPetConfig, load_config
from live2d_adapter.desktop_pet import (
    DesktopPetState,
    DesktopPetStateStore,
    clamp_window_position,
    initial_window_position,
)
from live2d_adapter.model_adapter import Live2DModelAdapter
from live2d_adapter.renderer import Live2DRenderer


class StubLogger:
    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: None


class DesktopPetStateTests(unittest.TestCase):
    def test_round_trips_desktop_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "state.json"
            store = DesktopPetStateStore(state_path)
            expected = DesktopPetState(
                x=120,
                y=240,
                scale=0.85,
                offset_x=0.1,
                offset_y=-0.2,
                always_on_top=False,
                click_through=True,
                visible=False,
            )

            store.save(expected)

            self.assertEqual(store.load(), expected)
            self.assertFalse(state_path.with_suffix(".json.tmp").exists())

    def test_corrupt_state_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "state.json"
            state_path.write_text("not-json", encoding="utf-8")

            self.assertEqual(DesktopPetStateStore(state_path).load(), DesktopPetState())

    def test_initial_positions_and_clamping(self) -> None:
        work_area = (0, 0, 1920, 1040)
        self.assertEqual(
            initial_window_position("bottom_right", 520, 760, work_area, 24),
            (1376, 256),
        )
        self.assertEqual(
            initial_window_position("bottom_left", 520, 760, work_area, 24),
            (24, 256),
        )
        self.assertEqual(
            clamp_window_position(5000, -5000, 520, 760, work_area, 24),
            (1376, 24),
        )


class DesktopPetRendererStateTests(unittest.TestCase):
    def test_renderer_restores_saved_visibility_and_scale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            model_path = root / "avatar.model3.json"
            model_path.write_text(json.dumps({"Version": 3}), encoding="utf-8")
            state_path = root / "state.json"
            DesktopPetStateStore(state_path).save(
                DesktopPetState(scale=0.85, always_on_top=False, visible=False)
            )

            renderer = Live2DRenderer(
                str(model_path),
                StubLogger(),
                queue.Queue(),
                model_adapter=Live2DModelAdapter.from_model_path(model_path),
                desktop_pet_config=DesktopPetConfig(
                    enabled=True,
                    state_path=state_path,
                ),
            )

            self.assertFalse(renderer.window_visible)
            self.assertAlmostEqual(renderer.scale, 0.85)
            # Old saved/configured non-topmost choices no longer demote the pet.
            self.assertTrue(renderer.always_on_top)

    def test_fit_to_window_caps_zoom_before_model_can_be_clipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            model_path = root / "avatar.model3.json"
            model_path.write_text(json.dumps({"Version": 3}), encoding="utf-8")

            renderer = Live2DRenderer(
                str(model_path),
                StubLogger(),
                queue.Queue(),
                scale=1.8,
                model_adapter=Live2DModelAdapter.from_model_path(model_path),
                desktop_pet_config=DesktopPetConfig(
                    enabled=True,
                    min_scale=0.45,
                    max_scale=1.8,
                    fit_to_window=True,
                ),
            )

            self.assertEqual(renderer.scale, 1.0)
            renderer.offset_x = 0.5
            renderer.offset_y = -0.5
            renderer._constrain_model_offset()
            self.assertEqual((renderer.offset_x, renderer.offset_y), (0.0, 0.0))

    def test_fit_to_window_can_be_disabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            model_path = root / "avatar.model3.json"
            model_path.write_text(json.dumps({"Version": 3}), encoding="utf-8")

            renderer = Live2DRenderer(
                str(model_path),
                StubLogger(),
                queue.Queue(),
                scale=1.8,
                model_adapter=Live2DModelAdapter.from_model_path(model_path),
                desktop_pet_config=DesktopPetConfig(
                    enabled=True,
                    min_scale=0.45,
                    max_scale=1.8,
                    fit_to_window=False,
                ),
            )

            self.assertEqual(renderer.scale, 1.8)


class DesktopPetRendererInteractionTests(unittest.TestCase):
    def test_double_click_opens_chat_after_click_motion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            model_path = root / "avatar.model3.json"
            model_path.write_text(json.dumps({"Version": 3}), encoding="utf-8")
            renderer = Live2DRenderer(
                str(model_path),
                StubLogger(),
                queue.Queue(),
                model_adapter=Live2DModelAdapter.from_model_path(model_path),
                desktop_pet_config=DesktopPetConfig(
                    enabled=True,
                    chat=DesktopChatConfig(enabled=True),
                ),
            )

            class StubChat:
                def __init__(self) -> None:
                    self.open_count = 0

                def open(self, *, from_pet: bool = False) -> None:
                    self.from_pet = from_pet
                    self.open_count += 1

            chat = StubChat()
            renderer._chat = chat  # type: ignore[assignment]

            # Each click reads the monotonic clock once for interaction state
            # and once when scheduling the next idle motion.
            with patch(
                "live2d_adapter.renderer.time.monotonic",
                side_effect=[100.0, 100.0, 100.2, 100.2],
            ):
                renderer._handle_pet_click(1)
                renderer._handle_pet_click(1)

            self.assertEqual(chat.open_count, 1)
            self.assertTrue(chat.from_pet)

    def test_pet_hover_gate_rejects_hidden_and_clickthrough_pet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_path = root / "avatar.model3.json"
            model_path.write_text(json.dumps({"Version": 3}), encoding="utf-8")
            renderer = Live2DRenderer(
                str(model_path),
                StubLogger(),
                queue.Queue(),
                model_adapter=Live2DModelAdapter.from_model_path(model_path),
                desktop_pet_config=DesktopPetConfig(enabled=True),
            )

            renderer.hwnd = 123
            renderer.window_visible = False
            self.assertFalse(renderer._cursor_over_pet_window())

            renderer.window_visible = True
            renderer.click_through = True
            self.assertFalse(renderer._cursor_over_pet_window())


class DesktopParameterRendererIntegrationTests(unittest.TestCase):
    def _renderer(self, root: Path) -> Live2DRenderer:
        model_path = root / "avatar.model3.json"
        model_path.write_text(json.dumps({"Version": 3}), encoding="utf-8")
        return Live2DRenderer(
            str(model_path),
            StubLogger(),
            queue.Queue(),
            model_adapter=Live2DModelAdapter.from_model_path(model_path),
            desktop_pet_config=DesktopPetConfig(enabled=True),
        )

    def test_manual_parameter_override_runs_after_update_before_draw_and_reset_releases_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            renderer = self._renderer(Path(directory))
            events: list[tuple[str, object]] = []
            parameter = SimpleNamespace(
                id="ParamAngleX",
                min=-30.0,
                max=30.0,
                default=0.0,
                value=0.0,
            )

            class NativeModel:
                def SetParameterValueById(self, parameter_id: str, value: float) -> None:
                    events.append(("set", (parameter_id, value)))
                    parameter.value = value

            class StubModel:
                _model = NativeModel()

                def GetParameterCount(self) -> int:
                    return 1

                def GetParameter(self, _index: int):
                    return parameter

                def GetParameterValue(self, _index: int) -> float:
                    return parameter.value

                def Update(self) -> None:
                    events.append(("update", None))

                def Draw(self) -> None:
                    events.append(("draw", None))

            model = StubModel()
            renderer.model = model
            renderer._parameter_controller.bind_model(model, renderer.model_adapter)
            renderer._smooth_physics_outputs = lambda: events.append(("smooth", None))  # type: ignore[method-assign]

            renderer._handle_command(
                "desktop_parameter_set",
                {"id": "ParamAngleX", "value": 12.5},
            )
            renderer._update_and_draw_model()

            self.assertEqual(
                events,
                [
                    ("update", None),
                    ("smooth", None),
                    ("set", ("ParamAngleX", 12.5)),
                    ("draw", None),
                ],
            )
            self.assertTrue(renderer._parameter_controller.snapshot()[0]["manual"])

            events.clear()
            renderer._handle_command("desktop_parameters_reset", None)
            renderer._update_and_draw_model()

            self.assertEqual(
                events,
                [
                    ("update", None),
                    ("smooth", None),
                    ("set", ("ParamAngleX", 0.0)),
                    ("draw", None),
                ],
            )
            self.assertFalse(renderer._parameter_controller.snapshot()[0]["manual"])

    def test_parameter_panel_open_command_is_routed_to_chat_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            renderer = self._renderer(Path(directory))

            class StubChat:
                def __init__(self) -> None:
                    self.opened = 0

                def open_parameters(self) -> None:
                    self.opened += 1

            chat = StubChat()
            renderer._chat = chat  # type: ignore[assignment]
            renderer._handle_command("desktop_open_parameters", None)

            self.assertEqual(chat.opened, 1)

    def test_disabled_chat_starts_hover_host_for_parameter_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            renderer = self._renderer(Path(directory))

            self.assertFalse(renderer.desktop_pet.chat.enabled)
            self.assertIsNotNone(renderer._chat)
            self.assertIsNone(renderer._chat._thread)

            # The desktop host owns the hover toolbar even when its composer is
            # disabled, so renderer startup must still start the hidden Tk host.
            with patch.object(renderer._chat, "start") as start_host:
                renderer._start_desktop_chat_host()
                start_host.assert_called_once_with()
            self.assertIsNone(renderer._chat._thread)

            renderer._handle_command("desktop_open_chat", None)
            self.assertIsNone(renderer._chat._thread)

    def test_disabled_chat_still_receives_pet_hover_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            renderer = self._renderer(Path(directory))
            self.assertFalse(renderer.desktop_pet.chat.enabled)
            renderer.hwnd = 123
            renderer.window_visible = True
            renderer.click_through = False
            renderer.always_on_top = False
            renderer._get_work_area = lambda: (0, 0, 1920, 1080)  # type: ignore[method-assign]
            renderer._cursor_over_pet_window = lambda: True  # type: ignore[method-assign]
            calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

            class StubChat:
                def sync_anchor(self, *args: object, **kwargs: object) -> None:
                    calls.append((args, kwargs))

            renderer._chat = StubChat()  # type: ignore[assignment]
            win32gui = SimpleNamespace(
                GetWindowRect=lambda _hwnd: (500, 100, 720, 500)
            )
            with patch.dict(sys.modules, {"win32gui": win32gui}):
                renderer._sync_chat_window(force=True)

            self.assertEqual(len(calls), 1)
            args, kwargs = calls[0]
            self.assertEqual(args[0], (500, 100, 720, 500))
            self.assertEqual(args[1], (0, 0, 1920, 1080))
            self.assertEqual(
                kwargs,
                {
                    "visible": True,
                    "topmost": False,
                    "click_through": False,
                    "hovered": True,
                    "model_scale": 1.0,
                },
            )

    def test_model_controller_unbinds_before_native_framework_disposal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            renderer = self._renderer(Path(directory))
            events: list[tuple[str, object]] = []
            parameter = SimpleNamespace(
                id="ParamAngleX",
                min=-30.0,
                max=30.0,
                default=0.0,
                value=0.0,
            )

            class Model:
                def GetParameterCount(self) -> int:
                    return 1

                def GetParameter(self, _index: int):
                    return parameter

                def DestroyRenderer(self) -> None:
                    events.append(("destroy_renderer", None))

                def __del__(self) -> None:
                    events.append(("model_finalized", None))

            class Live2D:
                def glRelease(self) -> None:
                    events.append(("gl_release", model_ref() is None))

                def dispose(self) -> None:
                    events.append(("dispose", model_ref() is None))

            model = Model()
            model_ref = weakref.ref(model)
            renderer.model = model
            renderer._parameter_controller.bind_model(model)
            renderer._parameter_controller.set_value("ParamAngleX", 12.0)
            del model
            self.assertIsNotNone(model_ref())
            renderer.live2d = Live2D()

            controller = renderer._parameter_controller
            original_unbind = controller.unbind_model

            def record_unbind() -> None:
                events.append(("unbind", model_ref() is not None))
                original_unbind()

            controller.unbind_model = record_unbind  # type: ignore[method-assign]
            with patch(
                "live2d_adapter.renderer.gc.collect",
                side_effect=lambda: events.append(("gc_collect", model_ref() is None)),
            ):
                renderer._dispose_live2d()

            self.assertIsNone(renderer.model)
            self.assertIsNone(model_ref())
            self.assertEqual(
                events,
                [
                    ("unbind", True),
                    ("destroy_renderer", None),
                    ("model_finalized", None),
                    ("gc_collect", True),
                    ("gl_release", True),
                    ("dispose", True),
                ],
            )
            self.assertEqual(renderer._parameter_controller.snapshot(), [])


class DesktopPetConfigTests(unittest.TestCase):
    @staticmethod
    def _write_config(root: Path, desktop_pet_lines: str) -> Path:
        model_path = root / "avatar.model3.json"
        model_path.write_text(json.dumps({"Version": 3}), encoding="utf-8")
        config_path = root / "config.toml"
        config_path.write_text(
            "\n".join(
                [
                    "[renderer]",
                    'model_path = "avatar.model3.json"',
                    "[desktop_pet]",
                    desktop_pet_lines,
                ]
            ),
            encoding="utf-8",
        )
        return config_path

    def test_loads_desktop_pet_settings_and_resolves_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = load_config(
                self._write_config(
                    root,
                    "\n".join(
                        [
                            "enabled = true",
                            'title = "Hiyori"',
                            'character_name = "小春"',
                            'chat_header = "小春 · NachoBot"',
                            'state_path = "state/pet.json"',
                            'fit_to_window = true',
                            'start_position = "center"',
                            'idle_motion_groups = ["Tap", "Flick"]',
                            "min_scale = 0.5",
                            "max_scale = 1.5",
                        ]
                    ),
                )
            )

            self.assertTrue(config.desktop_pet.enabled)
            self.assertEqual(config.desktop_pet.title, "Hiyori")
            self.assertEqual(config.desktop_pet.character_name, "小春")
            self.assertEqual(config.desktop_pet.chat_header, "小春 · NachoBot")
            self.assertTrue(config.desktop_pet.fit_to_window)
            self.assertEqual(config.desktop_pet.state_path, (root / "state/pet.json").resolve())
            self.assertEqual(config.desktop_pet.idle_motion_groups, ("Tap", "Flick"))

    def test_runtime_mode_can_be_overridden_without_editing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = self._write_config(root, 'title = "Hiyori"')

            config = load_config(config_path, runtime_mode_override="desktop_pet")

            self.assertEqual(config.runtime.mode, "desktop_pet")
            self.assertTrue(config.desktop_pet.enabled)

    def test_loads_desktop_chat_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = load_config(
                self._write_config(
                    root,
                    "\n".join(
                        [
                            "enabled = true",
                            "[desktop_pet.chat]",
                            "enabled = true",
                            'backend_url = "http://127.0.0.1:9999/"',
                            "follow_pet = false",
                            "remember_position = true",
                            'window_state_path = "state/chat.json"',
                            "reply_timeout_seconds = 90",
                            'tts_language = "ja"',
                            "max_input_chars = 300",
                        ]
                    ),
                )
            )

            self.assertTrue(config.desktop_pet.chat.enabled)
            self.assertEqual(config.desktop_pet.chat.backend_url, "http://127.0.0.1:9999")
            self.assertFalse(config.desktop_pet.chat.follow_pet)
            self.assertTrue(config.desktop_pet.chat.remember_position)
            self.assertEqual(
                config.desktop_pet.chat.window_state_path,
                (root / "state/chat.json").resolve(),
            )
            self.assertEqual(config.desktop_pet.chat.reply_timeout_seconds, 90.0)
            self.assertEqual(config.desktop_pet.chat.tts_language, "ja")
            self.assertEqual(config.desktop_pet.chat.max_input_chars, 300)

    def test_rejects_invalid_scale_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = self._write_config(
                Path(temporary_directory),
                "min_scale = 2.0\nmax_scale = 1.0",
            )
            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_explicit_runtime_mode_controls_desktop_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = self._write_config(root, "enabled = true")
            original = config_path.read_text(encoding="utf-8")
            config_path.write_text(
                '[runtime]\nmode = "live"\n' + original,
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.runtime.mode, "live")
            self.assertFalse(config.desktop_pet.enabled)

    def test_rejects_unknown_runtime_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = self._write_config(root, "")
            original = config_path.read_text(encoding="utf-8")
            config_path.write_text(
                '[runtime]\nmode = "unknown"\n' + original,
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
