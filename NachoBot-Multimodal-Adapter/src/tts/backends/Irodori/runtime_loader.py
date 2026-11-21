"""Memory-conscious Windows loader for quantized Irodori safetensors checkpoints."""

from __future__ import annotations

from functools import partial
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
from typing import Any


_MINIMUM_SAFETENSORS = (0, 8, 0)


def _is_windows() -> bool:
    import os

    return os.name == "nt"


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)([0-9A-Za-z.+-]*)\s*", value)
    if match is None:
        return None
    parts = tuple(int(part or 0) for part in match.groups()[:3])
    suffix = match.group(4).lower()
    if parts == _MINIMUM_SAFETENSORS and suffix and not suffix.startswith(("+", ".post")):
        return None
    return parts


def require_windows_pread() -> str:
    """Require the official safetensors release that exposes the PREAD backend."""

    try:
        installed = version("safetensors")
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "Windows Irodori safetensors loading requires safetensors>=0.8.0 "
            "for backend='pread'; upgrade the selected Irodori Python environment."
        ) from exc
    parsed = _version_tuple(installed)
    if parsed is None or parsed < _MINIMUM_SAFETENSORS:
        raise RuntimeError(
            f"Windows Irodori safetensors loading requires safetensors>=0.8.0 "
            f"for backend='pread'; found {installed}. Upgrade the selected Irodori Python environment."
        )
    return installed


def _required_hook(module: Any, name: str) -> Any:
    hook = getattr(module, name, None)
    if not callable(hook):
        raise RuntimeError(f"Irodori runtime lacks required loader hook {name!r}")
    return hook


def _sequential_tensor_loader(
    safe_open_pread: Any,
    target_device: Any,
):
    device_text = str(target_device).strip().lower()
    keep_on_cpu = device_text == "cpu" or device_text.startswith("cpu:")

    def load_file(filename: str | Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # Upstream supplies device="cpu"; quantized Windows checkpoints are
        # streamed directly to their final device one tensor at a time.
        if args:
            args = args[1:]
        if args:
            raise TypeError("unexpected positional safetensors load_file arguments")
        kwargs.pop("device", None)
        kwargs.pop("backend", None)
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"unexpected safetensors load_file arguments: {unexpected}")

        tensors: dict[str, Any] = {}
        with safe_open_pread(filename, framework="pt", device="cpu") as handle:
            offset_keys = getattr(handle, "offset_keys", None)
            get_tensor = getattr(handle, "get_tensor", None)
            if not callable(offset_keys) or not callable(get_tensor):
                raise RuntimeError(
                    "safetensors PREAD safe_open lacks offset_keys/get_tensor required for sequential loading"
                )
            for name in offset_keys():
                cpu_tensor = get_tensor(name)
                if keep_on_cpu:
                    tensors[name] = cpu_tensor
                    continue
                try:
                    tensors[name] = cpu_tensor.to(target_device, non_blocking=False)
                finally:
                    del cpu_tensor
        return tensors

    return load_file


def load_inference_runtime(runtime_module: Any, key: Any) -> Any:
    """Use upstream loading except for quantized Windows safetensors checkpoints."""

    upstream_runtime = _required_hook(runtime_module, "InferenceRuntime")
    from_key = getattr(upstream_runtime, "from_key", None)
    if not callable(from_key):
        raise RuntimeError("Irodori runtime lacks InferenceRuntime.from_key")

    checkpoint = Path(key.checkpoint)
    if not _is_windows() or checkpoint.suffix.lower() != ".safetensors":
        return from_key(key)

    require_windows_pread()
    original_loader = _required_hook(runtime_module, "load_safetensors_file")
    original_safe_open = _required_hook(runtime_module, "safe_open")
    parse_quantization_metadata = _required_hook(runtime_module, "parse_quantization_metadata")
    model_factory = getattr(runtime_module, "TextToLatentRFDiT", None)

    pread_safe_open = partial(original_safe_open, backend="pread")
    original_factory = model_factory
    factory_wrapped = False
    loader_changed = False
    safe_open_changed = False
    try:
        runtime_module.safe_open = pread_safe_open
        safe_open_changed = True
        runtime_module.load_safetensors_file = partial(original_loader, backend="pread")
        loader_changed = True

        try:
            with runtime_module.safe_open(str(checkpoint), framework="pt", device="cpu") as handle:
                metadata = handle.metadata() or {}
        except TypeError as exc:
            raise RuntimeError(
                "Windows Irodori loading requires safetensors safe_open backend='pread'; "
                "upgrade safetensors to >=0.8.0 in the selected Irodori Python environment."
            ) from exc

        quantization = parse_quantization_metadata(metadata)
        if quantization is None:
            return from_key(key)

        if not callable(model_factory):
            raise RuntimeError("Quantized Windows Irodori loading requires TextToLatentRFDiT constructor hook")
        try:
            accelerate = import_module("accelerate")
        except ImportError as exc:
            raise RuntimeError(
                "Quantized Windows Irodori loading requires accelerate.init_empty_weights; "
                "prepare the selected Irodori Python environment first."
            ) from exc
        init_empty_weights = getattr(accelerate, "init_empty_weights", None)
        if not callable(init_empty_weights):
            raise RuntimeError("accelerate lacks init_empty_weights required for quantized Windows Irodori loading")

        def empty_initialized_factory(*args: Any, **kwargs: Any) -> Any:
            with init_empty_weights(include_buffers=False):
                return model_factory(*args, **kwargs)

        runtime_module.TextToLatentRFDiT = empty_initialized_factory
        factory_wrapped = True
        runtime_module.load_safetensors_file = _sequential_tensor_loader(
            runtime_module.safe_open,
            key.model_device,
        )
        return from_key(key)
    finally:
        if factory_wrapped:
            runtime_module.TextToLatentRFDiT = original_factory
        if loader_changed:
            runtime_module.load_safetensors_file = original_loader
        if safe_open_changed:
            runtime_module.safe_open = original_safe_open
