import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

try:
    import tomllib as toml
except ImportError:  # pragma: no cover
    import toml  # type: ignore


@dataclass
class AdapterConfig:
    onebot_ws_url: str
    onebot_token: str
    onebot_reconnect_seconds: int
    nachobot_host: str
    nachobot_port: int
    platform: str
    group_list_type: str
    group_list: List[str]
    private_list_type: str
    private_list: List[str]
    ban_user_id: List[str]
    use_tts: bool
    log_level: str
    ffmpeg_path: str
    network_proxy: str


def _load_toml(path: Path) -> Dict[str, Any]:
    raw = path.read_bytes()
    if hasattr(toml, "loads"):
        return toml.loads(raw.decode("utf-8"))
    return toml.load(path)  # type: ignore[attr-defined]


def load_config(path: Path) -> AdapterConfig:
    data = _load_toml(path)
    onebot = data.get("onebot_server", {})
    nachobot = data.get("nachobot_server", {})
    chat = data.get("chat", {})
    voice = data.get("voice", {})
    debug = data.get("debug", {})
    ffmpeg = data.get("ffmpeg", {})
    network = data.get("network", {})

    ws_url = onebot.get("ws_url", "")
    if not ws_url:
        host = onebot.get("host", "127.0.0.1")
        port = int(onebot.get("port", 5140))
        path_part = onebot.get("path", "/onebot/v11/ws")
        ws_url = f"ws://{host}:{port}{path_part}"

    return AdapterConfig(
        onebot_ws_url=ws_url,
        onebot_token=str(onebot.get("token", "") or ""),
        onebot_reconnect_seconds=int(onebot.get("reconnect_seconds", 5)),
        nachobot_host=str(nachobot.get("host", "127.0.0.1")),
        nachobot_port=int(nachobot.get("port", 8070)),
        platform=str(nachobot.get("platform", "discord")),
        group_list_type=str(chat.get("group_list_type", "whitelist")),
        group_list=[str(x) for x in chat.get("group_list", [])],
        private_list_type=str(chat.get("private_list_type", "blacklist")),
        private_list=[str(x) for x in chat.get("private_list", [])],
        ban_user_id=[str(x) for x in chat.get("ban_user_id", [])],
        use_tts=bool(voice.get("use_tts", True)),
        log_level=str(debug.get("level", "INFO")),
        ffmpeg_path=str(ffmpeg.get("path", "") or ""),
        network_proxy=str(network.get("proxy", "") or ""),
    )


def setup_logging(level: str) -> logging.Logger:
    logger = logging.getLogger("koishi-onebot-adapter")
    if logger.handlers:
        return logger
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    return logger
