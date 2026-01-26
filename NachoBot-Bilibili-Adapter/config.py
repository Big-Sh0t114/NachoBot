"""Configuration dataclasses and loading functions for Bilibili Adapter."""

import json
import logging
import requests
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tomllib as toml
except ImportError:  # pragma: no cover
    import toml  # type: ignore


@dataclass
class AdapterConfig:
    nachobot_host: str
    nachobot_port: int
    platform: str
    sessdata: str
    bili_jct: str
    buvid3: str
    buvid4: str
    dede_user_id: str
    user_agent: str
    live_enable: bool
    room_ids: List[int]
    use_wss: bool
    heartbeat_interval: int
    reconnect_seconds: int
    max_reconnect_seconds: int
    live_open_timeout: int
    live_max_hosts: int
    live_max_attempts: int
    live_ws_proxy: str
    live_proxy_pool_path: str
    live_proxy_check_url: str
    live_proxy_check_timeout: int
    live_allow_self_danmu: bool
    live_log_danmu: bool
    live_mention_keywords: List[str]
    live_mention_prefixes: List[str]
    live_mention_any_at: bool
    live_disable_network_search: bool
    live_reply_prompt: str
    live_planner_prompt: str
    live_room_prompts: Dict[int, Dict[str, Any]]
    live_host_room_id: Optional[int]
    screen_manual_enable: bool
    screen_manual_duration_seconds: int
    screen_manual_user_ids: List[str]
    live_resolve_user_nickname: bool
    enable_reply_notice: bool
    comment_resolve_user_nickname: bool
    comment_force_mention: bool
    comment_poll_interval: int
    comment_max_items: int
    private_enable: bool
    private_poll_interval: int
    private_sessions: List["PrivateSessionConfig"]
    private_auto_sessions: bool
    private_auto_session_types: List[int]
    private_auto_session_refresh_seconds: int
    private_auto_session_size: int
    private_force_mention: bool
    disable_video_sender_plugin: bool
    disable_command_trigger: bool
    response_filter_enable: bool
    response_filter_blocked_markers: List[str]
    log_level: str
    # Mic ASR config
    mic_asr_enable: bool
    mic_asr_room_id: int
    mic_asr_subtitle_path: str
    mic_asr_silence_threshold: float
    mic_asr_silence_duration: float
    mic_asr_sample_rate: int


@dataclass
class PrivateSessionConfig:
    talker_id: int
    session_type: int


@dataclass
class VlmModelConfig:
    base_url: str
    api_key: str
    model: str
    max_tokens: int
    timeout: int
    temperature: float
    client_type: str


@dataclass
class AsrModelConfig:
    base_url: str
    api_key: str
    model: str
    timeout: int
    client_type: str


def _load_toml(path: Path) -> Dict[str, Any]:
    raw = path.read_bytes()
    if hasattr(toml, "loads"):
        return toml.loads(raw.decode("utf-8"))
    return toml.load(path)  # type: ignore[attr-defined]


def _load_proxy_pool(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    proxies: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ip = str(item.get("ip") or "").strip()
        port = str(item.get("port") or "").strip()
        if not ip or not port:
            continue
        proxy_url = f"http://{ip}:{port}"
        proxies.append({"http": proxy_url, "https": proxy_url})
    return proxies


def _check_proxy_list(
    proxy_list: List[Dict[str, str]],
    url: str,
    timeout: int,
    logger: logging.Logger,
) -> List[Dict[str, str]]:
    can_use: List[Dict[str, str]] = []
    if timeout <= 0:
        timeout = 1
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    }
    for proxy in proxy_list:
        try:
            resp = requests.get(url=url, headers=headers, proxies=proxy, timeout=timeout)
            if resp.status_code == 200:
                can_use.append(proxy)
        except requests.RequestException:
            continue
    if not can_use:
        logger.warning("No proxies passed check_url=%s", url)
    return can_use


def _proxy_dicts_to_urls(proxy_list: List[Dict[str, str]]) -> List[str]:
    urls: List[str] = []
    for proxy in proxy_list:
        url = proxy.get("http") or proxy.get("https")
        if url:
            urls.append(url)
    return urls


