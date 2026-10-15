from __future__ import annotations

import copy
import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path
from typing import Any

from service.config import ConfigError, DEFAULT_CONFIG, SCHEMA, load_config

_MIGRATION_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_config.py"
_MIGRATION_SPEC = importlib.util.spec_from_file_location("qqcall_config_migration", _MIGRATION_PATH)
assert _MIGRATION_SPEC is not None and _MIGRATION_SPEC.loader is not None
_MIGRATION = importlib.util.module_from_spec(_MIGRATION_SPEC)
_MIGRATION_SPEC.loader.exec_module(_MIGRATION)


def _toml_value(value: Any) -> str:
    if type(value) is str:
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        if type(value) is float and math.isnan(value):
            return "nan"
        if type(value) is float and math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + pairs + " }"
    raise TypeError(type(value).__name__)


def _toml_document(value: dict[str, Any]) -> bytes:
    lines: list[str] = []

    def table(current: dict[str, Any], prefix: str) -> None:
        if prefix:
            if lines:
                lines.append("")
            lines.append(f"[{prefix}]")
        for key, item in current.items():
            if not isinstance(item, dict):
                lines.append(f"{key} = {_toml_value(item)}")
        for key, item in current.items():
            if isinstance(item, dict):
                table(item, f"{prefix}.{key}" if prefix else key)

    for key, item in value.items():
        if not isinstance(item, dict):
            lines.append(f"{key} = {_toml_value(item)}")
    for key, item in value.items():
        if isinstance(item, dict):
            table(item, key)
    return ("\n".join(lines) + "\n").encode("utf-8")


