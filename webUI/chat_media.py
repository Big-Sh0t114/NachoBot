from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import re
import shutil
import uuid
from pathlib import Path
from typing import Any


MEDIA_TYPES = frozenset({"image", "emoji", "video", "file", "voice"})
# Leave headroom for base64 and NCNK JSON under Core's 16 MiB WS ceiling.
VOICE_MAX_BYTES = 10 * 1024 * 1024
# Core-produced audio travels outside the WebSocket frame and can reach the worker's file limit.
OUTGOING_VOICE_MAX_BYTES = 512 * 1024 * 1024
MEDIA_LIMITS = {
    "image": 16 * 1024 * 1024,
    "emoji": 16 * 1024 * 1024,
    "video": 64 * 1024 * 1024,
    # Core 的 file sandbox 当前硬限制为 1 MiB。
    "file": 1 * 1024 * 1024,
    "voice": VOICE_MAX_BYTES,
}


class ChatMediaError(ValueError):
    pass


class ChatMediaStore:
    """Persistent media store shared by WebUI Chat ingress and egress.

    Browser uploads are stored as files first. Core receives image/emoji/voice
    as base64 (the existing NCNK convention), while video/file receive a local
    path object.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or (Path(__file__).parent / "data" / "chat_media")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def normalize_type(media_type: str) -> str:
        kind = str(media_type or "").strip().lower()
        if kind not in MEDIA_TYPES:
            raise ChatMediaError("仅支持图片、表情包、视频、文件和语音附件")
        return kind

    @staticmethod
    def limit_for_type(media_type: str) -> int:
        return MEDIA_LIMITS[ChatMediaStore.normalize_type(media_type)]

    @staticmethod
    def _safe_suffix(name: str, mime_type: str = "") -> str:
        suffix = Path(str(name or "")).suffix.lower()
        # The 12-character allowance includes the leading dot so the stored
        # extension stays within the media-ID parser's accepted shape.
        if suffix and len(suffix) <= 12 and suffix[1:].isalnum():
            return suffix
        guessed = mimetypes.guess_extension(str(mime_type or "").split(";", 1)[0].strip())
        if guessed and len(guessed) <= 12 and guessed[1:].isalnum():
            return guessed.lower()
        return ""

    @staticmethod
    def _audio_format(data: bytes) -> tuple[str, str] | None:
        """Return the canonical extension and MIME for a passive audio header."""
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            return ".wav", "audio/wav"
        if data.startswith(b"OggS"):
            return ".ogg", "audio/ogg"
        if data.startswith(b"fLaC"):
            return ".flac", "audio/flac"
        if data.startswith(b"\x1a\x45\xdf\xa3"):
            # .weba keeps the response MIME unambiguously audio/webm.
            return ".weba", "audio/webm"
        if len(data) >= 12 and data[4:8] == b"ftyp":
            brand = data[8:12]
            if (
                brand in {b"M4A ", b"M4B ", b"M4P ", b"mp41", b"mp42"}
                or brand.startswith(b"iso")
            ):
                return ".m4a", "audio/mp4"
        if data.startswith(b"ID3") or (
            len(data) >= 2
            and data[0] == 0xFF
            and data[1] & 0xE0 == 0xE0
            and data[1] & 0x06 != 0
        ):
            return ".mp3", "audio/mpeg"
        if len(data) >= 7 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0:
            return ".aac", "audio/aac"
        return None

    @staticmethod
    def _safe_display_name(name: str, fallback: str) -> str:
        value = Path(str(name or "")).name.strip()
        value = "".join(ch for ch in value if ch >= " " and ch not in "\x7f/\\")
        return value[:180] or fallback

    def save_bytes(
        self,
        media_type: str,
        data: bytes,
        *,
        name: str = "",
        mime_type: str = "",
    ) -> dict[str, Any]:
        kind = self.normalize_type(media_type)
        limit = MEDIA_LIMITS[kind]
        size = len(data)
        if size <= 0:
            raise ChatMediaError("附件内容为空")
        if size > limit:
            raise ChatMediaError(f"{kind} 附件超过大小限制（最大 {limit // (1024 * 1024)} MiB）")

        canonical_audio = self._audio_format(data[:64]) if kind == "voice" else None
        if kind == "voice" and canonical_audio is None:
            raise ChatMediaError("不支持的语音格式")
        if canonical_audio:
            suffix, stored_mime_type = canonical_audio
        else:
            suffix = self._safe_suffix(name, mime_type)
            stored_mime_type = str(mime_type or "application/octet-stream")[:120]
        media_id = f"{uuid.uuid4().hex}{suffix}"
        path = self.root / media_id
        path.write_bytes(data)
        display_name = self._safe_display_name(name, media_id)
        return {
            "media_id": media_id,
            "type": kind,
            "name": display_name,
            "mime_type": stored_mime_type,
            "size": size,
            "url": f"/api/chat/media/{media_id}",
        }

    def resolve(self, media_id: str, media_type: str | None = None) -> Path:
        value = str(media_id or "").strip()
        if not self._is_generated_media_id(value):
            raise ChatMediaError("无效的附件标识")
        path = (self.root / value).resolve()
        if path.parent != self.root or not path.is_file():
            raise ChatMediaError("附件不存在或已失效")

        kind = self.normalize_type(media_type) if media_type else None
        if kind == "voice":
            if path.stat().st_size > VOICE_MAX_BYTES:
                limit_mib = VOICE_MAX_BYTES // (1024 * 1024)
                raise ChatMediaError(
                    f"voice 附件超过大小限制（最大 {limit_mib} MiB）"
                )
            with path.open("rb") as source:
                audio_format = self._audio_format(source.read(64))
            if audio_format is None or audio_format[0] != path.suffix.lower():
                raise ChatMediaError("不支持的语音格式或扩展名")
        return path

    @staticmethod
    def _is_generated_media_id(value: str) -> bool:
        """Accept only identifiers emitted by save_bytes and _copy_local."""
        if not value or "/" in value or "\\" in value:
            return False
        base, separator, suffix = value.partition(".")
        if separator and (
            not suffix or len(suffix) + 1 > 12 or not suffix.isalnum()
        ):
            return False
        return bool(
            re.fullmatch(r"[0-9a-f]{32}|core-[0-9a-f]{20}", base)
        )

    def to_core_segment(self, attachment: dict[str, Any]) -> dict[str, Any]:
        kind = self.normalize_type(str(attachment.get("type") or ""))
        path = self.resolve(str(attachment.get("media_id") or ""), kind)
        name = self._safe_display_name(str(attachment.get("name") or ""), path.name)

        if kind in {"image", "emoji", "voice"}:
            data: Any = base64.b64encode(path.read_bytes()).decode("ascii")
        else:
            data = {
                "path": str(path),
                "name": name,
                "file_size": path.stat().st_size,
            }
        return {"type": kind, "data": data}

    @staticmethod
    def _decode_base64(value: str) -> bytes | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            return base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error):
            return None

    @staticmethod
    def _guess_image_suffix(data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        return ".png"

    def capture_core_media(self, media_type: str, data: Any) -> dict[str, Any] | None:
        """Turn an outgoing Core media segment into browser-safe metadata."""
        kind = self.normalize_type(media_type)

        if kind in {"image", "emoji"} and isinstance(data, str):
            if data.startswith(("http://", "https://")):
                return {
                    "type": kind,
                    "name": "表情包" if kind == "emoji" else "图片",
                    "url": data,
                }
            decoded = self._decode_base64(data)
            if decoded:
                suffix = self._guess_image_suffix(decoded)
                return self.save_bytes(
                    kind,
                    decoded,
                    name=("emoji" if kind == "emoji" else "image") + suffix,
                    mime_type=mimetypes.guess_type("x" + suffix)[0] or "image/png",
                )
            path = Path(data)
            if path.is_file():
                stored = self._copy_local(kind, path, path.name)
                if stored:
                    return stored
            return None

        if kind == "voice":
            # NCNK voice payloads are base64 strings. Never interpret Core data
            # as a local path or read a Core-selected filesystem location.
            if not isinstance(data, str):
                return None
            decoded = self._decode_base64(data)
            if not decoded:
                return None
            return self.save_bytes(
                "voice",
                decoded,
                name="voice",
                mime_type="application/octet-stream",
            )

        source = data
        name = ""
        url = ""
        path_value = ""
        if isinstance(data, dict):
            name = str(data.get("name") or "")
            url = str(data.get("url") or "")
            path_value = str(data.get("path") or data.get("file") or "")
        elif isinstance(data, str):
            path_value = data

        if url.startswith(("http://", "https://")):
            return {
                "type": kind,
                "name": self._safe_display_name(name, "视频" if kind == "video" else "文件"),
                "url": url,
            }

        if path_value:
            path = Path(path_value)
            if path.is_file():
                return self._copy_local(kind, path, name or path.name)
        return None

    def capture_core_voicefile(self, data: Any, *, trusted_root: Path) -> dict[str, Any] | None:
        if not isinstance(data, str):
            return None
        source = Path(data)
        if not source.is_absolute() or not source.is_relative_to(trusted_root) or ".." in source.parts:
            return None
        source = source.resolve(strict=True)
        if not source.is_relative_to(trusted_root.resolve()) or not source.is_file():
            return None
        with source.open("rb") as audio:
            audio_format = self._audio_format(audio.read(64))
        if audio_format is None or audio_format[0] != source.suffix.lower():
            return None
        stored = self._copy_local("voice", source, source.name, max_bytes=OUTGOING_VOICE_MAX_BYTES)
        if stored:
            stored["mime_type"] = audio_format[1]
        return stored

    def _copy_local(
        self, media_type: str, source: Path, name: str, *, max_bytes: int | None = None
    ) -> dict[str, Any] | None:
        kind = self.normalize_type(media_type)
        try:
            source_stat = source.stat()
        except OSError:
            return None
        size = source_stat.st_size
        if size <= 0 or size > (max_bytes if max_bytes is not None else MEDIA_LIMITS[kind]):
            return None

        suffix = self._safe_suffix(name or source.name)
        digest = hashlib.sha256(f"{source.resolve()}:{source_stat.st_mtime_ns}:{size}".encode()).hexdigest()[:20]
        media_id = f"core-{digest}{suffix}"
        target = self.root / media_id
        if not target.exists():
            partial = self.root / f"{uuid.uuid4().hex}.part"
            try:
                shutil.copyfile(source, partial)
                if partial.stat().st_size != size:
                    return None
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
        display_name = self._safe_display_name(name, source.name)
        return {
            "media_id": media_id,
            "type": kind,
            "name": display_name,
            "size": size,
            "url": f"/api/chat/media/{media_id}",
        }


chat_media_store = ChatMediaStore()
