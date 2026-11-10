"""GPU inventory and persistent local model device assignments for WebUI."""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Any, Mapping


ROOT_DIR = Path(__file__).resolve().parent.parent
ADAPTER_ROOT = ROOT_DIR / "NachoBot-Multimodal-Adapter"
LEGACY_MODEL_DEVICES_PATH = ROOT_DIR / "webUI" / "data" / "model_devices.json"
DEFAULT_ASSIGNMENTS = {"tts": "auto", "vlm": "auto"}
_GPU_UUID = re.compile(r"^GPU-[A-Za-z0-9-]+$")
_PCI_DISPLAY_DEVICE_ID = re.compile(
    r"^PCI\\VEN_([0-9A-F]{4})&DEV_[0-9A-F]{4}(?:&|\\|$)", re.IGNORECASE
)
# Known virtual display vendors: VMware, VirtualBox, virtio, Hyper-V, QEMU/Bochs, and QXL.
_VIRTUAL_PCI_VENDORS = {"15AD", "80EE", "1AF4", "1414", "1234", "1B36"}
_STORE_LOCK = threading.RLock()

if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))

from nachobot_multimodal.utils.model_devices import (  # noqa: E402
    ModelDeviceConfigError,
    config_paths as adapter_config_paths,
    device_environment as adapter_device_environment,
    read_device_choice,
    read_model_devices,
    validate_device_choice,
)

try:
    import tomlkit
except ImportError as exc:  # pragma: no cover - WebUI declares tomlkit at runtime
    raise RuntimeError("WebUI model-device editing requires tomlkit") from exc


class ModelDeviceError(ValueError):
    """Invalid, unavailable, or unreadable model device configuration."""


def _number(value: object) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(match.group()) if match else None


def _nvidia_inventory() -> list[dict[str, Any]]:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []

    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in completed.stdout.splitlines():
        try:
            fields = next(csv.reader([line], skipinitialspace=True))
        except (csv.Error, StopIteration):
            continue
        try:
            if len(fields) < 4:
                continue
            index, uuid, name, total = (field.strip() for field in fields[:4])
            if not index.isdigit() or not _GPU_UUID.fullmatch(uuid) or not name or uuid in seen:
                continue
            total_mb = _number(total)
            if total_mb is None:
                continue
            free_mb = _number(fields[4]) if len(fields) > 4 else None
            devices.append(
                {
                    "index": int(index),
                    "uuid": uuid,
                    "name": name,
                    "vram_mb": total_mb,
                    "vram_free_mb": free_mb,
                    "cuda": True,
                    "selectable": True,
                    "source": "nvidia-smi",
                }
            )
            seen.add(uuid)
        except (TypeError, ValueError):
            continue
    return devices


