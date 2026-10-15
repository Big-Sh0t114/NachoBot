from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import httpx
import toml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_numpy_stub = types.ModuleType("numpy")
_numpy_stub.ndarray = object
_torch_stub = types.ModuleType("torch")
_torch_stub.set_float32_matmul_precision = lambda _precision: None
with patch.dict(sys.modules, {"numpy": _numpy_stub, "torch": _torch_stub}):
    from src.tts.backends.Vox import vox_api_server  # noqa: E402


class VoxPathSecurityTests(unittest.TestCase):
    def test_configured_external_audio_directories_are_allowed_and_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            configured_audio_dir = root / "external-voice-library"
            other_dir = root / "private"
            config_dir.mkdir()
            configured_audio_dir.mkdir()
            other_dir.mkdir()
            audio_file = configured_audio_dir / "reference.wav"
            other_file = other_dir / "private.wav"
            audio_file.write_bytes(b"audio")
            other_file.write_bytes(b"private")
            config_path = config_dir / "vox.toml"
            config_path.write_text(
                toml.dumps(
                    {
                        "tts": {
                            "models": {
                                "presets": {
                                    "default": {
                                        "ref_audio_path": str(audio_file),
                                        "prompt_wav_path": str(configured_audio_dir / "prompt.wav"),
                                    }
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            roots = vox_api_server._configured_audio_roots(config_path)
            self.assertEqual(roots, (configured_audio_dir.resolve(),))
            self.assertEqual(
                vox_api_server._resolve_optional_audio_path(str(audio_file), "reference", roots),
                str(audio_file.resolve()),
            )
            self.assertIsNone(
                vox_api_server._resolve_optional_audio_path(str(other_file), "reference", roots)
            )
            self.assertIsNone(
                vox_api_server._resolve_optional_audio_path(
                    str(configured_audio_dir / ".." / "external-voice-library" / "reference.wav"),
                    "reference",
                    roots,
                )
            )

    def test_configured_audio_root_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            target = outside / "private.wav"
            target.write_bytes(b"private")
            config_path = root / "vox.toml"
            config_path.write_text(
                '[tts.models.presets.default]\nref_audio_path = "allowed/reference.wav"\n',
                encoding="utf-8",
            )
            link = allowed / "reference.wav"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("file symlinks are unavailable on this host")
            roots = vox_api_server._configured_audio_roots(config_path)
            self.assertIsNone(
                vox_api_server._resolve_optional_audio_path(str(link), "reference", roots)
            )

    def test_tts_ingress_rejects_unbounded_delay_and_inference_ranges(self):
        async def request(path: str, **params):
            transport = httpx.ASGITransport(app=vox_api_server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(path, params={"text": "hello", **params})

        async def scenario():
            for path in ("/tts", "/tts_stream"):
                too_much_silence = await request(path, segment_gap_ms=100_000)
                too_many_steps = await request(path, inference_timesteps=100_000)
                self.assertEqual(too_much_silence.status_code, 422)
                self.assertEqual(too_many_steps.status_code, 422)

        asyncio.run(scenario())

    def test_tts_ingress_accepts_current_defaults_and_documented_numeric_ceiling(self):
        async def request(path: str, **params):
            transport = httpx.ASGITransport(app=vox_api_server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(path, params={"text": "hello", **params})

        async def scenario():
            for path in ("/tts", "/tts_stream"):
                default = await request(path)
                configured = await request(
                    path,
                    cfg_value=3.0,
                    inference_timesteps=10,
                    max_split_length=80,
                    segment_gap_ms=100,
                )
                capped = await request(
                    path,
                    cfg_value=20,
                    inference_timesteps=100,
                    max_split_length=2000,
                    segment_gap_ms=2000,
                )
                bounded_text = await request(
                    path,
                    text="x" * 10_000,
                    control_instruction="x" * 10_000,
                    prompt_text="x" * 10_000,
                    reference_wav_path="x" * 4096,
                    prompt_wav_path="x" * 4096,
                )
                # Inputs pass FastAPI validation; the raw engine is unloaded.
                self.assertEqual(default.status_code, 503)
                self.assertEqual(configured.status_code, 503)
                self.assertEqual(capped.status_code, 503)
                self.assertEqual(bounded_text.status_code, 503)

                over_text = await request(path, text="x" * 10_001)
                over_prompt = await request(path, prompt_text="x" * 10_001)
                over_path = await request(path, reference_wav_path="x" * 4097)
                self.assertEqual(over_text.status_code, 422)
                self.assertEqual(over_prompt.status_code, 422)
                self.assertEqual(over_path.status_code, 422)

        asyncio.run(scenario())
