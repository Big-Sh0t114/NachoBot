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
            {"components": ["discord", "bilibili"], "discord": {"token": "opaque-token"}}
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
        for field in ("sessdata", "bili_jct", "dede_user_id"):
            self.assertEqual(bilibili["bilibili"][field].strip(), "")

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


if __name__ == "__main__":
    unittest.main()