def _windows_display_inventory(cuda_devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enumerate physical PCI display adapters for visibility, never CUDA options."""
    if os.name != "nt":
        return []
    command = (
        "$ErrorActionPreference='Stop'; "
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM,PNPDeviceID | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return []
        payload = json.loads(completed.stdout)
        rows = payload if isinstance(payload, list) else [payload]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError):
        return []

    cuda_name_counts: dict[str, int] = {}
    for device in cuda_devices:
        name_key = str(device["name"]).casefold()
        cuda_name_counts[name_key] = cuda_name_counts.get(name_key, 0) + 1
    result: list[dict[str, Any]] = []
    for display_index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        device_id = row.get("PNPDeviceID")
        if not isinstance(device_id, str):
            continue
        device_match = _PCI_DISPLAY_DEVICE_ID.match(device_id.strip())
        if not device_match or device_match.group(1).upper() in _VIRTUAL_PCI_VENDORS:
            continue
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        name_key = name.casefold()
        if cuda_name_counts.get(name_key, 0):
            cuda_name_counts[name_key] -= 1
            continue
        ram_bytes = _number(row.get("AdapterRAM"))
        result.append(
            {
                "index": f"WMI-{display_index}",
                "uuid": None,
                "name": name,
                "vram_mb": ram_bytes / (1024 * 1024) if ram_bytes is not None else None,
                "vram_free_mb": None,
                "cuda": False,
                "selectable": False,
                "source": "wmi",
            }
        )
    return result


def detect_gpu_inventory() -> list[dict[str, Any]]:
    """Return every NVIDIA CUDA device followed by detected display-only adapters."""
    cuda_devices = _nvidia_inventory()
    return cuda_devices + _windows_display_inventory(cuda_devices)


def summarize_gpu_inventory(gpus: list[dict[str, Any]]) -> dict[str, Any]:
    """Preserve the legacy single-GPU summary, selecting the largest CUDA GPU."""
    cuda_devices = [device for device in gpus if device.get("cuda") is True]
    if cuda_devices:
        largest = max(cuda_devices, key=lambda device: float(device.get("vram_mb") or 0))
        vram_mb = float(largest.get("vram_mb") or 0)
        return {
            "status": "ok",
            "has_gpu": True,
            "gpu_name": largest["name"],
            "vram_mb": vram_mb,
            "message": f"{largest['name']} (显存 {vram_mb / 1024.0:.2f} GB；CUDA)；共检测到 {len(cuda_devices)} 张 NVIDIA CUDA 显卡",
            "gpus": gpus,
        }
    largest = max(
        gpus,
        key=lambda device: float(device.get("vram_mb") or 0),
        default=None,
    )
    if largest:
        vram_mb = largest.get("vram_mb")
        suffix = f" (显存 {float(vram_mb) / 1024.0:.2f} GB)" if vram_mb is not None else ""
        message = f"检测到显示适配器：{largest['name']}{suffix}；未检测到 NVIDIA CUDA 显卡"
        gpu_name = largest["name"]
        summary_vram = float(vram_mb or 0)
    else:
        message = "未检测到可用 NVIDIA 显卡"
        gpu_name = None
        summary_vram = 0.0
    return {
        "status": "ok",
        "has_gpu": False,
        "gpu_name": gpu_name,
        "vram_mb": summary_vram,
        "message": message,
        "gpus": gpus,
    }


def _validate_assignment_shape(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(DEFAULT_ASSIGNMENTS):
        raise ModelDeviceError("模型设备选择必须仅包含 tts 和 vlm 两项")
    try:
        return {model: validate_device_choice(model, choice) for model, choice in value.items()}
    except ModelDeviceConfigError as exc:
        raise ModelDeviceError(str(exc)) from exc


def legacy_model_devices_path(root_dir: Path | None = None) -> Path:
    root = Path(root_dir) if root_dir is not None else ROOT_DIR
    return root / "webUI" / "data" / "model_devices.json"


def _config_files(root_dir: Path) -> tuple[Path, Path, Path, Path]:
    base, perception = adapter_config_paths(root_dir=root_dir)
    adapter = base.parent.parent
    return (
        base,
        perception,
        adapter / "template_configs" / "base_template.toml",
        adapter / "template_configs" / "perception_template.toml",
    )


def _read_document(path: Path, template_path: Path, label: str):
    source = path if path.is_file() else template_path
    try:
        raw = source.read_text(encoding="utf-8")
        document = tomlkit.parse(raw)
        # stdlib parsing also validates exactly the syntax consumed by runtime.
        tomllib.loads(raw)
    except FileNotFoundError as exc:
        raise ModelDeviceError(f"缺少 {label} 配置模板，无法保存模型设备选择") from exc
    except (OSError, UnicodeDecodeError, tomlkit.exceptions.ParseError, tomllib.TOMLDecodeError) as exc:
        raise ModelDeviceError(f"{label} TOML 配置无法解析；已保留现有文件") from exc
    return document


def _document_choices(base_doc, perception_doc) -> dict[str, str]:
    model_devices = base_doc.get("model_devices", {})
    if not isinstance(model_devices, Mapping):
        raise ModelDeviceError("base.toml 的 [model_devices] 必须是 TOML 表")
    perception = perception_doc.get("perception", {})
    if not isinstance(perception, Mapping):
        raise ModelDeviceError("perception.toml 的 [perception] 必须是 TOML 表")
    device = perception.get("device", {})
    if not isinstance(device, Mapping):
        raise ModelDeviceError("perception.toml 的 [perception.device] 必须是 TOML 表")
    try:
        return {
            "tts": validate_device_choice("tts", model_devices.get("tts", "auto")),
            "vlm": validate_device_choice("vlm", device.get("vlm", "auto")),
        }
    except ModelDeviceConfigError as exc:
        raise ModelDeviceError(f"模型设备 TOML 字段无效：{exc}") from exc


def _canonical_tts_field_exists(base_path: Path) -> bool:
    if not base_path.is_file():
        return False
    try:
        document = tomllib.loads(base_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ModelDeviceError("base.toml 无法解析；已保留现有设备选择") from exc
    model_devices = document.get("model_devices", {})
    if not isinstance(model_devices, dict):
        raise ModelDeviceError("base.toml 的 [model_devices] 必须是 TOML 表")
    return "tts" in model_devices


def _set_document_choices(base_doc, perception_doc, assignments: Mapping[str, str]) -> tuple[str, str]:
    model_devices = base_doc.get("model_devices")
    if model_devices is None:
        model_devices = tomlkit.table()
        base_doc["model_devices"] = model_devices
    model_devices["tts"] = assignments["tts"]

    perception = perception_doc.get("perception")
    if perception is None:
        perception = tomlkit.table()
        perception_doc["perception"] = perception
    device = perception.get("device")
    if device is None:
        device = tomlkit.table()
        perception["device"] = device
    device["vlm"] = assignments["vlm"]
    return tomlkit.dumps(base_doc), tomlkit.dumps(perception_doc)


def _write_pair(contents: Mapping[Path, str]) -> None:
    originals: dict[Path, bytes | None] = {}
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for target, content in contents.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            originals[target] = target.read_bytes() if target.exists() else None
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=target.parent,
                prefix=f".{target.name}.", suffix=".tmp", delete=False,
            ) as stream:
                staged[target] = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for target, temporary in staged.items():
            os.replace(temporary, target)
            replaced.append(target)
    except OSError as exc:
        rollback_errors: list[str] = []
        for target in reversed(replaced):
            original = originals[target]
            try:
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", dir=target.parent, prefix=f".{target.name}.rollback.",
                        suffix=".tmp", delete=False,
                    ) as stream:
                        rollback = Path(stream.name)
                        stream.write(original)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(rollback, target)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target.name}: {type(rollback_exc).__name__}")
        for temporary in staged.values():
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        detail = "；原文件回滚失败：" + ", ".join(rollback_errors) if rollback_errors else ""
        raise ModelDeviceError(f"保存模型设备配置失败；原有配置已尽力恢复{detail}") from exc
    finally:
        for temporary in staged.values():
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _load_pair(root_dir: Path):
    base_path, perception_path, base_template, perception_template = _config_files(root_dir)
    base_doc = _read_document(base_path, base_template, "base.toml")
    perception_doc = _read_document(perception_path, perception_template, "perception.toml")
    return base_path, perception_path, base_doc, perception_doc


def load_model_assignments(root_dir: Path | None = None) -> dict[str, str]:
    """Read canonical TOML, migrating a valid old WebUI JSON store once."""
    root = Path(root_dir) if root_dir is not None else ROOT_DIR
    base_path, perception_path, _, _ = _config_files(root)
    with _STORE_LOCK:
        # A present canonical field always wins, even if old JSON is malformed.
        if _canonical_tts_field_exists(base_path):
            try:
                return read_model_devices(config_dir=base_path.parent)
            except ModelDeviceConfigError as exc:
                raise ModelDeviceError(f"模型设备 TOML 配置无效：{exc}") from exc

        legacy_path = legacy_model_devices_path(root)
        if legacy_path.exists():
            try:
                legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ModelDeviceError("旧模型设备 JSON 已损坏；已保留 canonical TOML") from exc
            try:
                assignments = _validate_assignment_shape(legacy)
            except ModelDeviceError as exc:
                raise ModelDeviceError(f"旧模型设备 JSON 格式无效：{exc}") from exc

            archive = legacy_path.with_name(legacy_path.name + ".migrated")
            if archive.exists():
                raise ModelDeviceError("旧模型设备迁移备份已存在；已保留所有文件，请检查配置")
            base_path, perception_path, base_doc, perception_doc = _load_pair(root)
            # Refuse malformed typed fields rather than silently replacing them.
            current = _document_choices(base_doc, perception_doc)
            # In the old JSON schema, VLM auto meant "leave perception.toml
            # alone". Preserve that effective setting during migration only.
            if assignments["vlm"] == "auto":
                assignments["vlm"] = current["vlm"]
            base_text, perception_text = _set_document_choices(base_doc, perception_doc, assignments)
            _write_pair({base_path: base_text, perception_path: perception_text})
            try:
                legacy_path.replace(archive)
            except OSError as exc:
                raise ModelDeviceError(
                    "设备选择已迁入 TOML，但旧 JSON 归档失败；canonical TOML 仍为权威来源"
                ) from exc
            return assignments

        try:
            return read_model_devices(config_dir=base_path.parent)
        except ModelDeviceConfigError as exc:
            raise ModelDeviceError(f"模型设备 TOML 配置无效：{exc}") from exc


def _validate_requested_gpus(
    assignments: Mapping[str, str],
    gpus: list[dict[str, Any]] | None,
) -> None:
    explicit = {choice for choice in assignments.values() if _GPU_UUID.fullmatch(choice)}
    if not explicit:
        return
    fresh_inventory = detect_gpu_inventory() if gpus is None else gpus
    current = {item.get("uuid") for item in fresh_inventory if item.get("cuda") is True}
    if explicit - current:
        raise ModelDeviceError(
            "所选 NVIDIA 显卡当前不可用或已变化，请重新检测并选择可用显卡，或改为自动/CPU"
        )


def save_model_assignments(
    assignments: object,
    *,
    gpus: list[dict[str, Any]] | None = None,
    root_dir: Path | None = None,
) -> dict[str, str]:
    """Replace the canonical TTS and VLM fields while preserving TOML context."""
    normalized = _validate_assignment_shape(assignments)
    _validate_requested_gpus(normalized, gpus)
    root = Path(root_dir) if root_dir is not None else ROOT_DIR
    with _STORE_LOCK:
        base_path, perception_path, base_doc, perception_doc = _load_pair(root)
        # Existing invalid device fields are reported; only an explicit raw edit can repair them.
        _document_choices(base_doc, perception_doc)
        base_text, perception_text = _set_document_choices(base_doc, perception_doc, normalized)
        _write_pair({base_path: base_text, perception_path: perception_text})
    return normalized


def model_devices_payload(root_dir: Path | None = None) -> dict[str, Any]:
    assignments = load_model_assignments(root_dir)
    gpus = detect_gpu_inventory()
    return {
        "assignments": assignments,
        "gpus": gpus,
        "restart_note": "保存后会在对应托管服务下次启动或重启时生效；当前运行中的服务不会自动重启。",
        "scope_note": "设备分配仅适用于本地 TTS 与 Florence-2 VLM；ASR 当前使用 CPU 运行。",
    }


def service_device_environment(
    service_id: str,
    runtime: str,
    *,
    root_dir: Path | None = None,
) -> dict[str, str]:
    """Resolve managed-service overrides from the same canonical TOML as BAT starts."""
    if service_id not in {"tts_runtime_full", "tts_runtime_lite", "perception"}:
        return {}
    root = Path(root_dir) if root_dir is not None else ROOT_DIR
    model = "vlm" if service_id == "perception" else "tts"
    base_path, perception_path = adapter_config_paths(root_dir=root)
    try:
        # Managed starts can be the first WebUI access after an older install.
        # Perform the same one-time migration as GET before resolving either model.
        if legacy_model_devices_path(root).exists() and not _canonical_tts_field_exists(base_path):
            load_model_assignments(root)
        choice = read_device_choice(
            model,
            base_path=base_path,
            perception_path=perception_path,
        )
        overrides = adapter_device_environment(
            model,
            choice,
            runtime_profile=runtime,
            verify_gpu=False,
        )
        if _GPU_UUID.fullmatch(choice):
            available = {
                device.get("uuid")
                for device in detect_gpu_inventory()
                if device.get("cuda") is True
            }
            if choice not in available:
                raise ModelDeviceError(
                    f"已指定的 NVIDIA 显卡 {choice} 当前不可用；请在“模型运行设备”中重新选择或改为自动/CPU"
                )
        return overrides
    except ModelDeviceConfigError as exc:
        raise ModelDeviceError(f"模型设备 TOML 配置无效：{exc}") from exc

