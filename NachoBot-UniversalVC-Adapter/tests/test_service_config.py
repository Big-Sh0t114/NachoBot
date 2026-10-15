from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from universalvc_service.service_config import ServiceConfigError, load_service_settings


class ServiceConfigTests(unittest.TestCase):
    def test_example_placeholder_token_fails_closed_and_env_override_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "service.toml"
            config.write_text(
                '[server]\nhost="127.0.0.1"\nport=8016\n'
                'token="REPLACE_WITH_A_RANDOM_SECRET"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ServiceConfigError, "replace server.token"):
                load_service_settings(config, environ={})
            settings = load_service_settings(
                config,
                environ={"NACHOBOT_UNIVERSALVC_TOKEN": "test-private-token"},
            )
        self.assertEqual(settings.token, "test-private-token")

    def test_service_refuses_non_loopback_bind_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "service.toml"
            config.write_text(
                '[server]\nhost="0.0.0.0"\nport=8016\ntoken="private-token"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ServiceConfigError, "loopback IP literal"):
                load_service_settings(config, environ={})


if __name__ == "__main__":
    unittest.main()
