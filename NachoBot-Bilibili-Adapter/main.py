import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import socket
import sys
import time
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Iterable

import aiohttp
import brotli
import requests
import urllib.parse
import websockets

try:
    import tomllib as toml
except ImportError:  # pragma: no cover
    import toml  # type: ignore

def _ensure_ncnk_message() -> None:
    root_dir = Path(__file__).resolve().parents[1]
    candidates = (
        "NachoBot-Napcat-Adapter",
        "nachobot_tts_adapter",
        "NachoBot",
    )
    for candidate in candidates:
        candidate_path = root_dir / candidate
        if candidate_path.exists():
            candidate_str = str(candidate_path)
            if candidate_str not in sys.path:
                sys.path.insert(0, candidate_str)
    # fallback: search ancestor directories for an ncnk_message package
    for parent in Path(__file__).resolve().parents:
        for candidate in candidates:
            package_root = parent / candidate / "ncnk_message"
            if package_root.exists():
                candidate_str = str(parent / candidate)
                if candidate_str not in sys.path:
                    sys.path.insert(0, candidate_str)
                return


try:
    from ncnk_message import (  # noqa: E402
        BaseMessageInfo,
        FormatInfo,
        GroupInfo,
        MessageBase,
        Router,
        RouteConfig,
        Seg,
        TargetConfig,
        TemplateInfo,
        UserInfo,
    )
except ModuleNotFoundError:
    _ensure_ncnk_message()
    from ncnk_message import (  # noqa: E402
        BaseMessageInfo,
        FormatInfo,
        GroupInfo,
        MessageBase,
        Router,
        RouteConfig,
        Seg,
        TargetConfig,
        TemplateInfo,
        UserInfo,
    )


ACCEPT_FORMAT = [
    "text",
    "reply",
    "command",
]
ACCEPT_FORMAT_PRIVATE = [
    "text",
    "image",
    "emoji",
    "reply",
    "command",
]

BUILD_TAG = "bilibili-adapter-build-2026-01-16"


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
    live_reply_prompt: str
    live_planner_prompt: str
    live_room_prompts: Dict[int, Dict[str, str]]
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


@dataclass
class PrivateSessionConfig:
    talker_id: int
    session_type: int


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
    room_prompts: Dict[int, Dict[str, str]] = {}
    if isinstance(room_prompts_raw, dict):
        for key, value in room_prompts_raw.items():
            try:
                room_id = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            room_prompts[room_id] = {
                "reply_prompt": str(value.get("reply_prompt", "") or ""),
                "planner_prompt": str(value.get("planner_prompt", "") or ""),
            }

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
        room_ids=[int(x) for x in live.get("room_ids", [])],
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
        live_reply_prompt=str(live.get("reply_prompt", "") or ""),
        live_planner_prompt=str(live.get("planner_prompt", "") or ""),
        live_room_prompts=room_prompts,
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


