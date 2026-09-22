"""Bounded parsing for OneBot card message segments.

SnowLuma exposes the same OneBot ``json``/``xml``/``share`` segments as
NapCat, but previously the bridge discarded their payloads at the adapter
boundary.  This module keeps the parser deliberately self-contained: card
payloads are untrusted message data, so parsing is size/depth bounded and the
fallback never echoes the original body.
"""

from __future__ import annotations

import base64
import html
import json
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ncnk_message import Seg

ImageLoader = Callable[[str], Awaitable[str]]

# A normal QQ card is only a few KiB.  These limits prevent a malformed card
# from consuming unbounded parser/output work while retaining the complete
# parsed mapping for the small metadata contract used by the Bilibili plugin.
MAX_CARD_INPUT_CHARS = 262_144
MAX_CARD_VALUE_CHARS = 65_536
MAX_CARD_NODES = 4_096
MAX_CARD_DEPTH = 12
MAX_CARD_OUTPUT_CHARS = 2_048
MAX_CARD_FIELDS = 24


async def parse_json_card(
    raw_message: Mapping[str, Any], image_loader: ImageLoader | None = None
) -> tuple[list[Seg], dict[str, Any] | None]:
    """Convert an OneBot ``json`` segment into readable segments.

    The metadata return value intentionally mirrors NapCat's contract.  It is
    only populated for mini-app cards so downstream plugins can inspect the
    complete parsed payload without scraping rendered text.
    """

    parsed_card = _load_json_payload(raw_message)
    if parsed_card is None:
        return [_text("[json]")], None

    app_name = _safe_text(parsed_card.get("app"), max_length=128)
    meta = parsed_card.get("meta")
    if not isinstance(meta, Mapping):
        meta = {}

    if app_name == "com.tencent.mannounce":
        return [_text(_build_announcement_text(meta))], None

    if app_name in {"com.tencent.music.lua", "com.tencent.structmsg"}:
        music_text = _build_music_text(meta)
        if music_text:
            return [_text(music_text)], None

    if app_name == "com.tencent.miniapp_01":
        card_metadata = {
            "type": "miniapp_card",
            "app": app_name,
            "payload": dict(parsed_card),
        }
        return (
            await _with_preview(
                _build_miniapp_text(meta),
                _extract_preview_url(meta, "detail_1"),
                image_loader,
            ),
            card_metadata,
        )

    if app_name == "com.tencent.giftmall.giftark":
        return [_text(_build_gift_text(meta))], None

    if app_name == "com.tencent.contact.lua":
        return [_text(_build_contact_text(meta, "推荐联系人"))], None

    if app_name == "com.tencent.troopsharecard":
        return [_text(_build_contact_text(meta, "推荐群聊"))], None

    if app_name == "com.tencent.tuwen.lua":
        return (
            await _with_preview(
                _build_news_text(meta, "图文分享"),
                _extract_preview_url(meta, "news"),
                image_loader,
            ),
            None,
        )

    if app_name == "com.tencent.feed.lua":
        return (
            await _with_preview(
                _build_feed_text(meta),
                _extract_preview_url(meta, "feed", "cover"),
                image_loader,
            ),
            None,
        )

    if app_name == "com.tencent.template.qqfavorite.share":
        return (
            await _with_preview(
                _build_favorite_text(meta),
                _extract_preview_url(meta, "news"),
                image_loader,
            ),
            None,
        )

    if app_name == "com.tencent.miniapp.lua":
        return (
            await _with_preview(
                _build_simple_title_text(meta, "miniapp", "QQ空间"),
                _extract_preview_url(meta, "miniapp"),
                image_loader,
            ),
            None,
        )

    if app_name == "com.tencent.forum":
        forum_segments = await _build_forum_segments(meta, image_loader)
        if forum_segments:
            return forum_segments, None

    if app_name == "com.tencent.map":
        return [_text(_build_location_text(meta))], None

    if app_name == "com.tencent.together":
        return [_text(_build_together_text(meta))], None

    prompt = _safe_text(parsed_card.get("prompt")) or _safe_text(meta.get("prompt"))
    fallback_text = prompt or app_name or "json"
    return [_text(_bounded_output(f"[json:{fallback_text}]"))], None


