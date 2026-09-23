import base64
import binascii
import logging
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional

from static_ffmpeg import run

from ncnk_message import MessageBase, Seg
from config import AdapterConfig
from utils import allow_reply


_BASE_ACCEPT_FORMAT = [
    "text",
    "image",
    "emoji",
    "reply",
    "voice",
    "voice_stream",
    "voiceurl",
    "voicefile",
    "music",
    "videourl",
    "video",
    "videofile",
    "file",
    "imageurl",
]


def get_accept_format(use_tts: bool) -> List[str]:
    """Return the formats this adapter can actually turn into OneBot data.

    ``tts_text`` is a Core-owned action capability rather than an outbound
    OneBot segment.  Advertise it only when this adapter is configured to
    request TTS, while keeping already-materialized ``voice`` media usable in
    either mode.
    """

    formats = list(_BASE_ACCEPT_FORMAT)
    if use_tts:
        formats.insert(formats.index("voiceurl"), "tts_text")
    return formats


# Keep the historical module-level export for callers that only inspect the
# default capability set.  Message construction below uses the live config.
ACCEPT_FORMAT = get_accept_format(True)

_SILK_HEADER = b"\x02#!SILK_V3"


def _mapping_value(value: Any, *keys: str) -> Any:
    if not isinstance(value, Mapping):
        return value
    for key in keys:
        candidate = value.get(key)
        if candidate not in (None, ""):
            return candidate
    return None


def _data_text(value: Any, *keys: str) -> str:
    candidate = _mapping_value(value, *keys)
    if candidate is None:
        return ""
    return str(candidate).strip()


def _media_ref(value: Any, *, default_scheme: str) -> str:
    """Normalize Core media data without changing existing OneBot URLs."""

    text = _data_text(value, "file", "path", "url", "data", "audio_base64", "audio")
    if not text:
        return ""
    if text.startswith(("base64://", "file://", "http://", "https://")):
        return text
    return f"{default_scheme}://{text}"


def _music_data(value: Any) -> Dict[str, Any]:
    """Build a OneBot music payload from an id or an already-shaped mapping."""

    if isinstance(value, Mapping):
        data = dict(value)
        if data.get("id") not in (None, ""):
            data["id"] = str(data["id"])
            data.setdefault("type", "163")
            return data
        url = _data_text(data, "url", "music_url", "audio")
        if url:
            return {
                "type": "custom",
                "url": url,
                **({"audio": _data_text(data, "audio")} if _data_text(data, "audio") else {}),
                **({"title": _data_text(data, "title")} if _data_text(data, "title") else {}),
            }
        return {}

    song_id = str(value or "").strip()
    return {"type": "163", "id": song_id} if song_id else {}


def _forward_nodes(
    value: Any, config: AdapterConfig, logger: logging.Logger
) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []

    nodes: List[Dict[str, Any]] = []
    for raw_item in value:
        try:
            item = raw_item if isinstance(raw_item, MessageBase) else MessageBase.from_dict(raw_item)
            segment = item.message_segment
            if segment.type == "id":
                if segment.data not in (None, ""):
                    nodes.append({"type": "node", "data": {"id": segment.data}})
                continue

            user_info = item.message_info.user_info
            if user_info is None:
                continue
            content = seg_to_onebot(segment, config, logger)
            if not content:
                continue
            nodes.append(
                {
                    "type": "node",
                    "data": {
                        "name": user_info.user_nickname or "QQ用户",
                        "uin": user_info.user_id,
                        "content": content,
                    },
                }
            )
        except Exception as exc:
            logger.warning("Skipping invalid forward node: %s", type(exc).__name__)
    return nodes


