"""
NachoBot WebUI — Memory Manager (A_Memorix)
Backend bridge: exposes A_Memorix search, stats, and maintenance APIs
via synchronous wrappers around the async MemoryService.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

import tomlkit

logger = logging.getLogger("webui.memory")


def _log_safe(value: object, max_len: int = 200) -> str:
    text = str(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    text = "".join(ch if ch >= " " and ch != "\x7f" else "?" for ch in text)
    if len(text) > max_len:
        return text[:max_len].rstrip() + "...[truncated]"
    return text


MEMORY_SEARCH_TIMEOUT_SECONDS = 15
MEMORY_STATS_TIMEOUT_SECONDS = 8
MEMORY_MAINTAIN_TIMEOUT_SECONDS = 20

_NACHOBOT_ROOT = Path(__file__).resolve().parent.parent / "NachoBot"
_BOT_CONFIG_PATH = _NACHOBOT_ROOT / "config" / "bot_config.toml"
_NACHOBOT_ENV_PATH = _NACHOBOT_ROOT / ".env"


def _get_core_auth_token() -> str:
    """Read the first configured Core token without importing the bot runtime."""
    environment_token = os.getenv("NACHOBOT_CORE_TOKEN", "").strip()
    if environment_token:
        return environment_token
    if not _BOT_CONFIG_PATH.exists():
        return ""
    try:
        doc = tomlkit.parse(_BOT_CONFIG_PATH.read_text(encoding="utf-8"))
        tokens = doc.get("ncnk_message", {}).get("auth_token", []) or []
        if isinstance(tokens, str):
            tokens = [tokens]
        for token in tokens:
            value = str(token).strip()
            if value:
                return value
    except Exception as exc:
        logger.warning("Failed to read Core API authentication settings: %s", _log_safe(exc))
    return ""


def is_available() -> bool:
    """Check whether A_Memorix is enabled without importing the bot runtime."""
    return _is_enabled_from_config()


def _is_enabled_from_config() -> bool:
    try:
        if not _BOT_CONFIG_PATH.exists():
            return False
        doc = tomlkit.parse(_BOT_CONFIG_PATH.read_text(encoding="utf-8"))
        memory_config = doc.get("a_memorix")
        if not isinstance(memory_config, dict):
            return False
        plugin_config = memory_config.get("plugin")
        if not isinstance(plugin_config, dict):
            return True
        return bool(plugin_config.get("enabled", True))
    except Exception as e:
        logger.warning("Failed to read A_Memorix enabled state: %s", e)
        return False


async def search_memory(query: str, chat_id: str = "", limit: int = 10, core_running: bool = True) -> dict:
    """Search long-term memory."""
    if not core_running:
        return {"success": False, "error": "NachoBot Core 未运行", "results": []}

    if not _is_enabled_from_config():
        return {"success": False, "error": "A_Memorix 未启用", "results": []}

    try:
        result = await asyncio.wait_for(
            _core_api_request("POST", "/api/memory/search", {"query": query, "chat_id": chat_id, "limit": limit}),
            timeout=MEMORY_SEARCH_TIMEOUT_SECONDS,
        )
        # Normalise to a dict the frontend can consume
        if isinstance(result, dict):
            if "results" not in result and isinstance(result.get("hits"), list):
                result = {**result, "results": result.get("hits", [])}
            return result
        # If result is an object with attributes
        return {
            "success": getattr(result, "success", True),
            "results": getattr(result, "data", None)
            or getattr(result, "results", None)
            or getattr(result, "hits", []),
        }
    except asyncio.TimeoutError:
        logger.warning("Memory search timed out")
        return {"success": False, "error": "长期记忆检索超时", "results": []}
    except Exception as e:
        logger.error("Memory search failed: %s", _log_safe(e))
        return {"success": False, "error": "长期记忆检索失败", "results": []}


async def get_stats(core_running: bool = True) -> dict:
    """Retrieve memory stats (total count, storage size, etc)."""
    if not _is_enabled_from_config():
        return {
            "enabled": False,
            "total_memories": 0,
            "storage_dir": "",
        }

    if not core_running:
        return {
            "enabled": True,
            "core_running": False,
            "total_memories": "N/A (核心未运行)",
            "storage_dir": str(_NACHOBOT_ROOT / "data" / "a_memorix"),
            "note": "NachoBot Core 未运行，启动核心后可查看长期记忆运行时统计。",
        }

    try:
        result = await asyncio.wait_for(
            _core_api_request("GET", "/api/memory/stats"),
            timeout=MEMORY_STATS_TIMEOUT_SECONDS,
        )
        if isinstance(result, dict):
            result["enabled"] = True
            return result
        return {
            "enabled": True,
            "total_memories": getattr(result, "total", 0),
            "details": str(result),
        }
    except asyncio.TimeoutError:
        logger.warning("Memory stats timed out")
        return {
            "enabled": True,
            "total_memories": "N/A (请求超时)",
            "storage_dir": str(_NACHOBOT_ROOT / "data" / "a_memorix"),
            "note": "A_Memorix 统计请求超时，已停止继续等待，避免页面一直加载。",
        }
    except Exception as e:
        logger.debug("Memory stats failed via core API: %s", _log_safe(e))
        return {
            "enabled": True,
            "total_memories": "N/A (Core API 不可用)",
            "storage_dir": str(_NACHOBOT_ROOT / "data" / "a_memorix"),
            "note": "NachoBot Core 正在运行，但长期记忆 API 调用失败。",
        }


async def maintain(action: str, target: str = "", reason: str = "", core_running: bool = True) -> dict:
    """Execute a maintenance action (reinforce/protect/restore/freeze)."""
    if not core_running:
        return {"success": False, "error": "NachoBot Core 未运行"}

    if not _is_enabled_from_config():
        return {"success": False, "error": "A_Memorix 未启用"}

    try:
        result = await asyncio.wait_for(
            _core_api_request("POST", "/api/memory/maintain", {"action": action, "target": target, "reason": reason}),
            timeout=MEMORY_MAINTAIN_TIMEOUT_SECONDS,
        )
        if isinstance(result, dict):
            return result
        return {"success": getattr(result, "success", True), "details": str(result)}
    except asyncio.TimeoutError:
        logger.warning("Memory maintain timed out: action=%s", action)
        return {"success": False, "error": "长期记忆维护请求超时"}
    except Exception as e:
        logger.error("Memory maintain failed: %s", _log_safe(e))
        return {"success": False, "error": "长期记忆维护失败"}


def _get_core_base_url() -> str:
    host = None
    port = None
    if _NACHOBOT_ENV_PATH.exists():
        try:
            for line in _NACHOBOT_ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key == "HOST":
                    host = value or host
                elif key == "PORT":
                    port = value or port
        except Exception as e:
            logger.warning("Failed to read NachoBot .env for Core API address: %s", _log_safe(e))

    if not host or not port:
        try:
            from process_manager import SERVICE_DEFS

            core_service = SERVICE_DEFS.get("nachobot")
            if core_service is not None:
                host = host or core_service.env_extra.get("HOST") or "127.0.0.1"
                port = port or str(core_service.port or core_service.env_extra.get("PORT") or "")
        except Exception as e:
            logger.warning("Failed to read NachoBot Core service definition: %s", _log_safe(e))

    if not host:
        host = "127.0.0.1"
    if not port:
        raise RuntimeError("无法确定 NachoBot Core 端口，请检查 NachoBot/.env 的 PORT 或 WebUI 服务配置")

    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


async def _core_api_request(method: str, path: str, body: dict | None = None) -> dict:
    return await asyncio.to_thread(_core_api_request_sync, method, path, body)


def _core_api_request_sync(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{_get_core_base_url()}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if token := _get_core_auth_token():
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=10) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urlerror.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            detail = payload.get("detail") or payload.get("error") or raw
        except Exception:
            detail = raw or str(e)
        raise RuntimeError(f"Core API HTTP {e.code}: {detail}") from e
    except urlerror.URLError as e:
        raise RuntimeError(f"Core API 连接失败: {e.reason}") from e
