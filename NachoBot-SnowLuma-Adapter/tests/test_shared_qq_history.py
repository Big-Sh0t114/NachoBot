from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import MethodType, SimpleNamespace

CORE_ROOT = Path(__file__).resolve().parents[2] / "NachoBot"
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

from ncnk_message import MessageBase, Seg  # noqa: E402
from ncnk_message.qq_history_identity import snow_message_id_for_native_sequence  # noqa: E402


ACCOUNT = "2955610491"
PEER = "19563"
STAMP = 1_780_000_100.25


def _load_offline_history():
    source = Path(__file__).resolve().parents[1] / "src" / "offline_history.py"
    spec = importlib.util.spec_from_file_location("snowluma_offline_history_identity_test", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load SnowLuma history integration from {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_send_methods():
    source = Path(__file__).resolve().parents[1] / "src" / "bridge.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    bridge_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SnowLumaBridge")
    functions = [
        node for node in bridge_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_outbound_segments"
    ] + [
        node for node in bridge_class.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_normal"
    ]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    from ncnk_message.qq_history_identity import resolve_qq_reply_segments

    logger = SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    namespace = {
        "MessageBase": MessageBase,
        "Seg": Seg,
        "Mapping": Mapping,
        "asyncio": asyncio,
        "time": time,
        "logger": logger,
        "segment_summary": lambda *_args, **_kwargs: "segment",
        "global_config": SimpleNamespace(
            send=SimpleNamespace(min_interval_sec=0),
            snowluma=SimpleNamespace(media_action_timeout_sec=1),
        ),
        "resolve_qq_reply_segments": resolve_qq_reply_segments,
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["_send_normal"], namespace["_outbound_segments"]


def _reply_hint(*, account=ACCOUNT, peer=PEER, original="450563", native_id=None):
    return {
        "version": 1,
        "scope": "qq_private_friend",
        "account_id": account,
        "platform": "qq",
        "peer_id": peer,
        "original_reply_id": original,
        "native_ids": {"snowluma": native_id or snow_message_id_for_native_sequence(19_563, peer)},
    }


class _FakeSnowClient:
    def __init__(self, account):
        self.account = account
        self.calls = []

    def connection_snapshot(self):
        return (object(), 1, self.account) if self.account else None

    async def call_action(self, action, params, **kwargs):
        self.calls.append((action, params, kwargs))
        return {"data": {"message_id": "snow-outgoing-id"}}

    @staticmethod
    def action_error(_response):
        return None


def _bridge_and_message(*, hint, account=ACCOUNT):
    send, convert = _load_send_methods()
    client = _FakeSnowClient(account)
    bridge = SimpleNamespace(
        client=client,
        _send_lock=asyncio.Lock(),
        _last_send_at=0.0,
        _extract_file_uploads=lambda _root: [],
        _extract_forward_nodes=lambda _root: [],
        _to_int=lambda value: int(value),
        _echo_message_id=lambda *_args: asyncio.sleep(0),
    )
    bridge._outbound_segments = MethodType(convert, bridge)
    root = Seg(
        type="seglist",
        data=[Seg(type="reply", data="450563"), Seg(type="text", data="正文保持")],
    )
    message = SimpleNamespace(
        message_info=SimpleNamespace(
            platform="qq",
            group_info=None,
            user_info=SimpleNamespace(user_id=PEER),
            additional_config={"qq_reply_identity": hint},
        ),
        message_segment=root,
    )
    return send, bridge, message


class SnowLumaSharedQQHistoryTests(unittest.TestCase):
    def test_live_normalizer_registers_snow_native_id_not_client_message_seq(self):
        module = _load_offline_history()
        runtime = module.SnowLumaOfflineHistory(
            platform="qq",
            settings=SimpleNamespace(enabled=False, page_size=5, max_pages=2, retry_delay_sec=0.1),
            source_instance="test-snow",
        )
        snow_id = snow_message_id_for_native_sequence(19_563, PEER)
        raw = {
            "message_type": "private",
            "sub_type": "friend",
            "self_id": ACCOUNT,
            "user_id": PEER,
            "message_id": snow_id,
            "message_seq": 777_777,
            "time": STAMP,
        }
        context = {"metadata": {"original_timestamp": STAMP}}
        result = asyncio.run(runtime.decorate_live_message(raw, context, account_id=ACCOUNT))
        delivery = result["metadata"]["delivery"]
        identity = delivery["qq_identity"]
        self.assertEqual(identity["snow_message_id"], snow_id)
        self.assertNotIn("native_nt_seq", identity)

    def test_actual_private_send_path_rewrites_to_snow_id_and_keeps_body(self):
        send, bridge, message = _bridge_and_message(hint=_reply_hint())
        async def run():
            bridge._send_lock = asyncio.Lock()
            await send(bridge, message)

        asyncio.run(run())
        payload = bridge.client.calls[0][1]["message"]
        self.assertEqual(payload, [
            {"type": "reply", "data": {"id": snow_message_id_for_native_sequence(19_563, PEER)}},
            {"type": "text", "data": {"text": "正文保持"}},
        ])

    def test_actual_private_send_path_drops_wrong_scope_or_unknown_alias_but_sends_body(self):
        hints = (
            _reply_hint(account="wrong-account"),
            _reply_hint(native_id=None) | {"native_ids": {}},
        )
        for hint in hints:
            with self.subTest(hint=hint):
                send, bridge, message = _bridge_and_message(hint=hint)

                async def run(send=send, bridge=bridge, message=message):
                    bridge._send_lock = asyncio.Lock()
                    await send(bridge, message)

                asyncio.run(run())
                self.assertEqual(bridge.client.calls[0][1]["message"], [
                    {"type": "text", "data": {"text": "正文保持"}},
                ])


if __name__ == "__main__":
    unittest.main()
