import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nachobot_multimodal.utils import tts_resolver
from nachobot_multimodal.utils import emotion_resolver
from nachobot_multimodal.utils.tts_resolver import TTSResolution, resolve_tts_model_snapshot
from nachobot_multimodal.utils.tts_runtime import TTSRuntime


class _FakeModel:
    created = 0

    def __init__(self, config_path=None):
        type(self).created += 1
        self.config_path = config_path


class _FailOnceModel:
    attempts = 0

    def __init__(self, config_path=None):
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise RuntimeError("temporary constructor failure")


class _BlockingModel:
    started = None
    release = None

    def __init__(self, config_path=None):
        type(self).started.set()
        type(self).release.wait(timeout=5)


class _HTTPResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def read(self):
        return b"wav"


class _HTTPClientSession:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params=None, **kwargs):
        type(self).calls.append((url, dict(params or {})))
        return _HTTPResponse()


class _RequestsResponse:
    status_code = 200

    @staticmethod
    def json():
        return {}


class _StreamContent:
    def __init__(self, chunks, *, hold=None, waiting=None):
        self.chunks = list(chunks)
        self.hold = hold
        self.waiting = waiting
        self.chunk_size = None

    async def iter_chunked(self, chunk_size):
        self.chunk_size = chunk_size
        for chunk in self.chunks:
            yield chunk
        if self.hold is not None:
            if self.waiting is not None:
                self.waiting.set()
            await self.hold.wait()


class _StreamResponse:
    status = 200

    def __init__(self, events, chunks, *, close_gate=None, content_hold=None, content_waiting=None):
        self.events = events
        self.content = _StreamContent(
            chunks,
            hold=content_hold,
            waiting=content_waiting,
        )
        self.close_gate = close_gate

    async def __aenter__(self):
        self.events.append("response_enter")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.events.append("response_exit")
        return False

    def raise_for_status(self):
        return None

    async def read(self):
        return b"complete"

    async def json(self):
        return {}


class _StreamSession:
    queue = []
    events = []
    calls = []

    def __init__(self, *args, **kwargs):
        self.response = type(self).queue.pop(0)

    async def __aenter__(self):
        type(self).events.append(f"session{len(type(self).calls) + 1}_enter")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        type(self).events.append(f"session{len(type(self).calls)}_exit")
        return False

    def get(self, url, params=None, **kwargs):
        type(self).calls.append((url, dict(params or {})))

        class RequestContext:
            async def __aenter__(inner_self):
                return await self.response.__aenter__()

            async def __aexit__(inner_self, exc_type, exc, tb):
                return await self.response.__aexit__(exc_type, exc, tb)

        return RequestContext()


def _snapshot(model_class, fingerprint, plugin="Custom"):
    return TTSResolution(model_class, None, fingerprint, plugin, None, None)


