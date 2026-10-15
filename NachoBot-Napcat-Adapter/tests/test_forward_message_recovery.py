from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CORE_ROOT = PROJECT_ROOT.parent / "NachoBot"
if str(CORE_ROOT) not in sys.path:
    sys.path.append(str(CORE_ROOT))

from ncnk_message import Seg  # noqa: E402
from src.recv_handler.message_handler import MessageHandler, _is_valid_forward_nodes  # noqa: E402


handler_module = importlib.import_module("src.recv_handler.message_handler")


def _node(segments, *, nickname="peer", group_id=None, self_id=None):
    node = {
        "sender": {"user_id": "200", "nickname": nickname},
        "message": segments,
    }
    if group_id is not None:
        node["group_id"] = group_id
    if self_id is not None:
        node["self_id"] = self_id
    return node


def _segment_tree(value):
    if isinstance(value, Seg):
        yield value
        yield from _segment_tree(value.data)
    elif isinstance(value, list):
        for item in value:
            yield from _segment_tree(item)


def _text_values(value):
    return [segment.data for segment in _segment_tree(value) if segment.type == "text"]


def test_embedded_forward_preserves_reply_text_order_and_defers_images(monkeypatch):
    handler = MessageHandler()
    image_calls = []

    class NoActionConnection:
        def __init__(self):
            self.send_calls = 0

        async def send(self, _payload):
            self.send_calls += 1
            raise AssertionError("valid embedded content must not send get_forward_msg")

    connection = NoActionConnection()
    handler.server_connection = connection

    async def get_image(url):
        image_calls.append(url)
        return "base64-image"

    async def no_forward_lookup(*_args):
        raise AssertionError("valid embedded content must not request get_forward_msg")

    async def no_reply_lookup(*_args):
        raise AssertionError("inner forwarded replies must not request message details")

    monkeypatch.setattr(handler_module, "get_image_base64", get_image)
    monkeypatch.setattr(handler_module, "get_response", no_forward_lookup)
    monkeypatch.setattr(handler_module, "get_message_detail", no_reply_lookup)
    content = [
        _node(
            [
                {"type": "text", "data": {"text": "before"}},
                {"type": "reply", "data": {"id": 123}},
                {"type": "image", "data": {"sub_type": 0, "url": "image-url"}},
                {"type": "text", "data": {"text": "after"}},
            ]
        )
    ]

    segments, _additional = asyncio.run(
        handler.handle_real_message(
            {"message": [{"type": "forward", "data": {"id": "forward-id", "content": content}}]}
        )
    )

    converted = list(_segment_tree(segments))
    values = [(segment.type, segment.data) for segment in converted]
    assert [segment_type for segment_type, _data in values] == [
        "seglist",
        "seglist",
        "text",
        "text",
        "image",
        "text",
        "text",
    ]
    assert values[2:] == [
        ("text", "【peer】:"),
        ("text", "before"),
        ("image", "base64-image"),
        ("text", "after"),
        ("text", "\n"),
    ]
    assert image_calls == ["image-url"]
    assert connection.send_calls == 0


def test_nested_forward_is_local_bounded_and_restores_author_prefix(monkeypatch):
    handler = MessageHandler()

    async def no_forward_lookup(*_args):
        raise AssertionError("embedded nested forwards must not issue get_forward_msg")

    monkeypatch.setattr(handler_module, "get_response", no_forward_lookup)
    deep = _node([{"type": "text", "data": {"text": "leaf"}}], nickname="leaf-author")
    for level in range(5):
        deep = _node(
            [{"type": "forward", "data": {"content": [deep]}}],
            nickname=f"nested-{level}",
        )
    root = _node(
        [
            {"type": "text", "data": {"text": "before-nested"}},
            {"type": "forward", "data": {"content": [deep]}},
            {"type": "text", "data": {"text": "after-nested"}},
        ],
        nickname="outer-author",
    )

    converted = asyncio.run(handler.handle_forward_message([root]))
    text = "".join(_text_values(converted))

    assert "【outer-author】:before-nested" in text
    assert text.index("before-nested") < text.index("合并转发")
    assert text.index("合并转发") < text.index("【转发消息】") < text.index("after-nested")
    assert text.rfind("【outer-author】:", 0, text.index("after-nested")) >= text.index("【转发消息】")
    assert "leaf" not in text


def test_forward_segment_conversion_uses_node_group_and_verified_outer_self(monkeypatch):
    handler = MessageHandler()
    handler.verified_account_id = "111"
    lookups = []

    async def member_info(_connection, *, group_id, user_id):
        lookups.append((group_id, user_id))
        return {"nickname": "group member", "user_id": user_id}

    monkeypatch.setattr(handler_module, "get_member_info", member_info)
    content = [
        _node(
            [{"type": "at", "data": {"qq": "222"}}],
            group_id="678",
            self_id="222",
        )
    ]

    segments, _additional = asyncio.run(
        handler.handle_real_message(
            {
                "self_id": "999",
                "group_id": "123",
                "message": [{"type": "forward", "data": {"content": content}}],
            }
        )
    )

    assert lookups == [("678", "222")]
    assert "@<group member:222>" in "".join(_text_values(segments))


def test_missing_empty_or_malformed_embedded_content_uses_one_fallback_request(monkeypatch):
    nodes = [_node([{"type": "text", "data": {"text": "fallback"}}])]

    class Connection:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    cases = [
        {"id": "forward-id"},
        {"id": "forward-id", "content": []},
        {"id": "forward-id", "content": [{"sender": {}, "message": []}]},
    ]
    for data in cases:
        handler = MessageHandler()
        connection = Connection()
        handler.server_connection = connection
        response_calls = []

        async def get_response(request_id, _response_calls=response_calls):
            _response_calls.append(request_id)
            return {"data": {"messages": nodes}}

        monkeypatch.setattr(handler_module, "get_response", get_response)
        result = asyncio.run(handler._get_forward_message({"data": data}))

        assert result == nodes
        assert len(connection.sent) == 1
        assert connection.sent[0]["action"] == "get_forward_msg"
        assert connection.sent[0]["params"] == {"message_id": "forward-id"}
        assert response_calls == [connection.sent[0]["echo"]]


def test_invalid_forward_shapes_and_oversized_node_lists_are_rejected():
    assert not _is_valid_forward_nodes([{"sender": {}, "message": []}])
    assert not _is_valid_forward_nodes(
        [_node([{"type": "image", "data": {"sub_type": 0}}])]
    )
    assert not _is_valid_forward_nodes(
        [_node([{"type": "text", "data": {"text": "ok"}}])] * 501
    )
