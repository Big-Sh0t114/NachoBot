"""Safe HTTP responses for untrusted WebUI chat attachments."""

import os
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

_INLINE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
}

_SECURITY_HEADERS = {
    "Cache-Control": "private, max-age=86400",
    "Content-Security-Policy": "sandbox; default-src 'none'",
    "X-Content-Type-Options": "nosniff",
}


def chat_media_file_response(path: Path, *, trusted_root: Path) -> FileResponse:
    """Serve only known passive media inline; force all other bytes to download."""
    try:
        root_value = os.fspath(trusted_root)
        if not root_value:
            raise ValueError("trusted root is empty")
        root_path = os.path.realpath(root_value)
        canonical_path = os.path.realpath(os.fspath(path))
        normalized_path = os.path.normcase(canonical_path)
    except (OSError, TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Media not found") from None

    root_prefix = os.path.normcase(os.path.join(root_path, ""))
    if not normalized_path.startswith(root_prefix) or not os.path.isfile(normalized_path):
        raise HTTPException(status_code=404, detail="Media not found")

    safe_filename = Path(canonical_path).name
    media_type = _INLINE_MEDIA_TYPES.get(Path(canonical_path).suffix.lower())
    headers = dict(_SECURITY_HEADERS)

    if media_type is not None:
        headers["Content-Disposition"] = "inline"
        return FileResponse(normalized_path, media_type=media_type, headers=headers)

    return FileResponse(
        normalized_path,
        media_type="application/octet-stream",
        filename=safe_filename,
        headers=headers,
    )
