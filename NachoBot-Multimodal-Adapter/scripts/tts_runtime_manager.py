from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

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


def log(message: str) -> None:
    print(f"[TTS Runtime] {message}", flush=True)


def require_uv() -> str:
    executable = shutil.which("uv")
    if not executable:
        raise RuntimeError("未找到 uv，请先安装 uv")
    return executable


def runtime_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


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


def ensure_venv(runtime_dir: Path, python_version: str) -> Path:
    venv_dir = runtime_dir / ".venv"
    python = runtime_python(venv_dir)
    if python.is_file():
        return python
    runtime_dir.mkdir(parents=True, exist_ok=True)
    run([require_uv(), "venv", str(venv_dir), "--python", python_version])
    if not python.is_file():
        raise FileNotFoundError(f"虚拟环境 Python 不存在: {python}")
    return python


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


def prepare_voxcpm() -> Path:
    runtime_dir = RUNTIME_ROOT / "voxcpm"
    python = ensure_venv(runtime_dir, "3.11")
    marker = runtime_dir / ".deps-voxcpm-2.0.3-torch211-triton36.ready"
    if marker.is_file():
        return python

    index_url = torch_index_url()
    log(f"安装 VoxCPM PyTorch 2.11: {index_url}")
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
    marker.write_text(f"torch_index={index_url}\n", encoding="utf-8")
    log(f"VoxCPM 托管运行时就绪: {runtime_dir}")
    return python


def resolve_vox_model_and_lora() -> tuple[str, str]:
    config_path = ADAPTER_ROOT / "configs" / "vox.toml"
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


def serve_voxcpm(port: int) -> int:
    python = prepare_voxcpm()
    model, lora = resolve_vox_model_and_lora()

    # VoxCPM.from_pretrained ultimately uses huggingface_hub.snapshot_download.
    # Resolve remote model IDs here first so we can fail over between mirrors
    # and then give VoxCPM a stable local directory.
    model_path = Path(model).expanduser()
    if not model_path.is_dir():
        model = str(resolve_hf_snapshot(model))

    server = ADAPTER_ROOT / "src" / "tts" / "backends" / "Vox" / "vox_api_server.py"
    cmd = [
        str(python), str(server),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--model-dir", model,
        "--no-denoiser",
    ]
    if lora:
        cmd.extend(["--lora-weights", lora])
    log(f"启动 VoxCPM API: 127.0.0.1:{port}，model={model}")
    return subprocess.call(cmd, cwd=str(ADAPTER_ROOT), env=base_env())


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
    venv_dir = runtime_dir / ".venv"
    for g2p_py in venv_dir.rglob("g2p.py") if venv_dir.is_dir() else []:
        if g2p_py.parent.name != "g2p_en":
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
    python = ensure_venv(runtime_dir, "3.10")
    marker = runtime_dir / f".deps-{GPT_REF}-v2.ready"
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
        f"ref={GPT_REF}\ntorch_index={index_url}\n",
        encoding="utf-8",
    )
    ensure_gpt_assets(python, source_dir, runtime_dir)
    log(f"GPT-SoVITS 托管运行时就绪: {runtime_dir}")
    return python, source_dir


def resolve_gpt_preset_weights() -> tuple[Path | None, Path | None]:
    config_path = ADAPTER_ROOT / "configs" / "gpt-sovits.toml"
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


def make_gpt_infer_config(source_dir: Path, runtime_dir: Path) -> Path:
    patch_gpt_version_parser(source_dir)
    gpt_weights, sovits_weights = resolve_gpt_preset_weights()
    if not gpt_weights or not sovits_weights:
        return source_dir / "GPT_SoVITS" / "configs" / "tts_infer.yaml"

    version = detect_sovits_version(sovits_weights)
    adapter_config = read_toml(ADAPTER_ROOT / "configs" / "gpt-sovits.toml")
    device = str(adapter_config.get("tts", {}).get("device", {}).get("tts", "cuda:0")).strip()
    runtime_device = "cuda" if device.startswith("cuda") else device
    is_half = runtime_device.startswith("cuda")

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


def serve_gpt_sovits(port: int) -> int:
    python, source_dir = prepare_gpt_sovits()
    runtime_dir = RUNTIME_ROOT / "gpt-sovits"
    infer_config = make_gpt_infer_config(source_dir, runtime_dir)
    env = base_env()
    env["PYTHONPATH"] = os.pathsep.join([
        str(source_dir),
        str(source_dir / "GPT_SoVITS"),
    ])

    config = read_toml(ADAPTER_ROOT / "configs" / "gpt-sovits.toml")
    device = str(config.get("tts", {}).get("device", {}).get("tts", "cuda:0")).strip()
    if device.startswith("cuda:"):
        env["CUDA_VISIBLE_DEVICES"] = device.split(":", 1)[1] or "0"
    else:
        env["CUDA_VISIBLE_DEVICES"] = ""

    cmd = [
        str(python), "-s", str(source_dir / "api_v2.py"),
        "-a", "127.0.0.1",
        "-p", str(port),
        "-c", str(infer_config),
    ]
    log(f"启动 GPT-SoVITS API: 127.0.0.1:{port}")
    return subprocess.call(cmd, cwd=str(source_dir), env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description="NachoBot managed TTS runtime")
    parser.add_argument("action", choices=["prepare", "serve"])
    parser.add_argument("--engine", required=True, choices=["gpt-sovits", "voxcpm"])
    parser.add_argument("--port", type=int, default=9880)
    args = parser.parse_args()

    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    HF_CACHE.mkdir(parents=True, exist_ok=True)

    if args.engine == "voxcpm":
        if args.action == "prepare":
            prepare_voxcpm()
            return 0
        return serve_voxcpm(args.port)

    if args.action == "prepare":
        prepare_gpt_sovits()
        return 0
    return serve_gpt_sovits(args.port)


if __name__ == "__main__":
    raise SystemExit(main())
