import base64
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ncnk_message import Seg
from config import AdapterConfig
from utils import allow_reply


def seg_to_onebot(
    seg_data: Seg, config: AdapterConfig, logger: logging.Logger
) -> List[Dict[str, Any]]:
    payload: List[Dict[str, Any]] = []
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
        if seg_data.data:
            payload.append(
                {
                    "type": "image",
                    "data": {"file": f"base64://{seg_data.data}", "subtype": 0},
                }
            )
    elif seg_data.type == "emoji":
        if seg_data.data:
            payload.append(
                {
                    "type": "image",
                    "data": {"file": f"base64://{seg_data.data}", "subtype": 1},
                }
            )
    elif seg_data.type in ("voice", "voice_stream"):
        if config.use_tts and seg_data.data:
            file_value = voice_to_record_file(
                str(seg_data.data),
                config,
                logger,
                stream=(seg_data.type == "voice_stream"),
            )
            if file_value:
                payload.append(
                    {"type": "record", "data": build_record_data(file_value, config)}
                )
    elif seg_data.type == "imageurl":
        if seg_data.data:
            payload.append({"type": "image", "data": {"file": str(seg_data.data)}})
    elif seg_data.type == "voiceurl":
        if seg_data.data:
            payload.append({"type": "record", "data": {"file": str(seg_data.data)}})
    elif seg_data.type == "file":
        if seg_data.data:
            # Core sends the absolute file path directly for sandbox files via custom_to_stream
            if isinstance(seg_data.data, str):
                file_path = seg_data.data
                file_name = os.path.basename(file_path)
                payload.append(
                    {
                        "type": "video",
                        "data": {"file": f"file://{file_path}", "name": file_name},
                    }
                )
            elif isinstance(seg_data.data, dict):
                file_path = seg_data.data.get("file") or seg_data.data.get("path") or ""
                file_name = seg_data.data.get("name", "file")
                if file_path:
                    payload.append(
                        {
                            "type": "video",
                            "data": {
                                "file": f"file://{file_path}",
                                "name": file_name,
                            },
                        }
                    )

    return payload


def contains_reply_segment(seg_data: Seg) -> bool:
    if seg_data.type == "reply":
        return True
    if seg_data.type == "seglist" and isinstance(seg_data.data, list):
        return any(contains_reply_segment(seg) for seg in seg_data.data)
    return False


def resolve_ffmpeg_exe(config: AdapterConfig, logger: logging.Logger) -> Optional[str]:
    candidates = []
    if config.ffmpeg_path:
        candidates.append(config.ffmpeg_path)
    env_path = os.environ.get("FFMPEG_PATH")
    if env_path:
        candidates.append(env_path)
    candidates.append("ffmpeg")

    for candidate in candidates:
        if candidate == "ffmpeg":
            return candidate
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
    return "ffmpeg"


def voice_to_record_file(
    audio_b64: str, config: AdapterConfig, logger: logging.Logger, stream: bool = False
) -> str:
    if not audio_b64:
        return ""
    if str(config.platform).lower() != "discord":
        return f"base64://{audio_b64}"
    if stream:
        logger.warning(
            "Discord voice bubble does not support voice_stream, send as raw record"
        )
        return f"base64://{audio_b64}"
    ogg_data_url = convert_to_opus_data_url(audio_b64, config, logger)
    if ogg_data_url:
        return ogg_data_url
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
    is_silk = audio_bytes.startswith(b"\x02#!SILK_V3")

    if is_silk:
        try:
            import rsilk

            # Decode to 24000Hz, mono, 16-bit PCM
            audio_bytes = rsilk.decode(audio_bytes, tencent=True)
            logger.info("SILK format detected, successfully decoded to PCM using rsilk")
        except ImportError:
            logger.warning(
                "SILK format detected but rsilk is not installed. ffmpeg conversion will likely fail."
            )
        except Exception as exc:
            logger.warning(f"Failed to decode SILK using rsilk: {exc}")

    ffmpeg_exe = resolve_ffmpeg_exe(config, logger)
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
