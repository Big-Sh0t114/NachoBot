from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from config_manager import ConfigManager  # noqa: E402
from plugin_manager import PluginManager  # noqa: E402


class ConfigManagerSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "NachoBot" / "config" / "bot_config.toml"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(
            '[provider]\napi_key = "top-secret"\nname = "visible"\n'
            'auth_token = ["alpha", "beta"]\npassword = "password-secret"\n'
            '[credentials]\nusername = "credential-user"\nvalue = "credential-secret"\n',
            encoding="utf-8",
        )
        env_path = self.root / "NachoBot" / ".env"
        env_path.write_text("HOST=127.0.0.1\nAPI_KEY=env-secret\n", encoding="utf-8")
        self.manager = ConfigManager(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_raw_editor_receives_real_values_while_structured_view_is_masked(self) -> None:
        editable = self.manager.read_config_raw("bot_config")
        self.assertIn("top-secret", editable)
        self.assertIn("alpha", editable)
        self.assertIn("beta", editable)
        self.assertIn("password-secret", editable)
        self.assertIn("credential-user", editable)
        self.assertIn("credential-secret", editable)

        structured = self.manager.read_config("bot_config", mask_sensitive=True)
        self.assertNotIn("top-secret", str(structured))
        self.assertNotIn("credential-user", str(structured))
        self.assertEqual(structured["credentials"]["username"], "****")

        env_editable = self.manager.read_config_raw("env")
        self.assertIn("API_KEY=env-secret", env_editable)

    def test_saving_raw_editor_text_preserves_unmodified_secrets(self) -> None:
        editable = self.manager.read_config_raw("bot_config")
        self.manager.write_config_raw("bot_config", editable.replace("visible", "changed"))
        saved = self.config_path.read_text(encoding="utf-8")
        self.assertIn('api_key = "top-secret"', saved)
        self.assertIn('auth_token = ["alpha", "beta"]', saved)
        self.assertIn('name = "changed"', saved)

        env_editable = self.manager.read_config_raw("env")
        self.manager.write_config_raw("env", env_editable.replace("127.0.0.1", "localhost"))
        saved_env = (self.root / "NachoBot" / ".env").read_text(encoding="utf-8")
        self.assertIn("API_KEY=env-secret", saved_env)
        self.assertIn("HOST=localhost", saved_env)

    def test_secret_can_be_explicitly_replaced_or_cleared(self) -> None:
        editable = self.manager.read_config_raw("bot_config")
        replaced = editable.replace("top-secret", "replacement", 1)
        self.manager.write_config_raw("bot_config", replaced)
        self.assertIn('api_key = "replacement"', self.config_path.read_text(encoding="utf-8"))

        editable = self.manager.read_config_raw("bot_config")
        cleared = editable.replace("replacement", "", 1)
        self.manager.write_config_raw("bot_config", cleared)
        self.assertIn('api_key = ""', self.config_path.read_text(encoding="utf-8"))

    def test_array_table_reordering_keeps_each_original_secret(self) -> None:
        import tomlkit

        self.config_path.write_text(
            '[[providers]]\nname = "first"\napi_key = "first-secret"\n\n'
            '[[providers]]\nname = "second"\napi_key = "second-secret"\n',
            encoding="utf-8",
        )
        editable = tomlkit.parse(self.manager.read_config_raw("bot_config"))
        editable["providers"].reverse()
        self.manager.write_config_raw("bot_config", tomlkit.dumps(editable))
        saved = tomlkit.parse(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [(item["name"], item["api_key"]) for item in saved["providers"]],
            [("second", "second-secret"), ("first", "first-secret")],
        )

    def test_restore_rejects_absolute_traversal_and_unrelated_backups(self) -> None:
        backup_name = Path(self.manager.backup_config("bot_config")).name
        with self.assertRaises(ValueError):
            self.manager.restore_backup("bot_config", str(self.config_path.parent / backup_name))
        with self.assertRaises(ValueError):
            self.manager.restore_backup("bot_config", f"..{os.sep}{backup_name}")

        unrelated = self.config_path.parent / "other.manual.20000101_000000.bak"
        unrelated.write_text("[x]\nvalue = 1\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.manager.restore_backup("bot_config", unrelated.name)

    def test_restore_rejects_symlink_escape_when_supported(self) -> None:
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        outside = Path(outside_dir.name) / "outside.bak"
        outside.write_text("[provider]\napi_key = \"stolen\"\n", encoding="utf-8")
        link = self.config_path.parent / "bot_config.manual.20000101_000000.bak"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaises(ValueError):
            self.manager.restore_backup("bot_config", link.name)

    def test_write_validator_sees_raw_editor_text_before_disk_change(self) -> None:
        editable = self.manager.read_config_raw("bot_config")
        original = self.config_path.read_text(encoding="utf-8")

        def reject_if_secret_is_preserved(raw: str) -> None:
            self.assertIn('api_key = "top-secret"', raw)
            raise ValueError("unsafe effective configuration")

        with self.assertRaisesRegex(ValueError, "unsafe effective"):
            self.manager.write_config_raw(
                "bot_config",
                editable,
                validator=reject_if_secret_is_preserved,
            )
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

    def test_restore_validator_runs_before_replacing_current_config(self) -> None:
        backup = self.config_path.parent / "bot_config.manual.20000101_000000.bak"
        backup.write_text('[server]\nhost = "0.0.0.0"\n', encoding="utf-8")
        original = self.config_path.read_text(encoding="utf-8")

        def reject_remote_bind(raw: str) -> None:
            self.assertIn('host = "0.0.0.0"', raw)
            raise ValueError("remote bind requires authentication")

        with self.assertRaisesRegex(ValueError, "requires authentication"):
            self.manager.restore_backup(
                "bot_config",
                backup.name,
                validator=reject_remote_bind,
            )
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

    def test_restore_rejects_invalid_toml_before_replacing_current_config(self) -> None:
        backup = self.config_path.parent / "bot_config.manual.20000101_000000.bak"
        backup.write_text('[broken\nsecret = "value"\n', encoding="utf-8")
        original = self.config_path.read_text(encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "无效 TOML"):
            self.manager.restore_backup("bot_config", backup.name)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

    def test_invalid_toml_raw_editor_returns_source_for_repair(self) -> None:
        invalid_raw = 'api_key = "leaked"\n[broken\n'
        self.config_path.write_text(invalid_raw, encoding="utf-8")
        self.assertEqual(self.manager.read_config_raw("bot_config"), invalid_raw)

    def test_plugin_editor_reads_and_writes_raw_config(self) -> None:
        plugin_dir = self.root / "NachoBot" / "plugins" / "example"
        plugin_dir.mkdir(parents=True)
        plugin_config = plugin_dir / "config.toml"
        plugin_config.write_text(
            '[api]\ntoken = "plugin-secret"\nendpoint = "local"\n',
            encoding="utf-8",
        )
        manager = PluginManager(self.root)
        editable = manager.read_plugin_config_raw("example")
        self.assertIn("plugin-secret", editable)
        manager.write_plugin_config_raw("example", editable.replace("local", "updated"))
        saved = plugin_config.read_text(encoding="utf-8")
        self.assertIn('token = "plugin-secret"', saved)
        self.assertIn('endpoint = "updated"', saved)


if __name__ == "__main__":
    unittest.main()
