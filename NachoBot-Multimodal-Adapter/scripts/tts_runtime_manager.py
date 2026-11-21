from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from urllib.error import URLError

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))

from nachobot_multimodal.utils.gpt_weights import FALLBACK_GPT, FALLBACK_SOVITS, select_gpt_weights  # noqa: E402
from nachobot_multimodal.utils.model_devices import (  # noqa: E402
    apply_device_environment,
    auto_cuda_available,
    detected_nvidia_gpu_uuids,
    GPU_UUID_PATTERN,
    read_device_choice,
)
from nachobot_multimodal.tts.backends.Irodori.config import load_irodori_config  # noqa: E402

PROJECT_ROOT = ADAPTER_ROOT.parent
RUNTIME_ROOT = ADAPTER_ROOT / ".runtime" / "tts"
HF_CACHE = ADAPTER_ROOT / "models" / "hf_cache"

# Apply before public-model imports and pass the same policy to managed raw
# engines. Windows OpenBLAS otherwise commits large idle per-worker buffers.
# Explicit deployment settings continue to take precedence.
if os.name == "nt":
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

# Keep Hub behaviour predictable on mainland-China networks. These values are
# also propagated to every managed TTS subprocess by base_env().
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "10")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")

GPT_REF = os.environ.get("NACHOBOT_GPT_SOVITS_REF", "20250606v2pro")
GPT_REPO = "https://github.com/RVC-Boss/GPT-SoVITS.git"
PYTORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
PYTORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu130"
TTS_TORCH_RECIPE = "torch213-audio211-triton371-v3"
TTS_TRITON_WINDOWS_VERSION = "3.7.1.post27"
TTS_GPT_ORT_GPU_VERSION = "1.20.2"
IRODORI_REPO_URL = "https://github.com/Aratako/Irodori-TTS.git"
IRODORI_SOURCE_COMMIT = "59be938c99d48b5653ca5aa3fdd0cddaa56940e5"
IRODORI_RUNTIME_DIR = RUNTIME_ROOT / "irodori"
IRODORI_SAFETENSORS_REQUIREMENT = "safetensors>=0.8,<0.9"
IRODORI_REQUIREMENTS_FILE = ADAPTER_ROOT / "src" / "tts" / "backends" / "Irodori" / "requirements-runtime.txt"
IRODORI_TORCHAO_VERSION = "0.16.0"
IRODORI_DACVAE_COMMIT = "414c20785fc3a28373073ea8ef7a1316eeeaca6e"
IRODORI_SILENTCIPHER_COMMIT = "d46d7d0893a583d8968ab3a6626e2289faec9152"

PUBLIC_DEFAULT_HOST = "127.0.0.1"
PUBLIC_DEFAULT_PORT = 9880
PRIVATE_DEFAULT_HOST = "127.0.0.1"
PRIVATE_DEFAULT_PORT = 9881
PUBLIC_MAIN_MODULE_NAME = "_nachobot_multimodal_public_main"
_ENGINE_ALIASES = {
    "vox": "voxcpm",
    "voxcpm": "voxcpm",
    "gpt_sovits": "gpt-sovits",
    "gpt-sovits": "gpt-sovits",
    "gptsovits": "gpt-sovits",
    "generic_http": "generic-http",
    "generic-http": "generic-http",
    "generichttp": "generic-http",
    "irodori": "irodori",
    "irodori_tts": "irodori",
    "irodori-tts": "irodori",
}


def normalize_engine(value: object) -> str:
    """Normalize CLI/env/base.toml backend names."""

    normalized = str(value or "").strip().lower()
    try:
        return _ENGINE_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError("TTS backend must be gpt-sovits, voxcpm, generic-http, or irodori") from exc


def resolve_engine(config_path: Path, override: str | None = None) -> str:
    """Resolve exactly one backend once at process startup.

    An explicit CLI/env value wins over the live base file.  The selected
    value is stored by the supervisor and is never re-read for a hot switch.
    """

    if str(override or "").strip():
        return normalize_engine(override)
    config = read_toml(Path(config_path))
    enabled = config.get("enabled_tts", {}).get("enabled", [])
    if not isinstance(enabled, list) or len(enabled) != 1:
        raise ValueError("base.toml must enable exactly one TTS backend")
    return normalize_engine(enabled[0])


def resolve_private_port(
    config_path: Path,
    engine: str,
    *,
    explicit_port: int | None = None,
    public_port: int = PUBLIC_DEFAULT_PORT,
) -> int:
    """Read one private backend port and reject a public-port collision."""

    normalized_engine = normalize_engine(engine)
    if normalized_engine == "generic-http":
        raise ValueError("GenericHTTP has no private backend port")
    if explicit_port is not None:
        port = int(explicit_port)
    else:
        filename = {
            "voxcpm": "vox.toml",
            "gpt-sovits": "gpt-sovits.toml",
            "irodori": "irodori.toml",
        }[normalized_engine]
        data = read_toml(Path(config_path).parent / filename)
        port = int(data.get("tts", {}).get("port", PRIVATE_DEFAULT_PORT))
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid private TTS port: {port}")
    if port == int(public_port):
        raise ValueError("private TTS backend cannot bind the public 9880 port")
    return port


def log(message: str) -> None:
    print(f"[TTS Runtime] {message}", flush=True)