class TTSRuntimeTests(unittest.TestCase):
    def test_content_change_replaces_and_unchanged_reuses(self):
        _FakeModel.created = 0
        current = [_snapshot(_FakeModel, "one")]
        with mock.patch(
            "nachobot_multimodal.utils.tts_runtime.resolve_tts_model_snapshot",
            side_effect=lambda *a, **k: current[0],
        ):
            runtime = TTSRuntime()
            self.assertTrue(runtime.ensure_tts_model())
            first = runtime.model
            self.assertTrue(runtime.ensure_tts_model())
            self.assertIs(runtime.model, first)
            current[0] = _snapshot(_FakeModel, "two")
            self.assertTrue(runtime.ensure_tts_model())
            self.assertIsNot(runtime.model, first)
            self.assertEqual(_FakeModel.created, 2)

    def test_failed_constructor_retries_same_fingerprint(self):
        _FailOnceModel.attempts = 0
        snapshot = _snapshot(_FailOnceModel, "retry")
        with mock.patch(
            "nachobot_multimodal.utils.tts_runtime.resolve_tts_model_snapshot",
            return_value=snapshot,
        ):
            runtime = TTSRuntime()
            self.assertFalse(runtime.ensure_tts_model())
            self.assertIsNone(runtime.model)
            self.assertTrue(runtime.ensure_tts_model())
            self.assertIsInstance(runtime.model, _FailOnceModel)

    def test_invalid_resolution_clears_stale_model(self):
        current = [_snapshot(_FakeModel, "good")]
        with mock.patch(
            "nachobot_multimodal.utils.tts_runtime.resolve_tts_model_snapshot",
            side_effect=lambda *a, **k: current[0],
        ):
            runtime = TTSRuntime()
            self.assertTrue(runtime.ensure_tts_model())
            current[0] = TTSResolution(None, "conflicting TTS backends", "bad", None, None, None)
            self.assertFalse(runtime.ensure_tts_model())
            self.assertIsNone(runtime.model)
            self.assertIn("conflicting", runtime.error)

    def test_cancelled_refresh_drains_constructor_before_lock_release(self):
        async def scenario():
            _BlockingModel.started = threading.Event()
            _BlockingModel.release = threading.Event()
            snapshot = _snapshot(_BlockingModel, "blocked")
            with mock.patch(
                "nachobot_multimodal.utils.tts_runtime.resolve_tts_model_snapshot",
                return_value=snapshot,
            ):
                runtime = TTSRuntime()

                async def use_runtime():
                    async with runtime.model_context():
                        return True

                task = asyncio.create_task(use_runtime())
                await asyncio.to_thread(_BlockingModel.started.wait, 2)
                task.cancel()
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())

                second = asyncio.create_task(use_runtime())
                await asyncio.sleep(0.05)
                self.assertFalse(second.done())
                _BlockingModel.release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(await second)

        asyncio.run(scenario())

    def test_status_does_not_wait_behind_blocking_constructor(self):
        async def scenario():
            _BlockingModel.started = threading.Event()
            _BlockingModel.release = threading.Event()
            snapshot = _snapshot(_BlockingModel, "health")
            with mock.patch(
                "nachobot_multimodal.utils.tts_runtime.resolve_tts_model_snapshot",
                return_value=snapshot,
            ):
                runtime = TTSRuntime()

                async def use_runtime():
                    async with runtime.model_context():
                        return True

                task = asyncio.create_task(use_runtime())
                await asyncio.to_thread(_BlockingModel.started.wait, 2)
                started = time.perf_counter()
                self.assertFalse(runtime.status()["ready"])
                self.assertLess(time.perf_counter() - started, 0.1)
                _BlockingModel.release.set()
                self.assertTrue(await task)

        asyncio.run(scenario())


