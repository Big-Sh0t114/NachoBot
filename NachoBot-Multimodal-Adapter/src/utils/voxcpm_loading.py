"""Construct local VoxCPM models on their existing runtime device.

VoxCPM's loader otherwise allocates the full float32 model on CPU, converts
it, loads weights, and only then transfers it. A scoped CUDA device preserves
the loader's dtype and checkpoint rules without retaining that CPU model.
"""
from __future__ import annotations

import json
import math
import struct
import importlib
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from unittest.mock import patch

_LOAD_LOCK = RLock()


@contextmanager
def _windows_worker_compile(torch: Any, *, thread_confined: bool = False):
    """Avoid thread-local CUDA graphs unless the caller confines inference.

    Starlette consumes synchronous streams on its worker threads. Torch 2.11
    CUDA graph trees initialized during warmup have no TLS container on those
    threads, causing an assertion before the first PCM chunk. A caller that
    loads, generates and closes iterators on one owner thread retains the
    original compile mode, including CUDA graphs.
    """
    if os.name != "nt" or thread_confined:
        yield
        return
    original = torch.compile

    def compile_model(*args, **kwargs):
        if kwargs.get("mode") == "reduce-overhead" and kwargs.get("backend", "inductor") == "inductor":
            options = dict(torch._inductor.list_mode_options("reduce-overhead"))
            options.update(kwargs.pop("options", None) or {})
            options["triton.cudagraphs"] = False
            kwargs.pop("mode")
            kwargs["options"] = options
        return original(*args, **kwargs)

    with patch.object(torch, "compile", compile_model):
        yield


def _stream_safetensors(path: str, *, torch: Any, device: str) -> dict[str, Any]:
    """Read exact tensor bytes with at most one tensor's CPU staging storage.

    A whole-file copy-on-write mapping can reserve the checkpoint's entire
    size from Windows commit even when the tensors are transferred to CUDA.
    """
    dtype_table = {
        "BF16": (torch.bfloat16, 2), "F16": (torch.float16, 2),
        "F32": (torch.float32, 4), "F64": (torch.float64, 8),
        "I64": (torch.int64, 8), "I32": (torch.int32, 4),
        "I16": (torch.int16, 2), "I8": (torch.int8, 1),
        "U8": (torch.uint8, 1), "BOOL": (torch.bool, 1),
    }
    with open(path, "rb") as checkpoint:
        length_bytes = checkpoint.read(8)
        if len(length_bytes) != 8:
            raise ValueError("VoxCPM checkpoint header is truncated")
        header_length = struct.unpack("<Q", length_bytes)[0]
        if not 2 <= header_length <= 16 * 1024 * 1024:
            raise ValueError("VoxCPM checkpoint header is invalid")
        encoded = checkpoint.read(header_length)
        if len(encoded) != header_length:
            raise ValueError("VoxCPM checkpoint header is truncated")
        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("VoxCPM checkpoint header has duplicate fields")
                result[key] = value
            return result
        header = json.loads(encoded, object_pairs_hook=unique_fields)
        if not isinstance(header, dict) or len(header) > 65536:
            raise ValueError("VoxCPM checkpoint header is invalid")
        base_offset = 8 + header_length
        checkpoint.seek(0, 2)
        data_length = checkpoint.tell() - base_offset
        tensors = []
        for name, item in header.items():
            if name == "__metadata__":
                continue
            if not isinstance(item, dict) or set(item) != {"dtype", "shape", "data_offsets"}:
                raise ValueError("VoxCPM checkpoint tensor header is invalid")
            shape, offsets = item["shape"], item["data_offsets"]
            if not isinstance(shape, list) or any(type(dimension) is not int or dimension < 0 for dimension in shape):
                raise ValueError("VoxCPM checkpoint tensor shape is invalid")
            if not isinstance(offsets, list) or len(offsets) != 2 or any(type(value) is not int for value in offsets):
                raise ValueError("VoxCPM checkpoint tensor offsets are invalid")
            if not isinstance(item["dtype"], str) or item["dtype"] not in dtype_table:
                raise ValueError("VoxCPM checkpoint tensor dtype is unsupported")
            dtype, width = dtype_table[item["dtype"]]
            start, end = offsets
            if not 0 <= start <= end <= data_length or end-start != math.prod(shape)*width or end-start > 1024**3:
                raise ValueError("VoxCPM checkpoint tensor bounds are invalid")
            tensors.append((start, end, name, shape, dtype))
        tensors.sort(key=lambda value: (value[0], value[1]))
        consumed = 0
        for start, end, *_ in tensors:
            if start != consumed:
                raise ValueError("VoxCPM checkpoint tensor ranges overlap or contain gaps")
            consumed = end
        if consumed != data_length:
            raise ValueError("VoxCPM checkpoint has trailing data")
        result = {}
        for start, end, name, shape, dtype in tensors:
            checkpoint.seek(base_offset + start)
            if end == start:
                tensor = torch.empty(shape, dtype=dtype, device=device)
            else:
                storage = bytearray(end-start)
                if checkpoint.readinto(storage) != len(storage):
                    raise ValueError("VoxCPM checkpoint tensor is truncated")
                tensor = torch.frombuffer(storage, dtype=dtype).reshape(shape).to(device)
            result[name] = tensor
        return result


