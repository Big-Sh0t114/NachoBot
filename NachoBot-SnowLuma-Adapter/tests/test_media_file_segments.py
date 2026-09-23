from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

_ROOT = Path(__file__).resolve().parents[2]
_ADAPTER_ROOT = _ROOT / "NachoBot-SnowLuma-Adapter"
if str(_ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_ROOT))
if str(_ROOT / "NachoBot") not in sys.path:
    sys.path.append(str(_ROOT / "NachoBot"))

from ncnk_message import BaseMessageInfo, FormatInfo, GroupInfo, MessageBase, Seg, UserInfo  # noqa: E402
from src.bridge import ACCEPT_FORMAT, SnowLumaBridge, get_accept_format  # noqa: E402


def test_local_media_paths_translate_to_onebot_segments() -> None:
    bridge = SnowLumaBridge()

    payload = bridge._outbound_segments(
        Seg(
            type="seglist",
            data=[
                Seg(type="voicefile", data="C:/tmp/example.silk"),
                Seg(type="videofile", data="C:/tmp/example.mp4"),
            ],
        )
    )

    assert payload == [
        {"type": "record", "data": {"file": "file://C:/tmp/example.silk"}},
        {"type": "video", "data": {"file": "file://C:/tmp/example.mp4"}},
    ]


def test_local_media_formats_are_advertised_to_core() -> None:
    assert "voicefile" in ACCEPT_FORMAT
    assert "videofile" in ACCEPT_FORMAT


def test_tts_capability_is_conditional_but_prebuilt_audio_stays_advertised() -> None:
    assert "tts_text" not in get_accept_format(False)
    assert "tts_text" in get_accept_format(True)
    assert "voice" in get_accept_format(False)
    assert "voicefile" in get_accept_format(False)


def test_videofile_is_sent_as_group_video_and_echoed() -> None:
    bridge = SnowLumaBridge()
    bridge.client.call_action = AsyncMock(
        return_value={"status": "ok", "retcode": 0, "data": {"message_id": 42}}
    )
    bridge.router.send_custom_message = AsyncMock(return_value=True)
    message = MessageBase(
        message_info=BaseMessageInfo(
            platform="qq",
            message_id="source-video",
            group_info=GroupInfo(platform="qq", group_id="722852338", group_name="synthetic-group"),
            user_info=UserInfo(platform="qq", user_id="2146014839"),
            format_info=FormatInfo(content_format=["videofile"], accept_format=["text"]),
        ),
        message_segment=Seg(type="videofile", data="C:/tmp/example.mp4"),
    )

    asyncio.run(bridge._send_normal(message))

    bridge.client.call_action.assert_awaited_once_with(
        "send_group_msg",
        {
            "group_id": 722852338,
            "message": [{"type": "video", "data": {"file": "file://C:/tmp/example.mp4"}}],
        },
    )
    bridge.router.send_custom_message.assert_awaited_once_with(
        platform="qq",
        message_type_name="message_id_echo",
        message={"type": "echo", "echo": "source-video", "actual_id": "42"},
    )


def test_empty_local_media_paths_are_rejected() -> None:
    bridge = SnowLumaBridge()

    assert bridge._outbound_segments(Seg(type="voicefile", data="")) == []
    assert bridge._outbound_segments(Seg(type="videofile", data=None)) == []


def test_prebuilt_voice_stream_survives_tts_disabled() -> None:
    bridge = SnowLumaBridge()

    assert bridge._outbound_segments(
        Seg(type="voice_stream", data={"audio_base64": "YWJj"})
    ) == [{"type": "record", "data": {"file": "base64://YWJj"}}]
