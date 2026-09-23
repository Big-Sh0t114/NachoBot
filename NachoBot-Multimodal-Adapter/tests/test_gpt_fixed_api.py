import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nachobot_multimodal.utils import tts_resolver
from nachobot_multimodal.utils.tts_resolver import TTSResolution
from nachobot_multimodal.utils.tts_runtime import TTSRuntime


class _WeightModel:
    def __init__(self, fail_sovits=False):
        self.config = SimpleNamespace(
            tts=SimpleNamespace(
                models=SimpleNamespace(
                    presets={
                        "default": SimpleNamespace(gpt_model="old-gpt", sovits_model="old-sovits"),
                        "alt": SimpleNamespace(gpt_model="alt-gpt", sovits_model="alt-sovits"),
                    }
                )
            )
        )
        self._loaded_gpt_weights = "old-gpt"
        self._loaded_sovits_weights = "old-sovits"
        self.fail_sovits = fail_sovits
        self.calls = []

    def set_gpt_weights(self, path):
        self.calls.append(("gpt", path))
        self._loaded_gpt_weights = path

    def set_sovits_weights(self, path):
        self.calls.append(("sovits", path))
        if self.fail_sovits:
            raise RuntimeError("sovits setter failed")
        self._loaded_sovits_weights = path

    async def tts(self, **kwargs):
        self.calls.append(("infer", kwargs))
        return b"wav"


class _BlockingWeightModel(_WeightModel):
    started = None
    release = None

    def set_gpt_weights(self, path):
        type(self).started.set()
        type(self).release.wait(timeout=5)
        super().set_gpt_weights(path)


class _Request:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class _Runtime:
    def __init__(self, model):
        self.model = model
        self.invalidated = []

    @asynccontextmanager
    async def model_context(self):
        if self.model is None:
            raise RuntimeError("no model")
        yield self.model

    async def call_blocking(self, function, *args, **kwargs):
        return function(*args, **kwargs)

    def invalidate(self, error=None):
        self.invalidated.append(error)
        self.model = None


class FixedGptApiTests(unittest.TestCase):
    def test_fixed_resolver_ignores_base_edits_but_tracks_gpt_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "base.toml"
            base.write_text('[enabled_tts]\nenabled = ["Vox"]\n[server]\nhost = "one"\n', encoding="utf-8")
            gpt = root / "gpt-sovits.toml"
            gpt.write_text('[tts]\nhost = "gpt-one"\n', encoding="utf-8")
            with mock.patch.object(tts_resolver, "_import_tts_model", return_value=_WeightModel):
                first = tts_resolver.resolve_tts_model_snapshot(base, fixed_backend="GPT_Sovits")
                base.write_text('[enabled_tts]\nenabled = ["GPT_Sovits"]\n[server]\nhost = "two"\n', encoding="utf-8")
                base_edit = tts_resolver.resolve_tts_model_snapshot(base, fixed_backend="GPT_Sovits")
                gpt.write_text('[tts]\nhost = "gpt-two"\n', encoding="utf-8")
                gpt_edit = tts_resolver.resolve_tts_model_snapshot(base, fixed_backend="GPT_Sovits")
            self.assertEqual(first.fingerprint, base_edit.fingerprint)
            self.assertNotEqual(first.fingerprint, gpt_edit.fingerprint)

    def test_load_model_updates_all_presets_and_infer_uses_repaired_runtime(self):
        async def scenario():
            from nachobot_multimodal.tts.backends.GPT_Sovits import api_server

            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                gpt = root / "new.ckpt"
                sovits = root / "new.pth"
                gpt.write_bytes(b"gpt")
                sovits.write_bytes(b"sovits")
                model = _WeightModel()
                runtime = _Runtime(model)
                old_runtime = api_server._runtime
                old_model = api_server.tts_model
                old_output = api_server.OUTPUT_DIR
                api_server._runtime = runtime
                # Lifespan publishes the eagerly initialized fixed client.
                api_server.tts_model = model
                api_server.OUTPUT_DIR = root / "outputs"
                try:
                    result = await api_server.load_model(
                        _Request({"gpt_path": str(gpt), "sovits_path": str(sovits)})
                    )
                    self.assertEqual(result["status"], "ok")
                    for preset in model.config.tts.models.presets.values():
                        self.assertEqual(preset.gpt_model, str(gpt.resolve()))
                        self.assertEqual(preset.sovits_model, str(sovits.resolve()))

                    infer_result = await api_server.infer(
                        _Request({"text": "hello", "platform": "alt"})
                    )
                    self.assertEqual(infer_result["status"], "ok")
                    self.assertEqual(model.calls[-1][0], "infer")
                finally:
                    api_server._runtime = old_runtime
                    api_server.tts_model = old_model
                    api_server.OUTPUT_DIR = old_output

        asyncio.run(scenario())

    def test_partial_load_invalidates_runtime_before_next_infer(self):
        async def scenario():
            from nachobot_multimodal.tts.backends.GPT_Sovits import api_server

            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                gpt = root / "new.ckpt"
                sovits = root / "new.pth"
                gpt.write_bytes(b"gpt")
                sovits.write_bytes(b"sovits")
                failed = _WeightModel(fail_sovits=True)
                runtime = _Runtime(failed)
                old_runtime = api_server._runtime
                old_model = api_server.tts_model
                api_server._runtime = runtime
                api_server.tts_model = failed
                try:
                    result = await api_server.load_model(
                        _Request({"gpt_path": str(gpt), "sovits_path": str(sovits)})
                    )
                    self.assertEqual(result["status"], "error")
                    self.assertIsNone(runtime.model)
                    self.assertEqual([name for name, _ in failed.calls], ["gpt", "sovits"])
                    self.assertTrue(runtime.invalidated)
                finally:
                    api_server._runtime = old_runtime
                    api_server.tts_model = old_model

        asyncio.run(scenario())

    def test_cancelled_weight_transaction_is_drained_before_runtime_reuse(self):
        async def scenario():
            from nachobot_multimodal.tts.backends.GPT_Sovits import api_server

            _BlockingWeightModel.started = __import__("threading").Event()
            _BlockingWeightModel.release = __import__("threading").Event()
            snapshot = TTSResolution(
                _BlockingWeightModel,
                None,
                "fixed-gpt",
                "Custom",
                None,
                None,
            )
            with mock.patch(
                "nachobot_multimodal.utils.tts_runtime.resolve_tts_model_snapshot",
                return_value=snapshot,
            ):
                runtime = TTSRuntime()
                async with runtime.model_context() as model:
                    transaction = asyncio.create_task(
                        runtime.call_blocking(
                            api_server._apply_weight_transaction,
                            model,
                            "new-gpt",
                            "new-sovits",
                        )
                    )
                    await asyncio.to_thread(_BlockingWeightModel.started.wait, 2)
                    transaction.cancel()
                    await asyncio.sleep(0.05)
                    self.assertFalse(transaction.done())
                    _BlockingWeightModel.release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await transaction

                # The one transaction completed both setters before the
                # context released its serialization lock, so reuse observes
                # a coherent pair of weights.
                self.assertEqual([name for name, _ in model.calls], ["gpt", "sovits"])
                self.assertEqual(model._loaded_gpt_weights, "new-gpt")
                self.assertEqual(model._loaded_sovits_weights, "new-sovits")
                async with runtime.model_context() as reused:
                    self.assertIs(reused, model)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
