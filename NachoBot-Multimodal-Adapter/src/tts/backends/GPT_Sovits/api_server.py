# 文件路径：src/plugins/GPT_SoVITS/api_server.py
from contextlib import asynccontextmanager
from collections.abc import Mapping
from fastapi import FastAPI, Request
import uvicorn
import logging
import asyncio
from pathlib import Path
import re
from .tts_model import TTSModel
from ....utils.tts_runtime import TTSRuntime

logger = logging.getLogger("api_server")
OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"
GPT_WEIGHT_SUFFIXES = {".ckpt", ".pth", ".pt", ".bin", ".safetensors"}
SOVITS_WEIGHT_SUFFIXES = {".pth", ".pt", ".ckpt", ".safetensors"}
PLATFORM_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# ======== 全局实例 ========
tts_model: TTSModel | None = None
gpt_weights: str | None = None
sovits_weights: str | None = None
_runtime = TTSRuntime(
    config_dir=Path(__file__).resolve().parents[4] / "configs",
    fixed_backend="GPT_Sovits",
)
_model_lock = asyncio.Lock()


def _log_safe(value: object, max_len: int = 200) -> str:
    text = str(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    text = "".join(ch if ch >= " " and ch != "\x7f" else "?" for ch in text)
    if len(text) > max_len:
        return text[:max_len].rstrip() + "...[truncated]"
    return text


def _configured_weight_roots(model: TTSModel, field_name: str) -> tuple[Path, ...]:
    """Return trusted directories declared by the active model presets."""
    config = getattr(model, "config", None)
    tts_config = getattr(config, "tts", None)
    models = getattr(tts_config, "models", None)
    presets = getattr(models, "presets", None)
    if not isinstance(presets, Mapping):
        return ()

    resolver = getattr(model, "_resolve_path", None)
    config_dir = getattr(model, "_config_dir", None)
    roots: set[Path] = set()
    for preset in presets.values():
        configured_path = getattr(preset, field_name, "")
        if not isinstance(configured_path, (str, Path)) or not str(configured_path).strip():
            continue
        try:
            lexical_path = Path(str(configured_path)).expanduser()
            if lexical_path.is_absolute():
                configured_parent = lexical_path.parent
            elif callable(resolver):
                # Resolve only the configured parent. Resolving the leaf first
                # would expand a configured symlink target into a new trust root.
                configured_parent = Path(resolver(str(lexical_path.parent)))
            elif config_dir is not None:
                configured_parent = Path(config_dir).expanduser() / lexical_path.parent
            else:
                continue
            roots.add(configured_parent.expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            continue
    return tuple(sorted(roots))


def _is_within_any_root(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _resolve_weight_file(
    raw_path: str | None,
    suffixes: set[str],
    label: str,
    allowed_roots: tuple[Path, ...],
) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise ValueError(f"{label}模型路径不能为空")
    if len(text) > 4096:
        raise ValueError(f"{label}模型路径过长")
    candidate = Path(text).expanduser()
    if ".." in candidate.parts:
        raise ValueError(f"{label}模型路径不能包含父目录跳转")
    try:
        path = candidate.resolve(strict=True)
        roots = tuple(root.resolve(strict=True) for root in allowed_roots)
    except (OSError, RuntimeError) as exc:
        raise FileNotFoundError(f"{label}模型文件或配置目录不存在") from exc
    if path.suffix.lower() not in suffixes:
        raise ValueError(f"{label}模型文件类型不支持: {path.suffix}")
    if not path.is_file():
        raise FileNotFoundError(f"{label}模型文件不存在")
    if not roots or not _is_within_any_root(path, roots):
        raise ValueError(f"{label}模型文件不在已配置的模型目录中")
    return path


def _safe_output_path(platform: str) -> Path:
    token = PLATFORM_FILENAME_RE.sub("_", str(platform or "default")).strip("._")
    if not token:
        token = "default"
    output_root = Path(OUTPUT_DIR).expanduser().resolve(strict=False)
    candidate = output_root / f"output_{token[:80]}.wav"
    resolved = candidate.resolve(strict=False)
    if candidate.is_symlink() or resolved.parent != output_root:
        raise ValueError("生成音频路径无效")
    return resolved


def _apply_weight_transaction(model: TTSModel, gpt_path: str, sovits_path: str) -> None:
    """Apply both fixed API weight changes as one serialized mutation."""

    model.set_gpt_weights(gpt_path)
    model.set_sovits_weights(sovits_path)
    # Keep explicit load_model overrides in memory for every preset so changing
    # the platform does not reload stale TOML weight paths.
    for preset in model.config.tts.models.presets.values():
        preset.gpt_model = gpt_path
        preset.sovits_model = sovits_path


def _sync_weight_metadata(model: TTSModel) -> None:
    global gpt_weights, sovits_weights
    gpt_weights = getattr(model, "_loaded_gpt_weights", None)
    sovits_weights = getattr(model, "_loaded_sovits_weights", None)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Eagerly construct the fixed GPT client before opening the listener."""
    global tts_model

    print("启动 GPT-SoVITS TTS 服务中 ...")
    ready = await asyncio.to_thread(_runtime.ensure_tts_model)
    model = _runtime.model
    if not ready or model is None:
        error = _runtime.error or "GPT-SoVITS model is unavailable"
        logger.error("TTS Model initialization failed: %s", _log_safe(error))
        raise RuntimeError(error)
    tts_model = model
    _sync_weight_metadata(model)
    print("默认配置加载完成")
    yield


app = FastAPI(title="GPT-SoVITS Adapter API", version="1.1", lifespan=lifespan)


# ==================== 模型加载接口 ====================
@app.post("/load_model")
async def load_model(request: Request):
    """
    动态加载新的 GPT 和 SoVITS 模型权重
    JSON 示例：
    {
        "gpt_path": "C:/Users/BigSh0t/Nacho-with-u/nachobot_tts_adapter/configs/ncnk1-e15.ckpt",
        "sovits_path": "C:/Users/BigSh0t/Nacho-with-u/nachobot_tts_adapter/configs/ncnk1_e10_s370.pth"
    }
    """
    global tts_model, gpt_weights, sovits_weights

    data = await request.json()
    gpt_path = data.get("gpt_path")
    sovits_path = data.get("sovits_path")

    try:
        async with _model_lock:
            model = tts_model
            if model is None:
                raise RuntimeError("GPT-SoVITS model is not loaded")
            gpt_path = _resolve_weight_file(
                gpt_path,
                GPT_WEIGHT_SUFFIXES,
                "GPT",
                _configured_weight_roots(model, "gpt_model"),
            )
            sovits_path = _resolve_weight_file(
                sovits_path,
                SOVITS_WEIGHT_SUFFIXES,
                "SoVITS",
                _configured_weight_roots(model, "sovits_model"),
            )
            try:
                await _runtime.call_blocking(
                    _apply_weight_transaction,
                    model,
                    str(gpt_path),
                    str(sovits_path),
                )
            except BaseException as exc:
                _runtime.invalidate(f"Fixed GPT weight transaction failed: {_log_safe(exc)}")
                tts_model = None
                raise
            gpt_weights, sovits_weights = str(gpt_path), str(sovits_path)
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("Model path validation failed: %s", _log_safe(exc))
        return {"status": "error", "msg": "模型路径无效或文件不存在"}
    except Exception as exc:
        logger.error("Model load failed: %s", _log_safe(exc))
        tts_model = _runtime.model
        return {"status": "error", "msg": "模型权重加载失败"}
    return {
        "status": "ok",
        "msg": "模型权重已加载成功",
        "gpt_model": gpt_path.name,
        "sovits_model": sovits_path.name,
    }


# ==================== 推理接口 ====================
@app.post("/infer")
async def infer(request: Request):
    """
    文本转语音接口
    JSON 示例：
    {
        "text": "你好，我是GPT-SoVITS测试语音。",
        "platform": "default"
    }
    """
    global tts_model
    data = await request.json()
    text = data.get("text", "").strip()
    platform = data.get("platform", "default")

    if not text:
        return {"status": "error", "msg": "缺少文本输入"}

    try:
        async with _model_lock:
            model = tts_model
            if model is None:
                raise RuntimeError("GPT-SoVITS model is not loaded")
            _sync_weight_metadata(model)
            # 调用已有的 TTS 接口（返回音频二进制）
            audio_bytes = await model.tts(text=text, platform=platform)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = _safe_output_path(platform)
        with output_path.open("wb") as f:
            f.write(audio_bytes)

        return {"status": "ok", "msg": "语音生成成功", "audio_file": str(output_path)}
    except Exception as e:
        logger.error("TTS inference failed: %s", _log_safe(e))
        return {"status": "error", "msg": "语音生成失败"}


# ==================== 主启动入口 ====================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9872)
