from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode
import shutil
import tomllib

from .log_safety import safe_endpoint

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.toml"
TEMPLATE_PATH = ROOT / "template_config.toml"
CORE_PORT = 8000


def _load_raw() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        shutil.copy2(TEMPLATE_PATH, CONFIG_PATH)
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def _list_int(values: Any) -> list[int]:
    if not isinstance(values, list):
        return []
    out: list[int] = []
    for item in values:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


@dataclass(slots=True)
class SnowLumaConfig:
    scheme: Literal["ws", "wss"] = "ws"
    host: str = "127.0.0.1"
    port: int = 3001
    path: str = ""
    token: str = ""
    reconnect_delay_sec: float = 5.0
    action_timeout_sec: float = 10.0
    heartbeat_sec: float = 30.0

    def ws_url(self) -> str:
        path = self.path.strip()
        if path and not path.startswith("/"):
            path = "/" + path
        base = f"{self.scheme}://{self.host}:{self.port}{path}"
        if not self.token:
            return base
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}{urlencode({'access_token': self.token})}"

    def safe_ws_url(self) -> str:
        """Return the configured endpoint without exposing the access token."""

        return safe_endpoint(self.ws_url())


@dataclass(slots=True)
class NachoBotConfig:
    host: str = "127.0.0.1"
    port: int = CORE_PORT
    platform_name: str = "qq"


@dataclass(slots=True)
class ChatConfig:
    enable_chat_list_filter: bool = True
    group_list_type: Literal["whitelist", "blacklist"] = "whitelist"
    group_list: list[int] = field(default_factory=list)
    private_list_type: Literal["whitelist", "blacklist"] = "blacklist"
    private_list: list[int] = field(default_factory=list)
    ban_user_id: list[int] = field(default_factory=list)
    ban_qq_bot: bool = False
    enable_poke: bool = True


@dataclass(slots=True)
class VoiceConfig:
    use_tts: bool = False


@dataclass(slots=True)
class DebugConfig:
    level: str = "INFO"
    raw_payload: bool = False
    raw_outbound: bool = False


@dataclass(slots=True)
class SendConfig:
    min_interval_sec: float = 0.5


@dataclass(slots=True)
class VisualTaskConfig:
    temperature: float
    max_tokens: int
    extra_params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "extra_params": dict(self.extra_params),
        }


@dataclass(slots=True)
class VisualConfig:
    image: VisualTaskConfig = field(default_factory=lambda: VisualTaskConfig(0.1, 220, {"enable_thinking": False}))
    emoji: VisualTaskConfig = field(default_factory=lambda: VisualTaskConfig(0.2, 180, {"enable_thinking": False}))
    video: VisualTaskConfig = field(default_factory=lambda: VisualTaskConfig(0.1, 280, {"enable_thinking": False}))

    def to_message_policy(self) -> dict[str, Any]:
        return {
            "version": 1,
            "profile": "qq-core-v1",
            "image": self.image.to_dict(),
            "emoji": self.emoji.to_dict(),
            "video": self.video.to_dict(),
        }


@dataclass(slots=True)
class Config:
    snowluma: SnowLumaConfig
    nachobot: NachoBotConfig
    chat: ChatConfig
    voice: VoiceConfig
    debug: DebugConfig
    send: SendConfig
    visual: VisualConfig


def _visual_task(raw: dict[str, Any], default: VisualTaskConfig) -> VisualTaskConfig:
    return VisualTaskConfig(
        temperature=float(raw.get("temperature", default.temperature)),
        max_tokens=int(raw.get("max_tokens", default.max_tokens)),
        extra_params=dict(raw.get("extra_params", default.extra_params) or {}),
    )


def load_config() -> Config:
    raw = _load_raw()
    s = raw.get("snowluma", {})
    # Prefer the dev NapCat-compatible section name; keep the v0.1.x name as a fallback.
    n = raw.get("nachobot_server", raw.get("nachobot", {}))
    c = raw.get("chat", {})
    v = raw.get("voice", {})
    d = raw.get("debug", {})
    se = raw.get("send", {})
    vi = raw.get("visual", {})
    defaults = VisualConfig()
    return Config(
        snowluma=SnowLumaConfig(
            scheme=str(s.get("scheme", "ws")),
            host=str(s.get("host", "127.0.0.1")),
            port=int(s.get("port", 3001)),
            path=str(s.get("path", "")),
            token=str(s.get("token", "")),
            reconnect_delay_sec=float(s.get("reconnect_delay_sec", 5.0)),
            action_timeout_sec=float(s.get("action_timeout_sec", 10.0)),
            heartbeat_sec=float(s.get("heartbeat_sec", 30.0)),
        ),
        nachobot=NachoBotConfig(
            host=str(n.get("host", "127.0.0.1")),
            port=int(n.get("port", CORE_PORT)),
            platform_name=str(n.get("platform_name", "qq")),
        ),
        chat=ChatConfig(
            enable_chat_list_filter=bool(c.get("enable_chat_list_filter", True)),
            group_list_type=str(c.get("group_list_type", "whitelist")),
            group_list=_list_int(c.get("group_list", [])),
            private_list_type=str(c.get("private_list_type", "blacklist")),
            private_list=_list_int(c.get("private_list", [])),
            ban_user_id=_list_int(c.get("ban_user_id", [])),
            ban_qq_bot=bool(c.get("ban_qq_bot", False)),
            enable_poke=bool(c.get("enable_poke", True)),
        ),
        voice=VoiceConfig(use_tts=bool(v.get("use_tts", False))),
        debug=DebugConfig(
            level=str(d.get("level", "INFO")).upper(),
            raw_payload=bool(d.get("raw_payload", False)),
            raw_outbound=bool(d.get("raw_outbound", False)),
        ),
        send=SendConfig(min_interval_sec=max(0.0, float(se.get("min_interval_sec", 0.5)))),
        visual=VisualConfig(
            image=_visual_task(dict(vi.get("image", {}) or {}), defaults.image),
            emoji=_visual_task(dict(vi.get("emoji", {}) or {}), defaults.emoji),
            video=_visual_task(dict(vi.get("video", {}) or {}), defaults.video),
        ),
    )


global_config = load_config()