_EMOJI_RE = re.compile(
    "["  # noqa: W605
    "\U0001F100-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002702-\U000027B0"
    "]",
    flags=re.UNICODE,
)
_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_IMAGE_PREFIX_RE = re.compile(r"^data:image/([a-zA-Z0-9.+-]+);base64,", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_COMMAND_GUARD_PREFIX = "\u200b"


def _strip_emoji(text: str) -> str:
    if not text:
        return ""
    return _EMOJI_RE.sub("", text)

def _normalize_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text
    if "\\u" in cleaned:
        try:
            cleaned = cleaned.encode("utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            pass
    cleaned = cleaned.replace("\r", " ").replace("\n", " ")
    cleaned = _CONTROL_RE.sub("", cleaned)
    return cleaned


def _guess_image_format(image_bytes: bytes) -> str:
    if not image_bytes:
        return ""
    if image_bytes.startswith(b"\xFF\xD8\xFF"):
        return "jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "gif"
    return ""


def _decode_image_base64(data: Any) -> Tuple[Optional[bytes], str]:
    if not data or not isinstance(data, str):
        return None, ""
    raw = data.strip()
    if raw.startswith("base64://"):
        raw = raw[len("base64://") :]
    fmt = ""
    match = _IMAGE_PREFIX_RE.match(raw)
    if match:
        fmt = match.group(1).lower()
        raw = raw[match.end() :]
    try:
        image_bytes = base64.b64decode(raw)
    except Exception:
        return None, ""
    if not fmt:
        fmt = _guess_image_format(image_bytes)
    if fmt == "jpg":
        fmt = "jpeg"
    return image_bytes, fmt


def _extract_plain_text(seg: Seg) -> str:
    if seg.type == "seglist" and isinstance(seg.data, list):
        parts = [_extract_plain_text(child) for child in seg.data]
        return "".join(parts)
    if seg.type == "text":
        return _strip_emoji(str(seg.data or ""))
    return ""


def _extract_image_base64(seg: Seg) -> str:
    if seg.type in ("image", "emoji"):
        return str(seg.data or "")
    if seg.type == "seglist" and isinstance(seg.data, list):
        for child in seg.data:
            if isinstance(child, Seg):
                image_data = _extract_image_base64(child)
                if image_data:
                    return image_data
    return ""


def _guard_command_segment(seg: Seg) -> None:
    if seg.type == "text":
        text = str(seg.data or "")
        if text and not text.startswith(_COMMAND_GUARD_PREFIX):
            seg.data = f"{_COMMAND_GUARD_PREFIX}{text}"
        return
    if seg.type == "seglist" and isinstance(seg.data, list):
        for child in seg.data:
            if child.type == "text":
                child_text = str(child.data or "")
                if child_text and not child_text.startswith(_COMMAND_GUARD_PREFIX):
                    child.data = f"{_COMMAND_GUARD_PREFIX}{child_text}"
                elif not child_text:
                    child.data = _COMMAND_GUARD_PREFIX
                return
        seg.data.insert(0, Seg(type="text", data=_COMMAND_GUARD_PREFIX))


def _find_reply_id(seg: Seg) -> Optional[str]:
    if seg.type == "reply":
        return str(seg.data)
    if seg.type == "seglist" and isinstance(seg.data, list):
        for child in seg.data:
            reply_id = _find_reply_id(child)
            if reply_id:
                return reply_id
    return None


class WbiSigner:
    _mixin_key_enc_tab = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
        33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
        61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
        36, 20, 34, 44, 52,
    ]

    def __init__(self, api: "BilibiliApi", logger: logging.Logger):
        self.api = api
        self.logger = logger
        self._img_key = ""
        self._sub_key = ""
        self._last_refresh = 0.0

    async def _refresh_keys(self) -> None:
        now = time.time()
        if now - self._last_refresh < 12 * 3600 and self._img_key and self._sub_key:
            return
        data = await self.api.request_json(
            "GET",
            "https://api.bilibili.com/x/web-interface/nav",
            params=None,
            data=None,
            use_wbi=False,
        )
        wbi_img = (data or {}).get("data", {}).get("wbi_img", {})
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        if not img_url or not sub_url:
            raise RuntimeError("Failed to fetch WBI keys")
        self._img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
        self._sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
        self._last_refresh = now

    def _get_mixin_key(self) -> str:
        raw = (self._img_key + self._sub_key).encode("utf-8")
        mixin = bytes(raw[i] for i in self._mixin_key_enc_tab)[:32]
        return mixin.decode("utf-8", errors="ignore")

    async def sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        await self._refresh_keys()
        mixin_key = self._get_mixin_key()
        wts = str(int(time.time()))
        params = {k: v for k, v in params.items() if v is not None}
        params["wts"] = wts
        sorted_items = sorted(params.items(), key=lambda x: x[0])
        filtered = {}
        for key, value in sorted_items:
            value_str = str(value)
            for ch in "!'()*":
                value_str = value_str.replace(ch, "")
            filtered[key] = value_str
        query = "&".join(
            f"{urllib.parse.quote(key, safe='')}"
            f"={urllib.parse.quote(str(value), safe='')}"
            for key, value in filtered.items()
        )
        sign = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        params["w_rid"] = sign
        return params


class BilibiliApi:
    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.session: Optional[aiohttp.ClientSession] = None
        self.signer = WbiSigner(self, logger)

    async def start(self) -> None:
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=20)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    def _cookie_header(self) -> str:
        cookies = []
        if self.config.sessdata:
            cookies.append(f"SESSDATA={self.config.sessdata}")
        if self.config.bili_jct:
            cookies.append(f"bili_jct={self.config.bili_jct}")
        if self.config.buvid3:
            cookies.append(f"buvid3={self.config.buvid3}")
        if self.config.buvid4:
            cookies.append(f"buvid4={self.config.buvid4}")
        if self.config.dede_user_id:
            cookies.append(f"DedeUserID={self.config.dede_user_id}")
        return "; ".join(cookies)

    def _build_headers(self, referer: str = "https://www.bilibili.com/") -> Dict[str, str]:
        headers = {
            "User-Agent": self.config.user_agent,
            "Referer": referer,
        }
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def request_json(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        use_wbi: bool = False,
        referer: str = "https://www.bilibili.com/",
    ) -> Dict[str, Any]:
        if self.session is None:
            raise RuntimeError("HTTP session not started")
        final_params = params or {}
        if use_wbi:
            final_params = await self.signer.sign(final_params)
        headers = self._build_headers(referer=referer)
        async with self.session.request(
            method,
            url,
            params=final_params,
            data=data,
            headers=headers,
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                self.logger.warning(
                    "HTTP error: status=%s url=%s body=%s",
                    resp.status,
                    url,
                    text[:200],
                )
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                self.logger.warning(f"Non-JSON response from {url}: {text[:200]}")
                return {}
            if isinstance(payload, dict):
                code = payload.get("code")
                if code not in (None, 0):
                    self.logger.warning(
                        "Bilibili API error: url=%s status=%s code=%s message=%s",
                        url,
                        resp.status,
                        code,
                        payload.get("message") or payload.get("msg"),
                    )
            return payload

    async def fetch_bytes(
        self, url: str, referer: str = "https://message.bilibili.com/"
    ) -> Optional[bytes]:
        if self.session is None:
            raise RuntimeError("HTTP session not started")
        headers = self._build_headers(referer=referer)
        async with self.session.get(url, headers=headers) as resp:
            if resp.status >= 400:
                self.logger.warning(
                    "HTTP error: status=%s url=%s",
                    resp.status,
                    url,
                )
                return None
            return await resp.read()

    async def fetch_base64(
        self, url: str, referer: str = "https://message.bilibili.com/"
    ) -> Optional[str]:
        data = await self.fetch_bytes(url, referer=referer)
        if not data:
            return None
        return base64.b64encode(data).decode("ascii")

    async def upload_dynamic_image(
        self,
        image_bytes: bytes,
        image_format: str = "",
        category: str = "daily",
    ) -> Dict[str, Any]:
        if self.session is None:
            raise RuntimeError("HTTP session not started")
        if not self.config.bili_jct:
            raise RuntimeError("bili_jct is required to upload images")
        if not image_bytes:
            return {}
        fmt = (image_format or "jpeg").lower()
        ext = "jpg" if fmt in ("jpeg", "jpg") else fmt
        content_type = f"image/{fmt}" if fmt else "application/octet-stream"
        form = aiohttp.FormData()
        form.add_field(
            "file_up",
            image_bytes,
            filename=f"image.{ext}",
            content_type=content_type,
        )
        form.add_field("category", category)
        form.add_field("biz", "new_dyn")
        form.add_field("csrf", self.config.bili_jct)
        headers = self._build_headers(referer="https://t.bilibili.com/")
        async with self.session.post(
            "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs",
            data=form,
            headers=headers,
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                self.logger.warning(
                    "HTTP error: status=%s url=%s body=%s",
                    resp.status,
                    "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs",
                    text[:200],
                )
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                self.logger.warning(
                    "Non-JSON response from upload_bfs: %s", text[:200]
                )
                return {}
            if isinstance(payload, dict):
                code = payload.get("code")
                if code not in (None, 0):
                    self.logger.warning(
                        "Upload image failed: code=%s message=%s",
                        code,
                        payload.get("message") or payload.get("msg"),
                    )
            return payload

    async def get_danmu_info(self, room_id: int) -> Dict[str, Any]:
        return await self.request_json(
            "GET",
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
            params={"id": room_id, "type": 0, "web_location": 444.8},
            use_wbi=True,
            referer=f"https://live.bilibili.com/{room_id}",
        )

    async def send_danmu(
        self,
        room_id: int,
        message: str,
        reply_mid: Optional[str] = None,
        reply_dmid: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.config.bili_jct:
            raise RuntimeError("bili_jct is required to send danmu")
        payload = {
            "roomid": room_id,
            "msg": message,
            "rnd": int(time.time()),
            "fontsize": 25,
            "color": 16777215,
            "mode": 1,
            "bubble": 0,
            "room_type": 0,
            "jumpfrom": 0,
            "statistics": '{"appId":100,"platform":5}',
            "csrf": self.config.bili_jct,
            "csrf_token": self.config.bili_jct,
        }
        if reply_mid:
            payload["reply_mid"] = reply_mid
        if reply_dmid:
            payload["reply_dmid"] = reply_dmid
        return await self.request_json(
            "POST",
            "https://api.live.bilibili.com/msg/send",
            data=payload,
            use_wbi=False,
            referer=f"https://live.bilibili.com/{room_id}",
        )

    async def send_comment_reply(
        self,
        comment_type: int,
        oid: int,
        message: str,
        root: Optional[int] = None,
        parent: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.config.bili_jct:
            raise RuntimeError("bili_jct is required to send comment replies")
        payload: Dict[str, Any] = {
            "type": comment_type,
            "oid": oid,
            "message": message,
            "plat": 1,
            "csrf": self.config.bili_jct,
        }
        if root is not None:
            payload["root"] = root
        if parent is not None:
            payload["parent"] = parent
        return await self.request_json(
            "POST",
            "https://api.bilibili.com/x/v2/reply/add",
            data=payload,
            use_wbi=False,
            referer="https://www.bilibili.com/",
        )

    async def get_reply_notifications(self, size: int) -> List[Dict[str, Any]]:
        params = {
            "build": 0,
            "mobi_app": "web",
        }
        resp = await self.request_json(
            "GET",
            "https://api.bilibili.com/x/msgfeed/reply",
            params=params,
            use_wbi=False,
        )
        items = (resp or {}).get("data", {}).get("items") or []
        return list(items)[:size]

    async def get_at_notifications(self, size: int) -> List[Dict[str, Any]]:
        params = {
            "build": 0,
            "mobi_app": "web",
        }
        resp = await self.request_json(
            "GET",
            "https://api.bilibili.com/x/msgfeed/at",
            params=params,
            use_wbi=False,
        )
        items = (resp or {}).get("data", {}).get("items") or []
        if (resp or {}).get("code") not in (None, 0):
            resp = await self.request_json(
                "GET",
                "https://api.vc.bilibili.com/x/im/web/msgfeed/at",
                params=params,
                use_wbi=False,
            )
            items = (resp or {}).get("data", {}).get("items") or []
        return list(items)[:size]

    async def get_user_info(self, mid: int) -> Dict[str, Any]:
        params = {
            "mid": mid,
            "jsonp": "jsonp",
        }
        return await self.request_json(
            "GET",
            "https://api.bilibili.com/x/space/acc/info",
            params=params,
            use_wbi=False,
        )

    async def get_sessions(
        self,
        session_type: int,
        size: int = 100,
        group_fold: int = 0,
        unfollow_fold: int = 0,
        sort_rule: int = 2,
    ) -> Dict[str, Any]:
        params = {
            "session_type": session_type,
            "group_fold": group_fold,
            "unfollow_fold": unfollow_fold,
            "sort_rule": sort_rule,
            "size": size,
            "build": 0,
            "mobi_app": "web",
        }
        return await self.request_json(
            "GET",
            "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions",
            params=params,
            use_wbi=False,
        )

    async def fetch_session_msgs(
        self,
        talker_id: int,
        session_type: int,
        size: int,
        begin_seqno: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "talker_id": talker_id,
            "session_type": session_type,
            "size": size,
            "sender_device_id": 1,
            "build": 0,
            "mobi_app": "web",
        }
        if begin_seqno is not None:
            params["begin_seqno"] = begin_seqno
        return await self.request_json(
            "GET",
            "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs",
            params=params,
            use_wbi=False,
        )

    async def send_private_message(
        self,
        talker_id: int,
        session_type: int,
        message: str,
        dev_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.config.bili_jct:
            raise RuntimeError("bili_jct is required to send private messages")
        if dev_id is None:
            dev_id = str(uuid.uuid4())
        sender_uid = self.config.dede_user_id
        content = json.dumps({"content": message}, ensure_ascii=False)
        params = {
            "w_sender_uid": sender_uid,
            "w_receiver_id": talker_id,
            "w_dev_id": dev_id,
        }
        payload = {
            "msg[sender_uid]": sender_uid,
            "msg[receiver_id]": talker_id,
            "msg[receiver_type]": session_type,
            "msg[msg_type]": 1,
            "msg[msg_status]": 0,
            "msg[dev_id]": dev_id,
            "msg[timestamp]": int(time.time()),
            "msg[content]": content,
            "csrf": self.config.bili_jct,
            "csrf_token": self.config.bili_jct,
            "build": 0,
            "mobi_app": "web",
        }
        return await self.request_json(
            "POST",
            "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg",
            params=params,
            data=payload,
            use_wbi=True,
            referer="https://message.bilibili.com/",
        )

    async def send_private_image_message(
        self,
        talker_id: int,
        session_type: int,
        content: Dict[str, Any],
        dev_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.config.bili_jct:
            raise RuntimeError("bili_jct is required to send private images")
        if dev_id is None:
            dev_id = str(uuid.uuid4())
        sender_uid = self.config.dede_user_id
        payload = {
            "msg[sender_uid]": sender_uid,
            "msg[receiver_id]": talker_id,
            "msg[receiver_type]": session_type,
            "msg[msg_type]": 2,
            "msg[msg_status]": 0,
            "msg[dev_id]": dev_id,
            "msg[timestamp]": int(time.time()),
            "msg[content]": json.dumps(content, ensure_ascii=False),
            "csrf": self.config.bili_jct,
            "csrf_token": self.config.bili_jct,
            "build": 0,
            "mobi_app": "web",
        }
        params = {
            "w_sender_uid": sender_uid,
            "w_receiver_id": talker_id,
            "w_dev_id": dev_id,
        }
        return await self.request_json(
            "POST",
            "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg",
            params=params,
            data=payload,
            use_wbi=True,
            referer="https://message.bilibili.com/",
        )


class LiveRoomWorker:
    def __init__(
        self,
        room_id: int,
        config: AdapterConfig,
        api: BilibiliApi,
        adapter: "BilibiliAdapter",
        logger: logging.Logger,
    ):
        self.room_id = room_id
        self.config = config
        self.api = api
        self.adapter = adapter
        self.logger = logger
        self._stop_event = asyncio.Event()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._proxy_index: int = 0
        self._proxy_cycle: Optional[List[str]] = None
        self._authed = False

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws:
            await self._ws.close()

    def _get_proxy_cycle(self) -> Optional[List[str]]:
        if self._proxy_cycle:
            return self._proxy_cycle
        pool_path = Path(self.config.live_proxy_pool_path)
        if not pool_path.is_absolute():
            pool_path = Path(__file__).resolve().parent / pool_path
        proxy_list = _load_proxy_pool(pool_path)
        if not proxy_list:
            self.logger.warning("Proxy pool is empty: %s", pool_path)
            return None
        check_url = (self.config.live_proxy_check_url or "").strip()
        if check_url:
            checked = _check_proxy_list(
                proxy_list,
                check_url,
                self.config.live_proxy_check_timeout,
                self.logger,
            )
            if checked:
                proxy_list = checked
        proxy_cycle = _proxy_dicts_to_urls(proxy_list)
        if not proxy_cycle:
            self.logger.warning("Proxy pool has no usable entries: %s", pool_path)
            return None
        self._proxy_cycle = proxy_cycle
        return proxy_cycle

    def _should_mark_mention(self, text: str) -> bool:
        if self.config.live_mention_any_at:
            if "@" in text or "＠" in text:
                return True
        for keyword in self.config.live_mention_keywords:
            if not keyword:
                continue
            if keyword in text:
                return True
            for prefix in self.config.live_mention_prefixes:
                if not prefix:
                    continue
                if f"{prefix}{keyword}" in text:
                    return True
        return False

    async def run(self) -> None:
        backoff = self.config.reconnect_seconds
        while not self._stop_event.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning(
                    "Room %s error: %s (%s)",
                    self.room_id,
                    exc,
                    type(exc).__name__,
                    exc_info=True,
                )
            if self._stop_event.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(self.config.max_reconnect_seconds, backoff * 2)

    async def _run_once(self) -> None:
        info = await self.api.get_danmu_info(self.room_id)
        info_code = (info or {}).get("code")
        info_msg = (info or {}).get("message") or (info or {}).get("msg") or ""
        data = (info or {}).get("data", {})
        token = data.get("token")
        host_list = data.get("host_list") or []
        self.logger.info(
            "Room %s getDanmuInfo: code=%s message=%s token=%s hosts=%s",
            self.room_id,
            info_code,
            info_msg,
            bool(token),
            len(host_list),
        )
        if host_list:
            host_preview = [
                {
                    "host": item.get("host"),
                    "wss_port": item.get("wss_port"),
                    "ws_port": item.get("ws_port"),
                }
                for item in host_list[:3]
                if isinstance(item, dict)
            ]
            if host_preview:
                self.logger.debug("Room %s host_list preview: %s", self.room_id, host_preview)
        if not token or not host_list:
            raise RuntimeError(
                f"getDanmuInfo missing token/host_list: code={info_code} message={info_msg}"
            )
        if self.config.live_max_hosts > 0:
            host_list = host_list[: self.config.live_max_hosts]
        schemes = ["wss", "ws"] if self.config.use_wss else ["ws", "wss"]
        uris: List[str] = []
        seen: set[str] = set()
        for scheme in schemes:
            for host_info in host_list:
                host = host_info.get("host")
                if not host:
                    continue
                if scheme == "wss":
                    port_candidates = [host_info.get("wss_port") or 443, 443]
                else:
                    port_candidates = [host_info.get("ws_port") or 80, 80]
                for port in port_candidates:
                    if not port:
                        continue
                    uri = f"{scheme}://{host}:{port}/sub"
                    if uri not in seen:
                        uris.append(uri)
                        seen.add(uri)

        last_exc: Optional[BaseException] = None
        ws_headers = {
            "Origin": "https://live.bilibili.com",
            "Referer": f"https://live.bilibili.com/{self.room_id}",
        }
        proxy_value = (self.config.live_ws_proxy or "").strip()
        proxy_lower = proxy_value.lower()
        proxy_cycle: Optional[List[str]] = None
        if proxy_lower in {"", "none", "false", "off", "disable"}:
            proxy_setting: object = None
        elif proxy_lower in {"auto", "env", "true", "on"}:
            proxy_setting = True
            env_flags = {
                name: bool(os.environ.get(name))
                for name in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "NO_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                    "no_proxy",
                )
            }
            self.logger.info("Room %s ws_proxy=auto env=%s", self.room_id, env_flags)
        elif proxy_lower in {"pool", "file", "proxy_pool"}:
            proxy_cycle = self._get_proxy_cycle()
            proxy_setting = proxy_cycle[0] if proxy_cycle else None
        else:
            proxy_setting = proxy_value
        connect_variants = [
            ("default", {}),
            ("no-compression", {"compression": None}),
            ("ipv4", {"family": socket.AF_INET}),
            ("ipv4-no-compression", {"compression": None, "family": socket.AF_INET}),
        ]

        proxy_cycle_size = len(proxy_cycle) if proxy_cycle else 0
        self.logger.info(
            "Room %s websocket proxy=%s use_wss=%s open_timeout=%s max_hosts=%s max_attempts=%s pool=%s",
            self.room_id,
            proxy_setting if proxy_setting is not True else "auto",
            self.config.use_wss,
            self.config.live_open_timeout,
            self.config.live_max_hosts,
            self.config.live_max_attempts,
            proxy_cycle_size,
        )

        attempt_count = 0
        for uri in uris:
            for variant_name, variant_kwargs in connect_variants:
                if self._stop_event.is_set():
                    return
                if proxy_cycle:
                    if not hasattr(self, "_proxy_index"):
                        self._proxy_index = 0
                    proxy_setting = proxy_cycle[self._proxy_index % len(proxy_cycle)]
                    self._proxy_index += 1
                if self.config.live_max_attempts > 0 and attempt_count >= self.config.live_max_attempts:
                    if last_exc:
                        raise last_exc
                    raise RuntimeError("websocket connect attempts exhausted")
                attempt_count += 1
                proxy_label = (
                    "auto" if proxy_setting is True else (proxy_setting if proxy_setting else "none")
                )
                self.logger.info(
                    "Room %s connecting: %s (%s) proxy=%s",
                    self.room_id,
                    uri,
                    variant_name,
                    proxy_label,
                )
                try:
                    connect = websockets.connect(
                        uri,
                        ping_interval=None,
                        additional_headers=ws_headers,
                        user_agent_header=self.config.user_agent,
                        proxy=proxy_setting,
                        open_timeout=self.config.live_open_timeout,
                        **variant_kwargs,
                    )
                    connect_task = asyncio.create_task(connect.__aenter__())
                    if self.config.live_open_timeout > 0:
                        done, pending = await asyncio.wait(
                            {connect_task},
                            timeout=self.config.live_open_timeout,
                        )
                        if not done:
                            connect_task.cancel()
                            self.logger.warning(
                                "Room %s connect timeout after %ss uri=%s variant=%s",
                                self.room_id,
                                self.config.live_open_timeout,
                                uri,
                                variant_name,
                            )
                            continue
                    try:
                        ws = connect_task.result()
                    except Exception as exc:
                        with contextlib.suppress(Exception):
                            await connect.__aexit__(type(exc), exc, exc.__traceback__)
                        raise
                    try:
                        self._ws = ws
                        self._authed = False
                        self.logger.info(
                            "Room %s websocket connected: %s (%s) proxy=%s",
                            self.room_id,
                            uri,
                            variant_name,
                            proxy_label,
                        )
                        auth_body = {
                            "uid": int(self.config.dede_user_id) if self.config.dede_user_id else 0,
                            "roomid": self.room_id,
                            "protover": 3,
                            "platform": "web",
                            "type": 2,
                            "key": token,
                        }
                        await ws.send(self._pack(auth_body, op=7))
                        self.logger.debug("Room %s auth packet sent", self.room_id)
                        heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                        try:
                            async for message in ws:
                                if isinstance(message, bytes):
                                    await self._handle_packet(message)
                        finally:
                            heartbeat_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await heartbeat_task
                        self.logger.warning(
                            "Room %s websocket closed: code=%s reason=%s",
                            self.room_id,
                            ws.close_code,
                            ws.close_reason,
                        )
                    finally:
                        self._ws = None
                        with contextlib.suppress(Exception):
                            await connect.__aexit__(None, None, None)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    self.logger.warning(
                        "Room %s connect failed: %s (%s) uri=%s variant=%s",
                        self.room_id,
                        exc,
                        type(exc).__name__,
                        uri,
                        variant_name,
                        exc_info=True,
                    )

        if last_exc:
            raise last_exc

    async def _heartbeat_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(self.config.heartbeat_interval)
            try:
                await ws.send(self._pack({}, op=2))
            except Exception as exc:
                self.logger.warning(
                    "Room %s heartbeat error: %s (%s)",
                    self.room_id,
                    exc,
                    type(exc).__name__,
                    exc_info=True,
                )
                break

    async def _handle_packet(self, data: bytes) -> None:
        for op, body in self._unpack(data):
            if op == 5:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                await self._handle_event(payload)
            elif op == 8 and not self._authed:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    payload = {}
                self._authed = True
                self.logger.info("Room %s auth ok: %s", self.room_id, payload)

    async def _handle_event(self, payload: Dict[str, Any]) -> None:
        cmd = payload.get("cmd") or ""
        if not cmd.startswith("DANMU_MSG"):
            return
        info = payload.get("info") or []
        if len(info) < 3:
            return
        message_text = str(info[1] or "")
        user_info = info[2] if isinstance(info[2], list) else []
        user_id = str(user_info[0] or "")
        user_name = str(user_info[1] or user_id)
        if (
            self.config.live_resolve_user_nickname
            and user_id
            and (not user_name or user_name == user_id)
        ):
            user_name = await self.adapter._resolve_user_nickname(user_id)
        timestamp_ms = 0
        if isinstance(info[0], list) and len(info[0]) > 4:
            timestamp_ms = int(info[0][4] or 0)
        message_id = self._extract_message_id(payload, info, timestamp_ms)
        reply_mid = ""
        reply_dmid = ""
        extra = self._extract_extra(info)
        if extra:
            reply_mid = str(extra.get("reply_mid") or "")
            reply_dmid = str(extra.get("reply_dmid") or extra.get("reply_id") or "")

        if self.adapter.is_self_danmu(self.room_id, user_id, message_id, message_text):
            if self.config.live_log_danmu:
                safe_text = _normalize_text(message_text)
                if len(safe_text) > 120:
                    safe_text = safe_text[:117] + "..."
                self.logger.info(
                    "Danmu ignored (self): room_id=%s user_id=%s message_id=%s text=%s",
                    self.room_id,
                    user_id,
                    message_id,
                    safe_text,
                )
            return
        if self.config.live_log_danmu:
            safe_text = _normalize_text(message_text)
            if len(safe_text) > 120:
                safe_text = safe_text[:117] + "..."
            self.logger.info(
                "Danmu received: room_id=%s user_id=%s message_id=%s text=%s",
                self.room_id,
                user_id,
                message_id,
                safe_text,
            )
        is_mentioned = self._should_mark_mention(message_text)
        if is_mentioned:
            self.logger.info(
                "Danmu mention detected: room_id=%s user_id=%s message_id=%s",
                self.room_id,
                user_id,
                message_id,
            )

        self.adapter.remember_danmu(self.room_id, message_id, user_id)
        await self.adapter.handle_incoming_danmu(
            room_id=self.room_id,
            message_id=message_id,
            text=message_text,
            user_id=user_id,
            user_name=user_name,
            timestamp=timestamp_ms / 1000 if timestamp_ms else time.time(),
            reply_mid=reply_mid,
            reply_dmid=reply_dmid,
            is_mentioned=is_mentioned,
        )

    @staticmethod
    def _pack(body: Any, op: int) -> bytes:
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8") if body else b""
        packet_len = 16 + len(body_bytes)
        header = packet_len.to_bytes(4, "big")
        header += (16).to_bytes(2, "big")
        header += (1).to_bytes(2, "big")
        header += op.to_bytes(4, "big")
        header += (1).to_bytes(4, "big")
        return header + body_bytes

    def _unpack(self, data: bytes) -> List[Tuple[int, bytes]]:
        packets: List[Tuple[int, bytes]] = []
        offset = 0
        data_len = len(data)
        while offset + 16 <= data_len:
            packet_len = int.from_bytes(data[offset : offset + 4], "big")
            header_len = int.from_bytes(data[offset + 4 : offset + 6], "big")
            version = int.from_bytes(data[offset + 6 : offset + 8], "big")
            op = int.from_bytes(data[offset + 8 : offset + 12], "big")
            body = data[offset + header_len : offset + packet_len]
            offset += packet_len
            if version == 2:
                decompressed = zlib.decompress(body)
                packets.extend(self._unpack(decompressed))
            elif version == 3:
                decompressed = brotli.decompress(body)
                packets.extend(self._unpack(decompressed))
            else:
                packets.append((op, body))
        return packets

    @staticmethod
    def _extract_message_id(payload: Dict[str, Any], info: list, timestamp_ms: int) -> str:
        msg_id = payload.get("msg_id")
        if msg_id:
            return str(msg_id)
        extra = LiveRoomWorker._extract_extra(info)
        if extra and extra.get("id_str"):
            return str(extra.get("id_str"))
        if timestamp_ms:
            return f"{timestamp_ms}-{uuid.uuid4().hex[:6]}"
        return uuid.uuid4().hex

    @staticmethod
    def _extract_extra(info: list) -> Optional[Dict[str, Any]]:
        try:
            extra_raw = info[0][15].get("extra")
        except Exception:
            return None
        if not extra_raw:
            return None
        try:
            return json.loads(extra_raw)
        except json.JSONDecodeError:
            return None


class BilibiliAdapter:
    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        route_config = RouteConfig(
            route_config={
                self.config.platform: TargetConfig(
                    url=f"ws://{self.config.nachobot_host}:{self.config.nachobot_port}/ws",
                    token=None,
                )
            }
        )
        self.router = Router(route_config, custom_logger=logger)
        self.router.register_class_handler(self.handle_from_nachobot)
        self.api = BilibiliApi(config, logger)
        self._danmu_cache: Dict[int, Dict[str, str]] = {}
        self._reply_seen: List[str] = []
        self._reply_seen_set: set[str] = set()
        self._dm_last_seqno: Dict[Tuple[int, int], int] = {}
        self._last_private_session: Optional[PrivateSessionConfig] = None
        self._private_session_by_group: Dict[str, PrivateSessionConfig] = {}
        self._auto_private_sessions: List[PrivateSessionConfig] = []
        self._auto_private_sessions_ts: float = 0.0
        self._user_name_cache: Dict[str, Tuple[str, float]] = {}
        self._user_name_cache_seconds = 3600
        self._comment_context: Dict[str, Dict[str, Any]] = {}
        self._comment_bootstrap_done = False
        self._self_danmu_ids: Dict[int, Dict[str, float]] = {}
        self._self_danmu_texts: Dict[int, List[Tuple[str, float]]] = {}

    async def run(self) -> None:
        await self.api.start()
        tasks = [self.router.run()]
        if self.config.live_enable:
            for room_id in self.config.room_ids:
                worker = LiveRoomWorker(room_id, self.config, self.api, self, self.logger)
                tasks.append(worker.run())
        else:
            self.logger.info("Live adapter disabled by config")
        if self.config.enable_reply_notice:
            tasks.append(self._comment_notice_loop())
        if self.config.private_enable and (
            self.config.private_sessions or self.config.private_auto_sessions
        ):
            tasks.append(self._private_message_loop())
        await asyncio.gather(*tasks)

    def remember_danmu(self, room_id: int, message_id: str, user_id: str) -> None:
        cache = self._danmu_cache.setdefault(room_id, {})
        cache[message_id] = user_id
        if len(cache) > 2000:
            for key in list(cache.keys())[:500]:
                cache.pop(key, None)

    def _filter_outgoing_text(self, text: str) -> str:
        if not text:
            return text
        if not self.config.response_filter_enable:
            return text
        markers = self.config.response_filter_blocked_markers
        if not markers:
            return text
        normalized = text.lower()
        for marker in markers:
            if marker and marker in normalized:
                self.logger.error(
                    "[BilibiliAdapter] Detected blocked marker in outgoing text, replaced with Filtered"
                )
                return "Filtered"
        return text

    @staticmethod
    def _normalize_image_url(url: str) -> str:
        if not url:
            return ""
        if url.startswith("//"):
            return f"https:{url}"
        return url

    def _extract_private_image_url(self, content: Any) -> str:
        image_keys = (
            "url",
            "image_url",
            "img_url",
            "image",
            "img",
            "src",
            "origin_url",
            "original_url",
            "preview",
            "cover",
            "thumb",
            "pic",
            "pic_url",
            "picture",
            "photo",
            "face",
            "raw_url",
        )

        def scan(value: Any) -> str:
            if isinstance(value, str):
                candidate = value.strip()
                if candidate.startswith(("http://", "https://", "//")):
                    return candidate
                match = _URL_RE.search(candidate)
                return match.group(0) if match else ""
            if isinstance(value, dict):
                for key in image_keys:
                    if key in value:
                        found = scan(value.get(key))
                        if found:
                            return found
                for item in value.values():
                    found = scan(item)
                    if found:
                        return found
            if isinstance(value, list):
                for item in value:
                    found = scan(item)
                    if found:
                        return found
            return ""

        content_value: Any = content
        if isinstance(content, str):
            trimmed = content.strip()
            if trimmed.startswith("{") or trimmed.startswith("["):
                try:
                    content_value = json.loads(trimmed)
                except json.JSONDecodeError:
                    content_value = content
        url = scan(content_value)
        return self._normalize_image_url(url)

    async def _download_private_image(self, url: str) -> Optional[str]:
        if not url:
            return None
        try:
            return await self.api.fetch_base64(url)
        except Exception as exc:
            self.logger.warning("Private image download failed: url=%s error=%s", url, exc)
            return None

    async def handle_incoming_danmu(
        self,
        room_id: int,
        message_id: str,
        text: str,
        user_id: str,
        user_name: str,
        timestamp: float,
        reply_mid: str = "",
        reply_dmid: str = "",
        is_mentioned: bool = False,
    ) -> None:
        if not text:
            return
        template_info = None
        reply_prompt, planner_prompt = self._resolve_live_prompts(room_id)
        if reply_prompt or planner_prompt:
            template_items: Dict[str, str] = {}
            if reply_prompt:
                template_items["replyer_prompt"] = reply_prompt
            if planner_prompt:
                template_items["planner_prompt"] = planner_prompt
            template_info = TemplateInfo(
                template_items=template_items,
                template_name=f"bilibili_live_{room_id}",
                template_default=False,
            )
        additional_config = {
            "room_id": room_id,
            "reply_mid": reply_mid,
            "reply_dmid": reply_dmid,
        }
        if is_mentioned:
            additional_config["is_mentioned"] = 1.0
        message_info = BaseMessageInfo(
            platform=self.config.platform,
            message_id=str(message_id),
            time=float(timestamp),
            user_info=UserInfo(
                platform=self.config.platform,
                user_id=user_id,
                user_nickname=user_name,
            ),
            group_info=GroupInfo(
                platform=self.config.platform,
                group_id=str(room_id),
                group_name=str(room_id),
            ),
            format_info=FormatInfo(
                content_format=["text"],
                accept_format=ACCEPT_FORMAT,
            ),
            template_info=template_info,
            additional_config=additional_config,
        )
        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="text", data=text),
            raw_message=None,
        )
        await self._send_to_nachobot(message)

    def _resolve_live_prompts(self, room_id: int) -> Tuple[str, str]:
        reply_prompt = self.config.live_reply_prompt
        planner_prompt = self.config.live_planner_prompt
        room_prompts = self.config.live_room_prompts.get(room_id)
        if room_prompts is not None:
            room_reply = str(room_prompts.get("reply_prompt", "") or "")
            room_planner = str(room_prompts.get("planner_prompt", "") or "")
            if room_reply:
                reply_prompt = room_reply
            if room_planner:
                planner_prompt = room_planner
        return reply_prompt, planner_prompt

    async def _comment_notice_loop(self) -> None:
        while True:
            reply_items: List[Dict[str, Any]] = []
            at_items: List[Dict[str, Any]] = []
            try:
                reply_items = await self.api.get_reply_notifications(
                    self.config.comment_max_items
                )
            except Exception as exc:
                self.logger.warning(f"Reply notice fetch error: {exc}")
            try:
                at_items = await self.api.get_at_notifications(
                    self.config.comment_max_items
                )
            except Exception as exc:
                self.logger.warning(f"At notice fetch error: {exc}")
            if reply_items or at_items:
                self.logger.info(
                    "Comment notices: reply=%s at=%s",
                    len(reply_items),
                    len(at_items),
                )
            else:
                self.logger.debug("Comment notices: 0")
            if not self._comment_bootstrap_done:
                for item in reply_items:
                    self._track_notice_key(self._notice_key("reply", item))
                for item in at_items:
                    self._track_notice_key(self._notice_key("at", item))
                self._comment_bootstrap_done = True
            else:
                await self._handle_reply_notifications(reply_items, source="reply")
                await self._handle_reply_notifications(at_items, source="at")
            await asyncio.sleep(self.config.comment_poll_interval)

    def _notice_key(self, source: str, item: Dict[str, Any]) -> str:
        notify_id = str(item.get("id") or "")
        if not notify_id:
            return ""
        return f"{source}:{notify_id}"

    def _track_notice_key(self, notify_key: str) -> bool:
        if not notify_key or notify_key in self._reply_seen_set:
            return False
        self._reply_seen_set.add(notify_key)
        self._reply_seen.append(notify_key)
        if len(self._reply_seen) > 500:
            old = self._reply_seen.pop(0)
            self._reply_seen_set.discard(old)
        return True

    def _is_at_me(self, at_details: Any) -> bool:
        if not self.config.dede_user_id:
            return False
        if not isinstance(at_details, list):
            return False
        for detail in at_details:
            if not isinstance(detail, dict):
                continue
            if str(detail.get("mid") or "") == self.config.dede_user_id:
                return True
        return False

    async def _handle_reply_notifications(
        self, items: List[Dict[str, Any]], source: str
    ) -> None:
        if not items:
            return
        for item in items:
            notify_id = str(item.get("id") or "")
            if not notify_id:
                continue
            notify_key = self._notice_key(source, item)
            if not self._track_notice_key(notify_key):
                continue

            user = item.get("user") or {}
            reply_item = item.get("item") or {}
            user_id = str(user.get("mid") or "")
            user_name = str(user.get("nickname") or user_id)
            if (
                self.config.comment_resolve_user_nickname
                and user_id
                and (not user_name or user_name == user_id)
            ):
                user_name = await self._resolve_user_nickname(user_id)
            business_id = reply_item.get("business_id")
            subject_id = reply_item.get("subject_id")
            content = (
                reply_item.get("source_content")
                or reply_item.get("target_reply_content")
                or reply_item.get("root_reply_content")
                or reply_item.get("title")
                or ""
            )
            at_details = reply_item.get("at_details") or []
            is_at_me = self._is_at_me(at_details)
            if source == "at" and self.config.dede_user_id and not is_at_me:
                self.logger.debug(
                    "At notice without bot mention: id=%s", notify_id
                )
            group_id = f"comment:{business_id}:{subject_id}"
            self._remember_comment_context(
                group_id=group_id,
                comment_type=business_id,
                comment_oid=subject_id,
                root_id=reply_item.get("root_id"),
                source_id=reply_item.get("source_id"),
                target_id=reply_item.get("target_id"),
            )
            now_ts = time.time()
            reply_time = float(item.get("reply_time") or now_ts)
            message_info = BaseMessageInfo(
                platform=self.config.platform,
                message_id=notify_id,
                time=now_ts,
                user_info=UserInfo(
                    platform=self.config.platform,
                    user_id=user_id,
                    user_nickname=user_name,
                ),
                group_info=GroupInfo(
                    platform=self.config.platform,
                    group_id=group_id,
                    group_name=group_id,
                ),
                format_info=FormatInfo(
                    content_format=["text"],
                    accept_format=ACCEPT_FORMAT,
                ),
                additional_config=self._build_comment_notice_config(
                    business_id=business_id,
                    subject_id=subject_id,
                    reply_item=reply_item,
                    source=source,
                    is_at_me=is_at_me,
                    reply_time=reply_time,
                ),
            )
            message = MessageBase(
                message_info=message_info,
                message_segment=Seg(type="text", data=str(content)),
                raw_message=json.dumps(item, ensure_ascii=True),
            )
            await self._send_to_nachobot(message)

    def _build_comment_notice_config(
        self,
        business_id: Any,
        subject_id: Any,
        reply_item: Dict[str, Any],
        source: str,
        is_at_me: bool,
        reply_time: float,
    ) -> Dict[str, Any]:
        config: Dict[str, Any] = {
            "comment_type": business_id,
            "comment_oid": subject_id,
            "root_id": reply_item.get("root_id"),
            "source_id": reply_item.get("source_id"),
            "target_id": reply_item.get("target_id"),
            "uri": reply_item.get("uri"),
            "notice_source": source,
            "reply_time": reply_time,
        }
        if self.config.comment_force_mention or source == "reply" or source == "at" or is_at_me:
            config["is_mentioned"] = 1.0
        return config

    async def handle_from_nachobot(self, raw_message_base_dict: dict) -> None:
        message = MessageBase.from_dict(raw_message_base_dict)
        self.logger.info(
            "Incoming from NachoBot: platform=%s group_id=%s user_id=%s",
            message.message_info.platform,
            getattr(message.message_info.group_info, "group_id", None),
            getattr(message.message_info.user_info, "user_id", None),
        )
        seg = message.message_segment
        if seg.type == "command":
            await self._handle_command(message)
            return

        image_data = _extract_image_base64(seg)
        if image_data:
            private_target = self._resolve_private_target(message)
            if private_target:
                await self._send_private_image(private_target, image_data)
                text = _extract_plain_text(seg).strip()
                if text:
                    await self._send_private_message(private_target, text)
            else:
                self.logger.warning("Image message unsupported for non-private target")
            return

        text = _extract_plain_text(seg).strip()
        if not text:
            return
        comment_target = self._resolve_comment_target(message)
        if comment_target:
            await self._send_comment_reply_from_context(comment_target, text)
            return
        room_id = self._resolve_room_id(message)
        if room_id is not None:
            reply_dmid = _find_reply_id(seg)
            reply_mid = ""
            if reply_dmid:
                reply_mid = self._lookup_reply_mid(room_id, reply_dmid)
            await self._send_danmu(room_id, text, reply_mid, reply_dmid)
            return
        private_target = self._resolve_private_target(message)
        if private_target:
            await self._send_private_message(private_target, text)
            return
        self.logger.warning("Missing room_id for outgoing danmu")

    def _resolve_room_id(self, message: MessageBase) -> Optional[int]:
        group_info = message.message_info.group_info
        if group_info and group_info.group_id:
            try:
                return int(group_info.group_id)
            except ValueError:
                return None
        additional = message.message_info.additional_config or {}
        room_id = additional.get("room_id")
        if room_id is None:
            return None
        try:
            return int(room_id)
        except ValueError:
            return None

    def _lookup_reply_mid(self, room_id: int, reply_dmid: str) -> str:
        cache = self._danmu_cache.get(room_id) or {}
        return str(cache.get(reply_dmid) or "")

    async def _send_danmu(
        self,
        room_id: int,
        text: str,
        reply_mid: Optional[str],
        reply_dmid: Optional[str],
    ) -> None:
        text = self._filter_outgoing_text(text)
        self.logger.info(
            "Send danmu: room_id=%s reply_mid=%s reply_dmid=%s text=%s",
            room_id,
            reply_mid or "",
            reply_dmid or "",
            text,
        )
        try:
            resp = await self.api.send_danmu(
                room_id=room_id,
                message=text,
                reply_mid=reply_mid or None,
                reply_dmid=reply_dmid or None,
            )
            if (resp or {}).get("code") != 0:
                self.logger.warning(f"Danmu send failed: {resp}")
            else:
                dmid = None
                data = (resp or {}).get("data", {})
                if isinstance(data, dict):
                    dmid = data.get("dmid") or data.get("dmid_str")
                self._remember_self_danmu(room_id, str(dmid) if dmid else "", text)
                self.logger.info("Danmu send ok")
        except Exception as exc:
            self.logger.error(f"Danmu send error: {exc}")

    def _remember_self_danmu(self, room_id: int, message_id: str, text: str) -> None:
        now = time.time()
        if message_id:
            room_cache = self._self_danmu_ids.setdefault(room_id, {})
            room_cache[message_id] = now
            if len(room_cache) > 500:
                for msg_id, ts in list(room_cache.items()):
                    if now - ts > 30:
                        room_cache.pop(msg_id, None)
        room_texts = self._self_danmu_texts.setdefault(room_id, [])
        if text:
            room_texts.append((text, now))
        if len(room_texts) > 200:
            self._self_danmu_texts[room_id] = [
                item for item in room_texts if now - item[1] <= 30
            ]

    def is_self_danmu(
        self,
        room_id: int,
        user_id: str,
        message_id: str,
        text: str,
    ) -> bool:
        if (not self.config.live_allow_self_danmu) and self.config.dede_user_id and user_id:
            if str(user_id) == str(self.config.dede_user_id):
                return True
        now = time.time()
        room_cache = self._self_danmu_ids.get(room_id, {})
        if message_id and message_id in room_cache:
            return True
        room_texts = self._self_danmu_texts.get(room_id, [])
        if text:
            window = 2.5 if len(text.strip()) <= 2 else 6.0
            for sent_text, ts in list(room_texts):
                if now - ts > 30:
                    continue
                if sent_text == text and (now - ts) <= window:
                    return True
        return False

    async def _handle_command(self, message: MessageBase) -> None:
        seg = message.message_segment
        command_data = seg.data if isinstance(seg.data, dict) else {}
        command_name = str(command_data.get("name") or "")
        args = command_data.get("args") or {}
        if command_name == "BILI_COMMENT_REPLY":
            await self._handle_comment_reply(args)
            return
        if command_name == "BILI_LIVE_REPLY":
            await self._handle_live_reply(args)
            return
        if command_name == "BILI_PRIVATE_SEND":
            await self._handle_private_send(args, message)
            return
        self.logger.warning(f"Unknown command: {command_name}")

    async def _handle_comment_reply(self, args: Dict[str, Any]) -> None:
        text = _strip_emoji(str(args.get("message") or "")).strip()
        if not text:
            self.logger.warning("Empty comment reply text")
            return
        text = self._filter_outgoing_text(text)
        try:
            comment_type = int(args.get("type"))
            oid = int(args.get("oid"))
        except (TypeError, ValueError):
            self.logger.warning("Invalid comment reply args")
            return
        root = args.get("root")
        parent = args.get("parent")
        root_id = int(root) if root not in (None, "", 0) else None
        parent_id = int(parent) if parent not in (None, "", 0) else None
        self.logger.info(
            "Send comment reply: type=%s oid=%s root=%s parent=%s",
            comment_type,
            oid,
            root_id,
            parent_id,
        )
        try:
            resp = await self.api.send_comment_reply(
                comment_type=comment_type,
                oid=oid,
                message=text,
                root=root_id,
                parent=parent_id,
            )
            if (resp or {}).get("code") != 0:
                self.logger.warning(f"Comment reply failed: {resp}")
            else:
                self.logger.info("Comment reply ok")
        except Exception as exc:
            self.logger.error(f"Comment reply error: {exc}")

    async def _handle_live_reply(self, args: Dict[str, Any]) -> None:
        text = _strip_emoji(str(args.get("message") or "")).strip()
        if not text:
            return
        try:
            room_id = int(args.get("room_id"))
        except (TypeError, ValueError):
            self.logger.warning("Invalid room_id for live reply")
            return
        reply_mid = str(args.get("reply_mid") or "")
        reply_dmid = str(args.get("reply_dmid") or "")
        await self._send_danmu(room_id, text, reply_mid or None, reply_dmid or None)

    async def _handle_private_send(self, args: Dict[str, Any], message: Optional[MessageBase]) -> None:
        text = _strip_emoji(str(args.get("message") or "")).strip()
        if not text:
            return
        talker_id = args.get("talker_id")
        session_type = args.get("session_type")
        target = None
        if talker_id not in (None, "", 0):
            try:
                target = PrivateSessionConfig(
                    talker_id=int(talker_id),
                    session_type=int(session_type or 1),
                )
            except (TypeError, ValueError):
                target = None
        if target is None and message is not None:
            target = self._resolve_private_target(message)
        if target is None:
            self.logger.warning("Missing private message target")
            return
        await self._send_private_message(target, text)

    async def _send_to_nachobot(self, message: MessageBase) -> None:
        self.logger.info(
            "Forward to NachoBot: platform=%s group_id=%s message_id=%s",
            message.message_info.platform,
            getattr(message.message_info.group_info, "group_id", None),
            message.message_info.message_id,
        )
        if self.config.disable_command_trigger:
            _guard_command_segment(message.message_segment)
        if self.config.disable_video_sender_plugin and message.raw_message:
            message.raw_message = None
        client = self.router.clients.get(self.config.platform)
        if client is not None:
            await client.send_message(message.to_dict())
            return
        await self.router.send_message(message)

    async def _private_message_loop(self) -> None:
        while True:
            try:
                await self._poll_private_messages()
            except Exception as exc:
                self.logger.warning(f"Private message loop error: {exc}")
            await asyncio.sleep(self.config.private_poll_interval)

    async def _get_private_sessions(self) -> List[PrivateSessionConfig]:
        sessions: Dict[Tuple[int, int], PrivateSessionConfig] = {
            (item.session_type, item.talker_id): item for item in self.config.private_sessions
        }
        if not self.config.private_auto_sessions:
            return list(sessions.values())

        now = time.time()
        refresh_seconds = max(5, self.config.private_auto_session_refresh_seconds)
        if (
            not self._auto_private_sessions
            or (now - self._auto_private_sessions_ts) >= refresh_seconds
        ):
            auto_sessions: Dict[Tuple[int, int], PrivateSessionConfig] = {}
            for session_type in self.config.private_auto_session_types:
                resp = await self.api.get_sessions(
                    session_type=session_type,
                    size=self.config.private_auto_session_size,
                )
                if isinstance(resp, dict) and resp.get("code") not in (None, 0):
                    self.logger.warning(
                        "Session list failed: session_type=%s code=%s message=%s",
                        session_type,
                        resp.get("code"),
                        resp.get("message") or resp.get("msg"),
                    )
                    continue
                data = (resp or {}).get("data", {})
                session_list = data.get("session_list") or []
                if not isinstance(session_list, list):
                    continue
                for item in session_list:
                    if not isinstance(item, dict):
                        continue
                    try:
                        talker_id = int(item.get("talker_id") or 0)
                        item_type = int(item.get("session_type") or session_type)
                    except (TypeError, ValueError):
                        continue
                    if item_type not in (1, 2):
                        continue
                    if not talker_id:
                        continue
                    auto_sessions[(item_type, talker_id)] = PrivateSessionConfig(
                        talker_id=talker_id,
                        session_type=item_type,
                    )
            self._auto_private_sessions = list(auto_sessions.values())
            self._auto_private_sessions_ts = now

        for item in self._auto_private_sessions:
            sessions.setdefault((item.session_type, item.talker_id), item)
        return list(sessions.values())

    async def _poll_private_messages(self) -> None:
        sessions = await self._get_private_sessions()
        if not sessions:
            self.logger.debug("Private polling: no sessions")
            return
        self.logger.debug("Private polling: %s sessions", len(sessions))
        for session in sessions:
            key = (session.session_type, session.talker_id)
            last_seqno = self._dm_last_seqno.get(key)
            resp = await self.api.fetch_session_msgs(
                talker_id=session.talker_id,
                session_type=session.session_type,
                size=20,
                begin_seqno=last_seqno,
            )
            if isinstance(resp, dict) and resp.get("code") not in (None, 0):
                self.logger.warning(
                    "Private poll failed: talker_id=%s session_type=%s code=%s message=%s",
                    session.talker_id,
                    session.session_type,
                    resp.get("code"),
                    resp.get("message") or resp.get("msg"),
                )
            data = (resp or {}).get("data", {})
            messages = data.get("messages") or []
            max_seqno = data.get("max_seqno")
            if max_seqno is None:
                continue
            if last_seqno is None:
                self._dm_last_seqno[key] = int(max_seqno)
                continue
            if not messages:
                if int(max_seqno) > last_seqno:
                    self._dm_last_seqno[key] = int(max_seqno)
                continue
            await self._emit_private_messages(session=session, messages=messages)
            self._dm_last_seqno[key] = int(max_seqno)

    async def _emit_private_messages(
        self,
        session: PrivateSessionConfig,
        messages: Iterable[Dict[str, Any]],
    ) -> None:
        messages_list = list(messages)
        if messages_list:
            self.logger.info(
                "Private messages: talker_id=%s session_type=%s count=%s",
                session.talker_id,
                session.session_type,
                len(messages_list),
            )
        for msg in reversed(messages_list):
            sender_uid = str(msg.get("sender_uid") or "")
            if sender_uid and self.config.dede_user_id and sender_uid == str(self.config.dede_user_id):
                continue
            msg_type = int(msg.get("msg_type") or 0)
            content = msg.get("content")
            content_text = ""
            segment: Optional[Seg] = None
            content_format = ["text"]
            image_url = ""
            if msg_type in (2, 6):
                image_url = self._extract_private_image_url(content)
                if image_url:
                    image_base64 = await self._download_private_image(image_url)
                    if image_base64:
                        segment = Seg(type="image", data=image_base64)
                        content_format = ["image"]
                if segment is None:
                    content_text = self._parse_private_content(msg_type, content) or "[image]"
            else:
                content_text = self._parse_private_content(msg_type, content)

            if segment is None:
                if not content_text:
                    continue
                segment = Seg(type="text", data=content_text)
            message_id = str(msg.get("msg_key") or msg.get("msg_seqno") or uuid.uuid4().hex)
            now_ts = time.time()
            msg_time = float(msg.get("timestamp") or now_ts)
            group_id = f"dm:{session.session_type}:{session.talker_id}"
            self._remember_private_session(group_id, session)
            sender_name = await self._resolve_user_nickname(sender_uid or str(session.talker_id))
            additional_config = {
                "session_type": session.session_type,
                "talker_id": session.talker_id,
                "msg_type": msg_type,
                "msg_seqno": msg.get("msg_seqno"),
                "message_time": msg_time,
            }
            if image_url:
                additional_config["image_url"] = image_url
            if self.config.private_force_mention:
                additional_config["is_mentioned"] = 1.0
            message_info = BaseMessageInfo(
                platform=self.config.platform,
                message_id=message_id,
                time=now_ts,
                user_info=UserInfo(
                    platform=self.config.platform,
                    user_id=sender_uid or str(session.talker_id),
                    user_nickname=sender_name,
                ),
                group_info=None,
                format_info=FormatInfo(
                    content_format=content_format,
                    accept_format=ACCEPT_FORMAT_PRIVATE,
                ),
                additional_config=additional_config,
            )
            message = MessageBase(
                message_info=message_info,
                message_segment=segment,
                raw_message=json.dumps(msg, ensure_ascii=False),
            )
            await self._send_to_nachobot(message)

    def _remember_private_session(self, group_id: str, session: PrivateSessionConfig) -> None:
        self._private_session_by_group[group_id] = session
        self._last_private_session = session

    def _remember_comment_context(
        self,
        group_id: str,
        comment_type: Optional[int],
        comment_oid: Optional[int],
        root_id: Any,
        source_id: Any,
        target_id: Any,
    ) -> None:
        if not group_id:
            return
        self._comment_context[group_id] = {
            "comment_type": comment_type,
            "comment_oid": comment_oid,
            "root_id": root_id,
            "source_id": source_id,
            "target_id": target_id,
            "ts": time.time(),
        }

    @staticmethod
    def _parse_comment_group_id(group_id: str) -> Optional[Tuple[int, int]]:
        if not group_id or not group_id.startswith("comment:"):
            return None
        parts = group_id.split(":")
        if len(parts) != 3:
            return None
        try:
            return int(parts[1]), int(parts[2])
        except ValueError:
            return None

    def _resolve_comment_target(
        self, message: MessageBase
    ) -> Optional[Tuple[int, int, Optional[int], Optional[int]]]:
        group_info = message.message_info.group_info
        group_id = group_info.group_id if group_info else None
        if not group_id:
            return None
        parsed = self._parse_comment_group_id(group_id)
        if not parsed:
            return None
        comment_type, comment_oid = parsed
        context = self._comment_context.get(group_id, {})
        root_id = context.get("root_id")
        source_id = context.get("source_id")
        target_id = context.get("target_id")
        root = None
        parent = None
        for value in (root_id, source_id, target_id):
            if value not in (None, "", 0):
                try:
                    root = int(value)
                    break
                except (TypeError, ValueError):
                    continue
        for value in (source_id, target_id, root_id):
            if value not in (None, "", 0):
                try:
                    parent = int(value)
                    break
                except (TypeError, ValueError):
                    continue
        return comment_type, comment_oid, root, parent

    async def _send_comment_reply_from_context(
        self,
        target: Tuple[int, int, Optional[int], Optional[int]],
        text: str,
    ) -> None:
        text = self._filter_outgoing_text(text)
        comment_type, oid, root_id, parent_id = target
        self.logger.info(
            "Send comment reply: type=%s oid=%s root=%s parent=%s",
            comment_type,
            oid,
            root_id,
            parent_id,
        )
        try:
            resp = await self.api.send_comment_reply(
                comment_type=comment_type,
                oid=oid,
                message=text,
                root=root_id,
                parent=parent_id,
            )
            if (resp or {}).get("code") != 0:
                self.logger.warning(f"Comment reply failed: {resp}")
            else:
                self.logger.info("Comment reply ok")
        except Exception as exc:
            self.logger.error(f"Comment reply error: {exc}")

    async def _resolve_user_nickname(self, user_id: str) -> str:
        if not user_id:
            return ""
        cached = self._user_name_cache.get(user_id)
        now = time.time()
        if cached and (now - cached[1]) < self._user_name_cache_seconds:
            return cached[0]
        try:
            mid = int(user_id)
        except ValueError:
            return user_id
        try:
            resp = await self.api.get_user_info(mid)
        except Exception as exc:
            self.logger.warning(f"User info fetch failed: mid={mid} error={exc}")
            return user_id
        name = (resp or {}).get("data", {}).get("name") or user_id
        self._user_name_cache[user_id] = (str(name), now)
        return str(name)

    def _resolve_private_target(self, message: MessageBase) -> Optional[PrivateSessionConfig]:
        group_info = message.message_info.group_info
        if group_info and group_info.group_id:
            parsed = self._parse_private_group_id(group_info.group_id)
            if parsed:
                return parsed
        additional = message.message_info.additional_config or {}
        talker_id = additional.get("talker_id")
        session_type = additional.get("session_type")
        if talker_id not in (None, "", 0):
            try:
                return PrivateSessionConfig(
                    talker_id=int(talker_id),
                    session_type=int(session_type or 1),
                )
            except (TypeError, ValueError):
                pass
        user_info = message.message_info.user_info
        if user_info and user_info.user_id:
            try:
                return PrivateSessionConfig(
                    talker_id=int(user_info.user_id),
                    session_type=int(session_type or 1),
                )
            except (TypeError, ValueError):
                pass
        return self._last_private_session

    @staticmethod
    def _parse_private_group_id(group_id: str) -> Optional[PrivateSessionConfig]:
        if not group_id or not group_id.startswith("dm:"):
            return None
        parts = group_id.split(":")
        if len(parts) != 3:
            return None
        try:
            session_type = int(parts[1])
            talker_id = int(parts[2])
        except ValueError:
            return None
        return PrivateSessionConfig(talker_id=talker_id, session_type=session_type)

    @staticmethod
    def _parse_private_content(msg_type: int, content: Any) -> str:
        if msg_type == 1 and content:
            text = ""
            if isinstance(content, dict):
                text = str(content.get("content") or content.get("text") or content.get("title") or "")
            elif isinstance(content, str):
                try:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        text = str(data.get("content") or data.get("text") or data.get("title") or "")
                    elif isinstance(data, str):
                        text = data
                except Exception:
                    text = content
            text = _normalize_text(text)
            return _strip_emoji(text).strip()
        if msg_type in (2, 6):
            return "[image]"
        return ""

    async def _send_private_message(self, session: PrivateSessionConfig, text: str) -> None:
        self.logger.info(
            "Send private message: talker_id=%s session_type=%s text=%s",
            session.talker_id,
            session.session_type,
            text,
        )
        try:
            safe_text = _normalize_text(text)
            safe_text = self._filter_outgoing_text(safe_text)
            if not safe_text:
                return
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = await self.api.send_private_message(
                        talker_id=session.talker_id,
                        session_type=session.session_type,
                        message=safe_text,
                    )
                except Exception as exc:
                    if attempt < max_attempts:
                        self.logger.warning(
                            "Private message send failed (attempt %s/%s): %s",
                            attempt,
                            max_attempts,
                            exc,
                        )
                        await asyncio.sleep(0.6 * attempt)
                        continue
                    raise
                if (resp or {}).get("code") != 0:
                    self.logger.warning(f"Private message failed: {resp}")
                    return
                self.logger.info("Private message ok")
                return
        except Exception as exc:
            self.logger.error(f"Private message error: {exc}")

    async def _send_private_image(self, session: PrivateSessionConfig, image_base64: str) -> None:
        image_bytes, image_format = _decode_image_base64(image_base64)
        if not image_bytes:
            self.logger.warning("Private image send failed: invalid image data")
            return
        try:
            upload_resp = await self.api.upload_dynamic_image(
                image_bytes=image_bytes,
                image_format=image_format,
                category="daily",
            )
        except Exception as exc:
            self.logger.error("Private image upload error: %s", exc)
            return
        data = (upload_resp or {}).get("data", {})
        image_url = str(data.get("image_url") or "")
        if not image_url:
            self.logger.warning("Private image upload missing url: %s", upload_resp)
            return
        content: Dict[str, Any] = {"url": image_url}
        if data.get("image_height"):
            content["height"] = data.get("image_height")
        if data.get("image_width"):
            content["width"] = data.get("image_width")
        if data.get("img_size"):
            content["size"] = data.get("img_size")
        if image_format:
            content["imageType"] = image_format
        self.logger.info(
            "Send private image: talker_id=%s session_type=%s url=%s",
            session.talker_id,
            session.session_type,
            image_url,
        )
        try:
            resp = await self.api.send_private_image_message(
                talker_id=session.talker_id,
                session_type=session.session_type,
                content=content,
            )
            if isinstance(resp, dict) and resp.get("code") not in (None, 0):
                self.logger.warning(f"Private image failed: {resp}")
            else:
                self.logger.info("Private image ok")
        except Exception as exc:
            self.logger.error(f"Private image error: {exc}")


async def main() -> None:
    config_path = Path(__file__).parent / "config.toml"
    config = load_config(config_path)
    logger = setup_logging(config.log_level)
    logger.info(f"Adapter build tag: {BUILD_TAG}")
    adapter = BilibiliAdapter(config, logger)
    await adapter.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
