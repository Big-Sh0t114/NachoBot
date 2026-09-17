from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_NACHOBOT_PATH = Path(__file__).resolve().parents[2] / "NachoBot"
if str(_NACHOBOT_PATH) not in sys.path:
    sys.path.append(str(_NACHOBOT_PATH))

from src.bridge import SnowLumaBridge  # noqa: E402
from src.card_handler import (  # noqa: E402
    MAX_CARD_DEPTH,
    MAX_CARD_INPUT_CHARS,
    parse_json_card,
    parse_share_card,
    parse_xml_card,
)


def _json_segment(payload: dict) -> dict:
    return {"type": "json", "data": {"data": json.dumps(payload, ensure_ascii=False)}}


def test_miniapp_card_is_readable_and_preserves_napcat_metadata() -> None:
    payload = {
        "app": "com.tencent.miniapp_01",
        "prompt": "[QQ小程序] 视频",
        "meta": {
            "detail_1": {
                "title": "视频标题",
                "desc": "视频描述",
                "preview": "https://preview.example/image.jpg",
                "url": "https://www.bilibili.com/video/BV1TEST",
                "qqdocurl": "https://www.bilibili.com/video/BV1DOC",
            }
        },
    }
    loader = AsyncMock(return_value="preview-base64")

    segments, metadata = asyncio.run(parse_json_card(_json_segment(payload), loader))

    assert segments[0].data == "[小程序] 视频标题：视频描述"
    assert len(segments) == 2
    assert segments[1].type == "image"
    assert metadata == {"type": "miniapp_card", "app": "com.tencent.miniapp_01", "payload": payload}
    loader.assert_awaited_once_with("https://preview.example/image.jpg")


def test_bridge_appends_miniapp_metadata_to_additional_config() -> None:
    payload = {
        "app": "com.tencent.miniapp_01",
        "meta": {
            "detail_1": {
                "title": "标题",
                "desc": "描述",
                "preview": "https://preview.example/must-not-be-fetched.jpg",
            }
        },
    }
    bridge = SnowLumaBridge()
    bridge._media_base64 = AsyncMock(return_value="must-not-be-used")
    additional: dict = {}

    converted = asyncio.run(bridge._convert_inbound_segment(_json_segment(payload), {}, additional))

    assert converted[0].data == "[小程序] 标题：描述"
    assert len(converted) == 1
    assert additional["platform_card_payloads"][0]["payload"] == payload
    bridge._media_base64.assert_not_awaited()


def test_malformed_json_keeps_bounded_placeholder() -> None:
    segment = {"type": "json", "data": {"data": "{" + "x" * 300_000}}
    segments, metadata = asyncio.run(parse_json_card(segment))

    assert [segment.data for segment in segments] == ["[json]"]
    assert metadata is None


def test_direct_json_mapping_enforces_aggregate_size_limit() -> None:
    chunk = "x" * 60_000
    payload = {
        "app": "com.tencent.miniapp_01",
        "meta": {"detail_1": {"title": "标题", "desc": "描述"}},
        "padding": {str(index): chunk for index in range(5)},
    }
    assert sum(len(value) for value in payload["padding"].values()) > MAX_CARD_INPUT_CHARS

    segments, metadata = asyncio.run(parse_json_card({"type": "json", "data": {"data": payload}}))

    assert [segment.data for segment in segments] == ["[json]"]
    assert metadata is None


def test_direct_json_mapping_enforces_depth_limit() -> None:
    payload: dict = {"app": "com.tencent.miniapp_01"}
    current = payload
    for _ in range(MAX_CARD_DEPTH + 1):
        nested: dict = {}
        current["nested"] = nested
        current = nested

    segments, metadata = asyncio.run(parse_json_card({"type": "json", "data": {"data": payload}}))

    assert [segment.data for segment in segments] == ["[json]"]
    assert metadata is None


def test_share_card_renders_common_fields() -> None:
    segment = {"type": "share", "data": {"title": "标题", "content": "摘要", "url": "https://example.test"}}

    converted = parse_share_card(segment)

    assert converted.data == "[分享] 标题：摘要 https://example.test"


def test_direct_share_mapping_enforces_aggregate_size_and_depth_limits() -> None:
    chunk = "x" * 60_000
    oversize = {"title": "标题", "padding": {str(index): chunk for index in range(5)}}
    assert parse_share_card({"type": "share", "data": oversize}).data == "[share]"

    deep: dict = {"title": "标题"}
    current = deep
    for _ in range(MAX_CARD_DEPTH + 1):
        nested = {}
        current["nested"] = nested
        current = nested
    assert parse_share_card({"type": "share", "data": deep}).data == "[share]"


def test_xml_card_renders_fields_and_decodes_entities() -> None:
    segment = {
        "type": "xml",
        "data": {
            "data": '<msg brief="简要"><item><title>标题 &amp; 更多</title><summary>摘要</summary>'
            "<url>https://example.test/video</url></item></msg>"
        },
    }

    converted = parse_xml_card(segment)

    assert converted.data == "[XML卡片] 标题 & 更多：摘要 https://example.test/video"


def test_xml_external_entity_and_malformed_cards_keep_original_placeholder() -> None:
    external = {"type": "xml", "data": {"data": '<!DOCTYPE foo SYSTEM "https://evil.test/x"><foo/>'}}
    malformed = {"type": "xml", "data": {"data": "<foo>"}}

    assert parse_xml_card(external).data == "[xml]"
    assert parse_xml_card(malformed).data == "[xml]"


@pytest.mark.parametrize(
    ("app", "expected"),
    [
        ("com.tencent.music.lua", "音乐分享"),
        ("com.tencent.contact.lua", "推荐联系人"),
        ("com.tencent.map", "位置"),
    ],
)
def test_known_json_cards_have_readable_fallbacks(app: str, expected: str) -> None:
    payload = {"app": app, "meta": {}}

    segments, metadata = asyncio.run(parse_json_card(_json_segment(payload)))

    assert expected in segments[0].data
    assert metadata is None
