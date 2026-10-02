from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomlkit


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    callback_path: str
    log_level: str


@dataclass(frozen=True)
class DouyinConfig:
    app_id: str
    room_id: str
    callback_secret: str
    allow_unsigned_local: bool
    timestamp_tolerance_seconds: int
    dedup_ttl_seconds: int
    message_types: tuple[str, ...]
    access_token: str


@dataclass(frozen=True)
class IMConfig:
    enabled: bool
    callback_path: str
    client_key: str
    webhook_secret: str
    access_token: str
    operator_open_id: str
    send_url: str
    private_platform: str
    reply_prompt: str
    reply_window_hours: int
    max_reply_chars: int
    timeout_seconds: int
    allow_unsigned_local: bool


@dataclass(frozen=True)
class NachoBotConfig:
    host: str
    port: int
    platform: str
    reply_prompt: str
    network_search_enabled: bool
    person_profile_enabled: bool


@dataclass(frozen=True)
class EventConfig:
    comments: bool
    gifts: bool
    likes: bool
    fansclub: bool
    minimum_like_count: int
    queue_size: int


@dataclass(frozen=True)
class OutputConfig:
    subtitle_file: Path
    console: bool


@dataclass(frozen=True)
class TTSConfig:
    enabled: bool
    url: str
    play_local: bool
    timeout_seconds: int


@dataclass(frozen=True)
class Live2DConfig:
    enabled: bool
    url: str
    token: str
    reconnect_seconds: float


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    douyin: DouyinConfig
    im: IMConfig
    nachobot: NachoBotConfig
    events: EventConfig
    output: OutputConfig
    tts: TTSConfig
    live2d: Live2DConfig


def load_config(path: Path) -> AppConfig:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}，请复制 config.example.toml 为 config.toml")
    data = tomlkit.parse(path.read_text(encoding="utf-8"))
    server = data.get("server", {})
    douyin = data.get("douyin", {})
    tasks = douyin.get("tasks", {})
    im = data.get("im", {})
    nachobot = data.get("nachobot", {})
    events = data.get("events", {})
    output = data.get("output", {})
    tts = data.get("tts", {})
    live2d = data.get("live2d", {})

    callback_path = str(server.get("callback_path", "/douyin/live/callback")).strip()
    if not callback_path.startswith("/"):
        callback_path = f"/{callback_path}"
    platform = str(nachobot.get("platform", "douyin.live")).strip() or "douyin.live"
    im_callback_path = str(im.get("callback_path", "/douyin/im/webhook")).strip()
    if not im_callback_path.startswith("/"):
        im_callback_path = f"/{im_callback_path}"

    subtitle_path = Path(str(output.get("subtitle_file", "runtime/douyin_subtitle.txt")))
    if not subtitle_path.is_absolute():
        subtitle_path = path.parent / subtitle_path

    message_types = tuple(str(item) for item in tasks.get("message_types", []))
    supported_types = {"live_comment", "live_gift", "live_like", "live_fansclub"}
    invalid_types = set(message_types) - supported_types
    if invalid_types:
        raise ValueError(f"不支持的抖音事件类型：{', '.join(sorted(invalid_types))}")

    return AppConfig(
        server=ServerConfig(
            host=str(server.get("host", "127.0.0.1")),
            port=int(server.get("port", 8788)),
            callback_path=callback_path,
            log_level=str(server.get("log_level", "INFO")),
        ),
        douyin=DouyinConfig(
            app_id=str(douyin.get("app_id", "")).strip(),
            room_id=str(douyin.get("room_id", "")).strip(),
            callback_secret=str(douyin.get("callback_secret", "")).strip(),
            allow_unsigned_local=bool(douyin.get("allow_unsigned_local", False)),
            timestamp_tolerance_seconds=max(
                30, int(douyin.get("timestamp_tolerance_seconds", 300))
            ),
            dedup_ttl_seconds=max(60, int(douyin.get("dedup_ttl_seconds", 600))),
            message_types=message_types,
            access_token=str(tasks.get("access_token", "")).strip(),
        ),
        im=IMConfig(
            enabled=bool(im.get("enabled", False)),
            callback_path=im_callback_path,
            client_key=str(im.get("client_key", "")).strip(),
            webhook_secret=str(im.get("webhook_secret", "")).strip(),
            access_token=str(im.get("access_token", "")).strip(),
            operator_open_id=str(im.get("operator_open_id", "")).strip(),
            send_url=str(
                im.get("send_url", "https://open.douyin.com/im/send/msg/")
            ).strip(),
            private_platform=str(im.get("private_platform", "douyin.private")).strip()
            or "douyin.private",
            reply_prompt=str(im.get("reply_prompt", "")).strip(),
            reply_window_hours=max(1, int(im.get("reply_window_hours", 24))),
            max_reply_chars=max(1, int(im.get("max_reply_chars", 500))),
            timeout_seconds=max(3, int(im.get("timeout_seconds", 15))),
            allow_unsigned_local=bool(im.get("allow_unsigned_local", False)),
        ),
        nachobot=NachoBotConfig(
            host=str(nachobot.get("host", "127.0.0.1")),
            port=int(nachobot.get("port", 8000)),
            platform=platform,
            reply_prompt=str(nachobot.get("reply_prompt", "")),
            network_search_enabled=bool(nachobot.get("network_search_enabled", False)),
            person_profile_enabled=bool(nachobot.get("person_profile_enabled", False)),
        ),
        events=EventConfig(
            comments=bool(events.get("comments", True)),
            gifts=bool(events.get("gifts", True)),
            likes=bool(events.get("likes", False)),
            fansclub=bool(events.get("fansclub", True)),
            minimum_like_count=max(1, int(events.get("minimum_like_count", 20))),
            queue_size=max(10, int(events.get("queue_size", 1000))),
        ),
        output=OutputConfig(
            subtitle_file=subtitle_path.resolve(),
            console=bool(output.get("console", True)),
        ),
        tts=TTSConfig(
            enabled=bool(tts.get("enabled", True)),
            url=str(tts.get("url", "http://127.0.0.1:8070/api/tts")),
            play_local=bool(tts.get("play_local", True)),
            timeout_seconds=max(5, int(tts.get("timeout_seconds", 180))),
        ),
        live2d=Live2DConfig(
            enabled=bool(live2d.get("enabled", False)),
            url=str(live2d.get("url", "ws://127.0.0.1:8766")),
            token=str(live2d.get("token", "")),
            reconnect_seconds=max(1.0, float(live2d.get("reconnect_seconds", 3.0))),
        ),
    )
