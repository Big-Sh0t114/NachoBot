import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "qr_login.py"
SPEC = importlib.util.spec_from_file_location("bilibili_qr_login", MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import contract
    raise RuntimeError(f"Unable to load {MODULE_PATH}")
qr_login = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qr_login)


class _FakeSession:
    def __init__(self) -> None:
        self.cookies = {
            "SESSDATA": "secret-sessdata",
            "bili_jct": "secret-csrf",
            "DedeUserID": "secret-user-id",
        }


class QrLoginOutputTests(unittest.TestCase):
    def test_success_output_never_contains_credential_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("[bilibili]\n", encoding="utf-8")
            output = io.StringIO()
            argv = [
                "qr_login.py",
                "--config",
                str(config_path),
                "--qr-output",
                str(Path(temp_dir) / "qr.png"),
            ]

            with (
                mock.patch.object(qr_login.requests, "Session", return_value=_FakeSession()),
                mock.patch.object(
                    qr_login,
                    "generate_qr",
                    return_value={"url": "https://example.test/qr", "qrcode_key": "key"},
                ),
                mock.patch.object(qr_login, "print_qr"),
                mock.patch.object(qr_login, "poll_login", return_value={"status": "success"}),
                mock.patch.object(
                    qr_login,
                    "fetch_buvid",
                    return_value={"buvid3": "secret-buvid3", "buvid4": "secret-buvid4"},
                ),
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(qr_login.main(), 0)

            rendered_output = output.getvalue()
            for secret in (
                "secret-sessdata",
                "secret-csrf",
                "secret-user-id",
                "secret-buvid3",
                "secret-buvid4",
            ):
                self.assertNotIn(secret, rendered_output)
                self.assertIn(secret, config_path.read_text(encoding="utf-8"))
            self.assertIn("values hidden", rendered_output)


if __name__ == "__main__":
    unittest.main()
