import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tomlkit

from config import (
    _LEGACY_STOCK_TTS_CONTRACT,
    _PLAIN_TTS_CONTRACT,
    load_config,
)
from config_validation import ConfigValidationError, validate_config_mapping


class DiscordCoreRoutingTests(unittest.TestCase):
    def _write_config(
        self,
        root: Path,
        *,
        host: str = "localhost",
        port: int = 8000,
        version: int = 2,
    ) -> Path:
        path = root / "config.toml"
        path.write_text(
            f'config_version = {version}\n[identity]\nrequired = false\n'
            f'[discord]\ntoken = "test"\n[nachobot]\nhost = "{host}"\nport = {port}\n',
            encoding="utf-8",
        )
        return path

    def test_core_port_is_read_directly_without_rewriting_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write_config(Path(temp_dir))
            original = path.read_text(encoding="utf-8")

            config = load_config(path)

            self.assertEqual(config.nachobot.port, 8000)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_container_core_endpoint_overrides_loopback_in_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"NACHOBOT_CORE_HOST": "core", "NACHOBOT_CORE_PORT": "8000"},
        ):
            config = load_config(self._write_config(Path(temp_dir)))

            self.assertEqual((config.nachobot.host, config.nachobot.port), ("core", 8000))

    def test_schema_versions_two_and_three_are_accepted_but_bool_and_future_fail(self):
        for version in (2, 3):
            with self.subTest(version=version):
                document = tomlkit.parse(
                    f'config_version = {version}\n[discord]\ntoken = "fixture"\n'
                )
                validate_config_mapping(document)

        for raw_version in ("true", "4", "2.0", '"2"'):
            with self.subTest(version=raw_version):
                document = tomlkit.parse(
                    f'config_version = {raw_version}\n[discord]\ntoken = "fixture"\n'
                )
                with self.assertRaises(ConfigValidationError):
                    validate_config_mapping(document)

    def test_legacy_stock_contract_is_normalized_in_memory_without_rewriting_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "NachoBot-Discord-Adapter" / "config.toml"
            path.parent.mkdir(parents=True)
            prefix = "Personality: {identity}\nCustom section.\n"
            suffix = "\nKeep this context: {background_dialogue_prompt} {tool_info_block}"
            prompt = prefix + _LEGACY_STOCK_TTS_CONTRACT + suffix
            document = tomlkit.document()
            document["config_version"] = 2
            document["discord"] = {"token": "fixture-token"}
            document["prompts"] = {"replyer_prompt": prompt}
            path.write_text(tomlkit.dumps(document), encoding="utf-8")
            original = path.read_bytes()

            config = load_config(path)

            self.assertEqual(config.prompts.replyer_prompt, prefix + _PLAIN_TTS_CONTRACT + suffix)
            self.assertIn("Custom section.", config.prompts.replyer_prompt)
            self.assertIn("{identity}", config.prompts.replyer_prompt)
            self.assertIn("{tool_info_block}", config.prompts.replyer_prompt)
            self.assertEqual(path.read_bytes(), original)

    def test_custom_reply_prompt_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "NachoBot-Discord-Adapter" / "config.toml"
            path.parent.mkdir(parents=True)
            prompt = "Custom voice style: {identity}. Keep the JSON example {x: 2}."
            document = tomlkit.document()
            document["config_version"] = 2
            document["discord"] = {"token": "fixture-token"}
            document["prompts"] = {"replyer_prompt": prompt}
            path.write_text(tomlkit.dumps(document), encoding="utf-8")

            config = load_config(path)

            self.assertEqual(config.prompts.replyer_prompt, prompt)

    def test_discord_capture_rejects_a_wav_sample_rate_that_does_not_match_pcm(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write_config(Path(temp_dir))
            path.write_text(
                path.read_text(encoding="utf-8")
                + '\n[voice]\nsample_rate = 16000\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "48000"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