class ServiceConfigTests(unittest.TestCase):
    def write(self, directory: Path, value: object, *, raw: bytes | None = None) -> Path:
        path = directory / "config.toml"
        path.write_bytes(raw if raw is not None else _toml_document(value))
        return path

    def test_disabled_default_normalizes_paths_and_active_core_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = self.write(directory, copy.deepcopy(DEFAULT_CONFIG))
            config = load_config(path)
        self.assertEqual(SCHEMA, 6)
        self.assertEqual(config["schema"], 6)
        self.assertFalse(config["enabled"])
        self.assertFalse(config["audio"]["enabled"])
        self.assertNotIn("enabled", DEFAULT_CONFIG["qqcall"]["audio"])
        self.assertEqual(config["audio"]["capture_device"], "")
        self.assertEqual(config["audio"]["playback_device"], "")
        self.assertEqual(config["audio"]["capture_mode"], "loopback")
        self.assertEqual(config["audio"]["device_scope"], {"capture": "virtual", "playback": "virtual"})
        self.assertEqual(config["account_id"], "")
        self.assertEqual(config["identity_file"], "")
        self.assertEqual(config["paths"]["loader_root"], str((directory / "../../../NapCat.Shell").resolve()))
        self.assertEqual(config["paths"]["python_exe"], str((directory / "../../.venv/Scripts/python.exe").resolve()))
        self.assertEqual(config["paths"]["node_exe"], "node")
        self.assertEqual(config["runtime_dir"], str((directory / "../../../NachoBot-QQCall/.runtime/service").resolve()))
        self.assertEqual(config["universal_api"]["url"], "http://127.0.0.1:8016")
        expected_uvc_token = Path(config["runtime_dir"]) / "universal-api.token.private"
        self.assertEqual(config["universal_api"]["token_file"], str(expected_uvc_token.resolve()))
        self.assertTrue(config["universal_api_token_file_defaulted"])
        self.assertFalse(expected_uvc_token.exists())
        self.assertEqual(config["core"], {
            "allow_planner_accept": False,
            "group_intent_model_task": "utils",
            "group_intent_timeout_seconds": 10,
        })

    def test_enabled_requires_account_identity_onebot_and_qq_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = copy.deepcopy(DEFAULT_CONFIG)
            value["qqcall"]["adapter"] = "napcat"
            value["plugin"]["enabled"] = True
            with self.assertRaisesRegex(ConfigError, "enabled_account_required"):
                load_config(self.write(Path(temporary), value))

            value["qqcall"]["account_id"] = "12345"
            with self.assertRaisesRegex(ConfigError, "enabled_identity_and_onebot_required"):
                load_config(self.write(Path(temporary), value))

            value["qqcall"]["onebot"]["url"] = "http://127.0.0.1:3001"
            with self.assertRaisesRegex(ConfigError, "enabled_qq_exe_required"):
                load_config(self.write(Path(temporary), value))

            value["qqcall"]["paths"]["qq_exe"] = r"C:\QQ\QQ.exe"
            value["qqcall"]["audio"]["capture_device"] = "Virtual Output (loopback)"
            value["qqcall"]["audio"]["playback_device"] = "Virtual Input"
            self.assertTrue(load_config(self.write(Path(temporary), value))["enabled"])

    def test_auto_mode_does_not_require_a_manually_selected_onebot_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = copy.deepcopy(DEFAULT_CONFIG)
            value["plugin"]["enabled"] = True
            value["qqcall"]["account_id"] = "12345"
            value["qqcall"]["paths"]["qq_exe"] = r"C:\QQ\QQ.exe"
            normalized = load_config(self.write(Path(temporary), value))
            assert normalized["adapter"] == "auto"
            assert normalized["onebot"]["url"] == ""

    def test_exact_schema_rejects_unknown_duplicate_and_non_schema6_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            value = copy.deepcopy(DEFAULT_CONFIG)
            value["unexpected"] = False
            with self.assertRaisesRegex(ConfigError, "config_fields_invalid"):
                load_config(self.write(directory, value))

            value = copy.deepcopy(DEFAULT_CONFIG)
            value["schema"] = 3
            with self.assertRaisesRegex(ConfigError, "config_schema_unsupported"):
                load_config(self.write(directory, value))

            value = copy.deepcopy(DEFAULT_CONFIG)
            value["schema"] = 2
            with self.assertRaisesRegex(ConfigError, "config_schema_unsupported"):
                load_config(self.write(directory, value))
            value["schema"] = True
            with self.assertRaisesRegex(ConfigError, "config_schema_unsupported"):
                load_config(self.write(directory, value))

            raw = b"schema = 6\nschema = 6\n"
            with self.assertRaisesRegex(ConfigError, "config_invalid_toml"):
                load_config(self.write(directory, {}, raw=raw))

            value = copy.deepcopy(DEFAULT_CONFIG)
            value["qqcall"]["unexpected"] = "must not be exposed"
            with self.assertRaisesRegex(ConfigError, "qqcall_fields_invalid") as raised:
                load_config(self.write(directory, value))
            self.assertNotIn("must not be exposed", str(raised.exception))

    def test_files_are_bounded_utf8_and_toml_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "large.toml"
            path.write_bytes(b" " * (64 * 1024 + 1))
            with self.assertRaisesRegex(ConfigError, "config_too_large"):
                load_config(path)
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(ConfigError, "config_invalid_utf8"):
                load_config(path)
            path.write_text("{\"schema\": 4}", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "config_invalid_toml"):
                load_config(path)

    def test_numeric_and_boolean_fields_are_strict_and_ports_are_unique(self) -> None:
        invalid_values = [float("nan"), float("inf"), True, 0]
        for invalid in invalid_values:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                value = copy.deepcopy(DEFAULT_CONFIG)
                value["qqcall"]["supervision"]["poll_interval_sec"] = invalid
                with self.assertRaisesRegex(ConfigError, "poll_interval_invalid"):
                    load_config(self.write(Path(temporary), value))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            value = copy.deepcopy(DEFAULT_CONFIG)
            value["qqcall"]["ports"]["host"] = True
            with self.assertRaisesRegex(ConfigError, "port_invalid"):
                load_config(self.write(directory, value))
            value = copy.deepcopy(DEFAULT_CONFIG)
            value["qqcall"]["ports"]["callback"] = value["qqcall"]["ports"]["host"]
            with self.assertRaisesRegex(ConfigError, "ports_must_differ"):
                load_config(self.write(directory, value))
            value = copy.deepcopy(DEFAULT_CONFIG)
            value["plugin"]["enabled"] = 0
            with self.assertRaisesRegex(ConfigError, "enabled_invalid"):
                load_config(self.write(directory, value))

    def test_plugin_audio_and_core_flags_have_one_authoritative_switch(self) -> None:
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temporary:
                value = copy.deepcopy(DEFAULT_CONFIG)
                value["plugin"]["enabled"] = enabled
                if enabled:
                    value["qqcall"].update({"account_id": "12345"})
                    value["qqcall"]["onebot"]["url"] = "http://127.0.0.1:3001"
                    value["qqcall"]["paths"]["qq_exe"] = r"C:\QQ\QQ.exe"
                    value["qqcall"]["audio"].update({
                        "capture_device": "Virtual Output (loopback)",
                        "playback_device": "Virtual Input",
                    })
                config = load_config(self.write(Path(temporary), value))
                self.assertIs(config["enabled"], enabled)
                self.assertIs(config["audio"]["enabled"], enabled)
                self.assertEqual(config["audio"]["capture_mode"], "loopback")
                self.assertEqual(config["audio"]["device_scope"], {"capture": "virtual", "playback": "virtual"})

            value = copy.deepcopy(DEFAULT_CONFIG)
            value["qqcall"]["audio"]["enabled"] = not enabled
            with self.assertRaisesRegex(ConfigError, "audio_fields_invalid"):
                with tempfile.TemporaryDirectory() as temporary:
                    load_config(self.write(Path(temporary), value))

    def test_audio_table_and_each_name_override_are_optional(self) -> None:
        for raw_audio in (None, {"capture_device": "Virtual Output [loopback]"}, {"playback_device": "Virtual Input"}):
            with self.subTest(raw_audio=raw_audio), tempfile.TemporaryDirectory() as temporary:
                value = copy.deepcopy(DEFAULT_CONFIG)
                value["plugin"]["enabled"] = True
                value["qqcall"]["account_id"] = "12345"
                value["qqcall"]["onebot"]["url"] = "http://127.0.0.1:3001"
                value["qqcall"]["paths"]["qq_exe"] = r"C:\QQ\QQ.exe"
                if raw_audio is None:
                    value["qqcall"].pop("audio")
                else:
                    value["qqcall"]["audio"] = raw_audio
                config = load_config(self.write(Path(temporary), value))
                self.assertTrue(config["audio"]["enabled"])
                self.assertEqual(config["audio"]["capture_device"], (raw_audio or {}).get("capture_device", ""))
                self.assertEqual(config["audio"]["playback_device"], (raw_audio or {}).get("playback_device", ""))

    def test_peer_map_is_uid_to_uin_without_a_separate_user_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            value = copy.deepcopy(DEFAULT_CONFIG)
            value["qqcall"]["peer_map"] = {"u_abcdefghijklmnopqrstuv": "10001"}
            normalized = load_config(self.write(directory, value))
            self.assertEqual(normalized["peer_map"], {"u_abcdefghijklmnopqrstuv": "10001"})
            self.assertEqual(normalized["policy"], {"auto_accept": False})
            value["qqcall"]["peer_map"] = {"10001": "u_abcdefghijklmnopqrstuv"}
            with self.assertRaisesRegex(ConfigError, "peer_map_invalid"):
                load_config(self.write(directory, value))
            value["qqcall"]["peer_map"] = {
                "u_abcdefghijklmnopqrstuv": "10001",
                "u_zyxwvutsrqponmlkjihgfe": "10001",
            }
            with self.assertRaisesRegex(ConfigError, "peer_map_invalid"):
                load_config(self.write(directory, value))
            value["qqcall"]["policy"]["allowed_users"] = ["10001"]
            with self.assertRaisesRegex(ConfigError, "policy_fields_invalid"):
                load_config(self.write(directory, value))

    def test_obsolete_public_fields_are_rejected(self) -> None:
        cases = (
            ("plugin_fields_invalid", lambda value: value["plugin"].update({"config_version": "1.0.0"})),
            ("qqcall_fields_invalid", lambda value: value["qqcall"].update({"allowed_peer_ids": []})),
            ("policy_fields_invalid", lambda value: value["qqcall"]["policy"].update({"allowed_users": []})),
        )
        for expected, add_field in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                value = copy.deepcopy(DEFAULT_CONFIG)
                add_field(value)
                with self.assertRaisesRegex(ConfigError, expected):
                    load_config(self.write(Path(temporary), value))

    def test_audio_universal_api_and_onebot_validation_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for retired in (
                {"capture_mode": "microphone"},
                {"device_scope": {"capture": "virtual", "playback": "virtual"}},
                {"enabled": True},
                {"unrecognized": "value"},
            ):
                value = copy.deepcopy(DEFAULT_CONFIG)
                value["qqcall"]["audio"].update(retired)
                with self.subTest(retired=tuple(retired)), self.assertRaisesRegex(ConfigError, "audio_fields_invalid"):
                    load_config(self.write(directory, value))

            value = copy.deepcopy(DEFAULT_CONFIG)
            value["qqcall"]["universal_api"]["url"] = "https://example.com:8016"
            with self.assertRaisesRegex(ConfigError, "universal_api_url_must_be_loopback"):
                load_config(self.write(directory, value))
            value = copy.deepcopy(DEFAULT_CONFIG)
            value["qqcall"]["universal_api"]["url"] = "http://127.0.0.1:8016/?token=secret"
            with self.assertRaisesRegex(ConfigError, "universal_api_url_invalid"):
                load_config(self.write(directory, value))
            value = copy.deepcopy(DEFAULT_CONFIG)
            value["qqcall"]["universal_api"]["url"] = "http://127.0.0.1"
            self.assertEqual(
                load_config(self.write(directory, value))["universal_api"]["url"],
                "http://127.0.0.1",
            )
            value = copy.deepcopy(DEFAULT_CONFIG)
            value["qqcall"]["onebot"]["url"] = "https://example.com"
            with self.assertRaisesRegex(ConfigError, "onebot_url_must_be_loopback"):
                load_config(self.write(directory, value))

            value = copy.deepcopy(DEFAULT_CONFIG)
            value["qqcall"]["universal_api"]["token_file"] = "secrets/universal.token"
            normalized = load_config(self.write(directory, value))
            self.assertEqual(
                normalized["universal_api"]["token_file"],
                str((directory / "secrets" / "universal.token").resolve()),
            )
            self.assertFalse(normalized["universal_api_token_file_defaulted"])

    def test_schema5_migration_preserves_unrelated_values_and_comments_and_validates_schema6(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = copy.deepcopy(DEFAULT_CONFIG)
            value["schema"] = 5
            value["qqcall"].pop("universal_api")
            value["qqcall"]["vad"] = {
                "engine": "universalvc",
                "model_path": "../../../NachoBot-UniversalVC-Adapter/models/silero_vad.onnx",
            }
            original = _toml_document(value).decode("utf-8")
            # Exercise comments around schema/table replacement and TOML CRLF.
            original = original.replace("schema = 5\n", "schema = 5 # keep schema note\n# preserve root comment\n")
            original = original.replace(
                "[qqcall.vad]\n",
                "[qqcall.vad]\n# preserve VAD note\nengine = \"universalvc\" # inline VAD note\nmodel_path = \"../../../NachoBot-UniversalVC-Adapter/models/silero_vad.onnx\"\n",
            )
            original = original.replace(
                'engine = "universalvc"\nmodel_path = "../../../NachoBot-UniversalVC-Adapter/models/silero_vad.onnx"\n',
                "",
                1,
            )
            original = original.replace("\n", "\r\n")

            migrated = _MIGRATION.migrate_text(original)
            self.assertIn("schema = 6 # keep schema note\r\n", migrated)
            self.assertIn("# preserve root comment\r\n", migrated)
            self.assertIn("# preserve VAD note\r\n", migrated)
            self.assertIn("# inline VAD note\r\n", migrated)
            self.assertNotIn("model_path =", migrated)
            parsed_old = __import__("tomllib").loads(original)
            parsed_new = __import__("tomllib").loads(migrated)
            self.assertEqual(parsed_new["schema"], 6)
            self.assertEqual(parsed_new["qqcall"]["universal_api"], {
                "url": "http://127.0.0.1:8016", "token_file": "",
            })
            old_without_vad = copy.deepcopy(parsed_old)
            old_without_vad["schema"] = 6
            del old_without_vad["qqcall"]["vad"]
            new_without_api = copy.deepcopy(parsed_new)
            del new_without_api["qqcall"]["universal_api"]
            self.assertEqual(old_without_vad, new_without_api)

            path = Path(temporary) / "migrated.toml"
            path.write_text(migrated, encoding="utf-8", newline="")
            normalized = load_config(path)
            self.assertEqual(normalized["schema"], 6)
            self.assertEqual(
                normalized["universal_api"]["token_file"],
                str((Path(normalized["runtime_dir"]) / "universal-api.token.private").resolve()),
            )
            self.assertTrue(normalized["universal_api_token_file_defaulted"])

    def test_schema5_migration_rejects_unknown_fields_invalid_source_and_repeat(self) -> None:
        value = copy.deepcopy(DEFAULT_CONFIG)
        value["schema"] = 5
        value["qqcall"].pop("universal_api")
        value["qqcall"]["vad"] = {"engine": "universalvc", "model_path": "model.onnx"}
        source = _toml_document(value).decode("utf-8")
        migrated = _MIGRATION.migrate_text(source)
        with self.assertRaisesRegex(_MIGRATION.MigrationError, "source_schema_not_5"):
            _MIGRATION.migrate_text(migrated)
        value["qqcall"]["unknown"] = "not allowed"
        with self.assertRaisesRegex(_MIGRATION.MigrationError, "source_fields_invalid"):
            _MIGRATION.migrate_text(_toml_document(value).decode("utf-8"))
        value.pop("qqcall")
        value["qqcall"] = copy.deepcopy(DEFAULT_CONFIG["qqcall"])
        value["qqcall"].pop("universal_api")
        value["qqcall"]["vad"] = {"engine": "invalid", "model_path": "model.onnx"}
        with self.assertRaisesRegex(_MIGRATION.MigrationError, "source_fields_invalid"):
            _MIGRATION.migrate_text(_toml_document(value).decode("utf-8"))

    def test_schema5_migration_target_rejects_symlink_and_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.toml"
            source.write_text("schema = 5\n", encoding="utf-8")
            hardlink = directory / "hardlink.toml"
            try:
                hardlink.hardlink_to(source)
            except (OSError, NotImplementedError):
                self.skipTest("hard links are unavailable on this filesystem")
            with self.assertRaisesRegex(_MIGRATION.MigrationError, "config_target_unsupported"):
                _MIGRATION._validate_config_target(hardlink)
            symlink = directory / "symlink.toml"
            try:
                symlink.symlink_to(source)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable on this filesystem")
            with self.assertRaisesRegex(_MIGRATION.MigrationError, "config_target_unsupported"):
                _MIGRATION._validate_config_target(symlink)


    def test_core_settings_are_typed_bounded_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            value = copy.deepcopy(DEFAULT_CONFIG)
            value["qqcall"]["allow_planner_accept"] = True
            value["qqcall"]["group_intent_model_task"] = "utils"
            value["qqcall"]["group_intent_timeout_seconds"] = 30
            core = load_config(self.write(directory, value))["core"]
            self.assertEqual(core, {
                "allow_planner_accept": True,
                "group_intent_model_task": "utils",
                "group_intent_timeout_seconds": 30,
            })

            for key, invalid in (
                ("allow_planner_accept", 1),
                ("group_intent_model_task", ""),
                ("group_intent_timeout_seconds", 31),
                ("group_intent_timeout_seconds", True),
            ):
                bad = copy.deepcopy(DEFAULT_CONFIG)
                bad["qqcall"][key] = invalid
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ConfigError):
                    load_config(self.write(directory, bad))


if __name__ == "__main__":
    unittest.main()