def _load_public_main() -> Any:
    """Load the repository-root public runtime without relying on cwd/import state."""

    main_path = ADAPTER_ROOT / "main.py"
    spec = importlib.util.spec_from_file_location(PUBLIC_MAIN_MODULE_NAME, main_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"unable to resolve public runtime module at {main_path}")

    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(PUBLIC_MAIN_MODULE_NAME)
    sys.modules[PUBLIC_MAIN_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if previous is None:
            sys.modules.pop(PUBLIC_MAIN_MODULE_NAME, None)
        else:
            sys.modules[PUBLIC_MAIN_MODULE_NAME] = previous
        raise
    return module


def require_uv() -> str:
    executable = shutil.which("uv")
    if not executable:
        raise RuntimeError("未找到 uv，请先安装 uv")
    return executable


def runtime_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def explicit_tts_runtime_profile() -> str | None:
    """Return the launcher-computed TTS profile, if one was provided."""
    explicit = os.environ.get("NACHOBOT_TTS_RUNTIME_PROFILE", "").strip().lower()
    return explicit if explicit in {"gpu", "cpu"} else None


def managed_venv_dir(runtime_dir: Path) -> Path:
    # Root .bat launchers set a computed NACHOBOT_TTS_RUNTIME_PROFILE. Without it (e.g.
    # WebUI), preserve the historical shared .venv behavior exactly.
    return runtime_dir / (".venv-cpu" if explicit_tts_runtime_profile() == "cpu" else ".venv")


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    displayed: list[str] = []
    redact_next = False
    for item in map(str, cmd):
        if redact_next:
            displayed.append("<index URL redacted>")
            redact_next = False
            continue
        displayed.append(item)
        if item in {"--index-url", "--extra-index-url", "--index"}:
            redact_next = True
    log("$ " + " ".join(displayed))
    # WebUI injects its own terminal bridge and interpreter. Keep their extra
    # dependencies out of the managed Torch/public/backend environments.
    effective_env = env if env is not None else os.environ
    progress_python = effective_env.get("NACHOBOT_INSTALL_PROGRESS_PYTHON", "")
    progress_helper = effective_env.get("NACHOBOT_INSTALL_PROGRESS_HELPER", "")
    launch_cmd = cmd
    if (
        progress_python and progress_helper
        and Path(progress_python).is_file() and Path(progress_helper).is_file()
        and Path(str(cmd[0])).stem.lower() == "uv"
    ):
        launch_cmd = [progress_python, "-I", progress_helper, "--", *map(str, cmd)]
    subprocess.run(
        launch_cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        check=True,
    )


def ensure_venv(runtime_dir: Path, python_requirement: str = ">=3.11,<3.13") -> Path:
    """Ensure a profile-isolated managed TTS venv using Python 3.11 or 3.12.

    GPU keeps the historical .venv path; CPU uses .venv-cpu. Existing
    compatible environments are reused. When a rebuild is required, uv is
    instructed to prefer Python installations already present on the user's
    system. uv may download a managed interpreter only when no compatible
    local Python can be found.
    """
    venv_dir = managed_venv_dir(runtime_dir)
    python = runtime_python(venv_dir)
    supported_versions = {"3.11", "3.12"}

    def probe_version(executable: Path) -> str:
        try:
            probe = subprocess.run(
                [
                    str(executable),
                    "-c",
                    "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return probe.stdout.strip() if probe.returncode == 0 else ""
        except Exception:
            return ""

    rebuild = False
    if python.is_file():
        actual_version = probe_version(python)
        if actual_version in supported_versions:
            log(f"复用 TTS 托管虚拟环境 Python {actual_version}: {python}")
            return python

        log(
            "托管虚拟环境 Python 版本不兼容: "
            f"当前={actual_version or 'unknown'}, 需要=3.11/3.12，正在重建"
        )
        rebuild = True
    elif venv_dir.exists():
        log(f"检测到不完整或损坏的 TTS 虚拟环境，正在重建: {venv_dir}")
        rebuild = True

    if rebuild:
        for marker in runtime_dir.glob(".deps-*.ready"):
            try:
                marker.unlink()
            except FileNotFoundError:
                pass

    runtime_dir.mkdir(parents=True, exist_ok=True)
    log("创建 TTS 托管虚拟环境：优先复用本机 Python 3.11/3.12")

    cmd = [
        require_uv(),
        "venv",
        str(venv_dir),
        "--python",
        python_requirement,
        "--python-preference",
        "system",
    ]
    if rebuild:
        # The target is always NachoBot's own managed .runtime/tts/*/.venv.
        # --force is required when a previous interrupted cleanup left a
        # partial directory that uv no longer recognizes as a virtualenv.
        cmd[3:3] = ["--clear", "--force"]

    run(cmd)

    if not python.is_file():
        raise FileNotFoundError(f"虚拟环境 Python 不存在: {python}")

    actual_version = probe_version(python)
    if actual_version not in supported_versions:
        raise RuntimeError(
            "虚拟环境 Python 版本错误: "
            f"需要 3.11 或 3.12，实际 {actual_version or 'unknown'}"
        )

    log(f"TTS 托管虚拟环境使用 Python {actual_version}: {python}")
    return python


def select_gpt_python_version() -> str:
    """Prefer an existing Python 3.12, then 3.11; otherwise bootstrap 3.12."""
    uv = require_uv()
    for version in ("3.12", "3.11"):
        try:
            probe = subprocess.run(
                [uv, "python", "find", version],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception:
            continue

        python_path = probe.stdout.strip()
        if probe.returncode == 0 and python_path:
            log(f"GPT-SoVITS 复用已有 Python {version}: {python_path}")
            return version

    log("未发现可复用的 Python 3.12/3.11，将由 uv 自动准备 Python 3.12")
    return "3.12"


def torch_index_url() -> str:
    override = os.environ.get("NACHOBOT_TTS_TORCH_INDEX", "").strip()
    profile = explicit_tts_runtime_profile()
    if override:
        index_kind = _known_pytorch_index_kind(override)
        if index_kind == "cu128":
            raise RuntimeError(
                "PyTorch 2.13.0 没有发布 cu128 wheel；请改用官方 cu130 源或 CPU 源"
            )
        if index_kind == "cu126":
            raise RuntimeError(
                "GPU TTS Runtime 已统一升级到 cu130；请移除旧的 cu126 覆盖设置，"
                "或改用官方 cu130 wheel 源"
            )
        if profile == "cpu" and index_kind == "cu130":
            raise RuntimeError("CPU TTS profile 不能使用 cu130 wheel 源，请选择官方 CPU 源")
        if profile == "gpu" and index_kind == "cpu":
            raise RuntimeError("GPU TTS profile 不能使用 CPU wheel 源，请选择官方 cu130 源")
        return override
    if profile == "cpu":
        return PYTORCH_CPU_INDEX
    if profile == "gpu" or shutil.which("nvidia-smi"):
        return PYTORCH_CUDA_INDEX
    return PYTORCH_CPU_INDEX


def _known_pytorch_index_kind(index_url: str) -> str | None:
    parsed = urlsplit(index_url.strip())
    if (parsed.hostname or "").lower() != "download.pytorch.org":
        return None
    path = parsed.path.rstrip("/").lower()
    if path == "/whl/cpu":
        return "cpu"
    if path == "/whl/cu126":
        return "cu126"
    if path == "/whl/cu130":
        return "cu130"
    if path == "/whl/cu128":
        return "cu128"
    return None


def _safe_torch_index_label(index_url: str) -> str:
    parsed = urlsplit(index_url)
    if (parsed.hostname or "").lower() == "download.pytorch.org":
        path = parsed.path.rstrip("/")
        return f"https://download.pytorch.org{path}"
    return "custom package index (credentials hidden)"


def _tts_torch_profile(index_url: str) -> str:
    explicit = explicit_tts_runtime_profile()
    if explicit:
        return explicit
    if _known_pytorch_index_kind(index_url) == "cpu":
        return "cpu"
    if _known_pytorch_index_kind(index_url) in {"cu126", "cu130"}:
        return "gpu"
    return "gpu" if shutil.which("nvidia-smi") else "cpu"


def _tts_torch_versions(profile: str) -> dict[str, str]:
    suffix = "+cpu" if profile == "cpu" else "+cu130"
    return {
        "torch": f"2.13.0{suffix}",
        "torchaudio": f"2.11.0{suffix}",
    }


def _tts_triton_required(profile: str, backend: str) -> bool:
    return os.name == "nt" and profile == "gpu" and backend == "voxcpm-2.0.3"


def _tts_onnxruntime_gpu_required(profile: str, backend: str) -> bool:
    if profile != "gpu" or backend != GPT_REF:
        return False

    machine = platform.machine().strip().lower()
    if machine:
        return machine in {"x86_64", "amd64"}

    # Match GPT-SoVITS' upstream wheel markers while tolerating hosts where
    # platform.machine() is empty (for example, sanitized Windows launches).
    build_arch = sysconfig.get_platform().lower().rsplit("-", 1)[-1]
    return build_arch in {"x86_64", "amd64"}


def _tts_recipe(profile: str, backend: str) -> str:
    if _tts_onnxruntime_gpu_required(profile, backend):
        version_tag = TTS_GPT_ORT_GPU_VERSION.replace(".", "")
        return f"{TTS_TORCH_RECIPE}-gpt-ortgpu{version_tag}-v1"
    return TTS_TORCH_RECIPE


def _tts_dependency_constraints(runtime_dir: Path, profile: str, backend: str) -> Path:
    versions = _tts_torch_versions(profile)
    versions["torchvision"] = f"0.28.0{'+cpu' if profile == 'cpu' else '+cu130'}"
    requirements = [f"{package}=={version}" for package, version in versions.items()]
    if _tts_triton_required(profile, backend):
        requirements.append(f"triton-windows=={TTS_TRITON_WINDOWS_VERSION}")
    if _tts_onnxruntime_gpu_required(profile, backend):
        requirements.append(f"onnxruntime-gpu=={TTS_GPT_ORT_GPU_VERSION}")
    path = runtime_dir / f"constraints-{_tts_recipe(profile, backend)}-{profile}.txt"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    content = "\n".join(requirements) + "\n"
    if not path.is_file() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
    return path


def _tts_marker_path(runtime_dir: Path, backend: str, python_version: str, profile: str) -> Path:
    return runtime_dir / (
        f".deps-{profile}-{backend}-py{python_version}-{_tts_recipe(profile, backend)}.ready"
    )


def _invalidate_old_tts_markers(runtime_dir: Path, current_marker: Path) -> None:
    for old_marker in runtime_dir.glob(".deps-*.ready"):
        if old_marker == current_marker:
            continue
        try:
            old_marker.unlink()
        except FileNotFoundError:
            pass


def _tts_marker_values(
    index_url: str,
    python_version: str,
    profile: str,
    backend: str,
) -> dict[str, str]:
    values = {
        "recipe": _tts_recipe(profile, backend),
        "profile": profile,
        "python": python_version,
        "index_sha256": hashlib.sha256(index_url.encode("utf-8")).hexdigest(),
        **_tts_torch_versions(profile),
    }
    if _tts_onnxruntime_gpu_required(profile, backend):
        values["onnxruntime-gpu"] = TTS_GPT_ORT_GPU_VERSION
    if _tts_triton_required(profile, backend):
        values["triton-windows"] = TTS_TRITON_WINDOWS_VERSION
    return values


def _read_tts_installed_versions(
    python: Path,
    include_triton: bool,
    include_onnxruntime_gpu: bool = False,
) -> dict[str, str] | None:
    packages = ["torch", "torchaudio"]
    if include_triton:
        packages.append("triton-windows")
    if include_onnxruntime_gpu:
        packages.append("onnxruntime-gpu")
    script = (
        "from importlib.metadata import PackageNotFoundError, version; import json; "
        f"names={packages!r}; result={{}}; "
        "\nfor name in names:\n"
        "    try: result[name] = version(name)\n"
        "    except PackageNotFoundError: result[name] = ''\n"
        "print(json.dumps(result, sort_keys=True))"
    )
    try:
        probe = subprocess.run(
            [str(python), "-c", script],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception:
        return None
    if probe.returncode != 0:
        return None
    try:
        result = json.loads(probe.stdout.strip())
    except (TypeError, json.JSONDecodeError):
        return None
    return result if isinstance(result, dict) else None


def _tts_stack_matches(python: Path, profile: str, backend: str) -> bool:
    expected = _tts_torch_versions(profile)
    include_triton = _tts_triton_required(profile, backend)
    include_onnxruntime_gpu = _tts_onnxruntime_gpu_required(profile, backend)
    if include_triton:
        expected["triton-windows"] = TTS_TRITON_WINDOWS_VERSION
    if include_onnxruntime_gpu:
        expected["onnxruntime-gpu"] = TTS_GPT_ORT_GPU_VERSION
    return _read_tts_installed_versions(
        python,
        include_triton,
        include_onnxruntime_gpu,
    ) == expected


def _tts_marker_is_valid(
    marker: Path,
    python: Path,
    index_url: str,
    python_version: str,
    profile: str,
    backend: str,
) -> bool:
    if not marker.is_file():
        return False
    try:
        values = dict(
            line.split("=", 1)
            for line in marker.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
    except OSError:
        return False
    return values == _tts_marker_values(index_url, python_version, profile, backend) and _tts_stack_matches(
        python, profile, backend
    )


def _write_tts_marker(
    marker: Path,
    python: Path,
    index_url: str,
    python_version: str,
    profile: str,
    backend: str,
) -> None:
    if not _tts_stack_matches(python, profile, backend):
        actual = _read_tts_installed_versions(
            python,
            _tts_triton_required(profile, backend),
            _tts_onnxruntime_gpu_required(profile, backend),
        )
        expected = _tts_torch_versions(profile)
        if _tts_triton_required(profile, backend):
            expected["triton-windows"] = TTS_TRITON_WINDOWS_VERSION
        if _tts_onnxruntime_gpu_required(profile, backend):
            expected["onnxruntime-gpu"] = TTS_GPT_ORT_GPU_VERSION
        raise RuntimeError(
            "TTS 安全依赖版本不匹配，拒绝写入就绪标记: "
            f"expected={expected}, installed={actual}"
        )
    values = _tts_marker_values(index_url, python_version, profile, backend)
    marker.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")


def _install_tts_torch_stack(
    python: Path,
    constraints: Path,
    index_url: str,
    profile: str,
    backend: str,
) -> None:
    versions = _tts_torch_versions(profile)
    packages = [f"{package}=={version}" for package, version in versions.items()]
    run([
        require_uv(), "pip", "install", "--python", str(python),
        "--constraint", str(constraints), *packages, "--index-url", index_url,
    ])
    if _tts_triton_required(profile, backend):
        run([
            require_uv(), "pip", "install", "--python", str(python),
            "--constraint", str(constraints),
            f"triton-windows=={TTS_TRITON_WINDOWS_VERSION}",
        ])


def read_toml(path: Path) -> dict:
    import tomllib

    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def hf_endpoints() -> list[str]:
    endpoints: list[str] = []
    for env_name in ("NACHOBOT_HF_ENDPOINT", "HF_ENDPOINT"):
        endpoint = os.environ.get(env_name, "").strip().rstrip("/")
        if endpoint:
            endpoints.append(endpoint)
    endpoints.extend(("https://hf-mirror.com", "https://huggingface.co"))
    return list(dict.fromkeys(endpoints))


def base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["HF_HOME"] = str(HF_CACHE)
    env["HF_HUB_DISABLE_XET"] = os.environ.get("HF_HUB_DISABLE_XET", "1")
    env["HF_HUB_ETAG_TIMEOUT"] = os.environ.get("HF_HUB_ETAG_TIMEOUT", "10")
    env["HF_HUB_DOWNLOAD_TIMEOUT"] = os.environ.get("HF_HUB_DOWNLOAD_TIMEOUT", "60")

    # Managed subprocesses use the first endpoint as their ordinary Hub
    # endpoint. Components that need failover resolve snapshots before launch.
    endpoints = hf_endpoints()
    if endpoints:
        env["HF_ENDPOINT"] = endpoints[0]

    ffmpeg_runtime = PROJECT_ROOT / ".runtime" / "ffmpeg"
    if ffmpeg_runtime.is_dir():
        executable_names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)
        for executable_name in executable_names:
            executable = next(ffmpeg_runtime.rglob(executable_name), None)
            if executable is not None:
                env["PATH"] = str(executable.parent) + os.pathsep + env.get("PATH", "")
                break
    return env


def _irodori_config_path(config_path: Path | None = None) -> Path:
    return (config_path or (ADAPTER_ROOT / "configs" / "irodori.toml")).expanduser().resolve(strict=False)


def _irodori_paths(config_path: Path | None = None) -> tuple[dict[str, Any], Path, Path | None, bool]:
    """Return parsed config, source, configured Python, and managed-source flag."""

    selected_config = _irodori_config_path(config_path)
    parsed = load_irodori_config(selected_config)
    runtime = parsed["runtime"]
    managed_source = runtime["repo_path"] is None
    source = runtime["repo_path"] or (IRODORI_RUNTIME_DIR / "source")
    return parsed, Path(source).expanduser().resolve(strict=False), runtime["python_path"], managed_source


def _ensure_pinned_irodori_source(source_dir: Path) -> None:
    """Create or validate the adapter-owned immutable Irodori source checkout."""

    newly_cloned = False
    if not source_dir.exists():
        source_dir.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--no-checkout", IRODORI_REPO_URL, str(source_dir)])
        newly_cloned = True
    if not (source_dir / ".git").exists():
        raise RuntimeError(f"managed Irodori source is not a Git checkout: {source_dir}")

    if newly_cloned:
        subprocess.run(
            ["git", "-C", str(source_dir), "fetch", "--depth", "1", "origin", IRODORI_SOURCE_COMMIT],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(source_dir), "checkout", "--detach", IRODORI_SOURCE_COMMIT],
            check=True,
        )
        return

    head = subprocess.run(
        ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if head == IRODORI_SOURCE_COMMIT:
        if (source_dir / "pyproject.toml").is_file() and (source_dir / "irodori_tts" / "inference_runtime.py").is_file():
            return
        subprocess.run(
            ["git", "-C", str(source_dir), "checkout", "--detach", IRODORI_SOURCE_COMMIT],
            check=True,
        )
        return
    dirty = subprocess.run(
        ["git", "-C", str(source_dir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError(
            f"managed Irodori source has local changes at {source_dir}; preserve them and set [runtime].repo_path"
        )
    subprocess.run(
        ["git", "-C", str(source_dir), "fetch", "--depth", "1", "origin", IRODORI_SOURCE_COMMIT],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source_dir), "checkout", "--detach", IRODORI_SOURCE_COMMIT],
        check=True,
    )


def _irodori_import_ready(
    python: Path,
    source_dir: Path,
    env: dict[str, str],
    *,
    require_recipe: bool = False,
    require_lora: bool = False,
) -> bool:
    if not python.is_file():
        return False
    probe_lines = [
        "import importlib.metadata as metadata",
        "import re",
        "import sys",
        "assert sys.version_info >= (3, 11)",
        "import sentencepiece, torch, torchaudio, torchao",
        "torchao_version = tuple(map(int, metadata.version('torchao').split('.')[:2]))",
        "assert torchao_version == (0, 16)",
        "from torchao.dtypes import AffineQuantizedTensor",
        "from torchao.prototype.safetensors.safetensors_support import unflatten_tensor_state_dict",
        "from dacvae import DACVAE",
        "assert callable(DACVAE.load)",
        "from transformers import AutoTokenizer",
        "from irodori_tts.codec import DACVAECodec",
        "from irodori_tts.inference_runtime import InferenceRuntime",
        "from irodori_tts.quantization import _require_torchao_safetensors",
        "_require_torchao_safetensors()",
    ]
    if require_lora:
        probe_lines.extend(("from peft import PeftModel", "from irodori_tts.lora import load_lora_adapter"))
    if os.name == "nt":
        probe_lines.append("from accelerate import init_empty_weights")
    if require_recipe:
        probe_lines.extend(
            (
                f"assert metadata.version('torchao') == {IRODORI_TORCHAO_VERSION!r}",
                "assert metadata.version('peft') == '0.18.1'",
                "assert metadata.version('accelerate') == '1.13.0'",
                "sp = re.fullmatch(r'(\\d+)\\.(\\d+)\\.(\\d+)(?:[+.-].*)?', metadata.version('sentencepiece'))",
                "assert sp and (0, 2, 1) <= tuple(map(int, sp.groups())) < (0, 3, 0)",
                "st = re.fullmatch(r'(\\d+)\\.(\\d+)\\.(\\d+)(?:[+.-].*)?', metadata.version('safetensors'))",
                "assert st and (0, 8, 0) <= tuple(map(int, st.groups())) < (0, 9, 0)",
                "import json, silentcipher",
                "assert callable(getattr(silentcipher, 'get_model', None))",
                "def vcs_commit(name):",
                "    raw = metadata.distribution(name).read_text('direct_url.json') or '{}'",
                "    return json.loads(raw).get('vcs_info', {}).get('commit_id', '')",
                f"assert vcs_commit('dacvae').startswith({IRODORI_DACVAE_COMMIT!r})",
                f"assert vcs_commit('silentcipher').startswith({IRODORI_SILENTCIPHER_COMMIT!r})",
            )
        )
    elif require_lora:
        probe_lines.append("from peft import PeftModel")
    probe_lines.extend(("from irodori_tts.tokenizer import PretrainedTextTokenizer", "from irodori_tts.watermark import SilentCipherWatermarker"))
    import_probe = "\n".join(probe_lines)
    probe_env = env.copy()
    # TorchAO 0.16's pure-Python quantized tensor path supports the shared
    # Torch build used here; skip its optional, version-coupled extension only
    # in this probe process and the eventual Irodori child.
    probe_env["TORCHAO_FORCE_SKIP_LOADING_SO_FILES"] = "1"
    try:
        result = subprocess.run(
            [
                str(python),
                "-c",
                import_probe,
            ],
            cwd=str(source_dir),
            env=probe_env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _irodori_runtime_profile(parsed: dict[str, Any]) -> str:
    """Resolve Irodori to the adapter's already selected GPU or CPU profile."""

    runtime = parsed["runtime"]
    selected_profile = explicit_tts_runtime_profile()
    configured_devices = tuple(
        str(device).strip().lower()
        for device in (runtime["model_device"], runtime["codec_device"])
    )
    if selected_profile is not None:
        if selected_profile == "cpu" and any(device.startswith("cuda") for device in configured_devices):
            raise RuntimeError(
                "the selected CPU TTS profile conflicts with an explicit CUDA device in irodori.toml"
            )
        return selected_profile
    if any(device.startswith("cuda") for device in configured_devices):
        return "gpu"
    if runtime["model_device"].strip().lower() == "cpu":
        return "cpu"

    config_dir = _irodori_config_path(parsed["config_path"]).parent
    choice = read_device_choice("tts", config_dir=config_dir)
    if choice == "cpu":
        return "cpu"
    if GPU_UUID_PATTERN.fullmatch(choice):
        if choice not in detected_nvidia_gpu_uuids():
            raise RuntimeError(
                f"configured NVIDIA GPU {choice} is unavailable; choose another GPU or set TTS to auto/CPU"
            )
        return "gpu"

    return "gpu" if auto_cuda_available(gpu_uuids=detected_nvidia_gpu_uuids()) else "cpu"


def _irodori_shared_runtime_ready(python: Path, profile: str) -> bool:
    """Match the existing WebUI shared-profile import and Torch-flavor checks."""

    if not python.is_file():
        return False
    expected_cuda = "True" if profile == "gpu" else "False"
    check = (
        "import importlib; "
        "[(importlib.import_module(name)) for name in ('torch', 'torchvision', 'torchaudio', 'transformers', 'timm', 'sherpa_onnx')]; "
        "import torch, torchaudio, torchvision; "
        "assert torch.__version__.split('+', 1)[0].startswith('2.13.'); "
        "assert torchaudio.__version__ and torchvision.__version__; "
        f"assert (torch.version.cuda is not None) is {expected_cuda}"
    )
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", check],
            cwd=str(ADAPTER_ROOT),
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _prepare_irodori_shared_profile(profile: str) -> None:
    """Use the established profile bootstrap if the selected shared env is absent or damaged."""

    from scripts import prepare_model_runtime

    environ = os.environ.copy()
    environ["NACHOBOT_TTS_ENGINE"] = "irodori"
    environ["NACHOBOT_TTS_RUNTIME_PROFILE"] = profile
    if profile == "cpu":
        environ["CUDA_VISIBLE_DEVICES"] = ""
    plan = prepare_model_runtime.build_runtime_plan(
        "lite",
        adapter_root=ADAPTER_ROOT,
        environ=environ,
        detected_uuids=detected_nvidia_gpu_uuids(),
    )
    # The direct WebUI profile selection is authoritative when TTS is set to
    # auto; all paths still use prepare_model_runtime's existing manifest sync.
    plan = replace(plan, tts_profile=profile)
    launch_dir = ADAPTER_ROOT / ".runtime" / "launch"
    transport = launch_dir / f"model-runtime-irodori-{uuid.uuid4().hex}.cmd"
    try:
        prepare_model_runtime.prepare_runtime(plan, transport)
    finally:
        transport.unlink(missing_ok=True)


def _irodori_installed_constraints(python: Path, source_dir: Path, env: dict[str, str]) -> tuple[str, str | None]:
    """Capture exact installed package versions and the narrow protobuf override."""

    inventory_script = (
        "import importlib.metadata as metadata, json; "
        "items = {}; "
        "normalize = lambda value: value.lower().replace('_', '-').replace('.', '-'); "
        "[items.setdefault(normalize(dist.metadata['Name']), (dist.metadata['Name'], dist.version)) "
        "for dist in metadata.distributions() if dist.metadata.get('Name')]; "
        "print(json.dumps([items[key] for key in sorted(items)]))"
    )
    protobuf_script = (
        "import importlib.metadata as metadata; "
        "\ntry: print(metadata.version('protobuf'))\n"
        "except metadata.PackageNotFoundError: print('')"
    )
    try:
        inventory = subprocess.run(
            [str(python), "-c", inventory_script],
            cwd=str(source_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        protobuf = subprocess.run(
            [str(python), "-c", protobuf_script],
            cwd=str(source_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot inspect installed shared runtime packages: {python}") from exc
    if inventory.returncode != 0 or protobuf.returncode != 0:
        raise RuntimeError(f"cannot inspect installed shared runtime packages: {python}")
    try:
        installed = json.loads(inventory.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("shared runtime returned invalid package metadata") from exc
    constraints = "\n".join(f"{name}=={version}" for name, version in installed) + "\n"
    protobuf_version = protobuf.stdout.strip()
    override = f"protobuf=={protobuf_version}\n" if protobuf_version else None
    return constraints, override


def _install_irodori_inference_requirements(
    python: Path,
    source_dir: Path,
    env: dict[str, str],
) -> None:
    if not IRODORI_REQUIREMENTS_FILE.is_file():
        raise FileNotFoundError(f"Irodori inference requirements are missing: {IRODORI_REQUIREMENTS_FILE}")
    constraints, override = _irodori_installed_constraints(python, source_dir, env)
    constraint_path: Path | None = None
    override_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix="-irodori-constraints.txt", delete=False
        ) as handle:
            handle.write(constraints)
            constraint_path = Path(handle.name)
        command = [
            require_uv(),
            "pip",
            "install",
            "--python",
            str(python),
            "--constraint",
            str(constraint_path),
        ]
        if override:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix="-irodori-overrides.txt", delete=False
            ) as handle:
                handle.write(override)
                override_path = Path(handle.name)
            command.extend(["--override", str(override_path)])
        command.extend(["--requirement", str(IRODORI_REQUIREMENTS_FILE)])
        log("Preparing Irodori inference packages in the selected shared Multimodal runtime")
        run(command, cwd=source_dir, env=env)
    finally:
        if constraint_path is not None:
            constraint_path.unlink(missing_ok=True)
        if override_path is not None:
            override_path.unlink(missing_ok=True)


def _irodori_requires_pread(checkpoint: Path | None) -> bool:
    if os.name != "nt":
        return False
    return checkpoint is None or checkpoint.suffix.lower() == ".safetensors"


def _irodori_safetensors_version(python: Path, source_dir: Path, env: dict[str, str]) -> str | None:
    """Read package metadata with the selected interpreter without importing Torch."""

    try:
        result = subprocess.run(
            [
                str(python),
                "-c",
                "from importlib.metadata import PackageNotFoundError, version; "
                "\ntry: print(version('safetensors'))\n"
                "except PackageNotFoundError: print('')",
            ],
            cwd=str(source_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _irodori_safetensors_is_pread_compatible(installed: str | None) -> bool:
    if not installed:
        return False
    match = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)([0-9A-Za-z.+-]*)\s*", installed)
    if match is None:
        return False
    parts = tuple(int(match.group(index)) for index in (1, 2, 3))
    suffix = match.group(4).lower()
    if parts == (0, 8, 0) and suffix and not suffix.startswith(("+", ".post")):
        return False
    return parts >= (0, 8, 0)


def _ensure_irodori_safetensors(
    python: Path,
    source_dir: Path,
    env: dict[str, str],
    *,
    checkpoint: Path | None,
    managed_environment: bool,
) -> None:
    if not _irodori_requires_pread(checkpoint):
        return
    installed = _irodori_safetensors_version(python, source_dir, env)
    if _irodori_safetensors_is_pread_compatible(installed):
        return
    actual = installed or "missing or unreadable"
    if not managed_environment:
        raise RuntimeError(
            f"configured Irodori Python {python} has safetensors {actual}; Windows requires "
            f"{IRODORI_SAFETENSORS_REQUIREMENT} for PREAD checkpoint loading. "
            "Upgrade this explicit environment manually; the TTS manager will not install or sync it."
        )
    log(f"Updating managed Irodori runtime dependency: {IRODORI_SAFETENSORS_REQUIREMENT}")
    run(
        [
            require_uv(),
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            IRODORI_SAFETENSORS_REQUIREMENT,
        ],
        cwd=source_dir,
        env=env,
    )
    installed = _irodori_safetensors_version(python, source_dir, env)
    if not _irodori_safetensors_is_pread_compatible(installed):
        raise RuntimeError(
            f"managed Irodori runtime still has safetensors {installed or 'missing or unreadable'} "
            f"after installing {IRODORI_SAFETENSORS_REQUIREMENT}; expected PREAD support."
        )


def prepare_irodori(config_path: Path | None = None) -> tuple[Path, Path]:
    """Prepare pinned Irodori source and inference deps in the selected shared profile."""

    parsed, source_dir, configured_python, managed_source = _irodori_paths(config_path)
    runtime = parsed["runtime"]
    if managed_source:
        _ensure_pinned_irodori_source(source_dir)
    if not (source_dir / "pyproject.toml").is_file() or not (source_dir / "irodori_tts" / "inference_runtime.py").is_file():
        raise RuntimeError(f"configured Irodori source is incomplete: {source_dir}")

    checkpoint = runtime["checkpoint"]
    generation = parsed["generation"]
    if checkpoint is not None and not checkpoint.is_file():
        raise FileNotFoundError(f"configured Irodori checkpoint does not exist: {checkpoint}")
    if generation["ref_wav"] is not None and not generation["ref_wav"].is_file():
        raise FileNotFoundError(f"configured Irodori reference audio does not exist: {generation['ref_wav']}")
    if generation["lora_adapter"] is not None and not generation["lora_adapter"].is_dir():
        raise FileNotFoundError(f"configured Irodori LoRA adapter does not exist: {generation['lora_adapter']}")

    if configured_python is not None:
        python = Path(configured_python)
        if not python.is_file():
            raise FileNotFoundError(f"configured Irodori Python does not exist: {python}")
        env = base_env()
        _ensure_irodori_safetensors(
            python,
            source_dir,
            env,
            checkpoint=runtime["checkpoint"],
            managed_environment=False,
        )
        if not _irodori_import_ready(
            python,
            source_dir,
            env,
            require_lora=generation["lora_adapter"] is not None,
        ):
            raise RuntimeError(
                "configured Irodori Python does not satisfy the pinned inference imports; "
                "prepare that explicit environment manually"
            )
    else:
        profile = _irodori_runtime_profile(parsed)
        venv_dir = ADAPTER_ROOT / (".venv-cpu" if profile == "cpu" else ".venv")
        python = runtime_python(venv_dir)
        env = base_env()
        if runtime["hf_home"] is not None:
            hf_home = Path(runtime["hf_home"])
            hf_home.mkdir(parents=True, exist_ok=True)
            env["HF_HOME"] = str(hf_home)
        if not _irodori_shared_runtime_ready(python, profile):
            log(f"Selected shared {profile.upper()} runtime is absent or damaged; preparing its standard profile")
            _prepare_irodori_shared_profile(profile)
        if not _irodori_shared_runtime_ready(python, profile):
            raise RuntimeError(
                f"the standard shared {profile.upper()} runtime preparation did not produce a valid profile: {python}"
            )

        if not _irodori_import_ready(python, source_dir, env, require_recipe=True, require_lora=True):
            _install_irodori_inference_requirements(python, source_dir, env)
        if not _irodori_import_ready(python, source_dir, env, require_recipe=True, require_lora=True):
            raise RuntimeError(
                f"the shared {profile.upper()} runtime still lacks the pinned Irodori inference imports: {python}"
            )

    log(f"Irodori runtime ready: source={source_dir}, python={python}")
    return python, source_dir


def build_irodori_command(
    port: int = PRIVATE_DEFAULT_PORT,
    host: str = PRIVATE_DEFAULT_HOST,
    config_path: Path | None = None,
) -> tuple[list[str], Path, dict[str, str]]:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Irodori raw API must bind to loopback")
    if int(port) == PUBLIC_DEFAULT_PORT or not 1 <= int(port) <= 65535:
        raise ValueError("Irodori raw API must use a valid private port")
    selected_config = _irodori_config_path(config_path)
    python, source_dir = prepare_irodori(selected_config)
    env = base_env()
    parsed = load_irodori_config(selected_config)
    if parsed["runtime"]["hf_home"] is not None:
        hf_home = Path(parsed["runtime"]["hf_home"])
        hf_home.mkdir(parents=True, exist_ok=True)
        env["HF_HOME"] = str(hf_home)
    env.pop("NACHOBOT_TTS_ENGINE_HOST", None)
    env.pop("NACHOBOT_TTS_ENGINE_PORT", None)
    env["TORCHAO_FORCE_SKIP_LOADING_SO_FILES"] = "1"
    server = ADAPTER_ROOT / "src" / "tts" / "backends" / "Irodori" / "irodori_api_server.py"
    cmd = [
        str(python),
        str(server),
        "--config",
        str(selected_config),
        "--repo-path",
        str(source_dir),
        "--host",
        host,
        "--port",
        str(port),
    ]
    log(f"启动 Irodori raw API: {host}:{port}，source={source_dir}")
    return cmd, source_dir, env


def serve_irodori(
    port: int = PRIVATE_DEFAULT_PORT,
    host: str = PRIVATE_DEFAULT_HOST,
    config_path: Path | None = None,
) -> int:
    cmd, cwd, env = build_irodori_command(port, host, config_path)
    return subprocess.call(cmd, cwd=str(cwd), env=env)


def hf_endpoint() -> str:
    endpoints = hf_endpoints()
    return endpoints[0] if endpoints else "https://huggingface.co"


def resolve_hf_snapshot(repo_id: str) -> Path:
    """Return a complete cached Hub snapshot, with endpoint failover."""
    from huggingface_hub import snapshot_download

    cache_dir = HF_CACHE / "hub"
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        cached = Path(
            snapshot_download(
                repo_id=repo_id,
                cache_dir=str(cache_dir),
                local_files_only=True,
            )
        )
        log(f"使用本地 Hugging Face 模型缓存: {repo_id} -> {cached}")
        return cached
    except Exception as exc:
        log(f"本地模型缓存不完整，将尝试在线下载: {repo_id} ({exc})")

    failures: list[str] = []
    for endpoint in hf_endpoints():
        try:
            log(f"通过 {endpoint} 下载模型快照: {repo_id}")
            snapshot = Path(
                snapshot_download(
                    repo_id=repo_id,
                    cache_dir=str(cache_dir),
                    endpoint=endpoint,
                    max_workers=4,
                )
            )
            log(f"模型快照下载完成: {snapshot}")
            return snapshot
        except Exception as exc:
            failures.append(f"{endpoint}: {exc}")
            log(f"通过 {endpoint} 下载失败: {exc}")

    raise RuntimeError(
        f"无法下载 Hugging Face 模型 {repo_id}；已尝试自定义端点、"
        f"hf-mirror.com 和 huggingface.co。{' | '.join(failures)}"
    )


def use_hf_mirror_direct_download() -> bool:
    return hf_endpoint().lower() in {"https://hf-mirror.com", "http://hf-mirror.com"}


def download_http(url: str, destination: Path) -> None:
    """Download with plain HTTP GET and show progress without Hub HEAD metadata."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = Request(url, headers={"User-Agent": "NachoBot-TTS-Runtime/1.0"})
    try:
        with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            total_raw = response.headers.get("Content-Length")
            total = int(total_raw) if total_raw and total_raw.isdigit() else 0
            downloaded = 0
            chunk_size = 1024 * 1024
            bar_width = 30

            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)

                downloaded_mb = downloaded / (1024 * 1024)
                if total > 0:
                    ratio = min(downloaded / total, 1.0)
                    filled = int(bar_width * ratio)
                    bar = "#" * filled + "-" * (bar_width - filled)
                    total_mb = total / (1024 * 1024)
                    print(
                        f"\r[TTS Runtime] [{bar}] {ratio * 100:6.2f}% "
                        f"{downloaded_mb:.1f}/{total_mb:.1f} MiB",
                        end="",
                        flush=True,
                    )
                else:
                    print(
                        f"\r[TTS Runtime] 下载中: {downloaded_mb:.1f} MiB",
                        end="",
                        flush=True,
                    )

            print(flush=True)
        temporary.replace(destination)
    except Exception:
        print(flush=True)
        temporary.unlink(missing_ok=True)
        raise


def download_hf_file_direct(repo_id: str, filename: str, destination: Path) -> None:
    endpoint = hf_endpoint() or "https://huggingface.co"
    url = f"{endpoint}/{repo_id}/resolve/main/{filename}"
    log(f"HF 直链下载: {url}")
    download_http(url, destination)


def prepare_emotion_classifier() -> Path | None:
    """预下载 Vox 情绪分类模型到 Adapter 共用的 Hugging Face 缓存。"""
    config_path = ADAPTER_ROOT / "configs" / "vox.toml"
    emotion = read_toml(config_path).get("emotion", {})
    if not emotion.get("enabled", True):
        log("情感分类已禁用，跳过情绪分类模型准备")
        return None

    model_name = str(
        emotion.get(
            "classifier_model",
            "tabularisai/multilingual-emotion-classification",
        )
    ).strip()
    if not model_name:
        model_name = "tabularisai/multilingual-emotion-classification"

    model_path = Path(model_name).expanduser()
    if model_path.is_dir():
        log(f"使用本地情绪分类模型: {model_path}")
        return model_path

    log(f"准备情绪分类模型: {model_name}")
    snapshot = resolve_hf_snapshot(model_name)
    log(f"情绪分类模型已就绪: {snapshot}")
    return snapshot


def prepare_voxcpm() -> Path:
    prepare_emotion_classifier()
    runtime_dir = RUNTIME_ROOT / "voxcpm"
    python = ensure_venv(runtime_dir)

    probe = subprocess.run(
        [str(python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    python_version = probe.stdout.strip() if probe.returncode == 0 else "unknown"
    index_url = torch_index_url()
    profile = _tts_torch_profile(index_url)
    marker = _tts_marker_path(runtime_dir, "voxcpm-2.0.3", python_version, profile)
    _invalidate_old_tts_markers(runtime_dir, marker)
    if _tts_marker_is_valid(marker, python, index_url, python_version, profile, "voxcpm-2.0.3"):
        return python

    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    constraints = _tts_dependency_constraints(runtime_dir, profile, "voxcpm-2.0.3")
    log(f"安装 VoxCPM 安全 PyTorch 运行时: {_safe_torch_index_label(index_url)} (Python {python_version})")
    _install_tts_torch_stack(python, constraints, index_url, profile, "voxcpm-2.0.3")
    run([
        require_uv(), "pip", "install", "--python", str(python),
        "--constraint", str(constraints), "voxcpm==2.0.3",
    ])
    _write_tts_marker(marker, python, index_url, python_version, profile, "voxcpm-2.0.3")
    log(f"VoxCPM 托管运行时就绪: {runtime_dir} (Python {python_version})")
    return python


def resolve_vox_model_and_lora(config_path: Path | None = None) -> tuple[str, str]:
    config_path = config_path or (ADAPTER_ROOT / "configs" / "vox.toml")
    tts = read_toml(config_path).get("tts", {})

    configured_model = str(tts.get("model_dir", "")).strip()
    model = configured_model or "openbmb/VoxCPM2"
    if configured_model:
        candidate = Path(configured_model).expanduser()
        if candidate.is_absolute() and not candidate.exists():
            log(f"旧 VoxCPM 模型路径不存在，自动改用 openbmb/VoxCPM2: {candidate}")
            model = "openbmb/VoxCPM2"

    lora = str(tts.get("lora_weights_path", "")).strip()
    if lora:
        candidate = Path(lora).expanduser()
        if not candidate.is_absolute():
            candidate = (config_path.parent / candidate).resolve()
        if candidate.is_dir():
            lora = str(candidate)
        else:
            log(f"LoRA 路径不存在，跳过: {candidate}")
            lora = ""
    return model, lora


def build_voxcpm_command(
    port: int = PRIVATE_DEFAULT_PORT,
    host: str = PRIVATE_DEFAULT_HOST,
    config_path: Path | None = None,
) -> tuple[list[str], Path, dict[str, str]]:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Vox raw API must bind to loopback")
    if int(port) == PUBLIC_DEFAULT_PORT:
        raise ValueError("Vox raw API cannot bind the public 9880 port")
    python = prepare_voxcpm()
    model, lora = resolve_vox_model_and_lora(config_path)

    # VoxCPM.from_pretrained ultimately uses huggingface_hub.snapshot_download.
    # Resolve remote model IDs here first so we can fail over between mirrors
    # and then give VoxCPM a stable local directory.
    model_path = Path(model).expanduser()
    if not model_path.is_dir():
        model = str(resolve_hf_snapshot(model))

    server = ADAPTER_ROOT / "src" / "tts" / "backends" / "Vox" / "vox_api_server.py"
    cmd = [
        str(python), str(server),
        "--host", host,
        "--port", str(port),
        "--config", str((config_path or (ADAPTER_ROOT / "configs" / "vox.toml")).expanduser().resolve(strict=False)),
        "--model-dir", model,
        "--no-denoiser",
    ]
    if lora:
        cmd.extend(["--lora-weights", lora])
    log(f"启动 VoxCPM API: {host}:{port}，model={model}")
    return cmd, ADAPTER_ROOT, base_env()


def serve_voxcpm(
    port: int = PRIVATE_DEFAULT_PORT,
    host: str = PRIVATE_DEFAULT_HOST,
    config_path: Path | None = None,
) -> int:
    cmd, cwd, env = build_voxcpm_command(port, host, config_path)
    return subprocess.call(cmd, cwd=str(cwd), env=env)


def ensure_gpt_source(runtime_dir: Path) -> Path:
    source_dir = runtime_dir / "source"
    api_file = source_dir / "api_v2.py"
    marker = source_dir / ".nachobot_ref"
    if api_file.is_file() and marker.is_file():
        if marker.read_text(encoding="utf-8").strip() == GPT_REF:
            return source_dir

    if source_dir.exists():
        shutil.rmtree(source_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    git = shutil.which("git")
    if not git:
        raise RuntimeError("自动部署 GPT-SoVITS 需要 Git")
    run([
        git, "clone", "--depth", "1", "--branch", GPT_REF,
        GPT_REPO, str(source_dir),
    ])
    if not api_file.is_file():
        raise FileNotFoundError(f"GPT-SoVITS API 不存在: {api_file}")
    marker.write_text(GPT_REF + "\n", encoding="utf-8")
    return source_dir


def ensure_gpt_assets(python: Path, source_dir: Path, runtime_dir: Path) -> None:
    pretrained_dir = source_dir / "GPT_SoVITS" / "pretrained_models"

    # v2Pro inference-only assets. Do not fetch discriminator/training weights (s2D*)
    # or unrelated model generations. SoVITS LoRA requires its matching generator base,
    # while both Pro variants require the SV speaker encoder.
    _, configured_sovits = resolve_gpt_preset_weights()
    sovits_version = detect_sovits_version(configured_sovits) if configured_sovits else "v2Pro"
    if sovits_version == "v2ProPlus":
        sovits_base = "v2Pro/s2Gv2ProPlus.pth"
    else:
        sovits_base = "v2Pro/s2Gv2Pro.pth"

    # Keep the upstream v2 default inference weights available as a fallback.
    # The managed inference config and public preset client select this same
    # complete pair when user-configured weights are unavailable.
    fallback_t2s = FALLBACK_GPT
    fallback_vits = FALLBACK_SOVITS

    inference_patterns = [
        "chinese-hubert-base/**",
        "chinese-roberta-wwm-ext-large/**",
        "s1v3.ckpt",
        sovits_base,
        fallback_t2s,
        fallback_vits,
        "sv/pretrained_eres2netv2w24s4ep4.ckpt",
    ]
    inference_markers = [
        pretrained_dir / "chinese-hubert-base" / "pytorch_model.bin",
        pretrained_dir / "chinese-roberta-wwm-ext-large" / "pytorch_model.bin",
        pretrained_dir / "s1v3.ckpt",
        pretrained_dir / sovits_base,
        pretrained_dir / fallback_t2s,
        pretrained_dir / fallback_vits,
        pretrained_dir / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt",
    ]
    if not all(path.is_file() for path in inference_markers):
        pretrained_dir.mkdir(parents=True, exist_ok=True)
        log("下载 GPT-SoVITS v2Pro 推理必需基础模型")
        if use_hf_mirror_direct_download():
            log("检测到 hf-mirror：使用 resolve 直链 GET，绕过 Hugging Face HEAD 元数据")
            direct_files = [
                "chinese-hubert-base/config.json",
                "chinese-hubert-base/preprocessor_config.json",
                "chinese-hubert-base/pytorch_model.bin",
                "chinese-roberta-wwm-ext-large/config.json",
                "chinese-roberta-wwm-ext-large/tokenizer.json",
                "chinese-roberta-wwm-ext-large/pytorch_model.bin",
                "s1v3.ckpt",
                sovits_base,
                fallback_t2s,
                fallback_vits,
                "sv/pretrained_eres2netv2w24s4ep4.ckpt",
            ]
            for filename in direct_files:
                destination = pretrained_dir / filename
                if not destination.is_file():
                    download_hf_file_direct("lj1995/GPT-SoVITS", filename, destination)
        else:
            script = (
                "from huggingface_hub import snapshot_download;"
                f"snapshot_download(repo_id='lj1995/GPT-SoVITS',local_dir=r'{pretrained_dir}',"
                f"allow_patterns={inference_patterns!r})"
            )
            run([str(python), "-c", script], env=base_env())

    fast_langdetect_dir = pretrained_dir / "fast_langdetect"
    fast_langdetect_model = fast_langdetect_dir / "lid.176.bin"
    if not fast_langdetect_model.is_file():
        fast_langdetect_dir.mkdir(parents=True, exist_ok=True)
        script = (
            "from pathlib import Path;"
            "from fast_langdetect.infer import ModelDownloader,FASTTEXT_LARGE_MODEL_URL;"
            f"p=Path(r'{fast_langdetect_model}');"
            "ModelDownloader.download(FASTTEXT_LARGE_MODEL_URL,p)"
        )
        log("下载 GPT-SoVITS fast_langdetect 语言识别模型 lid.176.bin")
        run([str(python), "-c", script], env=base_env())
        if not fast_langdetect_model.is_file():
            raise FileNotFoundError(f"fast_langdetect 模型下载失败: {fast_langdetect_model}")

    g2pw_dir = source_dir / "GPT_SoVITS" / "text" / "G2PWModel"
    if not g2pw_dir.is_dir():
        import zipfile

        assets_dir = runtime_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        archive = assets_dir / "G2PWModel.zip"
        log("下载 GPT-SoVITS G2PW 中文前端模型")
        if not archive.is_file():
            if use_hf_mirror_direct_download():
                download_hf_file_direct(
                    "XXXXRT/GPT-SoVITS-Pretrained",
                    "G2PWModel.zip",
                    archive,
                )
            else:
                script = (
                    "from huggingface_hub import hf_hub_download;"
                    f"hf_hub_download(repo_id='XXXXRT/GPT-SoVITS-Pretrained',filename='G2PWModel.zip',local_dir=r'{assets_dir}')"
                )
                run([str(python), "-c", script], env=base_env())

        text_dir = source_dir / "GPT_SoVITS" / "text"
        with zipfile.ZipFile(archive) as handle:
            handle.extractall(text_dir)
        candidates = [
            path for path in text_dir.iterdir()
            if path.is_dir() and path.name.startswith("G2PWModel")
        ]
        target = text_dir / "G2PWModel"
        if candidates and candidates[0] != target and not target.exists():
            shutil.move(str(candidates[0]), str(target))
        if not target.is_dir():
            raise FileNotFoundError(f"G2PW 模型解压后不存在: {target}")


def patch_gpt_runtime_compat(source_dir: Path, runtime_dir: Path) -> Path:
    """Apply managed-runtime compatibility patches.

    The g2p_en/NLTK patch is platform-independent so a fresh installation never
    downloads NLTK data during import or inference. Native dependency rewrites
    remain Windows-only.
    """
    requirements = source_dir / "requirements.txt"

    # GPT-SoVITS 自带英文 CMU 字典。g2p_en 的 import-time nltk.download()
    # 和 cmudict.dict() 对当前 en_G2p 都是冗余依赖，因此所有平台都禁用。
    venv_dir = managed_venv_dir(runtime_dir)
    g2p_candidates = [
        venv_dir / "Lib" / "site-packages" / "g2p_en" / "g2p.py",
    ]
    g2p_candidates.extend(
        venv_dir.glob("lib/python*/site-packages/g2p_en/g2p.py")
    )
    for g2p_py in g2p_candidates:
        if not g2p_py.is_file():
            continue
        content = g2p_py.read_text(encoding="utf-8")
        patched = content
        patched = patched.replace(
            "try:\n    nltk.data.find('taggers/averaged_perceptron_tagger.zip')\nexcept LookupError:\n    nltk.download('averaged_perceptron_tagger')\ntry:\n    nltk.data.find('corpora/cmudict.zip')\nexcept LookupError:\n    nltk.download('cmudict')\n",
            "# NachoBot managed runtime: do not download NLTK data at import time.\n",
        )
        patched = patched.replace("        self.cmu = cmudict.dict()\n", "        self.cmu = {}\n", 1)
        if patched != content:
            g2p_py.write_text(patched, encoding="utf-8")
            log(f"修补 g2p_en：禁用 NLTK 数据自动下载: {g2p_py}")

    text_dir = source_dir / "GPT_SoVITS" / "text"
    english_py = text_dir / "english.py"
    if english_py.is_file():
        content = english_py.read_text(encoding="utf-8")
        patched = content
        nltk_fallback_marker = "# Missing optional NLTK tagger data must not make TTS unavailable."
        if nltk_fallback_marker not in patched:
            patched = patched.replace(
                "        tokens = pos_tag(words)  # tuples of (word, tag)\n",
                "        try:\n"
                "            tokens = pos_tag(words)  # tuples of (word, tag)\n"
                "        except LookupError:\n"
                f"            {nltk_fallback_marker}\n"
                "            tokens = [(word, '') for word in words]\n",
                1,
            )
        patched = patched.replace("                if pos.startswith(pos1):\n", "                if pos and pos.startswith(pos1):\n", 1)
        patched = patched.replace(
            "                elif len(pos) < len(pos1) and pos == pos1[: len(pos)]:\n",
            "                elif pos and len(pos) < len(pos1) and pos == pos1[: len(pos)]:\n",
            1,
        )
        if patched != content:
            compile(patched, str(english_py), "exec")
            english_py.write_text(patched, encoding="utf-8")
            log("修补 GPT-SoVITS 英文 G2P：NLTK POS tagger 缺失时安全降级")

    if os.name != "nt":
        return requirements

    replacements = {
        "import jieba_fast as jieba": "import jieba",
        "import jieba_fast.posseg as psg": "import jieba.posseg as psg",
        "import jieba_fast": "import jieba",
        "jieba_fast.": "jieba.",
    }
    text_dir = source_dir / "GPT_SoVITS" / "text"
    if text_dir.is_dir():
        for py_file in text_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            patched = content
            for old, new in replacements.items():
                patched = patched.replace(old, new)
            if patched != content:
                py_file.write_text(patched, encoding="utf-8")
                log(f"Windows 兼容修补 jieba_fast -> jieba: {py_file.relative_to(source_dir)}")

    # torchaudio.load may require TorchCodec + full-shared FFmpeg on newer
    # torchaudio builds. GPT-SoVITS only needs ordinary reference-audio loading
    # here, so use soundfile directly and keep the managed runtime self-contained.
    tts_py = source_dir / "GPT_SoVITS" / "TTS_infer_pack" / "TTS.py"
    if tts_py.is_file():
        content = tts_py.read_text(encoding="utf-8")
        patched = content
        if "import soundfile as sf" not in patched:
            patched = patched.replace("import numpy as np\nimport torch", "import numpy as np\nimport soundfile as sf\nimport torch", 1)
        patched = patched.replace(
            "        raw_audio, raw_sr = torchaudio.load(ref_audio_path)\n"
            "        raw_audio = raw_audio.to(self.configs.device).float()",
            "        raw_audio_np, raw_sr = sf.read(ref_audio_path, dtype=\"float32\", always_2d=True)\n"
            "        raw_audio = torch.from_numpy(raw_audio_np.T.copy()).to(self.configs.device).float()",
            1,
        )
        if patched != content:
            tts_py.write_text(patched, encoding="utf-8")
            log("Windows 兼容修补参考音频加载: torchaudio/TorchCodec -> soundfile")

    filtered_requirements = runtime_dir / "requirements.windows.txt"
    lines = requirements.read_text(encoding="utf-8").splitlines()
    filtered = []
    for line in lines:
        normalized = line.strip().lower().replace("-", "_")
        if normalized == "jieba_fast":
            continue
        if line.strip().lower().startswith("--no-binary=opencc"):
            continue
        if normalized.startswith("pyopenjtalk"):
            filtered.append("pyopenjtalk-plus==0.4.1.post8")
            continue
        if normalized == "opencc":
            filtered.append("OpenCC==1.1.9")
            continue
        filtered.append(line)
    filtered_requirements.write_text("\n".join(filtered) + "\n", encoding="utf-8")
    return filtered_requirements


def prepare_gpt_sovits() -> tuple[Path, Path]:
    runtime_dir = RUNTIME_ROOT / "gpt-sovits"
    source_dir = ensure_gpt_source(runtime_dir)
    python = ensure_venv(runtime_dir)

    probe = subprocess.run(
        [str(python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    python_version = probe.stdout.strip() if probe.returncode == 0 else "unknown"
    index_url = torch_index_url()
    profile = _tts_torch_profile(index_url)
    marker = _tts_marker_path(runtime_dir, GPT_REF, python_version, profile)
    _invalidate_old_tts_markers(runtime_dir, marker)
    if _tts_marker_is_valid(marker, python, index_url, python_version, profile, GPT_REF):
        patch_gpt_runtime_compat(source_dir, runtime_dir)
        ensure_gpt_assets(python, source_dir, runtime_dir)
        return python, source_dir

    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    constraints = _tts_dependency_constraints(runtime_dir, profile, GPT_REF)
    log(f"安装 GPT-SoVITS 安全 PyTorch 运行时: {_safe_torch_index_label(index_url)} (Python {python_version})")
    _install_tts_torch_stack(python, constraints, index_url, profile, GPT_REF)
    extra_requirements = source_dir / "extra-req.txt"
    requirements = patch_gpt_runtime_compat(source_dir, runtime_dir)
    if not extra_requirements.is_file():
        raise FileNotFoundError(f"extra-req.txt 不存在: {extra_requirements}")
    if not requirements.is_file():
        raise FileNotFoundError(f"GPT-SoVITS requirements 不存在: {requirements}")
    run([
        require_uv(), "pip", "install", "--python", str(python),
        "--constraint", str(constraints), "-r", str(extra_requirements), "--no-deps",
    ])
    run([
        require_uv(), "pip", "install", "--python", str(python),
        "--constraint", str(constraints), "-r", str(requirements),
    ])
    if _tts_onnxruntime_gpu_required(profile, GPT_REF):
        # The upstream requirements guard this wheel with a platform_machine
        # marker. Install it explicitly as well so an empty marker environment
        # cannot leave a CUDA-ready marker with the wrong ORT distribution.
        run([
            require_uv(), "pip", "install", "--python", str(python),
            "--constraint", str(constraints),
            f"onnxruntime-gpu=={TTS_GPT_ORT_GPU_VERSION}",
        ])
    # requirements 安装后 g2p_en 才存在于托管 venv，因此再次应用源码兼容补丁。
    patch_gpt_runtime_compat(source_dir, runtime_dir)
    _write_tts_marker(marker, python, index_url, python_version, profile, GPT_REF)
    ensure_gpt_assets(python, source_dir, runtime_dir)
    log(f"GPT-SoVITS 托管运行时就绪: {runtime_dir} (Python {python_version})")
    return python, source_dir


def resolve_gpt_preset_weights(config_path: Path | None = None) -> tuple[Path | None, Path | None]:
    config_path = config_path or (ADAPTER_ROOT / "configs" / "gpt-sovits.toml")
    config = read_toml(config_path)
    default_preset = str(config.get("pipeline", {}).get("default_preset", "default"))
    preset = (
        config.get("tts", {})
        .get("models", {})
        .get("presets", {})
        .get(default_preset, {})
    )

    def resolve(value: object) -> Path | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = (config_path.parent / candidate).resolve()
        return candidate if candidate.is_file() else None

    return resolve(preset.get("gpt_model")), resolve(preset.get("sovits_model"))


def detect_sovits_version(weights: Path) -> str:
    """Detect GPT-SoVITS model generation from the weight header."""
    with weights.open("rb") as handle:
        header = handle.read(2)
    header_versions = {
        b"00": "v1",
        b"01": "v2",
        b"02": "v3",
        b"03": "v3",
        b"04": "v4",
        b"05": "v2Pro",
        b"06": "v2ProPlus",
    }
    if header in header_versions:
        return header_versions[header]

    # Legacy torch zip weights do not carry the new two-byte generation tag.
    size = weights.stat().st_size
    if size < 82978 * 1024:
        return "v1"
    if size < 700 * 1024 * 1024:
        return "v2"
    return "v3"


def patch_gpt_version_parser(source_dir: Path) -> None:
    """Fix upstream mixed-case v2Pro/v2ProPlus config parsing."""
    tts_py = source_dir / "GPT_SoVITS" / "TTS_infer_pack" / "TTS.py"
    if not tts_py.is_file():
        return
    content = tts_py.read_text(encoding="utf-8")
    old = '        version = configs.get("version", "v2").lower()\n'
    new = (
        '        version_raw = str(configs.get("version", "v2"))\n'
        '        version = {"v2pro": "v2Pro", "v2proplus": "v2ProPlus"}.get('\
        'version_raw.lower(), version_raw.lower())\n'
    )
    if old in content:
        tts_py.write_text(content.replace(old, new, 1), encoding="utf-8")
        log("修补 GPT-SoVITS v2Pro/v2ProPlus 版本配置解析")


def make_gpt_infer_config(
    source_dir: Path,
    runtime_dir: Path,
    config_path: Path | None = None,
) -> Path:
    patch_gpt_version_parser(source_dir)
    gpt_weights, sovits_weights = resolve_gpt_preset_weights(config_path)
    pretrained = source_dir / "GPT_SoVITS" / "pretrained_models"
    selected = select_gpt_weights(
        (config_path or ADAPTER_ROOT / "configs" / "gpt-sovits.toml").parent,
        gpt_weights, sovits_weights, pretrained,
    )
    gpt_weights, sovits_weights = selected.gpt, selected.sovits
    if selected.fallback:
        log("WARN: 自定义 GPT/SoVITS 权重缺失，整组回退到默认 v2 底模")

    version = detect_sovits_version(sovits_weights)
    try:
        import torch
        runtime_device = "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        runtime_device = "cpu"
    is_half = runtime_device == "cuda"

    config_path = runtime_dir / "tts_infer.nachobot.yaml"
    yaml_text = (
        f"version: {version}\n"
        "custom:\n"
        f"  device: {runtime_device}\n"
        f"  is_half: {'true' if is_half else 'false'}\n"
        f"  version: {version}\n"
        f"  t2s_weights_path: '{gpt_weights.as_posix()}'\n"
        f"  vits_weights_path: '{sovits_weights.as_posix()}'\n"
        f"  bert_base_path: '{(pretrained / 'chinese-roberta-wwm-ext-large').as_posix()}'\n"
        f"  cnhuhbert_base_path: '{(pretrained / 'chinese-hubert-base').as_posix()}'\n"
    )
    config_path.write_text(yaml_text, encoding="utf-8")
    log(f"GPT-SoVITS preset 权重: GPT={gpt_weights.name}, SoVITS={sovits_weights.name}, version={version}")
    return config_path


def build_gpt_sovits_command(
    port: int = PRIVATE_DEFAULT_PORT,
    host: str = PRIVATE_DEFAULT_HOST,
    config_path: Path | None = None,
) -> tuple[list[str], Path, dict[str, str]]:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("GPT-SoVITS raw API must bind to loopback")
    if int(port) == PUBLIC_DEFAULT_PORT:
        raise ValueError("GPT-SoVITS raw API cannot bind the public 9880 port")
    python, source_dir = prepare_gpt_sovits()
    runtime_dir = RUNTIME_ROOT / "gpt-sovits"
    infer_config = make_gpt_infer_config(source_dir, runtime_dir, config_path)
    env = base_env()
    env["PYTHONPATH"] = os.pathsep.join([
        str(source_dir),
        str(source_dir / "GPT_SoVITS"),
    ])

    cmd = [
        str(python), "-s", str(source_dir / "api_v2.py"),
        "-a", host,
        "-p", str(port),
        "-c", str(infer_config),
    ]
    log(f"启动 GPT-SoVITS API: {host}:{port}")
    return cmd, source_dir, env


def serve_gpt_sovits(
    port: int = PRIVATE_DEFAULT_PORT,
    host: str = PRIVATE_DEFAULT_HOST,
    config_path: Path | None = None,
) -> int:
    cmd, cwd, env = build_gpt_sovits_command(port, host, config_path)
    return subprocess.call(cmd, cwd=str(cwd), env=env)


class TTSRuntimeSupervisor:
    """Own public 9880 and optionally one private local TTS child."""

    def __init__(
        self,
        config_path: Path,
        *,
        engine: str | None = None,
        public_host: str | None = None,
        public_port: int = PUBLIC_DEFAULT_PORT,
        private_host: str = PRIVATE_DEFAULT_HOST,
        private_port: int | None = None,
        startup_timeout: float = 900.0,
        popen_factory: Callable[..., Any] | None = None,
        pipeline_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        explicit_engine = engine or os.environ.get("NACHOBOT_TTS_ENGINE", "")
        self.engine = resolve_engine(self.config_path, explicit_engine)
        configured_public_host = read_toml(self.config_path).get("server", {}).get("host")
        self.public_host = str(public_host or configured_public_host or PUBLIC_DEFAULT_HOST)
        self.public_port = int(public_port)
        self.private_host = str(private_host)
        if self.engine != "generic-http" and self.private_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("raw TTS backend must bind to loopback")
        if self.engine == "generic-http":
            self.private_port = None
        else:
            env_private_port = os.environ.get("NACHOBOT_TTS_PRIVATE_PORT", "").strip()
            if private_port is None and env_private_port:
                private_port = int(env_private_port)
            self.private_port = resolve_private_port(
                self.config_path,
                self.engine,
                explicit_port=private_port,
                public_port=self.public_port,
            )
        self.startup_timeout = float(startup_timeout)
        self._popen = popen_factory or subprocess.Popen
        self._pipeline_factory = pipeline_factory
        self.child: Any | None = None
        self.pipeline: Any | None = None
        self.public_server: Any | None = None
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._raw_spec: tuple[list[str], Path, dict[str, str]] | None = None
        self._started = False

    @property
    def backend_url(self) -> str:
        if self.private_port is None:
            raise RuntimeError("GenericHTTP does not have a local backend URL")
        return f"http://{self.private_host}:{self.private_port}"

    def child_command(self) -> list[str]:
        """Return the actual backend command owned by ``start``.

        Preparation is performed once and cached with its cwd/environment;
        the returned command is the Vox/GPT API process itself, not a
        ``serve-raw`` Python wrapper that could outlive this supervisor.
        """

        if self.engine == "generic-http":
            raise RuntimeError("GenericHTTP does not start a managed child process")
        if self._raw_spec is None:
            self._raw_spec = self._build_raw_spec()
        return list(self._raw_spec[0])

    def _build_raw_spec(self) -> tuple[list[str], Path, dict[str, str]]:
        if self.engine == "generic-http":
            raise RuntimeError("GenericHTTP does not have a raw child process")
        if self.engine == "irodori":
            return build_irodori_command(
                self.private_port,
                self.private_host,
                self.config_path.parent / "irodori.toml",
            )
        if self.engine == "voxcpm":
            return build_voxcpm_command(
                self.private_port,
                self.private_host,
                self.config_path.parent / "vox.toml",
            )
        return build_gpt_sovits_command(
            self.private_port,
            self.private_host,
            self.config_path.parent / "gpt-sovits.toml",
        )

    def _spawn_child(self) -> Any:
        if self.engine == "generic-http":
            raise RuntimeError("GenericHTTP does not start a managed child process")
        if self._raw_spec is None:
            self._raw_spec = self._build_raw_spec()
        command, cwd, env = self._raw_spec
        if self.engine == "irodori":
            # Irodori receives its private bind endpoint explicitly on argv.
            # Do not let a stale inherited client override redirect its own
            # managed endpoint or leak into the isolated child environment.
            env.pop("NACHOBOT_TTS_ENGINE_HOST", None)
            env.pop("NACHOBOT_TTS_ENGINE_PORT", None)
        else:
            env["NACHOBOT_TTS_ENGINE_HOST"] = self.private_host
            env["NACHOBOT_TTS_ENGINE_PORT"] = str(self.private_port)
        log(
            f"启动单一 TTS raw child: engine={self.engine} "
            f"bind={self.private_host}:{self.private_port} command={command[0]}"
        )
        process_kwargs: dict[str, Any] = {"cwd": str(cwd), "env": env}
        if os.name == "nt":
            # The PID is an explicit Windows process-tree boundary. stop()
            # uses taskkill /PID /T only for this process, never by name.
            process_kwargs["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )
        else:
            # Own all descendants in a dedicated POSIX process group.
            process_kwargs["start_new_session"] = True
        return self._popen(command, **process_kwargs)

    @staticmethod
    def _fetch_json(url: str, timeout: float = 1.0) -> tuple[int, dict[str, Any] | None]:
        request = Request(url, headers={"User-Agent": "NachoBot-TTS-Supervisor/1.0"})
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = None
                return int(getattr(response, "status", 200)), payload
        except Exception:
            return 0, None

    def _raw_socket_ready(self) -> bool:
        """Return whether a GPT raw HTTP socket responds at all."""

        status, _payload = self._fetch_json(f"{self.backend_url}/health")
        if status:
            return status < 500
        # Upstream GPT-SoVITS versions have not all exposed /health. A root
        # response proves that the API process has bound its socket; fixed
        # client validation below is the engine-specific readiness gate.
        request = Request(self.backend_url, headers={"User-Agent": "NachoBot-TTS-Supervisor/1.0"})
        try:
            with urlopen(request, timeout=1.0) as response:
                return int(getattr(response, "status", 200)) < 500
        except URLError as exc:
            status = getattr(exc, "code", None)
            if status is None:
                status = getattr(getattr(exc, "reason", None), "code", None)
            if status is None:
                status = getattr(getattr(exc, "reason", None), "status", None)
            return status is not None and int(status) < 500
        except Exception:
            return False

    def _raw_ready(self) -> bool:
        if self.engine == "voxcpm":
            status, payload = self._fetch_json(f"{self.backend_url}/health")
            return bool(status == 200 and isinstance(payload, dict) and payload.get("model_loaded") is True)
        if self.engine == "irodori":
            status, payload = self._fetch_json(f"{self.backend_url}/health")
            return bool(status == 200 and isinstance(payload, dict) and payload.get("model_loaded") is True)
        return self._raw_socket_ready()

    def wait_for_backend(self) -> None:
        """Wait for raw model readiness, not merely a listening process."""

        if self.engine == "generic-http":
            return

        deadline = time.monotonic() + self.startup_timeout
        last_error = "backend did not report ready"
        while time.monotonic() < deadline:
            if self.child is not None:
                return_code = self.child.poll()
                if return_code is not None:
                    raise RuntimeError(f"TTS raw child exited before readiness: {return_code}")
            try:
                if self._raw_ready():
                    return
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.2)
        raise TimeoutError(f"TTS raw backend startup timed out: {last_error}")

    def _build_pipeline(self) -> Any:
        if self._pipeline_factory is not None:
            return self._pipeline_factory(
                self.config_path,
                backend=self.engine,
                engine_host=self.private_host if self.private_port is not None else None,
                engine_port=self.private_port,
                backend_alive=True,
            )

        # Import only after the selected raw child has proven readiness. The
        # file-based loader is independent of sys.path[0] (which is the
        # scripts directory for direct execution) and cannot accidentally
        # reuse an unrelated preloaded ``main`` module.
        public_main = _load_public_main()

        selected_backend = {
            "voxcpm": "Vox",
            "gpt-sovits": "GPT_Sovits",
            "generic-http": "GenericHTTP",
            "irodori": "Irodori",
        }[self.engine]
        return public_main.TTSPipeline(
            self.config_path,
            backend=selected_backend,
            engine_host=self.private_host if self.private_port is not None else None,
            engine_port=self.private_port,
            backend_alive=True,
            public_host=self.public_host,
            public_port=self.public_port,
        )

    def _validate_fixed_client(self) -> Any:
        """Construct one fixed client after raw readiness is observed.

        The raw child has already passed its startup/readiness contract at
        this point. Import, config, and eager classifier failures are therefore
        deterministic public-pipeline initialization failures, not conditions
        that can be repaired by retrying the same constructor for 900 seconds.
        """

        try:
            return self._build_pipeline()
        except Exception as exc:
            if self.engine == "generic-http":
                raise RuntimeError(
                    "public pipeline initialization failed for generic-http; check the selected profile"
                ) from None
            raise RuntimeError(
                f"public pipeline initialization failed for {self.engine}: {exc}"
            ) from exc

    def _monitor_child(self) -> None:
        while not self._monitor_stop.wait(0.2):
            if self.child is None:
                return
            if self.child.poll() is not None:
                if self.pipeline is not None:
                    self.pipeline.set_backend_alive(False)
                log("TTS raw child exited; public runtime is unhealthy")
                return

    @staticmethod
    def _terminate_owned_process(process: Any) -> None:
        """Terminate/reap only the process tree created by this supervisor."""

        pid = getattr(process, "pid", None)
        running = process.poll() is None
        if os.name == "nt" and pid and running:
            # /T scopes the operation to this supervisor-owned PID tree.
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif os.name != "nt" and pid and running:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        elif running:
            process.terminate()

        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            if os.name != "nt" and pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            else:
                process.kill()
            process.wait(timeout=5)

    def start(self) -> Any:
        """Start an optional local child, then publish the fixed public pipeline."""

        if self._started:
            return self.pipeline
        if self.engine == "generic-http":
            try:
                # GenericHTTP validates local profile configuration at startup
                # but performs no external request until synthesis.
                self.pipeline = self._validate_fixed_client()
                self._started = True
                return self.pipeline
            except Exception:
                self.stop()
                raise

        self.child = self._spawn_child()
        try:
            self.wait_for_backend()
            self.pipeline = self._validate_fixed_client()
            if self.child.poll() is not None:
                raise RuntimeError("TTS raw child exited before public readiness")
            self._monitor_thread = threading.Thread(
                target=self._monitor_child,
                name="nachobot-tts-child-monitor",
                daemon=True,
            )
            self._monitor_thread.start()
            self._started = True
            return self.pipeline
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        """Drain monitor and terminate the one child cleanly."""

        self._monitor_stop.set()
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2.0)
        if self.pipeline is not None:
            self.pipeline.stop()
        child = self.child
        if child is not None:
            self._terminate_owned_process(child)
        self._started = False

    def run(self) -> int:
        """Run public 9880 until shutdown, then drain the raw child."""

        pipeline = self.start()
        try:
            # Keep every post-start operation in this boundary. Importing
            # uvicorn or constructing Config/Server can fail before bind, and
            # the raw child still belongs to this supervisor in that case.
            import uvicorn

            config = uvicorn.Config(
                pipeline.app,
                host=self.public_host,
                port=self.public_port,
                log_level="info",
            )
            # Config applies Uvicorn's logging tree; install the shared probe
            # filter only after that configuration is complete.
            from nachobot_multimodal.utils.uvicorn_logging import install_quiet_access_logging

            install_quiet_access_logging()
            self.public_server = uvicorn.Server(config)
            self.public_server.run()
        finally:
            self.stop()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NachoBot managed TTS runtime")
    parser.add_argument("action", choices=["prepare", "serve", "serve-raw"])
    parser.add_argument("--engine", default="")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--backend-port", type=int, default=None)
    parser.add_argument(
        "--config",
        type=Path,
        default=ADAPTER_ROOT / "configs" / "base.toml",
    )
    parser.add_argument("--startup-timeout", type=float, default=900.0)
    args = parser.parse_args(argv)

    engine = resolve_engine(args.config, args.engine or os.environ.get("NACHOBOT_TTS_ENGINE", ""))

    if args.action in {"serve", "serve-raw"}:
        apply_device_environment(
            "tts",
            config_dir=args.config.parent,
            base_path=args.config,
            runtime_profile=os.environ.get("NACHOBOT_TTS_RUNTIME_PROFILE", "gpu"),
            engine=engine,
        )

    if engine != "generic-http":
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        HF_CACHE.mkdir(parents=True, exist_ok=True)

    if engine == "generic-http":
        if args.action == "prepare":
            log("GenericHTTP uses the selected external service; no model installation is required")
            return 0
        if args.action == "serve-raw":
            raise ValueError("GenericHTTP has no managed raw server")
        supervisor = TTSRuntimeSupervisor(
            args.config,
            engine=engine,
            public_host=args.host,
            public_port=args.port if args.port is not None else PUBLIC_DEFAULT_PORT,
            startup_timeout=args.startup_timeout,
        )
        return supervisor.run()

    if engine == "irodori":
        if args.action == "prepare":
            prepare_irodori(args.config.parent / "irodori.toml")
            return 0
        if args.action == "serve-raw":
            return serve_irodori(
                args.port if args.port is not None else PRIVATE_DEFAULT_PORT,
                args.host or PRIVATE_DEFAULT_HOST,
                args.config.parent / "irodori.toml",
            )
        supervisor = TTSRuntimeSupervisor(
            args.config,
            engine=engine,
            public_host=args.host,
            public_port=args.port if args.port is not None else PUBLIC_DEFAULT_PORT,
            private_port=args.backend_port,
            startup_timeout=args.startup_timeout,
        )
        return supervisor.run()

    if engine == "voxcpm":
        if args.action == "prepare":
            prepare_voxcpm()
            return 0
        if args.action == "serve-raw":
            return serve_voxcpm(
                args.port if args.port is not None else PRIVATE_DEFAULT_PORT,
                args.host or PRIVATE_DEFAULT_HOST,
                args.config.parent / "vox.toml",
            )

    if args.action == "prepare":
        prepare_gpt_sovits()
        return 0
    if args.action == "serve-raw":
        return serve_gpt_sovits(
            args.port if args.port is not None else PRIVATE_DEFAULT_PORT,
            args.host or PRIVATE_DEFAULT_HOST,
            args.config.parent / "gpt-sovits.toml",
        )

    supervisor = TTSRuntimeSupervisor(
        args.config,
        engine=engine,
        public_host=args.host,
        public_port=args.port if args.port is not None else PUBLIC_DEFAULT_PORT,
        private_port=args.backend_port,
        startup_timeout=args.startup_timeout,
    )
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
