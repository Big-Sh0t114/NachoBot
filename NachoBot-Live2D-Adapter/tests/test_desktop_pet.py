from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from live2d_adapter.config import ConfigError, load_config
from live2d_adapter.desktop_pet import (
    DesktopPetState,
    DesktopPetStateStore,
    clamp_window_position,
    initial_window_position,
)


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
            (1896, -736),
        )


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
                            'state_path = "state/pet.json"',
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
            self.assertEqual(config.desktop_pet.state_path, (root / "state/pet.json").resolve())
            self.assertEqual(config.desktop_pet.idle_motion_groups, ("Tap", "Flick"))

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
                            "reply_timeout_seconds = 90",
                            'tts_language = "ja"',
                            "max_input_chars = 300",
                        ]
                    ),
                )
            )

            self.assertTrue(config.desktop_pet.chat.enabled)
            self.assertEqual(config.desktop_pet.chat.backend_url, "http://127.0.0.1:9999")
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
