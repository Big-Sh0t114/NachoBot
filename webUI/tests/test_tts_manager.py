from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request
from unittest.mock import patch

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import tts_manager  # noqa: E402


class _Response:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit: int | None = None) -> bytes:
        return self.body


def _write_tts_config(root: Path, *, engine: str = "Vox") -> Path:
    base = root / "base.toml"
    base.write_text(
        '[server]\nhost = "127.0.0.1"\nport = 9880\n\n'
        f'[enabled_tts]\nenabled = ["{engine}"]\n',
        encoding="utf-8",
    )
    (root / ("vox.toml" if engine == "Vox" else "gpt-sovits.toml")).write_text(
        '[tts]\nhost = "127.0.0.1"\nport = 9880\n',
        encoding="utf-8",
    )
    return base


def _core_health(
    profile: str,
    *,
    perception_ready: bool = True,
    tts_ready: bool = True,
) -> dict:
    perception_required, tts_required = {
        "full": (True, True),
        "lite": (False, True),
        "potato": (False, False),
    }[profile]
    ready = (
        (not perception_required or perception_ready)
        and (not tts_required or tts_ready)
    )
    return {
        "status": "ok" if ready else "degraded",
        "desired_profile": profile,
        "capabilities": {"tts": profile != "potato"},
        "observed_local": {
            "profile": profile,
            "ready": ready,
            "perception": {"required": perception_required, "ready": perception_ready},
            "tts": {"required": tts_required, "ready": tts_ready},
        },
    }


