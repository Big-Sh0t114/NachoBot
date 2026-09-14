from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomlkit


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    log_level: str


@dataclass(frozen=True)
class NachoBotConfig:
    host: str
    port: int
    platform: str
    studio_id: str
    host_user_id: str
    host_nickname: str
    reply_prompt: str
    network_search_enabled: bool
    person_profile_enabled: bool


@dataclass(frozen=True)
class OutputConfig:
    subtitle_file: Path
    speech_file: Path
    console: bool


@dataclass(frozen=True)
class TTSConfig:
    enabled: bool
    url: str
    play_local: bool
    timeout_seconds: int
    segmented_playback: bool = True
    segment_wait_seconds: float = 0.6
    segment_pause_step_seconds: float = 0.2
    segment_filler_enabled: bool = True
    segment_filler_text: str = "嗯。"
    segment_min_chars: int = 4
    segment_target_chars: int = 16


@dataclass(frozen=True)
class NeuralTTSConfig:
    enabled: bool
    voice: str
    rate: str
    pitch: str
    volume: str


@dataclass(frozen=True)
class BrowserTTSConfig:
    """Speech synthesis rendered by the captured browser window itself."""

    enabled: bool
    language: str
    preferred_voice: str
    rate: float
    pitch: float
    volume: float


@dataclass(frozen=True)
class AutoAnnouncementConfig:
    enabled: bool
    first_delay_seconds: int
    interval_seconds: int
    messages: tuple[str, ...]


@dataclass(frozen=True)
class Live2DConfig:
    enabled: bool
    url: str
    token: str


@dataclass(frozen=True)
class VRMConfig:
    """Settings for the optional full-body 3D avatar stage."""

    enabled: bool
    model_file: Path


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    nachobot: NachoBotConfig
    output: OutputConfig
    tts: TTSConfig
    neural_tts: NeuralTTSConfig
    browser_tts: BrowserTTSConfig
    auto_announcements: AutoAnnouncementConfig
    live2d: Live2DConfig
    vrm: VRMConfig


def load_config(path: Path) -> AppConfig:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}；请先复制 config.example.toml 为 config.toml")
    data = tomlkit.parse(path.read_text(encoding="utf-8"))
    server = data.get("server", {})
    nachobot = data.get("nachobot", {})
    output = data.get("output", {})
    tts = data.get("tts", {})
    neural_tts = data.get("neural_tts", {})
    browser_tts = data.get("browser_tts", {})
    auto_announcements = data.get("auto_announcements", {})
    live2d = data.get("live2d", {})
    vrm = data.get("vrm", {})

    subtitle_file = Path(str(output.get("subtitle_file", "runtime/current_subtitle.txt")))
    if not subtitle_file.is_absolute():
        subtitle_file = path.parent / subtitle_file
    speech_file = Path(str(output.get("speech_file", "runtime/latest_speech.mp3")))
    if not speech_file.is_absolute():
        speech_file = path.parent / speech_file
    vrm_model_file = Path(str(vrm.get("model_file", "models/xingyu-host-v1.vrm")))
    if not vrm_model_file.is_absolute():
        vrm_model_file = path.parent / vrm_model_file

    return AppConfig(
        server=ServerConfig(
            host=str(server.get("host", "127.0.0.1")),
            port=int(server.get("port", 8789)),
            log_level=str(server.get("log_level", "INFO")),
        ),
        nachobot=NachoBotConfig(
            host=str(nachobot.get("host", "127.0.0.1")),
            port=int(nachobot.get("port", 8000)),
            platform=str(nachobot.get("platform", "local.host")).strip() or "local.host",
            studio_id=str(nachobot.get("studio_id", "local-studio")).strip() or "local-studio",
            host_user_id=str(nachobot.get("host_user_id", "host")).strip() or "host",
            host_nickname=str(nachobot.get("host_nickname", "主播")).strip() or "主播",
            reply_prompt=str(nachobot.get("reply_prompt", "")).strip(),
            network_search_enabled=bool(nachobot.get("network_search_enabled", False)),
            person_profile_enabled=bool(nachobot.get("person_profile_enabled", False)),
        ),
        output=OutputConfig(
            subtitle_file=subtitle_file.resolve(),
            speech_file=speech_file.resolve(),
            console=bool(output.get("console", True)),
        ),
        tts=TTSConfig(
            enabled=bool(tts.get("enabled", True)),
            url=str(tts.get("url", "http://127.0.0.1:8070/api/tts")).strip(),
            play_local=bool(tts.get("play_local", True)),
            timeout_seconds=max(5, int(tts.get("timeout_seconds", 180))),
            segmented_playback=bool(tts.get("segmented_playback", True)),
            segment_wait_seconds=min(
                2.0,
                max(0.2, float(tts.get("segment_wait_seconds", 0.6))),
            ),
            segment_pause_step_seconds=min(
                0.5,
                max(0.1, float(tts.get("segment_pause_step_seconds", 0.2))),
            ),
            segment_filler_enabled=bool(tts.get("segment_filler_enabled", True)),
            segment_filler_text=(
                str(tts.get("segment_filler_text", "嗯。")).strip()
                or "嗯。"
            ),
            segment_min_chars=min(
                12,
                max(2, int(tts.get("segment_min_chars", 4))),
            ),
            segment_target_chars=min(
                40,
                max(8, int(tts.get("segment_target_chars", 16))),
            ),
        ),
        neural_tts=NeuralTTSConfig(
            enabled=bool(neural_tts.get("enabled", True)),
            voice=str(neural_tts.get("voice", "zh-CN-XiaoxiaoNeural")).strip()
            or "zh-CN-XiaoxiaoNeural",
            rate=str(neural_tts.get("rate", "+0%")).strip() or "+0%",
            pitch=str(neural_tts.get("pitch", "+2Hz")).strip() or "+2Hz",
            volume=str(neural_tts.get("volume", "+0%")).strip() or "+0%",
        ),
        browser_tts=BrowserTTSConfig(
            enabled=bool(browser_tts.get("enabled", False)),
            language=str(browser_tts.get("language", "zh-CN")).strip() or "zh-CN",
            preferred_voice=str(browser_tts.get("preferred_voice", "")).strip(),
            rate=min(2.0, max(0.5, float(browser_tts.get("rate", 1.0)))),
            pitch=min(2.0, max(0.0, float(browser_tts.get("pitch", 1.0)))),
            volume=min(1.0, max(0.0, float(browser_tts.get("volume", 1.0)))),
        ),
        auto_announcements=AutoAnnouncementConfig(
            enabled=bool(auto_announcements.get("enabled", False)),
            first_delay_seconds=max(0, int(auto_announcements.get("first_delay_seconds", 120))),
            interval_seconds=max(30, int(auto_announcements.get("interval_seconds", 600))),
            messages=tuple(
                str(message).strip()
                for message in auto_announcements.get("messages", [])
                if str(message).strip()
            ),
        ),
        live2d=Live2DConfig(
            enabled=bool(live2d.get("enabled", False)),
            url=str(live2d.get("url", "ws://127.0.0.1:8766")).strip(),
            token=str(live2d.get("token", "")).strip(),
        ),
        vrm=VRMConfig(
            enabled=bool(vrm.get("enabled", False)),
            model_file=vrm_model_file.resolve(),
        ),
    )