def seg_to_onebot(
    seg_data: Seg, config: AdapterConfig, logger: logging.Logger
) -> List[Dict[str, Any]]:
    payload: List[Dict[str, Any]] = []
    if isinstance(seg_data, dict):
        try:
            seg_data = Seg.from_dict(seg_data)
        except Exception:
            return payload

    if not isinstance(seg_data, Seg):
        return payload

    if seg_data.type == "seglist" and isinstance(seg_data.data, list):
        for seg in seg_data.data:
            payload.extend(seg_to_onebot(seg, config, logger))
        return payload

    if seg_data.type == "text":
        text = str(seg_data.data or "")
        if text:
            payload.append({"type": "text", "data": {"text": text}})
    elif seg_data.type == "reply":
        target_id = seg_data.data
        if target_id and allow_reply(config):
            payload.append({"type": "reply", "data": {"id": target_id}})
    elif seg_data.type == "image":
        file_value = _media_ref(seg_data.data, default_scheme="base64")
        if file_value:
            payload.append(
                {
                    "type": "image",
                    "data": {"file": file_value, "subtype": 0},
                }
            )
    elif seg_data.type == "emoji":
        file_value = _media_ref(seg_data.data, default_scheme="base64")
        if file_value:
            payload.append(
                {
                    "type": "image",
                    "data": {"file": file_value, "subtype": 1},
                }
            )
    elif seg_data.type in ("voice", "voice_stream"):
        if seg_data.data:
            audio_data = _data_text(seg_data.data, "audio_base64", "audio", "data")
            file_value = voice_to_record_file(
                audio_data,
                config,
                logger,
                stream=(seg_data.type == "voice_stream"),
            )
            if file_value:
                payload.append(
                    {"type": "record", "data": build_record_data(file_value, config)}
                )
    elif seg_data.type == "voicefile":
        file_value = _media_ref(seg_data.data, default_scheme="file")
        if file_value:
            payload.append({"type": "record", "data": build_record_data(file_value, config)})
    elif seg_data.type == "imageurl":
        file_value = _media_ref(seg_data.data, default_scheme="file")
        if file_value:
            payload.append({"type": "image", "data": {"file": file_value}})
    elif seg_data.type == "voiceurl":
        file_value = _media_ref(seg_data.data, default_scheme="file")
        if file_value:
            payload.append({"type": "record", "data": {"file": file_value}})
    elif seg_data.type == "music":
        music_data = _music_data(seg_data.data)
        if music_data:
            payload.append({"type": "music", "data": music_data})
    elif seg_data.type in ("video", "videofile", "videourl"):
        default_scheme = "base64" if seg_data.type == "video" else "file"
        file_value = _media_ref(seg_data.data, default_scheme=default_scheme)
        if file_value:
            payload.append({"type": "video", "data": {"file": file_value}})
    elif seg_data.type == "file":
        file_value = _media_ref(seg_data.data, default_scheme="file")
        if file_value:
            file_name = _data_text(seg_data.data, "name") or os.path.basename(file_value)
            payload.append(
                {"type": "file", "data": {"file": file_value, "name": file_name}}
            )
    elif seg_data.type == "forward":
        payload.extend(_forward_nodes(seg_data.data, config, logger))

    return payload


def contains_reply_segment(seg_data: Seg) -> bool:
    if seg_data.type == "reply":
        return True
    if seg_data.type == "seglist" and isinstance(seg_data.data, list):
        return any(contains_reply_segment(seg) for seg in seg_data.data)
    return False


def resolve_ffmpeg_exe(config: AdapterConfig, logger: logging.Logger) -> Optional[str]:
    """解析 FFmpeg 路径，显式覆盖优先，随后使用 static-ffmpeg。"""
    candidates = []
    if config.ffmpeg_path:
        candidates.append(config.ffmpeg_path)
    env_path = os.environ.get("FFMPEG_PATH")
    if env_path:
        candidates.append(env_path)

    for candidate in candidates:
        candidate_path = Path(candidate)
        if candidate_path.exists():
            if candidate_path.is_dir():
                bin_dir = candidate_path / "bin"
                if bin_dir.exists():
                    for name in ("ffmpeg.exe", "ffmpeg"):
                        exe_path = bin_dir / name
                        if exe_path.exists():
                            return str(exe_path)
                for name in ("ffmpeg.exe", "ffmpeg"):
                    exe_path = candidate_path / name
                    if exe_path.exists():
                        return str(exe_path)
            else:
                return str(candidate_path)

    if config.ffmpeg_path:
        logger.warning(f"ffmpeg path not found: {config.ffmpeg_path}")

    try:
        configured_dir = os.environ.get("NACHOBOT_FFMPEG_DIR", "").strip()
        shared_root = (
            Path(configured_dir).expanduser()
            if configured_dir
            else Path(__file__).resolve().parent.parent / ".runtime" / "ffmpeg"
        )
        platform_dir = shared_root.resolve() / run.get_platform_key()
        platform_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg_path, _ = run.get_or_fetch_platform_executables_else_raise(
            download_dir=str(platform_dir)
        )
        return ffmpeg_path
    except Exception as exc:
        logger.warning(f"static-ffmpeg 获取 ffmpeg 失败，尝试系统 PATH: {exc}")

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    logger.warning("ffmpeg not found")
    return None


