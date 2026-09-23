import base64
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "NachoBot") not in sys.path:
    sys.path.insert(0, str(_ROOT / "NachoBot"))

from message_builder import (
    convert_to_opus_data_url,
    get_accept_format,
    seg_to_onebot,
    voice_to_record_file,
)
from ncnk_message import Seg


def _config(*, use_tts: bool):
    return SimpleNamespace(platform="qq", use_tts=use_tts)


def test_prebuilt_voice_and_point_song_media_survive_tts_disabled():
    payload = seg_to_onebot(
        Seg(
            type="seglist",
            data=[
                Seg(type="voice", data="YWJj"),
                Seg(type="voicefile", data="C:/tmp/point-song.silk"),
                Seg(type="music", data="song-123"),
            ],
        ),
        _config(use_tts=False),
        Mock(),
    )

    assert payload == [
        {"type": "record", "data": {"file": "base64://YWJj"}},
        {"type": "record", "data": {"file": "file://C:/tmp/point-song.silk"}},
        {"type": "music", "data": {"type": "163", "id": "song-123"}},
    ]


def test_accept_format_only_advertises_tts_when_enabled():
    assert "tts_text" not in get_accept_format(False)
    assert "tts_text" in get_accept_format(True)
    # Koishi currently sends every normal outbound payload through send_msg;
    # OneBot forward nodes require send_forward_msg, so do not advertise it.
    assert "forward" not in get_accept_format(False)


def test_advertised_video_and_file_media_have_onebot_mappings():
    payload = seg_to_onebot(
        Seg(
            type="seglist",
            data=[
                Seg(type="video", data="YWJj"),
                Seg(type="videofile", data="C:/tmp/example.mp4"),
                Seg(type="videourl", data="https://example.invalid/video.mp4"),
                Seg(
                    type="file",
                    data={"url": "https://example.invalid/file.bin", "name": "file.bin"},
                ),
            ],
        ),
        _config(use_tts=False),
        Mock(),
    )

    assert payload == [
        {"type": "video", "data": {"file": "base64://YWJj"}},
        {"type": "video", "data": {"file": "file://C:/tmp/example.mp4"}},
        {"type": "video", "data": {"file": "https://example.invalid/video.mp4"}},
        {
            "type": "file",
            "data": {"file": "https://example.invalid/file.bin", "name": "file.bin"},
        },
    ]


def test_empty_local_media_does_not_create_placeholder_segment():
    payload = seg_to_onebot(
        Seg(type="seglist", data=[Seg(type="voicefile", data="")]),
        _config(use_tts=False),
        Mock(),
    )

    assert payload == []


def test_silk_decode_passes_pcm_to_ffmpeg_with_raw_pcm_flags():
    silk = b"\x02#!SILK_V3encoded"
    encoded = base64.b64encode(silk).decode("ascii")
    pcm = b"\x00\x01" * 4
    fake_rsilk = SimpleNamespace(decode=Mock(return_value=pcm))
    ffmpeg_result = SimpleNamespace(returncode=0, stdout=b"ogg", stderr=b"")

    with patch.dict(sys.modules, {"rsilk": fake_rsilk}), patch(
        "message_builder.resolve_ffmpeg_exe", return_value="ffmpeg"
    ), patch("message_builder.subprocess.run", return_value=ffmpeg_result) as run:
        result = convert_to_opus_data_url(encoded, _config(use_tts=False), Mock())

    assert result == "base64://b2dn"
    fake_rsilk.decode.assert_called_once_with(silk, tencent=True)
    command = run.call_args.args[0]
    assert ["-f", "s16le", "-ar", "24000", "-ac", "1"] == command[4:10]
    assert run.call_args.kwargs["input"] == pcm


def test_silk_decode_failure_drops_voice_without_running_ffmpeg():
    silk = b"\x02#!SILK_V3undecodable"
    encoded = base64.b64encode(silk).decode("ascii")
    fake_rsilk = SimpleNamespace(decode=Mock(side_effect=RuntimeError("bad silk")))
    logger = Mock()

    with patch.dict(sys.modules, {"rsilk": fake_rsilk}), patch(
        "message_builder.resolve_ffmpeg_exe", return_value="ffmpeg"
    ) as resolve_ffmpeg, patch("message_builder.subprocess.run") as run:
        discord_config = SimpleNamespace(platform="discord", use_tts=False)
        result = convert_to_opus_data_url(encoded, discord_config, logger)
        fallback = voice_to_record_file(encoded, discord_config, logger)
        stream_fallback = voice_to_record_file(
            encoded, discord_config, logger, stream=True
        )

    assert result is None
    assert fallback == ""
    assert stream_fallback == ""
    resolve_ffmpeg.assert_not_called()
    run.assert_not_called()
