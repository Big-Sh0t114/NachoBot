import base64
import logging
import httpx
from typing import Any, Dict, List, Optional, Tuple

from ncnk_message import Seg


async def parse_onebot_message(
    raw_message: Any, raw_string: str, logger: logging.Logger, proxy: str = ""
) -> Tuple[List[Seg], Dict[str, Any], List[str]]:
    segments: List[Seg] = []
    additional_config: Dict[str, Any] = {}
    content_format: List[str] = []

    if isinstance(raw_message, str):
        raw_message = [{"type": "text", "data": {"text": raw_message}}]
    elif isinstance(raw_message, dict):
        raw_message = [raw_message]
    if not isinstance(raw_message, list):
        return [], additional_config, content_format

    for seg in raw_message:
        seg_type = seg.get("type")
        seg_data = seg.get("data") or {}
        if seg_type == "text":
            text = seg_data.get("text") or ""
            if text:
                segments.append(Seg(type="text", data=text))
                if "text" not in content_format:
                    content_format.append("text")
        elif seg_type == "at":
            target = seg_data.get("qq") or seg_data.get("id") or "unknown"
            segments.append(Seg(type="text", data=f"@<{target}>"))
            if "text" not in content_format:
                content_format.append("text")
        elif seg_type == "reply":
            reply_id = seg_data.get("id")
            if reply_id is not None:
                additional_config["reply_message_id"] = reply_id
        elif seg_type == "image":
            image_data = await image_to_base64(seg_data, logger, proxy)
            if image_data:
                segments.append(Seg(type="image", data=image_data))
                if "image" not in content_format:
                    content_format.append("image")
            else:
                segments.append(Seg(type="text", data="[image]"))
                if "text" not in content_format:
                    content_format.append("text")
        elif seg_type == "record":
            record_data = await record_to_base64(seg_data, logger, proxy)
            if record_data:
                segments.append(Seg(type="voice", data=record_data))
                if "voice" not in content_format:
                    content_format.append("voice")
            else:
                segments.append(Seg(type="text", data="[voice]"))
                if "text" not in content_format:
                    content_format.append("text")
        elif seg_type == "file":
            # For file parsing, OneBot usually sends 'file', 'url' or 'path' in data, plus 'name'.
            file_url = (
                seg_data.get("url") or seg_data.get("file") or seg_data.get("path")
            )
            file_name = seg_data.get("name", "unknown_file")
            file_size = seg_data.get("file_size") or seg_data.get("size")

            if file_url:
                file_info = {"url": file_url, "name": file_name, "size": file_size}
                segments.append(Seg(type="file", data=file_info))
                if "file" not in content_format:
                    content_format.append("file")
            else:
                segments.append(Seg(type="text", data=f"[file: {file_name}]"))
                if "text" not in content_format:
                    content_format.append("text")

    # Koishi OneBot might wrap Discord files as <file .../> in raw_message but provide `[file]` in the message array.
    if raw_string and any(tag in raw_string for tag in ("<file", "<record", "<audio")):
        import re
        import html

        file_matches = list(
            re.finditer(r"<(file|record|audio)\s+([^>]+)/>", raw_string)
        )
        has_voice_match = False
        has_file_match = False

        for match in file_matches:
            tag_name = match.group(1).lower()
            attrs_str = match.group(2)
            attr_dict = dict(re.findall(r'([\w-]+)="([^"]*)"', attrs_str))

            src_url = attr_dict.get("src") or attr_dict.get("url")

            if src_url:
                src_url = html.unescape(src_url)

                if tag_name in ("record", "audio"):
                    has_voice_match = True
                    # Await inside a loop is fine since this is an async function
                    voice_data = await download_base64(src_url, logger, proxy)
                    if voice_data:
                        segments.append(Seg(type="voice", data=voice_data))
                        if "voice" not in content_format:
                            content_format.append("voice")

                elif tag_name == "file":
                    has_file_match = True
                    file_name = html.unescape(attr_dict.get("file", "unknown_file"))
                    file_size = attr_dict.get("size", 0)
                    file_info = {"url": src_url, "name": file_name, "size": file_size}
                    segments.append(Seg(type="file", data=file_info))
                    if "file" not in content_format:
                        content_format.append("file")

        # Remove literal "[file]" generated by Koishi
        if has_file_match:
            segments = [
                seg
                for seg in segments
                if not (seg.type == "text" and seg.data == "[file]")
            ]

        # Remove literal "[record]" generated by Koishi
        if has_voice_match:
            segments = [
                seg
                for seg in segments
                if not (
                    seg.type == "text"
                    and seg.data in ("[record]", "[voice]", "[audio]")
                )
            ]

    return segments, additional_config, content_format


async def image_to_base64(
    seg_data: Dict[str, Any], logger: logging.Logger, proxy: str = ""
) -> Optional[str]:
    url = seg_data.get("url") or seg_data.get("file")
    if not url:
        return None
    if isinstance(url, str) and url.startswith("base64://"):
        return url[len("base64://") :]
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return await download_base64(url, logger, proxy)
    return None


async def record_to_base64(
    seg_data: Dict[str, Any], logger: logging.Logger, proxy: str = ""
) -> Optional[str]:
    url = seg_data.get("url") or seg_data.get("file")
    if not url:
        return None
    if isinstance(url, str) and url.startswith("base64://"):
        return url[len("base64://") :]
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return await download_base64(url, logger, proxy)
    return None


async def download_base64(
    url: str, logger: logging.Logger, proxy: str = ""
) -> Optional[str]:
    try:
        async with httpx.AsyncClient(proxy=proxy if proxy else None) as client:
            resp = await client.get(
                url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0
            )
            if resp.status_code == 200:
                return base64.b64encode(resp.content).decode("ascii")
            else:
                logger.warning(
                    f"Media download failed for {url}: HTTP {resp.status_code}"
                )
                return None
    except Exception as exc:
        logger.warning(f"Media download failed for {url}: {repr(exc)}")
        return None
