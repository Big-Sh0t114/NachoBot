import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "NachoBot/src/plugins/built_in/qqcall_plugin"
SPEC = importlib.util.spec_from_file_location("qqcall_config_bootstrap", PLUGIN / "config_bootstrap.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
VALIDATOR_SPEC = importlib.util.spec_from_file_location("qqcall_config_validator", ROOT / "NachoBot-QQCall/service/config.py")
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(validator)


class ConfigBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "NachoBot/src/plugins/built_in/qqcall_plugin/config.toml"
        self.path.parent.mkdir(parents=True)
        self.bot = self.root / "NachoBot/config/bot_config.toml"
        self.bot.parent.mkdir()

    def load(self):
        return validator.load_config(self.path, bot_config_path=self.bot)

    def test_missing_config_generates_valid_defaults_without_template(self):
        self.assertTrue(bootstrap.ensure_default_config(self.path))
        self.assertNotIn("plugin", bootstrap.tomlkit.parse(self.path.read_text()))
        normalized = self.load()
        self.assertFalse(normalized["enabled"])
        self.assertEqual(normalized["core"]["group_intent_model_task"], "utils")
        self.assertEqual(Path(normalized["runtime_dir"]), self.root / "NachoBot-QQCall/.runtime/service")
        self.assertEqual(Path(normalized["paths"]["python_exe"]), self.root / "NachoBot/.venv/Scripts/python.exe")
        self.assertFalse(bootstrap.ensure_default_config(self.path))
        self.assertEqual(list(self.path.parent.glob(".qqcall-config-*")), [])

    def test_existing_valid_or_broken_config_is_preserved(self):
        for content in (b"[plugin]\nenabled = true\n", b"broken = ["):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.assertFalse(bootstrap.ensure_default_config(self.path))
                self.assertEqual(self.path.read_bytes(), content)

    def test_bot_switch_controls_plugin_and_audio_even_with_legacy_switch(self):
        bootstrap.ensure_default_config(self.path)
        with self.path.open("a") as stream:
            stream.write("\n[plugin]\nenabled = true\n")
        for enabled in (False, True, False):
            self.bot.write_text('[bot]\nqq_account = "123456789"\n[qqcall]\nenabled = ' + str(enabled).lower())
            settings = self.load()
            self.assertIs(settings["enabled"], enabled)
            self.assertIs(settings["audio"]["enabled"], enabled)

    def test_bad_bot_switch_fails_closed(self):
        bootstrap.ensure_default_config(self.path)
        for content in ('[qqcall]\nenabled = "true"', 'qqcall = true', 'broken = ['):
            with self.subTest(content=content):
                self.bot.write_text(content)
                with self.assertRaises(validator.ConfigError):
                    self.load()

    def test_adapter_launcher_uses_new_config_and_bot_switch(self):
        bootstrap.ensure_default_config(self.path)
        service = self.root / "NachoBot-QQCall/service"
        service.mkdir(parents=True)
        shutil.copy2(ROOT / "NachoBot-QQCall/service/config.py", service / "config.py")
        spec = importlib.util.spec_from_file_location("qqcall_adapter_extra_test", ROOT / "NachoBot-QQCall/scripts/adapter_extra.py")
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        with patch.object(adapter, "REPO_ROOT", self.root):
            for enabled in (False, True, False):
                self.bot.write_text('[bot]\nqq_account = "123456789"\n[qqcall]\nenabled = ' + str(enabled).lower())
                for name in ("napcat", "snowluma"):
                    with self.subTest(enabled=enabled, adapter=name):
                        self.assertEqual(adapter._select_extra(name), (adapter._EXTRA if enabled else adapter._NONE, False))

    def test_legacy_settings_migrate_with_same_resolved_paths(self):
        import copy
        legacy = self.root / "NachoBot/plugins/qqcall_plugin/config.toml"
        legacy.parent.mkdir(parents=True)
        values = copy.deepcopy(validator.DEFAULT_CONFIG)
        values["plugin"] = {"enabled": True}
        values["qqcall"]["runtime_dir"] = "../../../NachoBot-QQCall/custom-runtime"
        values["qqcall"]["paths"]["python_exe"] = "../../.venv/Scripts/python.exe"
        values["qqcall"]["paths"]["loader_root"] = "../../../NapCat.Shell"
        source = "# user comment\n" + bootstrap.tomlkit.dumps(values)
        legacy.write_text(source, encoding="utf-8")
        self.assertTrue(bootstrap.ensure_default_config(self.path))
        self.assertIn("# user comment", self.path.read_text())
        self.assertEqual(legacy.read_text(), source)
        migrated = bootstrap.tomlkit.parse(self.path.read_text()).unwrap()
        self.assertEqual(validator._normalize(values, legacy.parent), validator._normalize(migrated, self.path.parent))

    def test_account_inherits_without_writing_config(self):
        bootstrap.ensure_default_config(self.path)
        for omitted in (False, True):
            document = bootstrap.tomlkit.parse(self.path.read_text())
            if omitted:
                del document["qqcall"]["account_id"]
                del document["qqcall"]["identity_file"]
            self.path.write_text(bootstrap.tomlkit.dumps(document))
            before = self.path.read_bytes()
            for account in ("123456789", "987654321"):
                self.bot.write_text(f'[bot]\nqq_account = "{account}"\n[qqcall]\nenabled = true\n')
                settings = self.load()
                self.assertEqual(settings["account_id"], account)
                self.assertEqual(settings["identity_file"], "")
                self.assertEqual(settings["paths"]["qq_exe"], "")
                self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_account_must_match_bot(self):
        bootstrap.ensure_default_config(self.path)
        document = bootstrap.tomlkit.parse(self.path.read_text())
        document["qqcall"]["account_id"] = "123456789"
        self.path.write_text(bootstrap.tomlkit.dumps(document))
        self.bot.write_text('[bot]\nqq_account = "123456789"\n[qqcall]\nenabled = true')
        self.assertEqual(self.load()["account_id"], "123456789")
        self.bot.write_text('[bot]\nqq_account = "987654321"\n[qqcall]\nenabled = true')
        with self.assertRaisesRegex(validator.ConfigError, "^bot_account_mismatch$"):
            self.load()

    def test_missing_or_invalid_enabled_account_is_rejected(self):
        bootstrap.ensure_default_config(self.path)
        for value in ('""', 'true', '123456789', '"invalid"'):
            with self.subTest(value=value):
                self.bot.write_text(f'[bot]\nqq_account = {value}\n[qqcall]\nenabled = true')
                with self.assertRaises(validator.ConfigError):
                    self.load()

    def test_concurrent_creation_wins(self):
        def competing_link(source, target):
            self.path.write_bytes(b"user settings")
            raise FileExistsError()

        with patch.object(bootstrap.os, "link", side_effect=competing_link):
            self.assertFalse(bootstrap.ensure_default_config(self.path))
        self.assertEqual(self.path.read_bytes(), b"user settings")
        self.assertEqual(list(self.path.parent.glob(".qqcall-config-*")), [])

    def test_publish_failure_does_not_leave_partial_config(self):
        with patch.object(bootstrap.os, "link", side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                bootstrap.ensure_default_config(self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob(".qqcall-config-*")), [])


if __name__ == "__main__":
    unittest.main()
