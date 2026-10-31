"""Read-only admission checks for the configured Core and UniversalVC routes."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

DEPENDENCY_NAMES = {"core": "Core", "tts": "TTS", "perception": "ASR/感知服务", "universal": "UniversalVC"}
_LOGGER = logging.getLogger(__name__)


class _HealthProbeError(ValueError):
    def __init__(self, status: int):
        self.status = status
        super().__init__("health_unavailable")


def _report_probe_failure(dependency: str, error: Exception) -> None:
    # Never log exception text: transport errors can contain URLs or credentials.
    if isinstance(error, _HealthProbeError):
        reason = f"http_{error.status}"
    elif isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        reason = "timeout"
    elif isinstance(error, aiohttp.ClientError):
        reason = "transport_error"
    elif isinstance(error, OSError):
        reason = "local_io_error"
    else:
        reason = "invalid_health_or_config"
    _LOGGER.warning("QQCall dependency check failed dependency=%s reason=%s", dependency, reason)


def dependency_notice(code: str) -> str | None:
    prefix = "call_missing_"
    if not isinstance(code, str) or not code.startswith(prefix):
        return None
    missing = code[len(prefix):].split("_")
    if not missing or len(set(missing)) != len(missing) or any(item not in DEPENDENCY_NAMES for item in missing):
        return None
    return "暂不能接听或加入通话：" + "、".join(DEPENDENCY_NAMES[item] for item in missing) + "链路缺失或未就绪，请恢复后重新拨打/邀请。"


async def _health_json(url: str, token: str | None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3), trust_env=False) as session:
        async with session.get(url, headers=headers, allow_redirects=False) as response:
            if response.status != 200:
                raise _HealthProbeError(response.status)
            raw = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise ValueError("health_invalid")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("health_invalid")
            return value


def make_dependency_check(router, platform: str, universal_api):
    async def core_check():
        try:
            target = router.config.route_config[platform]
            parsed = urlsplit(target.url)
            if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("core_route_invalid")
            url = urlunsplit(("https" if parsed.scheme == "wss" else "http", parsed.netloc, "/api/multimodal/health", "", ""))
            value = await _health_json(url, target.token)
            observed = value.get("observed_local")
            capabilities = value.get("capabilities")
            if not isinstance(observed, dict) or not isinstance(capabilities, dict):
                return ["core"]
            missing = []
            tts = observed.get("tts")
            if capabilities.get("tts") is not True or not isinstance(tts, dict) or tts.get("required") is not True or tts.get("ready") is not True:
                missing.append("tts")
            elif isinstance(tts.get("observed"), dict) and "synthesis_ready" in tts["observed"] and tts["observed"]["synthesis_ready"] is not True:
                # A loaded model can still be warming up or resource-blocked.
                missing.append("tts")
            perception = observed.get("perception")
            if not isinstance(perception, dict) or type(perception.get("required")) is not bool or (
                perception.get("required") is True and perception.get("ready") is not True
            ):
                missing.append("perception")
            return missing
        except Exception as exc:
            _report_probe_failure("core", exc)
            return ["core"]

    async def universal_check():
        try:
            token_path = universal_api.get("token_file")
            token = None
            if token_path:
                with Path(token_path).open("rb") as token_file:
                    encoded = token_file.read(1025)
                if len(encoded) > 1024:
                    raise ValueError("universal_token_invalid")
                token = encoded.decode("utf-8").strip()
                if not token or len(token) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                    raise ValueError("universal_token_invalid")
            value = await _health_json(universal_api["url"].rstrip("/") + "/v1/health", token)
            capabilities = value.get("capabilities")
            instance_id = value.get("service_instance_id")
            if (
                type(value.get("protocol_version")) is not int or value["protocol_version"] != 1
                or value.get("ready") is not True
                or not isinstance(instance_id, str) or not instance_id or len(instance_id) > 128
                or not isinstance(capabilities, dict)
            ):
                return ["universal"]
            if any(not isinstance(capabilities.get(name), dict) or capabilities[name].get("available") is not True
                   for name in ("pipeline", "vad", "speaker")):
                return ["universal"]
            return []
        except Exception as exc:
            _report_probe_failure("universal", exc)
            return ["universal"]

    async def check():
        results = await asyncio.gather(core_check(), universal_check())
        missing = tuple(item for result in results for item in result)
        if missing:
            _LOGGER.warning("QQCall admission dependencies unavailable missing=%s", ",".join(missing))
        return missing

    return check