def voice_to_record_file(
    audio_b64: str, config: AdapterConfig, logger: logging.Logger, stream: bool = False
) -> str:
    if not audio_b64:
        return ""
    if str(config.platform).lower() != "discord":
        return f"base64://{audio_b64}"
    try:
        is_silk = base64.b64decode(audio_b64, validate=True).startswith(_SILK_HEADER)
    except (binascii.Error, TypeError, ValueError):
        is_silk = False
    if stream:
        if is_silk:
            logger.warning("SILK voice_stream cannot be labeled as Discord Ogg")
            return ""
        logger.warning(
            "Discord voice bubble does not support voice_stream, send as raw record"
        )
        return f"base64://{audio_b64}"
    ogg_data_url = convert_to_opus_data_url(audio_b64, config, logger)
    if ogg_data_url:
        return ogg_data_url
    # A Discord record segment is explicitly named as Ogg/Opus below.  Do not
    # fall back to the original Tencent SILK bytes after conversion failed:
    # Discord would receive a mislabeled/corrupt attachment.
    if is_silk:
        logger.warning("SILK conversion failed; dropping Discord voice segment")
        return ""
    return f"base64://{audio_b64}"


def build_record_data(file_value: str, config: AdapterConfig) -> Dict[str, Any]:
    data = {"file": file_value}
    if str(config.platform).lower() == "discord":
        data["file_name"] = "voice-message.ogg"
    return data


def convert_to_opus_data_url(
    audio_b64: str, config: AdapterConfig, logger: logging.Logger
) -> Optional[str]:
    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception as exc:
        logger.warning(f"Decode audio base64 failed: {exc}")
        return None

    # Check for SILK header
    is_silk = audio_bytes.startswith(_SILK_HEADER)

    if is_silk:
        try:
            import rsilk

            # Decode to 24000Hz, mono, 16-bit PCM
            audio_bytes = rsilk.decode(audio_bytes, tencent=True)
            logger.info("SILK format detected, successfully decoded to PCM using rsilk")
        except ImportError:
            logger.warning("SILK format detected but rsilk is not installed")
            return None
        except Exception as exc:
            logger.warning(f"Failed to decode SILK using rsilk: {exc}")
            return None
        if not isinstance(audio_bytes, (bytes, bytearray)) or not audio_bytes:
            logger.warning("SILK decoder returned no PCM data")
            return None

    ffmpeg_exe = resolve_ffmpeg_exe(config, logger)
    if not ffmpeg_exe:
        logger.warning("ffmpeg not found, cannot convert to opus/ogg")
        return None

    cmd = [
        ffmpeg_exe,
        "-hide_banner",
        "-loglevel",
        "error",
    ]

    if is_silk:
        # We now have raw PCM from rsilk: 24000Hz, 1 channel, 16-bit little-endian
        cmd.extend(["-f", "s16le", "-ar", "24000", "-ac", "1"])

    cmd.extend(
        [
            "-i",
            "pipe:0",
            "-c:a",
            "libopus",
            "-b:a",
            "64k",
            "-vbr",
            "on",
            "-f",
            "ogg",
            "pipe:1",
        ]
    )

    try:
        proc = subprocess.run(
            cmd,
            input=audio_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("ffmpeg not found, cannot convert to opus/ogg")
        return None
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", errors="ignore")
        logger.warning(f"ffmpeg convert failed: {err}")
        return None
    ogg_b64 = base64.b64encode(proc.stdout).decode("ascii")
    logger.info("ffmpeg convert ok, send ogg/opus voice bubble")
    return f"base64://{ogg_b64}"