class TTSManagerTests(unittest.TestCase):
    def test_core_resolution_and_token_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_path = root / ".env"
            bot_config = root / "bot_config.toml"
            env_path.write_text("HOST=0.0.0.0\nPORT=8000\n", encoding="utf-8")
            bot_config.write_text(
                '[ncnk_message]\nauth_token = ["config-secret", "other-secret"]\n',
                encoding="utf-8",
            )
            with (
                patch.object(tts_manager, "_NACHOBOT_ENV_PATH", env_path),
                patch.object(tts_manager, "_BOT_CONFIG_PATH", bot_config),
                patch.dict(os.environ, {"NACHOBOT_CORE_TOKEN": "env-secret"}),
            ):
                self.assertEqual(tts_manager._get_core_base_url(), "http://127.0.0.1:8000")
                self.assertEqual(tts_manager._get_core_auth_token(), "env-secret")

            with (
                patch.object(tts_manager, "_NACHOBOT_ENV_PATH", env_path),
                patch.object(tts_manager, "_BOT_CONFIG_PATH", bot_config),
                patch.dict(os.environ, {}, clear=False),
            ):
                os.environ.pop("NACHOBOT_CORE_TOKEN", None)
                self.assertEqual(tts_manager._get_core_auth_token(), "config-secret")

    def test_load_endpoints_does_not_use_legacy_multimodal_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _write_tts_config(Path(temp_dir))
            manager = tts_manager.TTSManager(config_path=config, cache_dir=Path(temp_dir) / "cache")
            with (
                patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"),
                patch.object(tts_manager, "_get_core_auth_token", return_value="secret"),
            ):
                endpoints = manager._load_endpoints()

        self.assertEqual(endpoints["core_url"], "http://127.0.0.1:8000")
        self.assertEqual(endpoints["runtime_port"], 9880)
        self.assertNotIn("engine_port", endpoints)
        self.assertNotIn("adapter_url", endpoints)

    def test_status_queries_core_and_reports_potato_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _write_tts_config(Path(temp_dir))
            manager = tts_manager.TTSManager(config_path=config, cache_dir=Path(temp_dir) / "cache")
            calls: list[tuple[str, str]] = []

            def request_json(url: str, _timeout: float, token: str = ""):
                calls.append((url, token))
                return _core_health("potato")

            with (
                patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"),
                patch.object(tts_manager, "_get_core_auth_token", return_value="core-secret"),
                patch.object(manager, "_request_json", side_effect=request_json),
                patch.object(manager, "_port_is_open", side_effect=AssertionError("potato must not probe TTS")),
            ):
                result = asyncio.run(manager.status())

        self.assertFalse(result["ready"])
        self.assertTrue(result["text_only"])
        self.assertIn("text-only", result["error"])
        self.assertEqual(calls, [("http://127.0.0.1:8000/api/multimodal/health", "core-secret")])

    def test_status_checks_core_and_selected_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _write_tts_config(Path(temp_dir))
            manager = tts_manager.TTSManager(config_path=config, cache_dir=Path(temp_dir) / "cache")
            with (
                patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"),
                patch.object(tts_manager, "_get_core_auth_token", return_value="core-secret"),
                patch.object(
                    manager,
                    "_request_json",
                    return_value=_core_health("lite"),
                ) as health,
                patch.object(
                    manager,
                    "_port_is_open",
                    side_effect=AssertionError("status must use Core observed 9880 readiness"),
                ) as engine,
            ):
                result = asyncio.run(manager.status())

        self.assertTrue(result["ready"])
        self.assertTrue(result["core_ready"])
        self.assertEqual(result["engine"], "Vox")
        self.assertEqual(tts_manager.CORE_HEALTH_TIMEOUT_SECONDS, 4.0)
        health.assert_called_once_with(
            "http://127.0.0.1:8000/api/multimodal/health",
            tts_manager.CORE_HEALTH_TIMEOUT_SECONDS,
            "core-secret",
        )
        engine.assert_not_called()

    def test_status_rejects_incomplete_or_inconsistent_core_health(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _write_tts_config(Path(temp_dir))
            manager = tts_manager.TTSManager(config_path=config, cache_dir=Path(temp_dir) / "cache")
            valid = _core_health("lite")
            malformed = (
                {"status": "ok", "desired_profile": "lite", "capabilities": {"tts": True}},
                {**valid, "observed_local": {**valid["observed_local"], "tts": {"required": True, "ready": "yes"}}},
                {**valid, "status": "degraded"},
                {**valid, "desired_profile": []},
            )
            for payload in malformed:
                with self.subTest(payload=payload), \
                     patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                     patch.object(tts_manager, "_get_core_auth_token", return_value="core-secret"), \
                     patch.object(manager, "_request_json", return_value=valid):
                    self.assertTrue(asyncio.run(manager.status())["ready"])
                with patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                     patch.object(tts_manager, "_get_core_auth_token", return_value="core-secret"), \
                     patch.object(manager, "_request_json", return_value=payload):
                    result = asyncio.run(manager.status())
                self.assertFalse(result["ready"])
                self.assertNotIn("transient", result)
                self.assertIsNone(manager._last_ready_status)

    def test_status_transport_grace_is_short_and_only_reuses_matching_ready_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _write_tts_config(root)
            manager = tts_manager.TTSManager(config_path=config, cache_dir=root / "cache")
            healthy = _core_health("lite")
            with patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                 patch.object(tts_manager, "_get_core_auth_token", return_value="core-secret"), \
                 patch.object(manager, "_request_json", return_value=healthy):
                first = asyncio.run(manager.status())
            self.assertTrue(first["ready"])

            with patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                 patch.object(tts_manager, "_get_core_auth_token", return_value="core-secret"), \
                 patch.object(manager, "_request_json", side_effect=URLError(TimeoutError("listener reconnecting"))):
                transient = asyncio.run(manager.status())
            self.assertTrue(transient["ready"])
            self.assertTrue(transient["transient"])
            self.assertEqual(transient["engine"], "Vox")

            manager._last_ready_status_at = time.monotonic() - tts_manager._TTS_STATUS_TRANSPORT_GRACE_SECONDS - 1
            with patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                 patch.object(tts_manager, "_get_core_auth_token", return_value="core-secret"), \
                 patch.object(manager, "_request_json", side_effect=TimeoutError("still unavailable")):
                expired = asyncio.run(manager.status())
            self.assertFalse(expired["ready"])
            self.assertTrue(expired["transient"])

    def test_status_transport_grace_never_masks_auth_or_missing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _write_tts_config(root)
            manager = tts_manager.TTSManager(config_path=config, cache_dir=root / "cache")
            healthy = _core_health("lite")

            def establish_ready():
                with patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                     patch.object(tts_manager, "_get_core_auth_token", return_value="core-secret"), \
                     patch.object(manager, "_request_json", return_value=healthy):
                    return asyncio.run(manager.status())

            self.assertTrue(establish_ready()["ready"])
            unauthorized = HTTPError(Request("http://127.0.0.1:8000/api/multimodal/health"), 401, "Unauthorized", {}, None)
            with patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                 patch.object(tts_manager, "_get_core_auth_token", return_value="core-secret"), \
                 patch.object(manager, "_request_json", side_effect=unauthorized):
                denied = asyncio.run(manager.status())
            self.assertFalse(denied["ready"])
            self.assertNotIn("transient", denied)
            self.assertIsNone(manager._last_ready_status)

            self.assertTrue(establish_ready()["ready"])
            (root / "vox.toml").unlink()
            with patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                 patch.object(tts_manager, "_get_core_auth_token", return_value="core-secret"), \
                 patch.object(manager, "_request_json", side_effect=TimeoutError("Core offline")):
                missing_config = asyncio.run(manager.status())
            self.assertFalse(missing_config["ready"])
            self.assertTrue(missing_config["transient"])

    def test_first_transport_failure_is_retryable_but_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _write_tts_config(root)
            manager = tts_manager.TTSManager(config_path=config, cache_dir=root / "cache")
            with patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"), \
                 patch.object(tts_manager, "_get_core_auth_token", return_value="core-secret"), \
                 patch.object(manager, "_request_json", side_effect=TimeoutError("Core starting")):
                result = asyncio.run(manager.status())

        self.assertFalse(result["ready"])
        self.assertTrue(result["transient"])
        self.assertIsNone(manager._last_ready_status)

    def test_full_remote_perception_fallback_does_not_disable_tts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _write_tts_config(Path(temp_dir))
            manager = tts_manager.TTSManager(config_path=config, cache_dir=Path(temp_dir) / "cache")
            with (
                patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"),
                patch.object(tts_manager, "_get_core_auth_token", return_value="core-secret"),
                patch.object(
                    manager,
                    "_request_json",
                    return_value=_core_health("full", perception_ready=False),
                ),
                patch.object(
                    manager,
                    "_port_is_open",
                    side_effect=AssertionError("status must use Core observed 9880 readiness"),
                ),
            ):
                result = asyncio.run(manager.status())

        self.assertTrue(result["ready"])
        self.assertTrue(result["core_ready"])
        self.assertTrue(result["adapter_ready"])

    def test_request_audio_parses_json_base64_and_sends_bearer(self) -> None:
        audio = b"RIFF-test-audio"
        captured = {}

        def open_request(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _Response(json.dumps({"audio_base64": base64.b64encode(audio).decode("ascii")}).encode())

        with patch.object(tts_manager, "urlopen", side_effect=open_request):
            result = tts_manager.TTSManager._request_audio(
                "http://127.0.0.1:8000/api/multimodal/tts", "hello", "core-secret"
            )

        self.assertEqual(result, audio)
        self.assertEqual(captured["authorization"], "Bearer core-secret")
        self.assertEqual(captured["body"], {"text": "hello", "platform": "webui"})
        self.assertEqual(captured["timeout"], 180)

    def test_request_audio_rejects_invalid_base64(self) -> None:
        with patch.object(
            tts_manager,
            "urlopen",
            return_value=_Response(b'{"audio_base64":"not-valid-base64!"}'),
        ):
            with self.assertRaises(tts_manager.TTSGenerationError):
                tts_manager.TTSManager._request_audio(
                    "http://127.0.0.1:8000/api/multimodal/tts", "hello"
                )

    def test_generate_preserves_cache_hit_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _write_tts_config(root)
            manager = tts_manager.TTSManager(config_path=config, cache_dir=root / "cache")
            with (
                patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"),
                patch.object(tts_manager, "_get_core_auth_token", return_value="secret"),
                patch.object(manager, "status", return_value={"ready": True}) as status,
                patch.object(manager, "_request_audio", return_value=b"RIFF-test") as request_audio,
            ):
                first_path, first_hit = asyncio.run(manager.generate("hello"))
                second_path, second_hit = asyncio.run(manager.generate("hello"))
                cached_audio = first_path.read_bytes()

        self.assertEqual(first_path, second_path)
        self.assertFalse(first_hit)
        self.assertTrue(second_hit)
        self.assertEqual(cached_audio, b"RIFF-test")
        self.assertEqual(status.await_count, 2)
        request_audio.assert_called_once_with(
            "http://127.0.0.1:8000/api/multimodal/tts", "hello", "secret"
        )

    def test_cached_audio_requires_current_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _write_tts_config(root)
            manager = tts_manager.TTSManager(config_path=config, cache_dir=root / "cache")
            with (
                patch.object(tts_manager, "_get_core_base_url", return_value="http://127.0.0.1:8000"),
                patch.object(tts_manager, "_get_core_auth_token", return_value="secret"),
                patch.object(
                    manager,
                    "status",
                    side_effect=(
                        {"ready": True},
                        {"ready": False, "text_only": True, "error": "TTS unavailable in text-only mode"},
                    ),
                ) as status,
                patch.object(manager, "_request_audio", return_value=b"RIFF-test") as request_audio,
            ):
                first_path, first_hit = asyncio.run(manager.generate("hello"))
                with self.assertRaisesRegex(
                    tts_manager.TTSUnavailableError,
                    "TTS unavailable in text-only mode",
                ):
                    asyncio.run(manager.generate("hello"))
                self.assertFalse(first_hit)
                self.assertTrue(first_path.exists())
                self.assertEqual(first_path.read_bytes(), b"RIFF-test")
                self.assertEqual(status.await_count, 2)
                request_audio.assert_called_once()


if __name__ == "__main__":
    unittest.main()