def parse_share_card(raw_message: Mapping[str, Any]) -> Seg:
    """Render the common OneBot ``share`` fields without echoing raw JSON."""

    data = raw_message.get("data")
    if isinstance(data, Mapping):
        if not _payload_within_limits(data):
            return _text("[share]")
        payload: Mapping[str, Any] = data
    elif isinstance(data, str):
        payload = _load_mapping_string(data) or {}
    else:
        payload = {}

    title = _safe_text(payload.get("title"))
    content = _safe_text(payload.get("content") or payload.get("desc"))
    url = _safe_text(payload.get("url") or payload.get("jumpUrl"))
    parts = [part for part in (title, content, url) if part]
    if not parts:
        return _text("[share]")
    rendered = "[分享] " + "：".join(parts[:2])
    if len(parts) > 2:
        rendered += f" {parts[2]}"
    return _text(_bounded_output(rendered))


def parse_xml_card(raw_message: Mapping[str, Any]) -> Seg:
    """Extract safe, readable fields from a common OneBot ``xml`` card."""

    data = raw_message.get("data")
    if isinstance(data, Mapping):
        raw_xml = data.get("data")
    else:
        raw_xml = data
    if not isinstance(raw_xml, str):
        return _text("[xml]")

    rendered = _render_xml_card(raw_xml)
    return _text(rendered or "[xml]")


def _text(content: str) -> Seg:
    return Seg(type="text", data=_bounded_output(content))


def _load_json_payload(raw_message: Mapping[str, Any]) -> dict[str, Any] | None:
    segment_data = raw_message.get("data")
    if not isinstance(segment_data, Mapping):
        return None
    raw_card = segment_data.get("data")
    if isinstance(raw_card, Mapping):
        parsed_card: Any = dict(raw_card)
    elif isinstance(raw_card, str):
        raw_text = raw_card.strip()
        if not raw_text or len(raw_text) > MAX_CARD_INPUT_CHARS:
            return None
        try:
            parsed_card = json.loads(raw_text)
        except (MemoryError, RecursionError, TypeError, ValueError):
            return None
        # A few OneBot implementations wrap the actual JSON object as a JSON
        # string.  Accept one extra level, but never recurse indefinitely.
        if isinstance(parsed_card, str):
            nested_text = parsed_card.strip()
            if len(nested_text) > MAX_CARD_INPUT_CHARS:
                return None
            try:
                parsed_card = json.loads(nested_text)
            except (MemoryError, RecursionError, TypeError, ValueError):
                return None
    else:
        return None

    if not isinstance(parsed_card, Mapping):
        return None
    if not _payload_within_limits(parsed_card):
        return None
    return dict(parsed_card)


def _load_mapping_string(raw_text: str) -> Mapping[str, Any] | None:
    normalized = raw_text.strip()
    if not normalized or len(normalized) > MAX_CARD_INPUT_CHARS:
        return None
    try:
        payload = json.loads(normalized)
    except (MemoryError, RecursionError, TypeError, ValueError):
        return None
    if isinstance(payload, Mapping) and _payload_within_limits(payload):
        return payload
    return None


def _payload_within_limits(payload: Any) -> bool:
    stack: list[tuple[Any, int]] = [(payload, 0)]
    nodes = 0
    total_chars = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        total_chars += 1
        if nodes > MAX_CARD_NODES or depth > MAX_CARD_DEPTH:
            return False
        if isinstance(value, str):
            if len(value) > MAX_CARD_VALUE_CHARS:
                return False
            total_chars += len(value)
        elif isinstance(value, Mapping):
            if len(value) > MAX_CARD_NODES:
                return False
            for key, nested in value.items():
                try:
                    key_text = str(key)
                except (MemoryError, RecursionError, ValueError):
                    return False
                if len(key_text) > 512:
                    return False
                total_chars += len(key_text)
                stack.append((nested, depth + 1))
        elif isinstance(value, (list, tuple)):
            if len(value) > MAX_CARD_NODES:
                return False
            stack.extend((nested, depth + 1) for nested in value)
        elif value is not None:
            try:
                total_chars += len(str(value))
            except (MemoryError, RecursionError, ValueError):
                return False
        if total_chars > MAX_CARD_INPUT_CHARS:
            return False
    return True


