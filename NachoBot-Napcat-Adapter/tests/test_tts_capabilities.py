from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ADAPTER_ROOT = _ROOT / "NachoBot-Napcat-Adapter"
if str(_ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_ROOT))
if str(_ROOT / "NachoBot") not in sys.path:
    sys.path.append(str(_ROOT / "NachoBot"))

from ncnk_message import Seg  # noqa: E402
from src.recv_handler import get_accept_format  # noqa: E402
from src.send_handler.send_message_handler import SendMessageHandleClass  # noqa: E402


def test_tts_capability_is_conditional_but_prebuilt_audio_is_always_advertised():
    assert "tts_text" not in get_accept_format(False)
    assert "tts_text" in get_accept_format(True)
    assert "voice" in get_accept_format(False)
    assert "voicefile" in get_accept_format(False)


def test_prebuilt_voice_stream_and_music_survive_tts_disabled():
    payload = SendMessageHandleClass.parse_seg_to_nc_format(
        Seg(
            type="seglist",
            data=[
                Seg(type="voice", data="YWJj"),
                Seg(type="voice_stream", data="ZGVm"),
                Seg(type="music", data="song-123"),
            ],
        )
    )

    assert payload == [
        {"type": "record", "data": {"file": "base64://YWJj"}},
        {"type": "record", "data": {"file": "base64://ZGVm"}},
        {"type": "music", "data": {"type": "163", "id": "song-123"}},
    ]


def test_voice_mapping_extracts_supported_audio_base64_fields():
    payload = SendMessageHandleClass.parse_seg_to_nc_format(
        Seg(
            type="seglist",
            data=[
                Seg(type="voice_stream", data={"audio_base64": "YWJj"}),
                Seg(type="voice", data={"binary_data_base64": "ZGVm"}),
                Seg(type="voice_stream", data={"audio": "Z2hp"}),
            ],
        )
    )

    assert payload == [
        {"type": "record", "data": {"file": "base64://YWJj"}},
        {"type": "record", "data": {"file": "base64://ZGVm"}},
        {"type": "record", "data": {"file": "base64://Z2hp"}},
    ]


def test_malformed_voice_mapping_is_dropped_without_placeholder():
    payload = SendMessageHandleClass.parse_seg_to_nc_format(
        Seg(
            type="seglist",
            data=[
                Seg(type="voice_stream", data={}),
                Seg(type="voice", data={"audio_base64": None}),
                Seg(type="voice_stream", data={"audio_base64": 123}),
                Seg(type="voice", data={"audio": "not-base64"}),
            ],
        )
    )

    assert payload == []


def test_empty_handlers_are_not_appended_as_empty_segments():
    payload = SendMessageHandleClass.parse_seg_to_nc_format(
        Seg(
            type="seglist",
            data=[
                Seg(type="voice", data=""),
                Seg(type="voicefile", data=""),
                Seg(type="music", data=""),
            ],
        )
    )

    assert payload == []
