from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from identity_map import IdentityMap
from migration import (
    MigrationError,
    export_identity_map,
    migrate_legacy_config,
    needs_migration,
)
import tomlkit


class DiscordMigrationTests(unittest.TestCase):
    def _write_sources(self, root: Path, *, group_list: str = '["10"]') -> dict[str, Path]:
        target = root / "new" / "config.toml"
        target.parent.mkdir(parents=True)
        target.write_text(
            'log_level = "INFO"\n'
            '[discord]\n'
            'token = "voice-token-secret"\n'
            'app_id = "app-id"\n'
            'proxy_enabled = true\n'
            'proxy_url = "https://proxy.example:7890"\n'
            '[nachobot]\n'
            'host = "core.internal"\n'
            'port = 8000\n'
            '[voice]\n'
            'enabled = true\n'
            'sample_rate = 48000\n'
            '[prompts]\n'
            'planner_prompt = "line one\\nline two"\n'
            'replyer_prompt = "reply prompt"\n',
            encoding="utf-8",
        )
        text_config = root / "text.toml"
        text_config.write_text(
            '[nachobot_server]\nplatform = "discord"\nhost = "127.0.0.1"\nport = 8001\n'
            '[chat]\ngroup_list_type = "whitelist"\n'
            f'group_list = {group_list}\n'
            'private_list_type = "blacklist"\nprivate_list = []\nban_user_id = []\n'
            '[voice]\nuse_tts = true\n'
            '[visual.image]\ntemperature = 0.1\nmax_tokens = 240\nextra_params = { enable_thinking = false }\n'
            '[network]\nproxy = "https://media-proxy.example:7897"\n',
            encoding="utf-8",
        )
        koishi = root / "koishi.yml"
        koishi.write_text(
            'plugins:\n'
            '  adapter-discord:abc:\n'
            '    token: "voice-token-secret"\n'
            '  proxy-agent:def:\n'
            '    proxyAgent: "https://koishi-proxy.example:7897"\n',
            encoding="utf-8",
        )
        database = root / "koishi.db"
        connection = sqlite3.connect(database)
        connection.executescript(
            "CREATE TABLE binding (aid INTEGER, bid INTEGER, pid TEXT, platform TEXT, botselfid TEXT);"
            "CREATE TABLE bindingchannel (aid INTEGER, channelId TEXT);"
            "CREATE TABLE channelprivate (userId TEXT, channelId TEXT, botSelfId TEXT, platform TEXT);"
        )
        connection.execute(
            "INSERT INTO binding VALUES (?, ?, ?, ?, ?)",
            (10, 10, "100000000000000001", "discord", "100000000000000099"),
        )
        connection.execute(
            "INSERT INTO bindingchannel VALUES (?, ?)", (10, "200000000000000001")
        )
        connection.execute(
            "INSERT INTO channelprivate VALUES (?, ?, ?, ?)",
            ("100000000000000001", "300000000000000001", "100000000000000099", "discord"),
        )
        connection.commit()
        connection.close()
        return {
            "target": target,
            "text": text_config,
            "koishi": koishi,
            "database": database,
            "identity": root / "new" / "data" / "identity_map.json",
        }

    @staticmethod
    def _migrate(paths: dict[str, Path]):
        return migrate_legacy_config(
            target_config_path=paths["target"],
            legacy_discordvc_config_path=paths["target"],
            legacy_koishi_adapter_config_path=paths["text"],
            koishi_config_path=paths["koishi"],
            identity_path=paths["identity"],
            koishi_db_path=paths["database"],
        )

    def test_in_place_migration_exports_identity_and_preserves_private_history(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self._write_sources(Path(temp))
            original = paths["target"].read_bytes()
            result = self._migrate(paths)

            self.assertTrue(result.success, result.error)
            self.assertTrue(result.migrated)
            self.assertEqual((result.user_count, result.channel_count, result.private_channel_count), (1, 1, 1))
            self.assertEqual(paths["target"].with_name("config.toml.legacy-v1.bak").read_bytes(), original)
            self.assertTrue(paths["identity"].is_file())
            config = tomlkit.parse(paths["target"].read_text(encoding="utf-8"))
            self.assertEqual(config["config_version"], 3)
            self.assertTrue(config["identity"]["required"])
            self.assertEqual(config["chat"]["group_list"], ["10"])
            self.assertEqual(config["discord"]["token"], "voice-token-secret")
            self.assertEqual(config["prompts"]["planner_prompt"], "line one\nline two")
            identity = IdentityMap.load(paths["identity"])
            self.assertEqual(identity.logical_user("100000000000000001"), "10")
            self.assertEqual(identity.logical_channel("200000000000000001"), "10")
            self.assertEqual(identity.native_user("10"), "100000000000000001")
            self.assertEqual(identity.native_channel("10"), "200000000000000001")

    def test_repeat_is_idempotent_and_current_config_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self._write_sources(Path(temp))
            first = self._migrate(paths)
            self.assertTrue(first.success)
            config_before = paths["target"].read_bytes()
            identity_before = paths["identity"].read_bytes()

            second = self._migrate(paths)

            self.assertTrue(second.success)
            self.assertFalse(second.migrated)
            self.assertEqual(paths["target"].read_bytes(), config_before)
            self.assertEqual(paths["identity"].read_bytes(), identity_before)

    def test_native_v2_config_does_not_trigger_koishi_migration_or_rewrite(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "config.toml"
            target.write_text(
                'config_version = 2\n[discord]\ntoken = "fixture-token"\n',
                encoding="utf-8",
            )
            original = target.read_bytes()

            self.assertFalse(needs_migration(target))
            self.assertEqual(target.read_bytes(), original)

    def test_conflicting_map_or_malformed_input_fails_before_any_destination_write(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self._write_sources(Path(temp))
            original = paths["target"].read_bytes()
            paths["identity"].parent.mkdir(parents=True)
            paths["identity"].write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "user_native_to_logical": {"100000000000000001": "other"},
                        "channel_native_to_logical": {"200000000000000001": "10"},
                        "private_channels": [],
                        "bot_self_ids": [],
                    }
                ),
                encoding="utf-8",
            )
            result = self._migrate(paths)
            self.assertFalse(result.success)
            self.assertIn("conflicts", result.error)
            self.assertEqual(paths["target"].read_bytes(), original)
            self.assertFalse(paths["target"].with_name("config.toml.legacy-v1.bak").exists())

        with tempfile.TemporaryDirectory() as temp:
            paths = self._write_sources(Path(temp))
            paths["text"].write_text("[chat\ninvalid", encoding="utf-8")
            result = self._migrate(paths)
            self.assertFalse(result.success)
            self.assertFalse(paths["identity"].exists())
            self.assertFalse(paths["target"].with_name("config.toml.legacy-v1.bak").exists())

    def test_unknown_whitelist_and_invalid_runtime_settings_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self._write_sources(Path(temp), group_list='["999999999999999999"]')
            result = self._migrate(paths)
            self.assertFalse(result.success)
            self.assertIn("unmappable", result.error)
            self.assertFalse(paths["identity"].exists())

        with tempfile.TemporaryDirectory() as temp:
            paths = self._write_sources(Path(temp))
            paths["text"].write_text(
                paths["text"].read_text(encoding="utf-8").replace(
                    'platform = "discord"', 'platform = "qq"'
                ),
                encoding="utf-8",
            )
            result = self._migrate(paths)
            self.assertFalse(result.success)
            self.assertIn("not for Discord", result.error)
            self.assertFalse(paths["identity"].exists())

    def test_voice_channel_filter_uses_exported_logical_channel_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self._write_sources(Path(temp))
            current = paths["target"].read_text(encoding="utf-8")
            paths["target"].write_text(
                current.replace(
                    "sample_rate = 48000",
                    'sample_rate = 48000\nallowed_channel_list_type = "whitelist"\n'
                    'allowed_channel_list = ["200000000000000001"]',
                ),
                encoding="utf-8",
            )

            result = self._migrate(paths)

            self.assertTrue(result.success, result.error)
            config = tomlkit.parse(paths["target"].read_text(encoding="utf-8"))
            self.assertEqual(config["voice"]["allowed_channel_list"], ["10"])

        with tempfile.TemporaryDirectory() as temp:
            paths = self._write_sources(Path(temp))
            current = paths["target"].read_text(encoding="utf-8")
            paths["target"].write_text(
                current.replace(
                    "sample_rate = 48000",
                    'sample_rate = 48000\nallowed_channel_list_type = "whitelist"\n'
                    'allowed_channel_list = ["999999999999999999"]',
                ),
                encoding="utf-8",
            )
            original = paths["target"].read_bytes()

            result = self._migrate(paths)

            self.assertFalse(result.success)
            self.assertIn("unmappable", result.error)
            self.assertEqual(paths["target"].read_bytes(), original)

    def test_identity_export_is_read_only_and_bijective(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self._write_sources(Path(temp))
            before = paths["database"].read_bytes()
            exported = export_identity_map(paths["database"])
            self.assertEqual(exported.user_native_to_logical, {"100000000000000001": "10"})
            self.assertEqual(exported.channel_native_to_logical, {"200000000000000001": "10"})
            self.assertEqual(paths["database"].read_bytes(), before)
            self.assertTrue(needs_migration(paths["target"]))


if __name__ == "__main__":
    unittest.main()
