from __future__ import annotations

import tempfile
import tomllib
import sys
import unittest
from pathlib import Path
from unittest import mock

import tomlkit
import yaml

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import setup_checks
import setup_deployment
import process_manager
from multimodal_runtime import MultimodalRuntimeManager


class SetupConfigCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "koishi-app").mkdir()
        (self.root / "NachoBot-DiscordVC-Adapter").mkdir()
        (self.root / "koishi-app/koishi_template.yml").write_text(
            "plugins:\n  adapter-discord:fixture:\n    token: <YOUR_DISCORD_BOT_TOKEN_HERE>\n",
            encoding="utf-8",
        )
        (self.root / "NachoBot-DiscordVC-Adapter/config.toml.example").write_text(
            '[discord]\ntoken = "YOUR_DISCORD_BOT_TOKEN"\n',
            encoding="utf-8",
        )
        self.old_values = (
            setup_deployment.ROOT_DIR,
            setup_deployment.TEMPLATE_MAP,
            setup_deployment.BACKUP_DIR,
        )
        setup_deployment.ROOT_DIR = self.root
        setup_deployment.TEMPLATE_MAP = {
            "koishi-app/koishi_template.yml": "koishi-app/koishi.yml",
            "NachoBot-DiscordVC-Adapter/config.toml.example": "NachoBot-DiscordVC-Adapter/config.toml",
            setup_deployment.BUILTIN_BILIBILI_TEMPLATE: (
                setup_deployment.BILIBILI_TARGET
            ),
        }
        setup_deployment.BACKUP_DIR = self.root / "backups"

    def tearDown(self) -> None:
        (
            setup_deployment.ROOT_DIR,
            setup_deployment.TEMPLATE_MAP,
            setup_deployment.BACKUP_DIR,
        ) = self.old_values
        self.temp_dir.cleanup()

    def test_discord_token_is_written_to_both_targets_safely(self) -> None:
        token = 'opaque-token:"\\\nnext'
        result = setup_deployment.ConfigInitializer.generate_configs(
            {"components": ["discord"], "discord": {"token": token}}
        )

        self.assertEqual(result["errors"], [])
        koishi = yaml.safe_load(
            (self.root / "koishi-app/koishi.yml").read_text(encoding="utf-8")
        )
        discord = tomlkit.parse(
            (self.root / "NachoBot-DiscordVC-Adapter/config.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(koishi["plugins"]["adapter-discord:fixture"]["token"], token)
        self.assertEqual(str(discord["discord"]["token"]), token)

    def test_missing_discord_token_fails_before_any_target_write(self) -> None:
        result = setup_deployment.ConfigInitializer.generate_configs(
            {"components": ["discord"], "discord": {"token": ""}}
        )

        self.assertTrue(result["errors"])
        self.assertFalse((self.root / "koishi-app/koishi.yml").exists())
        self.assertFalse(
            (self.root / "NachoBot-DiscordVC-Adapter/config.toml").exists()
        )

    def test_changed_placeholder_fails_closed_without_writing_token(self) -> None:
        koishi_template = self.root / "koishi-app/koishi_template.yml"
        koishi_template.write_text("plugins: {}\n", encoding="utf-8")
        existing = self.root / "koishi-app/koishi.yml"
        existing.write_text("plugins: {sentinel: true}\n", encoding="utf-8")

        result = setup_deployment.ConfigInitializer.generate_configs(
            {"components": ["discord"], "discord": {"token": "opaque-token"}}
        )

        self.assertTrue(result["errors"])
        self.assertEqual(
            existing.read_text(encoding="utf-8"), "plugins: {sentinel: true}\n"
        )

    def test_misplaced_koishi_placeholder_fails_closed(self) -> None:
        koishi_template = self.root / "koishi-app/koishi_template.yml"
        koishi_template.write_text(
            "plugins:\n"
            "  group:adapter:\n"
            "    adapter-discord:fixture:\n"
            "      token: already-not-placeholder\n"
            "    unrelated-plugin:fixture:\n"
            "      value: <YOUR_DISCORD_BOT_TOKEN_HERE>\n",
            encoding="utf-8",
        )
        existing = self.root / "koishi-app/koishi.yml"
        existing.write_text("plugins: {sentinel: true}\n", encoding="utf-8")

        result = setup_deployment.ConfigInitializer.generate_configs(
            {"components": ["discord"], "discord": {"token": "opaque-token"}}
        )

        self.assertTrue(result["errors"])
        self.assertEqual(
            existing.read_text(encoding="utf-8"), "plugins: {sentinel: true}\n"
        )

    def test_defaults_never_include_a_current_discord_token(self) -> None:
        defaults = setup_deployment.ConfigInitializer.get_defaults()
        self.assertEqual(defaults["discord"], {"token": ""})

    def test_selected_bilibili_missing_or_invalid_uid_fails_before_writes(self) -> None:
        target = self.root / setup_deployment.BILIBILI_TARGET
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("sentinel", encoding="utf-8")

        for bilibili in ({}, {"bot_account": "  "}, {"bot_account": "１２３"}, {"bot_account": "12x"}):
            with self.subTest(bilibili=bilibili):
                result = setup_deployment.ConfigInitializer.generate_configs(
                    {"components": ["bilibili"], "bilibili": bilibili}
                )
                self.assertTrue(result["errors"])
                self.assertEqual(result["generated"], [])
                self.assertEqual(result["backups"], [])
                self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")
        self.assertFalse(setup_deployment.BACKUP_DIR.exists())

    def test_valid_bilibili_uid_is_written_as_trimmed_string(self) -> None:
        result = setup_deployment.ConfigInitializer.generate_configs(
            {"components": ["bilibili"], "bilibili": {"bot_account": " 0012345 "}}
        )

        self.assertEqual(result["errors"], [])
        document = tomllib.loads(
            (self.root / setup_deployment.BILIBILI_TARGET).read_text(encoding="utf-8")
        )
        self.assertEqual(document["bilibili"]["bot_account"], "0012345")
        for field in ("sessdata", "bili_jct", "buvid3", "buvid4", "dede_user_id"):
            self.assertEqual(document["bilibili"][field], "")

    def test_bilibili_defaults_stay_blank_even_with_live_template_value(self) -> None:
        template = self.root / "NachoBot-Bilibili-Adapter" / "config_template.toml"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text('[bilibili]\nbot_account = "99887766"\n', encoding="utf-8")

        defaults = setup_deployment.ConfigInitializer.get_defaults()

        self.assertEqual(defaults["bilibili"], {"bot_account": ""})

    def test_malformed_bilibili_builtin_template_fails_before_writes(self) -> None:
        target = self.root / setup_deployment.BILIBILI_TARGET
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("sentinel", encoding="utf-8")
        with mock.patch.dict(
            setup_deployment.BUILTIN_TEMPLATE_TEXT,
            {setup_deployment.BUILTIN_BILIBILI_TEMPLATE: "[bilibili\n"},
        ):
            result = setup_deployment.ConfigInitializer.generate_configs(
                {"components": ["bilibili"], "bilibili": {"bot_account": "12345"}}
            )

        self.assertTrue(result["errors"])
        self.assertEqual(result["generated"], [])
        self.assertEqual(result["backups"], [])
        self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")

    def test_bilibili_builtin_mapping_must_point_to_target(self) -> None:
        target = self.root / setup_deployment.BILIBILI_TARGET
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("sentinel", encoding="utf-8")
        broken_map = dict(setup_deployment.TEMPLATE_MAP)
        broken_map[setup_deployment.BUILTIN_BILIBILI_TEMPLATE] = "wrong/config.toml"
        with mock.patch.object(setup_deployment, "TEMPLATE_MAP", broken_map):
            result = setup_deployment.ConfigInitializer.generate_configs(
                {"components": ["bilibili"], "bilibili": {"bot_account": "12345"}}
            )

        self.assertTrue(result["errors"])
        self.assertEqual(result["generated"], [])
        self.assertEqual(result["backups"], [])
        self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")


class BuiltinTemplatePackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "NachoBot-DiscordVC-Adapter").mkdir()
        (self.root / "NachoBot-DiscordVC-Adapter/config.toml.example").write_text(
            '[discord]\ntoken = "YOUR_DISCORD_BOT_TOKEN"\n',
            encoding="utf-8",
        )
        self.old_values = (
            setup_deployment.ROOT_DIR,
            setup_deployment.TEMPLATE_MAP,
            setup_deployment.BACKUP_DIR,
            setup_checks.ROOT_DIR,
        )
        setup_deployment.ROOT_DIR = self.root
        setup_deployment.TEMPLATE_MAP = {
            setup_deployment.BUILTIN_KOISHI_TEMPLATE: "koishi-app/koishi.yml",
            "NachoBot-DiscordVC-Adapter/config.toml.example": (
                "NachoBot-DiscordVC-Adapter/config.toml"
            ),
            setup_deployment.BUILTIN_BILIBILI_TEMPLATE: (
                "NachoBot-Bilibili-Adapter/config.toml"
            ),
        }
        setup_deployment.BACKUP_DIR = self.root / "backups"
        setup_checks.ROOT_DIR = self.root

    def tearDown(self) -> None:
        (
            setup_deployment.ROOT_DIR,
            setup_deployment.TEMPLATE_MAP,
            setup_deployment.BACKUP_DIR,
            setup_checks.ROOT_DIR,
        ) = self.old_values
        self.temp_dir.cleanup()

    def test_builtin_templates_work_without_user_template_files(self) -> None:
        self.assertFalse((self.root / "koishi-app/koishi_template.yml").exists())
        self.assertFalse(
            (self.root / "NachoBot-Bilibili-Adapter/config_template.toml").exists()
        )
        self.assertEqual(setup_deployment.ConfigInitializer._validate_discord_templates(), [])

        result = setup_deployment.ConfigInitializer.generate_configs(
            {
                "components": ["discord", "bilibili"],
                "discord": {"token": "opaque-token"},
                "bilibili": {"bot_account": "0012345"},
            }
        )

        self.assertEqual(result["errors"], [])
        koishi = yaml.safe_load(
            (self.root / "koishi-app/koishi.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            koishi["plugins"]["group:adapter"]["adapter-discord:97kjzj"]["token"],
            "opaque-token",
        )
        discord = tomlkit.parse(
            (self.root / "NachoBot-DiscordVC-Adapter/config.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(str(discord["discord"]["token"]), "opaque-token")

        bilibili = tomllib.loads(
            (self.root / "NachoBot-Bilibili-Adapter/config.toml").read_text(
                encoding="utf-8"
            )
        )
        for field in ("sessdata", "bili_jct", "buvid3", "buvid4", "dede_user_id"):
            self.assertEqual(bilibili["bilibili"][field], "")
        self.assertEqual(bilibili["bilibili"]["bot_account"], "0012345")

    def test_builtin_mapping_and_prevalidation_avoid_user_template_paths(self) -> None:
        self.assertNotIn("koishi-app/koishi_template.yml", setup_checks.TEMPLATE_MAP)
        self.assertNotIn(
            "NachoBot-Bilibili-Adapter/config_template.toml",
            setup_checks.TEMPLATE_MAP,
        )
        self.assertIn(
            setup_deployment.BUILTIN_KOISHI_TEMPLATE,
            setup_checks.TEMPLATE_MAP,
        )
        self.assertIn(
            setup_deployment.BUILTIN_BILIBILI_TEMPLATE,
            setup_checks.TEMPLATE_MAP,
        )
        statuses = {
            entry["template"]: entry["template_exists"]
            for entry in setup_checks.EnvironmentChecker.check_configs()
        }
        self.assertTrue(statuses[setup_deployment.BUILTIN_KOISHI_TEMPLATE])
        self.assertTrue(statuses[setup_deployment.BUILTIN_BILIBILI_TEMPLATE])
        self.assertEqual(setup_deployment.ConfigInitializer._validate_discord_templates(), [])


class MultimodalRuntimeTests(unittest.TestCase):
    def test_install_tasks_preserve_selected_runtime(self) -> None:
        for runtime in ("gpu", "cpu", "relay"):
            with self.subTest(runtime=runtime):
                tasks = setup_deployment.DependencyInstaller.get_install_tasks(
                    ["tts"], runtime
                )
                task = next(item for item in tasks if item["id"] == "tts")
                self.assertEqual(task["runtime"], runtime)
                self.assertIn(
                    MultimodalRuntimeManager.PROFILE_META[runtime]["label"],
                    task["name"],
                )

    def test_initial_deployment_always_includes_relay_runtime(self) -> None:
        tasks = setup_deployment.DependencyInstaller.get_install_tasks([], "gpu")
        relay = [item for item in tasks if item["id"] == "tts_relay"]
        self.assertEqual(len(relay), 1)
        self.assertEqual(relay[0]["runtime"], "relay")

        tasks = setup_deployment.DependencyInstaller.get_install_tasks(["tts"], "gpu")
        self.assertEqual(
            [(item["id"], item.get("runtime")) for item in tasks if item["id"].startswith("tts")],
            [("tts", "gpu"), ("tts_relay", "relay")],
        )

        tasks = setup_deployment.DependencyInstaller.get_install_tasks(["tts"], "relay")
        self.assertEqual(
            [(item["id"], item.get("runtime")) for item in tasks if item["id"].startswith("tts")],
            [("tts", "relay")],
        )

    def test_null_runtime_alias_maps_to_relay(self) -> None:
        self.assertEqual(MultimodalRuntimeManager.normalize_profile("null"), "relay")

    def test_relay_dependencies_exclude_local_model_stack(self) -> None:
        relay = {dependency.lower() for dependency in MultimodalRuntimeManager.RELAY_DEPENDENCIES}
        forbidden_prefixes = (
            "torch",
            "torchvision",
            "torchaudio",
            "transformers",
            "timm",
            "onnxruntime",
            "sherpa",
        )
        self.assertFalse(
            any(
                dependency.startswith(prefix)
                for dependency in relay
                for prefix in forbidden_prefixes
            )
        )

    def test_potato_runtime_prefers_relay_then_falls_back_to_gpu_cpu(self) -> None:
        manager = object.__new__(process_manager.ProcessManager)
        manager._launch_runtime = "gpu"

        installed = {"relay": True, "gpu": True, "cpu": True}
        with mock.patch.object(
            process_manager.MultimodalRuntimeManager,
            "get_status",
            side_effect=lambda profile: {"installed": installed[profile]},
        ):
            self.assertEqual(manager._resolve_potato_runtime("cpu"), "relay")

        installed = {"relay": False, "gpu": True, "cpu": True}
        with mock.patch.object(
            process_manager.MultimodalRuntimeManager,
            "get_status",
            side_effect=lambda profile: {"installed": installed[profile]},
        ):
            self.assertEqual(manager._resolve_potato_runtime("cpu"), "cpu")

        installed = {"relay": False, "gpu": False, "cpu": True}
        with mock.patch.object(
            process_manager.MultimodalRuntimeManager,
            "get_status",
            side_effect=lambda profile: {"installed": installed[profile]},
        ):
            self.assertEqual(manager._resolve_potato_runtime("gpu"), "cpu")

        installed = {"relay": False, "gpu": False, "cpu": False}
        with mock.patch.object(
            process_manager.MultimodalRuntimeManager,
            "get_status",
            side_effect=lambda profile: {"installed": installed[profile]},
        ):
            with self.assertRaises(RuntimeError):
                manager._resolve_potato_runtime("gpu")

    def test_potato_command_uses_resolved_runtime_python_without_uv(self) -> None:
        manager = object.__new__(process_manager.ProcessManager)
        manager._launch_runtime = "cpu"
        runtime_python = Path(r"C:\runtime\cpu\Scripts\python.exe")
        with mock.patch.object(
            process_manager.MultimodalRuntimeManager,
            "require_python",
            return_value=runtime_python,
        ) as require_python:
            cmd, env = manager._resolve_multimodal_runtime_cmd(
                "potato_relay",
                ["uv", "run", "python", "main.py", "--no-local-models"],
                {},
            )

        require_python.assert_called_once_with("cpu")
        self.assertEqual(cmd, [str(runtime_python), "main.py", "--no-local-models"])
        self.assertNotIn("uv", cmd)
        self.assertEqual(env, {})


if __name__ == "__main__":
    unittest.main()