async def _with_preview(text: str, preview_url: str, image_loader: ImageLoader | None) -> list[Seg]:
    segments = [_text(text or "[卡片消息]")]
    image_segment = await _load_preview_image(preview_url, image_loader)
    if image_segment is not None:
        segments.append(image_segment)
    return segments


async def _load_preview_image(preview_url: str, image_loader: ImageLoader | None) -> Seg | None:
    normalized_url = _safe_text(preview_url, max_length=MAX_CARD_VALUE_CHARS)
    if not normalized_url or image_loader is None:
        return None
    if not normalized_url.lower().startswith(("http://", "https://")):
        return None
    try:
        image_base64 = await image_loader(normalized_url)
    except Exception:
        return None
    if not image_base64:
        return None
    return Seg(type="image", data=image_base64)


def _build_announcement_text(meta: Mapping[str, Any]) -> str:
    announcement = meta.get("mannounce", {})
    if not isinstance(announcement, Mapping):
        announcement = {}
    title = _safe_text(announcement.get("title"))
    content = _safe_text(announcement.get("text"))
    if announcement.get("encode") == 1:
        title = _safe_base64_decode(title)
        content = _safe_base64_decode(content)
    if title and content:
        return _bounded_output(f"[{title}]：{content}")
    if title:
        return _bounded_output(f"[{title}]")
    return content or "[群公告]"


def _build_music_text(meta: Mapping[str, Any]) -> str:
    music = meta.get("music", {})
    if not isinstance(music, Mapping):
        return ""
    title = _safe_text(music.get("title"))
    singer = _safe_text(music.get("desc") or music.get("singer"))
    tag = _safe_text(music.get("tag")) or "音乐分享"
    parts = [f"[{tag}]"]
    if title:
        parts.append(title)
    if singer:
        parts.append(f"- {singer}")
    return _bounded_output(" ".join(parts)) or "[音乐分享]"


def _build_miniapp_text(meta: Mapping[str, Any]) -> str:
    detail = meta.get("detail_1", {})
    if not isinstance(detail, Mapping):
        return "[小程序]"
    title = _safe_text(detail.get("title"))
    description = _safe_text(detail.get("desc"))
    if title and description:
        return _bounded_output(f"[小程序] {title}：{description}")
    return _bounded_output(f"[小程序] {title or description}").strip()


def _build_gift_text(meta: Mapping[str, Any]) -> str:
    gift = meta.get("giftark", {})
    if not isinstance(gift, Mapping):
        return "[赠送礼物]"
    gift_name = _safe_text(gift.get("title")) or "礼物"
    description = _safe_text(gift.get("desc"))
    suffix = f" {description}" if description else ""
    return _bounded_output(f"[赠送礼物: {gift_name}]{suffix}")


def _build_contact_text(meta: Mapping[str, Any], default_tag: str) -> str:
    contact = meta.get("contact", {})
    if not isinstance(contact, Mapping):
        return f"[{default_tag}]"
    name = _safe_text(contact.get("nickname")) or "未知对象"
    tag = _safe_text(contact.get("tag")) or default_tag
    return _bounded_output(f"[{tag}] {name}")


def _build_news_text(meta: Mapping[str, Any], default_tag: str) -> str:
    news = meta.get("news", {})
    if not isinstance(news, Mapping):
        return f"[{default_tag}]"
    title = _safe_text(news.get("title")) or "未知标题"
    description = _safe_text(news.get("desc")).replace("[图片]", "").strip()
    tag = _safe_text(news.get("tag")) or default_tag
    if tag in title:
        title = _trim_card_title(title.replace(tag, "", 1))
    if description:
        return _bounded_output(f"[{tag}] {title}：{description}")
    return _bounded_output(f"[{tag}] {title}")