class ResolverTests(unittest.TestCase):
    def test_resolver_uses_supplied_base_path_and_selected_content_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_path = root / "custom-base.toml"
            base_path.write_text(
                '[enabled_tts]\nenabled = ["Vox"]\n[server]\nhost = "one"\nport = 1\n',
                encoding="utf-8",
            )
            vox_path = root / "vox.toml"
            vox_path.write_text('[tts]\nhost = "one"\n', encoding="utf-8")
            gpt_path = root / "gpt-sovits.toml"
            gpt_path.write_text('[tts]\nhost = "gpt-one"\n', encoding="utf-8")
            with mock.patch.object(tts_resolver, "_import_tts_model", return_value=_FakeModel):
                first = resolve_tts_model_snapshot(base_path)
                gpt_path.write_text('[tts]\nhost = "gpt-two"\n', encoding="utf-8")
                inactive_edit = resolve_tts_model_snapshot(base_path)
                vox_path.write_text('[tts]\nhost = "two"\n', encoding="utf-8")
                active_edit = resolve_tts_model_snapshot(base_path)
            self.assertEqual(first.base_config_path, base_path)
            self.assertEqual(first.backend_config_path, vox_path)
            self.assertEqual(first.fingerprint, inactive_edit.fingerprint)
            self.assertNotEqual(first.fingerprint, active_edit.fingerprint)

    def test_emotion_url_refreshes_and_retries_after_invalid_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_path = Path(temp_dir) / "base.toml"
            original = '[server]\nhost = "one"\nport = 8070\n'
            base_path.write_text(original, encoding="utf-8")
            emotion_resolver._cache_initialized = False
            emotion_resolver._cached_base_fingerprint = None
            emotion_resolver._cached_base_path = None
            emotion_resolver._cached_emotion_api_url = None
            self.assertEqual(
                emotion_resolver._get_emotion_api_url(base_path),
                "http://one:8070/api/emotion_preset",
            )
            base_path.write_text('[server]\nhost = "two"\nport = 8070\n', encoding="utf-8")
            self.assertEqual(
                emotion_resolver._get_emotion_api_url(base_path),
                "http://two:8070/api/emotion_preset",
            )
            base_path.write_text("[server\n", encoding="utf-8")
            self.assertIsNone(emotion_resolver._get_emotion_api_url(base_path))
            base_path.write_text(original, encoding="utf-8")
            self.assertEqual(
                emotion_resolver._get_emotion_api_url(base_path),
                "http://one:8070/api/emotion_preset",
            )

    def test_selected_config_edit_with_same_size_and_mtime_refreshes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_path = root / "base.toml"
            base_path.write_text('[enabled_tts]\nenabled = ["Vox"]\n', encoding="utf-8")
            vox_path = root / "vox.toml"
            first = '[tts]\nhost = "a"\n'
            second = '[tts]\nhost = "b"\n'
            self.assertEqual(len(first), len(second))
            vox_path.write_text(first, encoding="utf-8")
            original_stat = vox_path.stat()
            with mock.patch.object(tts_resolver, "_import_tts_model", return_value=_FakeModel):
                before = resolve_tts_model_snapshot(base_path)
                vox_path.write_text(second, encoding="utf-8")
                # Keep both observable timestamp and file size unchanged.
                import os

                os.utime(vox_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                after = resolve_tts_model_snapshot(base_path)
            self.assertNotEqual(before.fingerprint, after.fingerprint)

    def test_invalid_conflict_missing_and_repaired_selection_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_path = root / "base.toml"
            vox_path = root / "vox.toml"
            gpt_path = root / "gpt-sovits.toml"
            vox_path.write_text("[tts]\nhost = \"vox\"\n", encoding="utf-8")
            gpt_path.write_text("[tts]\nhost = \"gpt\"\n", encoding="utf-8")
            cases = [
                ("[enabled_tts]\nenabled = []\n", "No TTS plugins"),
                ("[enabled_tts]\nenabled = [\"Vox\", \"GPT_Sovits\"]\n", "Both GPT"),
            ]
            with mock.patch.object(tts_resolver, "_import_tts_model", return_value=_FakeModel):
                for content, expected in cases:
                    base_path.write_text(content, encoding="utf-8")
                    snapshot = resolve_tts_model_snapshot(base_path)
                    self.assertIsNone(snapshot.model_class)
                    self.assertIn(expected, snapshot.error)

                base_path.write_text('[enabled_tts]\nenabled = ["Vox"]\n', encoding="utf-8")
                vox_path.unlink()
                missing = resolve_tts_model_snapshot(base_path)
                self.assertIsNone(missing.model_class)
                self.assertIn("not found", missing.error)

                vox_path.write_text("[tts]\nhost = \"vox-repaired\"\n", encoding="utf-8")
                repaired = resolve_tts_model_snapshot(base_path)
                self.assertIs(repaired.model_class, _FakeModel)
                self.assertIsNone(repaired.error)


class BackendParameterTests(unittest.TestCase):
    def _write_backend_configs(self, root: Path, enabled: str) -> Path:
        base_path = root / "base.toml"
        base_path.write_text(
            f'[enabled_tts]\nenabled = ["{enabled}"]\n'
            '[server]\nhost = "emotion-host"\nport = 8070\n',
            encoding="utf-8",
        )
        (root / "vox.toml").write_text(
            "\n".join(
                [
                    "[tts]",
                    'host = "vox-host"',
                    "port = 9880",
                    'model_dir = ""',
                    "cfg_value = 3.0",
                    "inference_timesteps = 10",
                    "normalize = false",
                    "seed = 0",
                    'split_method = "cut3"',
                    "max_split_length = 80",
                    "segment_gap_ms = 100",
                    "[tts.models.presets.default]",
                    'name = "default"',
                    'ref_audio_path = "voice.wav"',
                    'control_instruction = ""',
                    'prompt_text = "prompt"',
                    "cfg_value = 3.0",
                    "inference_timesteps = 10",
                    "normalize = false",
                    "seed = 0",
                    "[pipeline]",
                    'default_preset = "default"',
                    '[pipeline.platform_presets]\nwebui = "default"',
                    "[emotion]\nenabled = false",
                ]
            ),
            encoding="utf-8",
        )
        (root / "gpt-sovits.toml").write_text(
            "\n".join(
                [
                    "[tts]",
                    'host = "gpt-host"',
                    "port = 9881",
                    "top_k = 12",
                    "top_p = 1.0",
                    "temperature = 1.0",
                    "batch_size = 1",
                    "batch_threshold = 0.75",
                    'text_split_method = "cut5"',
                    "repetition_penalty = 1.35",
                    "sample_steps = 32",
                    "super_sampling = false",
                    "[tts.models.presets.default]",
                    'name = "default"',
                    'gpt_model = "gpt.ckpt"',
                    'sovits_model = "sovits.pth"',
                    'ref_audio_path = "voice.wav"',
                    "aux_ref_audio_paths = []",
                    'prompt_text = "prompt"',
                    'text_language = "auto"',
                    'prompt_language = "ja"',
                    "speed_factor = 1.0",
                    "[pipeline]",
                    'default_preset = "default"',
                    '[pipeline.platform_presets]\nwebui = "default"',
                ]
            ),
            encoding="utf-8",
        )
        return base_path

    def test_real_runtime_switches_vox_gpt_vox_and_captures_backend_fields(self):
        async def scenario():
            from nachobot_multimodal.tts.backends.GPT_Sovits import tts_model as gpt_module
            from nachobot_multimodal.tts.backends.Vox import tts_model as vox_module

            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                base_path = self._write_backend_configs(root, "Vox")
                _HTTPClientSession.calls = []
                with mock.patch.object(vox_module.aiohttp, "ClientSession", _HTTPClientSession), mock.patch.object(
                    gpt_module.aiohttp, "ClientSession", _HTTPClientSession
                ), mock.patch.object(gpt_module.requests, "get", return_value=_RequestsResponse()):
                    runtime = TTSRuntime(config_dir=base_path)

                    self.assertTrue(runtime.ensure_tts_model())
                    vox = runtime.model
                    with mock.patch.object(
                        vox,
                        "_resolve_emotion_preset_remote",
                        new=mock.AsyncMock(side_effect=AssertionError("remote emotion must be skipped")),
                    ):
                        await vox.tts(
                            text="vox text",
                            platform="webui",
                            text_lang="zh",
                            preset_name=None,
                            skip_remote_emotion=True,
                        )

                    base_path.write_text('[enabled_tts]\nenabled = ["GPT_Sovits"]\n[server]\nhost = "emotion-host"\nport = 8070\n', encoding="utf-8")
                    self.assertTrue(runtime.ensure_tts_model())
                    gpt = runtime.model
                    await gpt.tts(
                        text="gpt text",
                        platform="webui",
                        text_lang="zh",
                        prompt_lang="ja",
                    )

                    base_path.write_text('[enabled_tts]\nenabled = ["Vox"]\n[server]\nhost = "emotion-host"\nport = 8070\n', encoding="utf-8")
                    self.assertTrue(runtime.ensure_tts_model())
                    vox_again = runtime.model
                    await vox_again.tts(
                        text="vox again",
                        platform="webui",
                        text_lang="zh",
                        preset_name="default",
                        skip_remote_emotion=True,
                    )

                self.assertIsNot(vox, gpt)
                self.assertIsNot(gpt, vox_again)
                self.assertEqual([call[0] for call in _HTTPClientSession.calls], [
                    "http://vox-host:9880/tts",
                    "http://gpt-host:9881/tts",
                    "http://vox-host:9880/tts",
                ])
                vox_params, gpt_params, vox_again_params = [call[1] for call in _HTTPClientSession.calls]
                self.assertIn("reference_wav_path", vox_params)
                self.assertIn("cfg_value", vox_params)
                self.assertNotIn("ref_audio_path", vox_params)
                self.assertNotIn("prompt_lang", vox_params)
                self.assertIn("ref_audio_path", gpt_params)
                self.assertIn("prompt_lang", gpt_params)
                self.assertNotIn("reference_wav_path", gpt_params)
                self.assertNotIn("cfg_value", gpt_params)
                self.assertIn("reference_wav_path", vox_again_params)
                self.assertNotIn("ref_audio_path", vox_again_params)

        asyncio.run(scenario())

    def test_real_gpt_stream_uses_selected_preset_and_closes_async_resources(self):
        async def scenario():
            from nachobot_multimodal.tts.backends.GPT_Sovits import tts_model as gpt_module

            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                base_path = self._write_backend_configs(root, "GPT_Sovits")
                _StreamSession.events = []
                _StreamSession.calls = []
                _StreamSession.queue = [_StreamResponse(_StreamSession.events, [b"one", b"two"])]
                with mock.patch.object(gpt_module.aiohttp, "ClientSession", _StreamSession), mock.patch.object(
                    gpt_module.requests, "get", return_value=_RequestsResponse()
                ):
                    runtime = TTSRuntime(config_dir=base_path)
                    self.assertTrue(runtime.ensure_tts_model())
                    model = runtime.model
                    stream = model.tts_stream(
                        text="stream text",
                        platform="webui",
                        text_lang="zh",
                        prompt_lang="ja",
                    )
                    chunks = [chunk async for chunk in stream]

                self.assertEqual(chunks, [b"one", b"two"])
                self.assertEqual(_StreamSession.events, [
                    "session1_enter",
                    "response_enter",
                    "response_exit",
                    "session1_exit",
                ])
                params = _StreamSession.calls[0][1]
                self.assertEqual(params["streaming_mode"], "True")
                self.assertEqual(params["prompt_lang"], "ja")
                self.assertIn("ref_audio_path", params)
                self.assertNotIn("cfg_value", params)
                self.assertNotIn("reference_wav_path", params)

        asyncio.run(scenario())

    def test_cancelled_real_gpt_stream_closes_before_next_runtime_synthesis(self):
        async def scenario():
            from nachobot_multimodal.tts.backends.GPT_Sovits import tts_model as gpt_module

            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                base_path = self._write_backend_configs(root, "GPT_Sovits")
                close_gate = asyncio.Event()
                hold_owner = asyncio.Event()
                _StreamSession.events = []
                _StreamSession.calls = []
                _StreamSession.queue = [
                    _StreamResponse(_StreamSession.events, [b"first"], close_gate=close_gate),
                    _StreamResponse(_StreamSession.events, []),
                ]
                with mock.patch.object(gpt_module.aiohttp, "ClientSession", _StreamSession), mock.patch.object(
                    gpt_module.requests, "get", return_value=_RequestsResponse()
                ):
                    runtime = TTSRuntime(config_dir=base_path)
                    self.assertTrue(runtime.ensure_tts_model())

                    async def consume_stream():
                        async with runtime.model_context() as model:
                            stream = model.tts_stream(
                                text="stream text",
                                platform="webui",
                                text_lang="zh",
                                prompt_lang="ja",
                            )
                            try:
                                self.assertEqual(await stream.__anext__(), b"first")
                                await hold_owner.wait()
                            finally:
                                await runtime.close_stream(stream)

                    first_task = None
                    second_task = None
                    try:
                        first_task = asyncio.create_task(consume_stream())
                        for _ in range(100):
                            if "response_enter" in _StreamSession.events:
                                break
                            await asyncio.sleep(0.01)
                        self.assertIn("response_enter", _StreamSession.events)

                        second_task = asyncio.create_task(
                            runtime.synthesize("next", platform="webui")
                        )
                        await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
                        self.assertFalse(second_task.done())

                        first_task.cancel()
                        await asyncio.sleep(0.05)
                        self.assertFalse(first_task.done())
                        self.assertFalse(second_task.done())
                        self.assertNotIn("response_exit", _StreamSession.events)

                        close_gate.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await first_task
                        self.assertEqual(await second_task, b"complete")
                    finally:
                        close_gate.set()
                        pending = [
                            task
                            for task in (first_task, second_task)
                            if task is not None and not task.done()
                        ]
                        for task in pending:
                            task.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)

                events = _StreamSession.events
                self.assertLess(events.index("response_exit"), events.index("session1_exit"))
                self.assertLess(events.index("session1_exit"), events.index("session2_enter"))
                self.assertEqual(_StreamSession.calls[0][1]["streaming_mode"], "True")

        asyncio.run(scenario())

    def test_cancelled_gpt_stream_waiting_for_next_http_chunk_is_nonblocking(self):
        async def scenario():
            from nachobot_multimodal.tts.backends.GPT_Sovits import tts_model as gpt_module

            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                base_path = self._write_backend_configs(root, "GPT_Sovits")
                chunk_gate = asyncio.Event()
                chunk_waiting = asyncio.Event()
                _StreamSession.events = []
                _StreamSession.calls = []
                _StreamSession.queue = [
                    _StreamResponse(
                        _StreamSession.events,
                        [b"first"],
                        content_hold=chunk_gate,
                        content_waiting=chunk_waiting,
                    ),
                    _StreamResponse(_StreamSession.events, []),
                ]
                with mock.patch.object(gpt_module.aiohttp, "ClientSession", _StreamSession), mock.patch.object(
                    gpt_module.requests, "get", return_value=_RequestsResponse()
                ):
                    runtime = TTSRuntime(config_dir=base_path)
                    self.assertTrue(runtime.ensure_tts_model())

                    async def consume_until_cancelled():
                        async with runtime.model_context() as model:
                            stream = model.tts_stream(
                                text="stream text",
                                platform="webui",
                                text_lang="zh",
                                prompt_lang="ja",
                            )
                            try:
                                self.assertEqual(await stream.__anext__(), b"first")
                                await stream.__anext__()
                            finally:
                                await runtime.close_stream(stream)

                    first_task = asyncio.create_task(consume_until_cancelled())
                    await asyncio.wait_for(chunk_waiting.wait(), timeout=1.0)
                    second_task = asyncio.create_task(
                        runtime.synthesize("next", platform="webui")
                    )
                    await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
                    self.assertFalse(second_task.done())

                    # A pending HTTP read must yield to the event loop and be
                    # cancellable; the runtime lock remains held until cleanup.
                    first_task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(first_task, timeout=1.0)
                    self.assertEqual(await second_task, b"complete")

                events = _StreamSession.events
                self.assertLess(events.index("response_exit"), events.index("session1_exit"))
                self.assertLess(events.index("session1_exit"), events.index("session2_enter"))

        asyncio.run(scenario())

    def test_vox_and_gpt_parameter_names_are_disjoint(self):
        from nachobot_multimodal.tts.backends.GPT_Sovits.tts_config import TTSBaseConfig
        from nachobot_multimodal.tts.backends.GPT_Sovits.tts_model import TTSModel as GPTModel
        from nachobot_multimodal.tts.backends.Vox.tts_model import TTSModel as VoxModel

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vox_path = root / "vox.toml"
            vox_path.write_text(
                "\n".join(
                    [
                        '[pipeline]',
                        'default_preset = "default"',
                        '[pipeline.platform_presets]',
                        'test = "default"',
                        '[tts]',
                        'host = "vox-host"',
                        'port = 9880',
                        'model_dir = ""',
                        'cfg_value = 3.0',
                        'inference_timesteps = 10',
                        'normalize = false',
                        'seed = 0',
                        'split_method = "cut3"',
                        'max_split_length = 80',
                        'segment_gap_ms = 100',
                        '[tts.models.presets.default]',
                        'name = "default"',
                        'ref_audio_path = "voice.wav"',
                        'control_instruction = ""',
                        'prompt_text = "prompt"',
                        'cfg_value = 3.0',
                        'inference_timesteps = 10',
                        'normalize = false',
                        'seed = 0',
                    ]
                ),
                encoding="utf-8",
            )
            vox = VoxModel(config_path=vox_path)
            vox_params = vox.build_parameters("hello", vox.get_preset("default"), split_method="cut0")
            self.assertIn("reference_wav_path", vox_params)
            self.assertIn("cfg_value", vox_params)
            self.assertNotIn("ref_audio_path", vox_params)
            self.assertNotIn("prompt_lang", vox_params)

            gpt_path = root / "gpt-sovits.toml"
            gpt_path.write_text(
                "\n".join(
                    [
                        '[pipeline]',
                        'default_preset = "default"',
                        '[pipeline.platform_presets]',
                        'test = "default"',
                        '[tts]',
                        'host = "gpt-host"',
                        'port = 9881',
                        'top_k = 12',
                        'top_p = 1.0',
                        'temperature = 1.0',
                        'batch_size = 1',
                        'batch_threshold = 0.75',
                        'text_split_method = "cut5"',
                        'repetition_penalty = 1.35',
                        'sample_steps = 32',
                        'super_sampling = false',
                        '[tts.models.presets.default]',
                        'name = "default"',
                        'gpt_model = "gpt.ckpt"',
                        'sovits_model = "sovits.pth"',
                        'ref_audio_path = "voice.wav"',
                        'aux_ref_audio_paths = []',
                        'prompt_text = "prompt"',
                        'text_language = "auto"',
                        'prompt_language = "ja"',
                        'speed_factor = 1.0',
                    ]
                ),
                encoding="utf-8",
            )
            gpt = GPTModel.__new__(GPTModel)
            gpt.config = TTSBaseConfig(str(gpt_path))
            gpt._config_dir = root.resolve()
            gpt._ref_audio_path = str(root / "voice.wav")
            gpt._prompt_text = "prompt"
            gpt._initialized = True
            gpt_params = gpt.build_parameters(
                "hello",
                text_lang="zh",
                prompt_lang="ja",
                preset_name="default",
            )
            self.assertIn("ref_audio_path", gpt_params)
            self.assertIn("prompt_lang", gpt_params)
            self.assertNotIn("cfg_value", gpt_params)
            self.assertNotIn("reference_wav_path", gpt_params)


if __name__ == "__main__":
    unittest.main()