def _resolve_asr_model_config(path: Path, logger: logging.Logger) -> Optional[AsrModelConfig]:
    if not path.exists():
        logger.warning(f"Model config not found at {path}")
        return None
    try:
        config = _load_toml(path)
        
        # 1. Get voice model list
        voice_task = config.get("model_task_config", {}).get("voice", {})
        model_list = voice_task.get("model_list", [])
        if not model_list:
            logger.warning("No voice models configured in [model_task_config.voice]")
            return None
        
        target_model_name = model_list[0]
        
        # 2. Find model config
        models = config.get("models", [])
        target_model_conf = None
        for m in models:
            if m.get("name") == target_model_name or m.get("model_identifier") == target_model_name:
                target_model_conf = m
                break
        
        if not target_model_conf:
            logger.warning(f"Model definition not found for voice model: {target_model_name}")
            return None
            
        provider_name = target_model_conf.get("api_provider")
        model_identifier = target_model_conf.get("model_identifier", target_model_name)
        
        # 3. Find provider config
        providers = config.get("api_providers", [])
        provider_conf = None
        for p in providers:
            if p.get("name") == provider_name:
                provider_conf = p
                break
                
        if not provider_conf:
            logger.warning(f"API provider not found: {provider_name}")
            return None
            
        return AsrModelConfig(
            base_url=str(provider_conf.get("base_url", "")).rstrip("/"),
            api_key=str(provider_conf.get("api_key", "")),
            model=model_identifier,
            timeout=int(provider_conf.get("timeout", 30)),
            client_type=str(provider_conf.get("client_type", "openai")),
        )
            
    except Exception as e:
        logger.error(f"Failed to resolve ASR model config: {e}")
        return None