def _build_feed_text(meta: Mapping[str, Any]) -> str:
    feed = meta.get("feed", {})
    if not isinstance(feed, Mapping):
        return "[群相册]"
    title = _safe_text(feed.get("title")) or "群相册"
    tag = _safe_text(feed.get("tagName")) or "群相册"
    description = _safe_text(feed.get("forwardMessage"))
    if tag in title:
        title = _trim_card_title(title.replace(tag, "", 1))
    if description:
        return _bounded_output(f"[{tag}] {title}：{description}")
    return _bounded_output(f"[{tag}] {title}")


def _build_favorite_text(meta: Mapping[str, Any]) -> str:
    news = meta.get("news", {})
    if not isinstance(news, Mapping):
        return "[QQ收藏]"
    description = _safe_text(news.get("desc")).replace("[图片]", "").strip()
    tag = _safe_text(news.get("tag")) or "QQ收藏"
    return _bounded_output(f"[{tag}] {description}").strip()


def _build_simple_title_text(meta: Mapping[str, Any], key: str, default_tag: str) -> str:
    payload = meta.get(key, {})
    if not isinstance(payload, Mapping):
        return f"[{default_tag}]"
    title = _safe_text(payload.get("title")) or "未知标题"
    tag = _safe_text(payload.get("tag")) or default_tag
    return _bounded_output(f"[{tag}] {title}")


async def _build_forum_segments(meta: Mapping[str, Any], image_loader: ImageLoader | None) -> list[Seg]:
    detail = meta.get("detail", {})
    if not isinstance(detail, Mapping):
        return []
    feed = detail.get("feed", {})
    poster = detail.get("poster", {})
    channel_info = detail.get("channel_info", {})
    if not all(isinstance(item, Mapping) for item in (feed, poster, channel_info)):
        return []

    guild_name = _safe_text(channel_info.get("guild_name"))
    nickname = _safe_text(poster.get("nick")) or "QQ用户"
    title = _extract_forum_title(feed)
    face_content = _extract_forum_face_text(feed)
    prefix = f"[频道帖子] [{guild_name}]" if guild_name else "[频道帖子]"
    segments = [_text(f"{prefix}{nickname}:{title}{face_content}")]

    images = feed.get("images", [])
    if not isinstance(images, list):
        return segments
    for item in images[:MAX_CARD_FIELDS]:
        if not isinstance(item, Mapping):
            continue
        image_segment = await _load_preview_image(_safe_text(item.get("pic_url")), image_loader)
        if image_segment is not None:
            segments.append(image_segment)
    return segments


def _extract_forum_title(feed: Mapping[str, Any]) -> str:
    title_payload = feed.get("title", {})
    if not isinstance(title_payload, Mapping):
        return "帖子"
    contents = title_payload.get("contents", [])
    if not isinstance(contents, list) or not contents or not isinstance(contents[0], Mapping):
        return "帖子"
    text_content = contents[0].get("text_content", {})
    if not isinstance(text_content, Mapping):
        return "帖子"
    return _safe_text(text_content.get("text")) or "帖子"


def _extract_forum_face_text(feed: Mapping[str, Any]) -> str:
    contents_payload = feed.get("contents", {})
    if not isinstance(contents_payload, Mapping):
        return ""
    contents = contents_payload.get("contents", [])
    if not isinstance(contents, list):
        return ""
    face_parts: list[str] = []
    for item in contents[:MAX_CARD_FIELDS]:
        if not isinstance(item, Mapping):
            continue
        emoji_content = item.get("emoji_content", {})
        if not isinstance(emoji_content, Mapping):
            continue
        emoji_id = _safe_text(emoji_content.get("id"))
        if emoji_id:
            face_parts.append(f"[表情:{emoji_id}]")
    return "".join(face_parts)


