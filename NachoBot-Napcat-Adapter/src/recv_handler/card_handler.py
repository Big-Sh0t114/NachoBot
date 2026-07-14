"""NapCat/OneBot 入站 JSON 卡片解析。"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ncnk_message import Seg

from .qq_emoji_list import qq_face

ImageLoader = Callable[[str], Awaitable[str]]


async def parse_json_card(
    raw_message: Mapping[str, Any], image_loader: ImageLoader
) -> tuple[list[Seg], dict[str, Any] | None]:
    """把 OneBot ``json`` 消息段转换为核心可读取的文本/图片段。

    返回值中的第二项仅用于保留需要原样传递给核心的平台卡片元数据。
    """

    segment_data = raw_message.get("data", {})
    if not isinstance(segment_data, Mapping):
        return [_text("[json]")], None

    raw_card = segment_data.get("data")
    if isinstance(raw_card, Mapping):
        parsed_card: Any = dict(raw_card)
    else:
        json_data = str(raw_card or "").strip()
        if not json_data:
            return [_text("[json]")], None
        try:
            parsed_card = json.loads(json_data)
        except (TypeError, ValueError):
            return [_text("[json]")], None

    if not isinstance(parsed_card, Mapping):
        return [_text("[json]")], None

    app_name = str(parsed_card.get("app") or "").strip()
    meta = parsed_card.get("meta", {})
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

    prompt = str(parsed_card.get("prompt") or meta.get("prompt") or "").strip()
    fallback_text = prompt or app_name or "json"
    return [_text(f"[json:{fallback_text}]")], None


def _text(content: str) -> Seg:
    return Seg(type="text", data=content)


async def _with_preview(text: str, preview_url: str, image_loader: ImageLoader) -> list[Seg]:
    segments = [_text(text or "[卡片消息]")]
    image_segment = await _load_preview_image(preview_url, image_loader)
    if image_segment is not None:
        segments.append(image_segment)
    return segments


async def _load_preview_image(preview_url: str, image_loader: ImageLoader) -> Seg | None:
    normalized_url = str(preview_url or "").strip()
    if not normalized_url:
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
    title = str(announcement.get("title") or "").strip()
    content = str(announcement.get("text") or "").strip()
    if announcement.get("encode") == 1:
        title = _safe_base64_decode(title)
        content = _safe_base64_decode(content)
    if title and content:
        return f"[{title}]：{content}"
    if title:
        return f"[{title}]"
    return content or "[群公告]"


def _build_music_text(meta: Mapping[str, Any]) -> str:
    music = meta.get("music", {})
    if not isinstance(music, Mapping):
        return ""
    title = str(music.get("title") or "").strip()
    singer = str(music.get("desc") or music.get("singer") or "").strip()
    tag = str(music.get("tag") or "音乐分享").strip() or "音乐分享"
    parts = [f"[{tag}]"]
    if title:
        parts.append(title)
    if singer:
        parts.append(f"- {singer}")
    return " ".join(parts).strip() or "[音乐分享]"


def _build_miniapp_text(meta: Mapping[str, Any]) -> str:
    detail = meta.get("detail_1", {})
    if not isinstance(detail, Mapping):
        return "[小程序]"
    title = str(detail.get("title") or "").strip()
    description = str(detail.get("desc") or "").strip()
    if title and description:
        return f"[小程序] {title}：{description}"
    return f"[小程序] {title or description}".strip()


def _build_gift_text(meta: Mapping[str, Any]) -> str:
    gift = meta.get("giftark", {})
    if not isinstance(gift, Mapping):
        return "[赠送礼物]"
    gift_name = str(gift.get("title") or "礼物").strip() or "礼物"
    description = str(gift.get("desc") or "").strip()
    suffix = f" {description}" if description else ""
    return f"[赠送礼物: {gift_name}]{suffix}"


def _build_contact_text(meta: Mapping[str, Any], default_tag: str) -> str:
    contact = meta.get("contact", {})
    if not isinstance(contact, Mapping):
        return f"[{default_tag}]"
    name = str(contact.get("nickname") or "未知对象").strip() or "未知对象"
    tag = str(contact.get("tag") or default_tag).strip() or default_tag
    return f"[{tag}] {name}"


def _build_news_text(meta: Mapping[str, Any], default_tag: str) -> str:
    news = meta.get("news", {})
    if not isinstance(news, Mapping):
        return f"[{default_tag}]"
    title = str(news.get("title") or "未知标题").strip() or "未知标题"
    description = str(news.get("desc") or "").replace("[图片]", "").strip()
    tag = str(news.get("tag") or default_tag).strip() or default_tag
    if tag in title:
        title = _trim_card_title(title.replace(tag, "", 1))
    if description:
        return f"[{tag}] {title}：{description}"
    return f"[{tag}] {title}".strip()


def _build_feed_text(meta: Mapping[str, Any]) -> str:
    feed = meta.get("feed", {})
    if not isinstance(feed, Mapping):
        return "[群相册]"
    title = str(feed.get("title") or "群相册").strip() or "群相册"
    tag = str(feed.get("tagName") or "群相册").strip() or "群相册"
    description = str(feed.get("forwardMessage") or "").strip()
    if tag in title:
        title = _trim_card_title(title.replace(tag, "", 1))
    if description:
        return f"[{tag}] {title}：{description}"
    return f"[{tag}] {title}".strip()


def _build_favorite_text(meta: Mapping[str, Any]) -> str:
    news = meta.get("news", {})
    if not isinstance(news, Mapping):
        return "[QQ收藏]"
    description = str(news.get("desc") or "").replace("[图片]", "").strip()
    tag = str(news.get("tag") or "QQ收藏").strip() or "QQ收藏"
    return f"[{tag}] {description}".strip()


def _build_simple_title_text(meta: Mapping[str, Any], key: str, default_tag: str) -> str:
    payload = meta.get(key, {})
    if not isinstance(payload, Mapping):
        return f"[{default_tag}]"
    title = str(payload.get("title") or "未知标题").strip() or "未知标题"
    tag = str(payload.get("tag") or default_tag).strip() or default_tag
    return f"[{tag}] {title}".strip()


async def _build_forum_segments(meta: Mapping[str, Any], image_loader: ImageLoader) -> list[Seg]:
    detail = meta.get("detail", {})
    if not isinstance(detail, Mapping):
        return []
    feed = detail.get("feed", {})
    poster = detail.get("poster", {})
    channel_info = detail.get("channel_info", {})
    if not all(isinstance(item, Mapping) for item in (feed, poster, channel_info)):
        return []

    guild_name = str(channel_info.get("guild_name") or "").strip()
    nickname = str(poster.get("nick") or "QQ用户").strip() or "QQ用户"
    title = _extract_forum_title(feed)
    face_content = _extract_forum_face_text(feed)
    prefix = f"[频道帖子] [{guild_name}]" if guild_name else "[频道帖子]"
    segments = [_text(f"{prefix}{nickname}:{title}{face_content}")]

    images = feed.get("images", [])
    if not isinstance(images, list):
        return segments
    for item in images:
        if not isinstance(item, Mapping):
            continue
        image_segment = await _load_preview_image(str(item.get("pic_url") or ""), image_loader)
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
    return str(text_content.get("text") or "帖子").strip() or "帖子"


def _extract_forum_face_text(feed: Mapping[str, Any]) -> str:
    contents_payload = feed.get("contents", {})
    if not isinstance(contents_payload, Mapping):
        return ""
    contents = contents_payload.get("contents", [])
    if not isinstance(contents, list):
        return ""
    face_parts: list[str] = []
    for item in contents:
        if not isinstance(item, Mapping):
            continue
        emoji_content = item.get("emoji_content", {})
        if not isinstance(emoji_content, Mapping):
            continue
        emoji_id = str(emoji_content.get("id") or "").strip()
        if emoji_id in qq_face:
            face_parts.append(qq_face[emoji_id])
    return "".join(face_parts)


def _build_location_text(meta: Mapping[str, Any]) -> str:
    location = meta.get("Location.Search", {})
    if not isinstance(location, Mapping):
        return "[位置]"
    name = str(location.get("name") or "未知地点").strip() or "未知地点"
    address = str(location.get("address") or "").strip()
    if address:
        return f"[位置] {address} · {name}"
    return f"[位置] {name}"


def _build_together_text(meta: Mapping[str, Any]) -> str:
    invite = meta.get("invite", {})
    if not isinstance(invite, Mapping):
        return "[一起听歌]"
    title = str(invite.get("title") or "一起听歌").strip() or "一起听歌"
    summary = str(invite.get("summary") or "").strip()
    return f"[{title}] {summary}".strip()


def _extract_preview_url(meta: Mapping[str, Any], key: str, field_name: str = "preview") -> str:
    payload = meta.get(key, {})
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get(field_name) or "").strip()


def _trim_card_title(title: str) -> str:
    return re.sub(r"^[：:\s\-—]+|[：:\s\-—]+$", "", str(title or "").strip())


def _safe_base64_decode(encoded_text: str) -> str:
    normalized_text = str(encoded_text or "").strip()
    if not normalized_text:
        return ""
    try:
        return base64.b64decode(normalized_text).decode("utf-8", errors="ignore")
    except Exception:
        return normalized_text