def load_config(path: Path) -> AdapterConfig:
    data = _load_toml(path)
    nachobot = data.get("nachobot_server", {})
    bilibili = data.get("bilibili", {})
    live = data.get("live", {})
    comment = data.get("comment", {})
    private_message = data.get("private_message", {})
    compat = data.get("compat", {})
    response_filter = data.get("response_filter", {})
    debug = data.get("debug", {})
    mic_asr = data.get("mic_asr", {})
    screen_monitor = live.get("screen_monitor", {}) or {}

    sessions_raw = private_message.get("sessions", []) or []
    sessions: List[PrivateSessionConfig] = []
    if isinstance(sessions_raw, list):
        for item in sessions_raw:
            if not isinstance(item, dict):
                continue
            talker_id = int(item.get("talker_id") or 0)
            session_type = int(item.get("session_type") or 1)
            if talker_id:
                sessions.append(
                    PrivateSessionConfig(
                        talker_id=talker_id,
                        session_type=session_type,
                    )
                )

    auto_session_types_raw = private_message.get("auto_session_types", [4])
    if isinstance(auto_session_types_raw, list):
        auto_session_types = [int(x) for x in auto_session_types_raw if int(x) > 0]
    elif auto_session_types_raw is None:
        auto_session_types = []
    else:
        auto_session_types = [int(auto_session_types_raw)]
    if not auto_session_types:
        auto_session_types = [4]

    mention_keywords_raw = live.get("mention_keywords", [])
    if isinstance(mention_keywords_raw, list):
        mention_keywords = [str(x) for x in mention_keywords_raw if str(x).strip()]
    elif mention_keywords_raw is None:
        mention_keywords = []
    else:
        mention_keywords = [str(mention_keywords_raw)]

    mention_prefixes_raw = live.get("mention_prefixes", ["@", "＠"])
    if isinstance(mention_prefixes_raw, list):
        mention_prefixes = [str(x) for x in mention_prefixes_raw if str(x).strip()]
    elif mention_prefixes_raw is None:
        mention_prefixes = ["@", "＠"]
    else:
        mention_prefixes = [str(mention_prefixes_raw)]

    room_prompts_raw = live.get("room_prompts", {}) or {}
    room_prompts: Dict[int, Dict[str, Any]] = {}
    host_room_ids: List[int] = []
    if isinstance(room_prompts_raw, dict):
        for key, value in room_prompts_raw.items():
            try:
                room_id = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            host_flag = bool(value.get("host", False))
            room_prompts[room_id] = {
                "reply_prompt": str(value.get("reply_prompt", "") or ""),
                "planner_prompt": str(value.get("planner_prompt", "") or ""),
                "live_category": str(value.get("live_category", "") or ""),
                "live_title": str(value.get("live_title", "") or ""),
                "live_content": str(value.get("live_content", "") or ""),
                "live_detail": str(value.get("live_detail", "") or ""),
                "host": host_flag,
                "tts": value.get("tts", {}),
            }
            if host_flag:
                host_room_ids.append(room_id)

    room_ids = [int(x) for x in live.get("room_ids", [])]
    if len(host_room_ids) > 1:
        raise ValueError(f"Multiple host rooms detected: {host_room_ids}")
    host_room_id = host_room_ids[0] if host_room_ids else None
    if host_room_id is not None and host_room_id not in room_ids:
        raise ValueError(f"Host room_id {host_room_id} not in live.room_ids")

    manual_enable = bool(screen_monitor.get("manual_enable", True))
    manual_duration_minutes = int(screen_monitor.get("manual_duration_minutes", 30))
    manual_user_ids_raw = screen_monitor.get("manual_user_ids", []) or []
    if isinstance(manual_user_ids_raw, list):
        manual_user_ids = [str(x).strip() for x in manual_user_ids_raw if str(x).strip()]
    elif manual_user_ids_raw is None:
        manual_user_ids = []
    else:
        manual_user_ids = [str(manual_user_ids_raw).strip()]
    if not manual_user_ids and bilibili.get("dede_user_id"):
        manual_user_ids = [str(bilibili.get("dede_user_id"))]

    response_filter_enable = bool(response_filter.get("enable", True))
    blocked_markers_raw = response_filter.get("blocked_markers", [])
    response_filter_blocked_markers: List[str] = []
    if isinstance(blocked_markers_raw, list):
        response_filter_blocked_markers = [
            str(marker).strip().lower()
            for marker in blocked_markers_raw
            if str(marker).strip()
        ]
    elif blocked_markers_raw is not None:
        marker = str(blocked_markers_raw).strip()
        if marker:
            response_filter_blocked_markers = [marker.lower()]

    return AdapterConfig(
        nachobot_host=str(nachobot.get("host", "127.0.0.1")),
        nachobot_port=int(nachobot.get("port", 8070)),
        platform=str(nachobot.get("platform", "bilibili")),
        sessdata=str(bilibili.get("sessdata", "") or ""),
        bili_jct=str(bilibili.get("bili_jct", "") or ""),
        buvid3=str(bilibili.get("buvid3", "") or ""),
        buvid4=str(bilibili.get("buvid4", "") or ""),
        dede_user_id=str(bilibili.get("dede_user_id", "") or ""),
        user_agent=str(
            bilibili.get(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            )
        ),
        live_enable=bool(live.get("enable", True)),
        room_ids=room_ids,
        use_wss=bool(live.get("use_wss", True)),
        heartbeat_interval=int(live.get("heartbeat_interval", 30)),
        reconnect_seconds=int(live.get("reconnect_seconds", 5)),
        max_reconnect_seconds=int(live.get("max_reconnect_seconds", 60)),
        live_open_timeout=int(live.get("open_timeout", 10)),
        live_max_hosts=int(live.get("max_hosts", 0)),
        live_max_attempts=int(live.get("max_attempts", 0)),
        live_ws_proxy=str(live.get("ws_proxy", "auto") or "auto"),
        live_proxy_pool_path=str(live.get("proxy_pool_path", "proxy.json") or "proxy.json"),
        live_proxy_check_url=str(
            live.get("proxy_check_url", "https://www.baidu.com") or "https://www.baidu.com"
        ),
        live_proxy_check_timeout=int(live.get("proxy_check_timeout", 1)),
        live_allow_self_danmu=bool(live.get("allow_self_danmu", False)),
        live_log_danmu=bool(live.get("log_danmu", False)),
        live_mention_keywords=mention_keywords,
        live_mention_prefixes=mention_prefixes,
        live_mention_any_at=bool(live.get("mention_any_at", False)),
        live_disable_network_search=bool(live.get("disable_network_search", False)),
        live_reply_prompt=str(live.get("reply_prompt", "") or ""),
        live_planner_prompt=str(live.get("planner_prompt", "") or ""),
        live_room_prompts=room_prompts,
        live_host_room_id=host_room_id,
        screen_manual_enable=manual_enable,
        screen_manual_duration_seconds=max(60, manual_duration_minutes * 60),
        screen_manual_user_ids=manual_user_ids,
        live_resolve_user_nickname=bool(live.get("resolve_user_nickname", False)),
        enable_reply_notice=bool(comment.get("enable_reply_notice", True)),
        comment_poll_interval=int(comment.get("poll_interval_seconds", 20)),
        comment_max_items=int(comment.get("max_items_per_poll", 20)),
        comment_resolve_user_nickname=bool(comment.get("resolve_user_nickname", False)),
        comment_force_mention=bool(comment.get("force_mention", False)),
        private_enable=bool(private_message.get("enable", False)),
        private_poll_interval=int(private_message.get("poll_interval_seconds", 20)),
        private_sessions=sessions,
        private_auto_sessions=bool(private_message.get("auto_sessions", False)),
        private_auto_session_types=auto_session_types,
        private_auto_session_refresh_seconds=int(
            private_message.get("auto_session_refresh_seconds", 60)
        ),
        private_auto_session_size=int(private_message.get("auto_session_size", 100)),
        private_force_mention=bool(private_message.get("force_mention", True)),
        disable_video_sender_plugin=bool(
            compat.get("disable_video_sender_plugin", False)
        ),
        disable_command_trigger=bool(
            compat.get("disable_command_trigger", False)
        ),
        response_filter_enable=response_filter_enable,
        response_filter_blocked_markers=response_filter_blocked_markers,
        log_level=str(debug.get("level", "INFO")),
        mic_asr_enable=bool(mic_asr.get("enable", False)),
        mic_asr_room_id=int(mic_asr.get("room_id", 0)),
        mic_asr_subtitle_path=str(mic_asr.get("subtitle_path", "subtitles1.txt") or "subtitles1.txt"),
        mic_asr_silence_threshold=float(mic_asr.get("silence_threshold", 500.0)),
        mic_asr_silence_duration=float(mic_asr.get("silence_duration", 1.0)),
        mic_asr_sample_rate=int(mic_asr.get("sample_rate", 16000)),
    )