@contextmanager
def _defer_checkpoint_parameters(torch: Any, dtype: Any):
    """Defer large pretrained weights, retaining real buffers and LoRA state."""
    with ExitStack() as stack:
        for module_class in (torch.nn.Linear, torch.nn.Embedding):
            original = module_class.__init__
            def initialize(module, *args, _original=original, **kwargs):
                kwargs = dict(kwargs, device="meta")
                _original(module, *args, **kwargs)
                module.to(dtype=dtype)
            stack.enter_context(patch.object(module_class, "__init__", initialize))
        original_load = torch.nn.Module.load_state_dict
        def load(module, state_dict, *args, **kwargs):
            expected = module.state_dict()
            adjusted = {key: value.to(dtype=expected[key].dtype) if key in expected else value
                        for key, value in state_dict.items()}
            kwargs["assign"] = True
            result = original_load(module, adjusted, *args, **kwargs)
            if any(parameter.is_meta for parameter in module.parameters()):
                raise ValueError("VoxCPM checkpoint is missing pretrained parameters")
            return result
        stack.enter_context(patch.object(torch.nn.Module, "load_state_dict", load))
        yield


@contextmanager
def _cast_initialized_modules(torch: Any, dtype: Any):
    """Cast each initialized large layer before constructing the next one.

    Initialization still runs with the original float32 dtype and RNG rules;
    buffers and AudioVAE construction keep their original precision. Module
    classes retain their identity, including for VoxCPM's LoRA wrapping.
    """
    with ExitStack() as stack:
        for module_class in (torch.nn.Linear, torch.nn.Embedding):
            original = module_class.__init__
            def initialize(module, *args, _original=original, **kwargs):
                _original(module, *args, **kwargs)
                module.to(dtype=dtype)
            stack.enter_context(patch.object(module_class, "__init__", initialize))
        yield


def load_pretrained_on_runtime_device(
    factory: Callable[..., Any], options: dict[str, Any], *, torch: Any,
    resolve_device: Callable[..., str], resolve_dtype: Callable[..., Any],
    thread_confined: bool = False,
) -> Any:
    config_path = Path(options["hf_model_id"]) / "config.json"
    # Remote-ID resolution remains the installed library's responsibility.
    if not config_path.is_file():
        return factory(**options)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    device = resolve_device(options.get("device"), config.get("device", "cuda"))
    if not device.startswith("cuda"):
        return factory(**options)
    load_options = dict(options, device=device)
    # Do not change the default dtype: AudioVAE and computed buffers retain
    # their original precision, and VoxCPM performs its normal dtype casts.
    # torch.device restores the previous default even when loading raises.
    dtype = resolve_dtype(device, config.get("dtype", "bfloat16"))
    # The installed VoxCPM2 loader is retained, including AudioVAE float32,
    # LoRA loading and warmup. Only weight construction and storage change.
    if config.get("architecture", "voxcpm").lower() != "voxcpm2":
        with _LOAD_LOCK, torch.device(device), _windows_worker_compile(torch, thread_confined=thread_confined), _cast_initialized_modules(torch, dtype):
            return factory(**load_options)
    model_module = importlib.import_module("voxcpm.model.voxcpm2")
    def load_file(path, device=None):
        return _stream_safetensors(path, torch=torch, device=device or load_options["device"])
    with _LOAD_LOCK, torch.device(device), _windows_worker_compile(torch, thread_confined=thread_confined), _defer_checkpoint_parameters(torch, dtype), patch.object(model_module, "load_file", load_file):
        return factory(**load_options)
