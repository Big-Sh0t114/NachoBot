from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CORE_ROOT = PROJECT_ROOT.parent / "NachoBot"
if str(CORE_ROOT) not in sys.path:
    sys.path.append(str(CORE_ROOT))

from ncnk_message import Seg  # noqa: E402

from src.send_handler.send_message_handler import SendMessageHandleClass  # noqa: E402


def test_generic_local_voice_path_translates_to_record_segment():
    payload = SendMessageHandleClass.parse_seg_to_nc_format(Seg(type="voicefile", data="C:/tmp/example.silk"))
    assert payload == [{"type": "record", "data": {"file": "file://C:/tmp/example.silk"}}]


def test_generic_local_video_path_translates_to_video_segment():
    payload = SendMessageHandleClass.parse_seg_to_nc_format(Seg(type="videofile", data="/tmp/example.mp4"))
    assert payload == [{"type": "video", "data": {"file": "file:///tmp/example.mp4"}}]


def test_empty_generic_local_path_is_rejected():
    assert SendMessageHandleClass.handle_voicefile_message("") == {}
    assert SendMessageHandleClass.handle_videofile_message(None) == {}
