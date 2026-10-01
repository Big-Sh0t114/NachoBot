from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import shutil
import uuid
from pathlib import Path
from typing import Any


MEDIA_TYPES = frozenset({"image", "emoji", "video", "file"})
MEDIA_LIMITS = {
    "image": 16 * 1024 * 1024,
    "emoji": 16 * 1024 * 1024,
    "video": 64 * 1024 * 1024,
    # Core 的 file sandbox 当前硬限制为 1 MiB。
    "file": 1 * 1024 * 1024,
}


class ChatMediaError(ValueError):
    pass


class ChatMediaStore:
    """Persistent media store shared by WebUI Chat ingress and egress.

    Browser uploads are stored as files first.  Core receives image/emoji as
    base64 (the existing NCNK convention), while video/file receive a local
    path object.  Voice is intentionally not part of this store/protocol.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or (Path(__file__).parent / "data" / "chat_media")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def normalize_type(media_type: str) -> str:
        kind = str(media_type or "").strip().lower()
        if kind not in MEDIA_TYPES:
            raise ChatMediaError("仅支持图片、表情包、视频和文件附件")
        return kind

    @staticmethod
    def limit_for_type(media_type: str) -> int:
        return MEDIA_LIMITS[ChatMediaStore.normalize_type(media_type)]

    @staticmethod
    def _safe_suffix(name: str, mime_type: str = "") -> str:
        suffix = Path(str(name or "")).suffix.lower()
        if suffix and len(suffix) <= 12 and suffix[1:].isalnum():
            return suffix
        guessed = mimetypes.guess_extension(str(mime_type or "").split(";", 1)[0].strip())
        if guessed and len(guessed) <= 12:
            return guessed.lower()
        return ""

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

        suffix = self._safe_suffix(name, mime_type)
        media_id = f"{uuid.uuid4().hex}{suffix}"
        path = self.root / media_id
        path.write_bytes(data)
        display_name = self._safe_display_name(name, media_id)
        return {
            "media_id": media_id,
            "type": kind,
            "name": display_name,
            "mime_type": str(mime_type or "application/octet-stream")[:120],
            "size": size,
            "url": f"/api/chat/media/{media_id}",
        }

    def resolve(self, media_id: str) -> Path:
        value = str(media_id or "").strip()
        if not value or Path(value).name != value:
            raise ChatMediaError("无效的附件标识")
        path = (self.root / value).resolve()
        if path.parent != self.root or not path.is_file():
            raise ChatMediaError("附件不存在或已失效")
        return path

    def to_core_segment(self, attachment: dict[str, Any]) -> dict[str, Any]:
        kind = self.normalize_type(str(attachment.get("type") or ""))
        path = self.resolve(str(attachment.get("media_id") or ""))
        name = self._safe_display_name(str(attachment.get("name") or ""), path.name)

        if kind in {"image", "emoji"}:
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

    def _copy_local(self, media_type: str, source: Path, name: str) -> dict[str, Any] | None:
        kind = self.normalize_type(media_type)
        try:
            size = source.stat().st_size
        except OSError:
            return None
        if size <= 0 or size > MEDIA_LIMITS[kind]:
            return None

        suffix = self._safe_suffix(name or source.name)
        digest = hashlib.sha256(f"{source.resolve()}:{source.stat().st_mtime_ns}:{size}".encode()).hexdigest()[:20]
        media_id = f"core-{digest}{suffix}"
        target = self.root / media_id
        if not target.exists():
            shutil.copyfile(source, target)
        display_name = self._safe_display_name(name, source.name)
        return {
            "media_id": media_id,
            "type": kind,
            "name": display_name,
            "size": size,
            "url": f"/api/chat/media/{media_id}",
        }


chat_media_store = ChatMediaStore()
