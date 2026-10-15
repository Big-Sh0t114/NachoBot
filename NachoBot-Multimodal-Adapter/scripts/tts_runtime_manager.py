from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from urllib.error import URLError

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ADAPTER_ROOT.parent
RUNTIME_ROOT = ADAPTER_ROOT / ".runtime" / "tts"
HF_CACHE = ADAPTER_ROOT / "models" / "hf_cache"

# Keep Hub behaviour predictable on mainland-China networks. These values are
# also propagated to every managed TTS subprocess by base_env().
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "10")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")

GPT_REF = os.environ.get("NACHOBOT_GPT_SOVITS_REF", "20250606v2pro")
GPT_REPO = "https://github.com/RVC-Boss/GPT-SoVITS.git"

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
}


def normalize_engine(value: object) -> str:
    """Normalize CLI/env/base.toml backend names."""

    normalized = str(value or "").strip().lower()
    try:
        return _ENGINE_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError("TTS backend must be gpt-sovits or voxcpm") from exc


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

    if explicit_port is not None:
        port = int(explicit_port)
    else:
        filename = "vox.toml" if normalize_engine(engine) == "voxcpm" else "gpt-sovits.toml"
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
    """Return the .bat-selected TTS profile, if one was explicitly provided."""
    explicit = os.environ.get("NACHOBOT_TTS_RUNTIME_PROFILE", "").strip().lower()
    return explicit if explicit in {"gpu", "cpu"} else None


def managed_venv_dir(runtime_dir: Path) -> Path:
    # Only .bat launchers set NACHOBOT_TTS_RUNTIME_PROFILE. Without it (e.g.
    # WebUI), preserve the historical shared .venv behavior exactly.
    return runtime_dir / (".venv-cpu" if explicit_tts_runtime_profile() == "cpu" else ".venv")


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    log("$ " + " ".join(map(str, cmd)))
    subprocess.run(
        cmd,
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
    if override:
        return override
    if shutil.which("nvidia-smi"):
        return "https://download.pytorch.org/whl/cu128"
    return "https://download.pytorch.org/whl/cpu"


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
    profile = explicit_tts_runtime_profile()
    marker_prefix = f"{profile}-" if profile else ""
    marker = runtime_dir / f".deps-{marker_prefix}voxcpm-2.0.3-py{python_version}-torch211-triton36.ready"
    if marker.is_file():
        return python

    index_url = torch_index_url()
    log(f"安装 VoxCPM PyTorch 2.11: {index_url} (Python {python_version})")
    run([
        require_uv(), "pip", "install", "--python", str(python),
        "torch>=2.11,<2.12", "torchaudio>=2.11,<2.12", "--index-url", index_url,
    ])
    if os.name == "nt" and index_url != "https://download.pytorch.org/whl/cpu":
        log("安装 Windows torch.compile 后端: triton-windows 3.6.x")
        run([
            require_uv(), "pip", "install", "--python", str(python),
            "triton-windows>=3.6,<3.7",
        ])
    run([
        require_uv(), "pip", "install", "--python", str(python),
        "voxcpm==2.0.3",
    ])
    marker.write_text(
        f"python={python_version}\ntorch_index={index_url}\n",
        encoding="utf-8",
    )
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
    # If a user-configured preset is missing, make_gpt_infer_config() falls back
    # to GPT-SoVITS' bundled tts_infer.yaml, which references these two files.
    fallback_t2s = "gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt"
    fallback_vits = "gsv-v2final-pretrained/s2G2333k.pth"

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
    profile = explicit_tts_runtime_profile()
    marker_prefix = f"{profile}-" if profile else ""
    marker = runtime_dir / f".deps-{marker_prefix}{GPT_REF}-py{python_version}-v2.ready"
    if marker.is_file():
        patch_gpt_runtime_compat(source_dir, runtime_dir)
        ensure_gpt_assets(python, source_dir, runtime_dir)
        return python, source_dir

    index_url = torch_index_url()
    run([
        require_uv(), "pip", "install", "--python", str(python),
        "torch", "torchaudio", "--index-url", index_url,
    ])
    extra_requirements = source_dir / "extra-req.txt"
    requirements = patch_gpt_runtime_compat(source_dir, runtime_dir)
    if not extra_requirements.is_file():
        raise FileNotFoundError(f"extra-req.txt 不存在: {extra_requirements}")
    if not requirements.is_file():
        raise FileNotFoundError(f"GPT-SoVITS requirements 不存在: {requirements}")
    run([
        require_uv(), "pip", "install", "--python", str(python),
        "-r", str(extra_requirements), "--no-deps",
    ])
    run([
        require_uv(), "pip", "install", "--python", str(python),
        "-r", str(requirements),
    ])
    # requirements 安装后 g2p_en 才存在于托管 venv，因此再次应用源码兼容补丁。
    patch_gpt_runtime_compat(source_dir, runtime_dir)
    marker.write_text(
        f"ref={GPT_REF}\npython={python_version}\ntorch_index={index_url}\n",
        encoding="utf-8",
    )
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
    if not gpt_weights or not sovits_weights:
        return source_dir / "GPT_SoVITS" / "configs" / "tts_infer.yaml"

    version = detect_sovits_version(sovits_weights)
    try:
        import torch
        runtime_device = "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        runtime_device = "cpu"
    is_half = runtime_device == "cuda"

    config_path = runtime_dir / "tts_infer.nachobot.yaml"
    pretrained = source_dir / "GPT_SoVITS" / "pretrained_models"
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
    """Own public 9880 and exactly one private raw TTS child."""

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
        if self.private_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("raw TTS backend must bind to loopback")
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
        return f"http://{self.private_host}:{self.private_port}"

    def child_command(self) -> list[str]:
        """Return the actual backend command owned by ``start``.

        Preparation is performed once and cached with its cwd/environment;
        the returned command is the Vox/GPT API process itself, not a
        ``serve-raw`` Python wrapper that could outlive this supervisor.
        """

        if self._raw_spec is None:
            self._raw_spec = self._build_raw_spec()
        return list(self._raw_spec[0])

    def _build_raw_spec(self) -> tuple[list[str], Path, dict[str, str]]:
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
        if self._raw_spec is None:
            self._raw_spec = self._build_raw_spec()
        command, cwd, env = self._raw_spec
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
        return self._raw_socket_ready()

    def wait_for_backend(self) -> None:
        """Wait for raw model readiness, not merely a listening process."""

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
                engine_host=self.private_host,
                engine_port=self.private_port,
                backend_alive=True,
            )

        # Import only after the selected raw child has proven readiness. The
        # file-based loader is independent of sys.path[0] (which is the
        # scripts directory for direct execution) and cannot accidentally
        # reuse an unrelated preloaded ``main`` module.
        public_main = _load_public_main()

        return public_main.TTSPipeline(
            self.config_path,
            backend="Vox" if self.engine == "voxcpm" else "GPT_Sovits",
            engine_host=self.private_host,
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
        """Start child, prove readiness, then publish the fixed public pipeline."""

        if self._started:
            return self.pipeline
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

    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    HF_CACHE.mkdir(parents=True, exist_ok=True)

    engine = resolve_engine(args.config, args.engine or os.environ.get("NACHOBOT_TTS_ENGINE", ""))

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
