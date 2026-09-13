"""情感预设远程解析器 — 独立于 TTS 模型的通用工具

提供 resolve_emotion_preset_remote() 供所有客户端适配器
（Bilibili / UniversalVC / DiscordVC）直接调用，
无需依赖 TTS 模型实例是否具备 _resolve_emotion_preset_remote 方法。

解析接口地址从 base.toml [server] 自动获取。
"""

import logging
import hashlib
from pathlib import Path
from typing import Optional

import aiohttp
import toml

logger = logging.getLogger(__name__)

# 缓存解析好的 URL，避免每次调用都读配置文件
_cached_emotion_api_url: Optional[str] = None
_cached_base_fingerprint: Optional[str] = None
_cached_base_path: Optional[str] = None
_cache_initialized: bool = False


def _get_emotion_api_url(base_config_path: Optional[Path] = None) -> Optional[str]:
    """读取 base.toml 中的服务器地址，拼接情感分类 API URL"""
    global _cached_emotion_api_url, _cached_base_fingerprint, _cached_base_path, _cache_initialized
    try:
        base_toml_path = Path(base_config_path) if base_config_path else Path(__file__).resolve().parents[2] / "configs" / "base.toml"
        cache_path = str(base_toml_path.resolve())
        raw = base_toml_path.read_bytes()
        fingerprint = hashlib.sha256(raw).hexdigest()
        if (
            _cache_initialized
            and _cached_base_path == cache_path
            and _cached_base_fingerprint == fingerprint
        ):
            return _cached_emotion_api_url

        cfg = toml.loads(raw.decode("utf-8"))
        if not isinstance(cfg, dict) or not isinstance(cfg.get("server", {}), dict):
            raise ValueError("base.toml [server] must be a table")
        host = cfg.get("server", {}).get("host", "127.0.0.1")
        port = cfg.get("server", {}).get("port", 8070)
        url = f"http://{host}:{port}/api/emotion_preset"
        # Publish only a successfully parsed URL. Invalid/missing files do not
        # poison the cache, so the next request retries after a repair.
        _cached_emotion_api_url = url
        _cached_base_fingerprint = fingerprint
        _cached_base_path = cache_path
        _cache_initialized = True
        logger.info(f"情感分类远程接口已解析: {url}")
        return url
    except FileNotFoundError:
        logger.warning(f"base.toml 不存在: {base_config_path or Path(__file__).resolve().parents[2] / 'configs' / 'base.toml'}")
        _cached_emotion_api_url = None
        _cached_base_fingerprint = None
        _cached_base_path = None
        _cache_initialized = False
        return None
    except Exception as e:
        logger.warning(f"无法解析情感分类接口地址: {e}")
        _cached_emotion_api_url = None
        _cached_base_fingerprint = None
        _cached_base_path = None
        _cache_initialized = False
        return None


async def resolve_emotion_preset_remote(text: str, base_config_path: Optional[Path] = None) -> Optional[str]:
    """通过 HTTP 调用 TTS Adapter 服务端的 /api/emotion_preset 接口

    此函数独立于具体的 TTS 模型，任何适配器均可直接调用。
    当 TTS Adapter 服务端未启用情感分类时，接口返回 preset_name=null。

    Args:
        text: 待分类的文本

    Returns:
        preset_name: 匹配的预设名称，或 None 表示使用平台默认
    """
    url = _get_emotion_api_url(base_config_path)
    if not url:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=5, connect=2)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params={"text": text}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    preset_name = data.get("preset_name")
                    if preset_name:
                        logger.info(f"远程情感分类选择预设: {preset_name}")
                    return preset_name
                else:
                    logger.warning(f"情感分类接口返回 {resp.status}")
                    return None
    except Exception as e:
        logger.debug(f"情感分类远程调用失败，使用平台默认预设: {e}")
        return None