def setup_logging(level: str) -> logging.Logger:
    logger = logging.getLogger("nachobot-bilibili-adapter")
    if logger.handlers:
        return logger
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    return logger


def _resolve_vlm_model_config(
    model_config_path: Path, logger: logging.Logger
) -> Optional[VlmModelConfig]:
    if not model_config_path.exists():
        logger.warning("Model config not found: %s", model_config_path)
        return None
    try:
        data = _load_toml(model_config_path)
    except Exception as exc:
        logger.warning("Failed to load model config: %s", exc)
        return None
    task_config = data.get("model_task_config", {}) or {}
    vlm_config = task_config.get("vlm", {}) or {}
    model_list = vlm_config.get("model_list", []) or []
    if not model_list:
        logger.warning("model_task_config.vlm.model_list is empty")
        return None
    model_name = str(model_list[0])
    models = data.get("models", []) or []
    selected_model = None
    for item in models:
        if str(item.get("name") or "") == model_name:
            selected_model = item
            break
    if selected_model is None:
        for item in models:
            if str(item.get("model_identifier") or "") == model_name:
                selected_model = item
                break
    if selected_model is None:
        logger.warning("VLM model not found in model_config: %s", model_name)
        return None
    model_identifier = str(selected_model.get("model_identifier") or model_name)
    provider_name = str(selected_model.get("api_provider") or "")
    providers = data.get("api_providers", []) or []
    provider = None
    for item in providers:
        if str(item.get("name") or "") == provider_name:
            provider = item
            break
    if provider is None:
        logger.warning("VLM provider not found: %s", provider_name)
        return None
    base_url = str(provider.get("base_url") or "")
    if not base_url:
        logger.warning("VLM provider base_url empty for %s", provider_name)
        return None
    api_key = str(provider.get("api_key") or "")
    client_type = str(provider.get("client_type") or "openai")
    timeout = int(provider.get("timeout") or 30)
    max_tokens = int(vlm_config.get("max_tokens") or 800)
    temperature = float(vlm_config.get("temperature") or 0.2)
    return VlmModelConfig(
        base_url=base_url,
        api_key=api_key,
        model=model_identifier,
        max_tokens=max_tokens,
        timeout=timeout,
        temperature=temperature,
        client_type=client_type,
    )