def _build_location_text(meta: Mapping[str, Any]) -> str:
    location = meta.get("Location.Search", {})
    if not isinstance(location, Mapping):
        return "[位置]"
    name = _safe_text(location.get("name")) or "未知地点"
    address = _safe_text(location.get("address"))
    if address:
        return _bounded_output(f"[位置] {address} · {name}")
    return _bounded_output(f"[位置] {name}")


def _build_together_text(meta: Mapping[str, Any]) -> str:
    invite = meta.get("invite", {})
    if not isinstance(invite, Mapping):
        return "[一起听歌]"
    title = _safe_text(invite.get("title")) or "一起听歌"
    summary = _safe_text(invite.get("summary"))
    return _bounded_output(f"[{title}] {summary}").strip()


def _extract_preview_url(meta: Mapping[str, Any], key: str, field_name: str = "preview") -> str:
    payload = meta.get(key, {})
    if not isinstance(payload, Mapping):
        return ""
    return _safe_text(payload.get(field_name), max_length=MAX_CARD_VALUE_CHARS)


def _render_xml_card(raw_xml: str) -> str:
    normalized = raw_xml.strip()
    if not normalized or len(normalized) > MAX_CARD_INPUT_CHARS:
        return ""
    # ElementTree does not fetch external entities, but rejecting declarations
    # keeps this boundary explicit and avoids entity-expansion surprises.
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)|\b(?:SYSTEM|PUBLIC)\b", normalized, re.IGNORECASE):
        return ""
    try:
        root = ElementTree.fromstring(normalized)
    except (ElementTree.ParseError, RecursionError, ValueError):
        return ""

    fields: dict[str, str] = {}
    stack: list[tuple[ElementTree.Element, int]] = [(root, 0)]
    nodes = 0
    while stack:
        element, depth = stack.pop()
        nodes += 1
        if nodes > MAX_CARD_NODES or depth > MAX_CARD_DEPTH:
            return ""
        local_name = str(element.tag).rsplit("}", 1)[-1].lower()
        if local_name in {"title", "summary", "brief", "desc", "url", "jumpurl", "actiondata"}:
            value = _safe_text(" ".join(element.itertext()), max_length=MAX_CARD_VALUE_CHARS)
            if value and local_name not in fields:
                fields[local_name] = value
        for key, value in element.attrib.items():
            normalized_key = str(key).rsplit("}", 1)[-1].lower()
            if normalized_key in {"title", "summary", "brief", "desc", "url", "jumpurl", "actiondata"}:
                cleaned = _safe_text(value, max_length=MAX_CARD_VALUE_CHARS)
                if cleaned and normalized_key not in fields:
                    fields[normalized_key] = cleaned
        stack.extend((child, depth + 1) for child in reversed(list(element)))

    title = fields.get("title") or fields.get("brief")
    description = fields.get("summary") or fields.get("desc")
    url = fields.get("url") or fields.get("jumpurl") or fields.get("actiondata")
    parts = [part for part in (title, description) if part]
    if not parts and not url:
        return ""
    rendered = "[XML卡片] " + "：".join(parts[:2])
    if url:
        rendered += f" {url}"
    return _bounded_output(rendered)


def _trim_card_title(title: str) -> str:
    return re.sub(r"^[：:\s\-—]+|[：:\s\-—]+$", "", str(title or "").strip())


def _safe_text(value: Any, *, max_length: int = MAX_CARD_VALUE_CHARS) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    text = html.unescape(text)
    text = " ".join(text.split()).strip()
    if len(text) > max_length:
        return text[: max_length - 1] + "…"
    return text


def _bounded_output(value: str) -> str:
    normalized = _safe_text(value, max_length=MAX_CARD_OUTPUT_CHARS)
    if len(normalized) > MAX_CARD_OUTPUT_CHARS:
        return normalized[: MAX_CARD_OUTPUT_CHARS - 1] + "…"
    return normalized


def _safe_base64_decode(encoded_text: str) -> str:
    normalized_text = _safe_text(encoded_text)
    if not normalized_text:
        return ""
    try:
        return _safe_text(base64.b64decode(normalized_text).decode("utf-8", errors="ignore"))
    except Exception:
        return normalized_text
