from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from live2d_adapter.config import ConfigError, load_config
from live2d_adapter.launcher import (
    LaunchSession,
    LauncherError,
    OwnedServices,
    SessionStarter,
    SessionSupervisor,
    _log_paths,
)


class ComponentTests(unittest.TestCase):
    def test_switching_core_mode_replaces_the_old_pet_instead_of_reusing_it(self):
        import json

        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                supervisor = SessionSupervisor(self.adapter)
                old_config = replace(
                    self.config, components=replace(self.config.components, core=not enabled)
                )
                new_config = replace(
                    self.config, components=replace(self.config.components, core=enabled)
                )
                old_services = Mock()
                old = LaunchSession(
                    "desktop_pet", old_config, Mock(pid=11), old_services, self.path
                )
                old.pet.poll.return_value = None
                new = LaunchSession("desktop_pet", new_config, Mock(pid=22), Mock(), self.path)
                supervisor.session = old
                request = self.root / f"switch-{enabled}.json"
                request.write_text(
                    json.dumps(
                        {"id": f"switch-{enabled}", "config": str(self.path), "mode": "desktop_pet"}
                    )
                )
                with (
                    patch("live2d_adapter.launcher.load_config", return_value=new_config),
                    patch.object(supervisor.starter, "start", return_value=new) as start,
                    patch.object(supervisor, "_open_configured_log_windows", return_value=[]),
                ):
                    supervisor._handle_request(request)
                old_services.stop.assert_called_once()
                start.assert_called_once()
                self.assertIs(supervisor.session, new)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = self.root / "NachoBot-Live2D-Adapter"
        self.adapter.mkdir()
        self.path = self.adapter / "config.toml"
        self.base = '[renderer]\nmodel_path="avatar.model3.json"\n'
        self.path.write_text(self.base, encoding="utf-8")
        self.config = load_config(self.path)
        self.config = replace(
            self.config,
            desktop_pet=replace(
                self.config.desktop_pet,
                chat=replace(self.config.desktop_pet.chat, enabled=True, transport="core"),
            ),
        )
        self.starter = SessionStarter(self.adapter)

    def test_config_defaults_boolean_validation_and_removed_online_provider(self):
        self.assertTrue(self.config.components.core)
        self.assertFalse(self.config.components.tts)
        self.assertEqual(self.config.desktop_pet.chat.tts_provider, "multimodal")
        for extra in ('[components]\ncore="false"', '[desktop_pet.chat]\ntts_provider="neural"'):
            self.path.write_text(self.base + extra, encoding="utf-8")
            with self.subTest(extra=extra), self.assertRaises(ConfigError):
                load_config(self.path)
        self.path.write_text(self.base + "[components]\ncore=false\ntts=true", encoding="utf-8")
        config = load_config(self.path)
        self.assertFalse(config.components.core)
        self.assertFalse(config.components.tts)

    def test_shared_switches_override_legacy_and_core_off_is_a_real_standalone_mode(self):
        directory = self.root / "NachoBot-Multimodal-Adapter"
        directory.mkdir()
        startup = directory / "startup.toml"
        startup.write_text(
            "[components]\ncore=false\ntts=true\nperception=true\n", encoding="utf-8"
        )
        self.path.write_text(
            self.base + "[components]\ncore=true\n"
            "[desktop_pet]\nshow_core_console=true\n"
            '[desktop_pet.chat]\nenabled=true\ntransport="core"\nplay_audio=true\n'
            '[desktop_pet.perception]\nvision=true\nspeech=true\npush_to_talk_key="F8"',
            encoding="utf-8",
        )
        config = load_config(self.path, runtime_mode_override="desktop_pet")
        self.assertFalse(
            config.components.core or config.components.tts or config.components.perception
        )
        self.assertFalse(config.desktop_pet.chat.enabled)
        self.assertFalse(config.desktop_pet.chat.play_audio)
        self.assertFalse(
            config.desktop_pet.chat.perception.vision or config.desktop_pet.chat.perception.speech
        )
        self.assertFalse(config.desktop_pet.show_core_console)
        self.assertTrue(config.desktop_pet.enabled)
        from live2d_adapter.chat import DesktopPetChat

        with (
            patch("live2d_adapter.core_chat.CoreChatClient") as core,
            patch("live2d_adapter.chat.DesktopPetBackendClient") as bridge,
            patch("live2d_adapter.perception.DesktopPerception") as perception,
        ):
            chat = DesktopPetChat(config.desktop_pet.chat, "pet", lambda *args: None, Mock())
        core.assert_not_called()
        bridge.assert_not_called()
        perception.assert_not_called()
        self.assertIsNone(chat._perception)
        self.assertTrue(callable(chat.open_parameters))
        services = Mock()
        with patch("live2d_adapter.launcher.listener_pid") as listener:
            self.starter._start_chat_services(config, services)
        listener.assert_not_called()
        services.start.assert_not_called()
        # Selecting standalone must not rewrite the user's online settings.
        self.assertIn("enabled=true", self.path.read_text(encoding="utf-8"))
        startup.write_text("[components]\ncore=true\ntts=true\nperception=true\n", encoding="utf-8")
        online = load_config(self.path)
        self.assertTrue(online.desktop_pet.chat.enabled)
        self.assertTrue(online.components.tts and online.components.perception)
        self.assertTrue(online.desktop_pet.chat.perception.vision)

    def test_disabled_components_do_not_launch_or_require_environments(self):
        config = replace(self.config, components=replace(self.config.components, core=False))
        services = Mock()
        with (
            patch("live2d_adapter.launcher.listener_pid", return_value=None),
            patch("live2d_adapter.launcher._open_log_viewer") as viewer,
        ):
            self.starter._start_chat_services(config, services)
            self.assertEqual(
                SessionSupervisor(self.adapter)._open_configured_log_windows(config, "turn"), []
            )
        services.start.assert_not_called()
        services.start_background.assert_not_called()
        viewer.assert_not_called()

    def test_only_core_starts_even_with_legacy_tts_and_perception_start_flags(self):
        self.path.write_text(
            self.base + "[components]\ncore=true\ntts=true\nperception=true\n"
            '[desktop_pet.chat]\nenabled=true\ntransport="core"\n'
            "[desktop_pet.perception]\nvision=true\nspeech=true",
            encoding="utf-8",
        )
        config = load_config(self.path)
        # No Multimodal Adapter environment or model config exists here.
        python = self.root / "NachoBot" / ".venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        services = Mock()
        with (
            patch("live2d_adapter.launcher.listener_pid", return_value=None),
            patch("live2d_adapter.launcher.urllib.request.urlopen") as health,
        ):
            self.starter._start_chat_services(config, services)
        services.start.assert_called_once()
        self.assertEqual(services.start.call_args.args[0], "core")
        services.start_background.assert_not_called()
        health.assert_not_called()
        self.assertEqual(
            _log_paths(self.adapter, "LIVE2D"),
            [
                self.adapter / "logs" / "live2d.out.log",
                self.adapter / "logs" / "live2d.err.log",
            ],
        )
        self.assertNotIn(self.adapter / "logs" / "tts.err.log", _log_paths(self.adapter, "ALL"))

    def test_cleanup_reaches_remaining_owned_components_after_one_fails(self):
        bridge, core, pet = Mock(), Mock(), Mock()
        core.stop.side_effect = LauncherError("shutdown timeout")
        services = OwnedServices(self.adapter / "logs", processes=[bridge, core, pet])
        with self.assertRaises(LauncherError):
            services.stop()
        for process in (bridge, core, pet):
            process.stop.assert_called_once()
        self.assertEqual(services.processes, [])
