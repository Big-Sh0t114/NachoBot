import json
import importlib.util
import sys
import tempfile
import struct
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nachobot_multimodal.utils.voxcpm_loading import (
    _cast_initialized_modules, _defer_checkpoint_parameters, _stream_safetensors, _windows_worker_compile,
    load_pretrained_on_runtime_device,
)


class FakeLinear:
    def __init__(self):
        self.dtype = "float32"
    def to(self, *, dtype):
        self.dtype = dtype

class FakeEmbedding(FakeLinear):
    pass


class VoxLoadingTests(unittest.TestCase):
    def test_confined_worker_preserves_original_cuda_graph_compile_mode(self):
        compiler = Mock(return_value="compiled")
        fake_torch = SimpleNamespace(compile=compiler)
        with patch("nachobot_multimodal.utils.voxcpm_loading.os.name", "nt"):
            with self.assertRaisesRegex(RuntimeError, "failure"):
                with _windows_worker_compile(fake_torch, thread_confined=True):
                    self.assertIs(fake_torch.compile, compiler)
                    fake_torch.compile("model", mode="reduce-overhead", fullgraph=True)
                    raise RuntimeError("failure")
        compiler.assert_called_once_with("model", mode="reduce-overhead", fullgraph=True)
        self.assertIs(fake_torch.compile, compiler)

    def test_worker_compilation_keeps_options_and_restores_on_failure(self):
        compiler = Mock(return_value="compiled")
        fake_torch = SimpleNamespace(compile=compiler, _inductor=SimpleNamespace(
            list_mode_options=Mock(return_value={"triton.cudagraphs": True, "other": True})))
        with patch("nachobot_multimodal.utils.voxcpm_loading.os.name", "nt"):
            with self.assertRaisesRegex(RuntimeError, "failure"):
                with _windows_worker_compile(fake_torch):
                    self.assertEqual(fake_torch.compile("model", mode="reduce-overhead", fullgraph=True), "compiled")
                    compiler.assert_called_once_with("model", fullgraph=True,
                                                     options={"triton.cudagraphs": False, "other": True})
                    raise RuntimeError("failure")
        self.assertIs(fake_torch.compile, compiler)

    @unittest.skipUnless(importlib.util.find_spec("torch") and importlib.util.find_spec("safetensors"), "Managed TTS Python required")
    def test_streamed_checkpoint_preserves_tensor_bits_and_rejects_invalid_bounds(self):
        import torch
        from safetensors.torch import save_file
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "model.safetensors")
            values = {
                "bf16": torch.arange(64, dtype=torch.float32).to(torch.bfloat16).reshape(8, 8),
                "fp32": torch.linspace(-1, 1, 16),
                "integers": torch.arange(16, dtype=torch.int64),
                "scalar": torch.tensor(0.5),
                "empty": torch.empty(0),
            }
            save_file(values, str(path), metadata={"test": "exact checkpoint bytes"})
            loaded = _stream_safetensors(str(path), torch=torch, device="cpu")
            self.assertEqual(set(values), set(loaded))
            for key, expected in values.items():
                self.assertEqual(loaded[key].dtype, expected.dtype)
                self.assertTrue(torch.equal(loaded[key], expected), key)
            header = json.dumps({"invalid": {"dtype": "BF16", "shape": [2], "data_offsets": [0, 100]}}).encode()
            path.write_bytes(struct.pack("<Q", len(header)) + header + bytes(4))
            with self.assertRaisesRegex(ValueError, "bounds"):
                _stream_safetensors(str(path), torch=torch, device="cpu")

    @unittest.skipUnless(importlib.util.find_spec("torch"), "Managed TTS Python required")
    def test_deferred_parameters_load_exact_weights_preserve_lora_buffers_and_restore_hooks(self):
        import torch
        class Model(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(8, 4)
                self.embedding = torch.nn.Embedding(16, 4)
                self.lora = torch.nn.Parameter(torch.zeros(2, 4))
                self.register_buffer("computed", torch.arange(4, dtype=torch.float32))
        initializer = torch.nn.Linear.__init__
        loader = torch.nn.Module.load_state_dict
        checkpoint = {"linear.weight": torch.arange(32).reshape(4, 8).to(torch.bfloat16),
                      "linear.bias": torch.ones(4, dtype=torch.bfloat16),
                      "embedding.weight": torch.ones(16, 4, dtype=torch.bfloat16),
                      "computed": torch.arange(4, dtype=torch.float32)}
        with _defer_checkpoint_parameters(torch, torch.bfloat16):
            model = Model()
            self.assertTrue(model.linear.weight.is_meta)
            self.assertTrue(model.embedding.weight.is_meta)
            self.assertFalse(model.lora.is_meta)
            self.assertEqual(model.computed.dtype, torch.float32)
            model.to(torch.bfloat16)
            model.load_state_dict(checkpoint, strict=False)
            self.assertFalse(any(parameter.is_meta for parameter in model.parameters()))
            model.to("cpu")
        self.assertIs(torch.nn.Linear.__init__, initializer)
        self.assertIs(torch.nn.Module.load_state_dict, loader)
        for name, expected in checkpoint.items():
            self.assertTrue(torch.equal(model.state_dict()[name], expected.to(torch.bfloat16)), name)
        self.assertTrue(torch.equal(model.lora, torch.zeros(2, 4, dtype=torch.bfloat16)))
        with self.assertRaisesRegex(ValueError, "missing pretrained parameters"):
            with _defer_checkpoint_parameters(torch, torch.bfloat16):
                Model().load_state_dict({}, strict=False)
        self.assertIs(torch.nn.Linear.__init__, initializer)
        self.assertIs(torch.nn.Module.load_state_dict, loader)

    @unittest.skipUnless(importlib.util.find_spec("torch"), "Run numerical check with managed TTS Python")
    def test_layerwise_cast_preserves_parameters_rng_and_computed_buffers(self):
        import torch

        class Model(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(32, 16)
                self.embedding = torch.nn.Embedding(128, 16)
                self.norm = torch.nn.LayerNorm(16)
                # Match LoRA's separately allocated float32 parameters and RNG.
                self.lora = torch.nn.Parameter(torch.empty(4, 16))
                torch.nn.init.kaiming_uniform_(self.lora)
                self.register_buffer("computed", torch.sin(torch.arange(16, dtype=torch.float32)))

        default_dtype = torch.get_default_dtype()
        original_init = torch.nn.Linear.__init__
        torch.manual_seed(42)
        reference = Model().to(torch.bfloat16)
        torch.manual_seed(42)
        with _cast_initialized_modules(torch, torch.bfloat16):
            candidate = Model()
            self.assertEqual(candidate.linear.weight.dtype, torch.bfloat16)
            self.assertEqual(candidate.embedding.weight.dtype, torch.bfloat16)
            self.assertEqual(candidate.lora.dtype, torch.float32)
            self.assertEqual(candidate.computed.dtype, torch.float32)
        candidate.to(torch.bfloat16)
        self.assertIs(torch.nn.Linear.__init__, original_init)
        self.assertEqual(torch.get_default_dtype(), default_dtype)
        self.assertEqual(set(reference.state_dict()), set(candidate.state_dict()))
        for name, expected in reference.state_dict().items():
            self.assertTrue(torch.equal(expected, candidate.state_dict()[name]), name)

    def test_cuda_placement_restores_context_and_preserves_checkpoint_options_on_success_and_error(self):
        for raises in (False, True):
            with self.subTest(raises=raises), tempfile.TemporaryDirectory() as directory:
                Path(directory, "config.json").write_text(json.dumps({"device": "cuda", "dtype": "bfloat16"}))
                current = {"device": "cpu", "dtype": "float32"}

                @contextmanager
                def device_context(device, current=current):
                    previous = current["device"]
                    current["device"] = device
                    try:
                        yield
                    finally:
                        current["device"] = previous

                options = {"hf_model_id": directory, "optimize": True, "load_denoiser": False, "lora_weights_path": "original-lora"}
                original = dict(options)
                observed = []
                def factory(*, current=current, observed=observed, raises=raises, **kwargs):
                    observed.append(kwargs)
                    self.assertEqual(current, {"device": "cuda:0", "dtype": "float32"})
                    self.assertEqual(FakeLinear().dtype, "bfloat16")
                    if raises:
                        raise RuntimeError("load failed")
                    return "loaded"
                torch = Mock(device=device_context, nn=SimpleNamespace(Linear=FakeLinear, Embedding=FakeEmbedding))
                resolver = Mock(return_value="cuda:0")
                dtype = Mock(return_value="bfloat16")
                if raises:
                    with self.assertRaisesRegex(RuntimeError, "load failed"):
                        load_pretrained_on_runtime_device(factory, options, torch=torch, resolve_device=resolver, resolve_dtype=dtype)
                else:
                    self.assertEqual(load_pretrained_on_runtime_device(factory, options, torch=torch, resolve_device=resolver, resolve_dtype=dtype), "loaded")
                self.assertEqual(current, {"device": "cpu", "dtype": "float32"})
                self.assertEqual(options, original)
                self.assertEqual(observed, [dict(original, device="cuda:0")])
                resolver.assert_called_once_with(None, "cuda")
                self.assertEqual(FakeLinear().dtype, "float32")

    def test_cpu_and_remote_paths_preserve_existing_loader_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "config.json").write_text('{"device":"cpu"}')
            for source, resolve_called in [(directory, True), ("model/remote-id", False)]:
                options = {"hf_model_id": source, "optimize": True}
                factory = Mock(return_value="loaded")
                torch = Mock()
                resolver = Mock(return_value="cpu")
                self.assertEqual(load_pretrained_on_runtime_device(factory, options, torch=torch, resolve_device=resolver, resolve_dtype=Mock()), "loaded")
                factory.assert_called_once_with(**options)
                torch.device.assert_not_called()
                self.assertEqual(resolver.called, resolve_called)


if __name__ == "__main__":
    unittest.main()
