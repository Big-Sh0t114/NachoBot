from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TRACKED_PLUGIN_FILES = (
    ROOT / "plugins/bilibili_video_sender_plugin/plugin.py",
    ROOT / "plugins/bilibili_video_sender_plugin/README.md",
    ROOT / "plugins/bilibili_video_sender_plugin/_manifest.json",
    ROOT / "plugins/mus_library/plugin.py",
    ROOT / "plugins/mus_library/README.md",
    ROOT / "plugins/mus_library/_manifest.json",
    ROOT / "plugins/diary_plugin/plugin.py",
    ROOT / "plugins/diary_plugin/README.md",
    ROOT / "plugins/diary_plugin/core/actions.py",
    ROOT / "plugins/diary_plugin/core/commands.py",
    ROOT / "plugins/diary_plugin/core/diary_service.py",
    ROOT / "plugins/diary_plugin/core/storage.py",
)
OPTIONAL_IGNORED_MAIZONE_FILES = (
    ROOT / "plugins/Maizone/plugin.py",
    ROOT / "plugins/Maizone/README.md",
    ROOT / "plugins/Maizone/actions.py",
    ROOT / "plugins/Maizone/commands.py",
    ROOT / "plugins/Maizone/cookie_manager.py",
    ROOT / "plugins/Maizone/scheduled_tasks.py",
)
PLUGIN_FILES = TRACKED_PLUGIN_FILES + tuple(
    path for path in OPTIONAL_IGNORED_MAIZONE_FILES if path.exists()
)


def test_migrated_plugins_have_no_direct_transport_configuration_or_routes():
    forbidden = (
        "send_group_msg",
        "send_private_msg",
        "upload_group_file",
        "/get_cookies",
        "onebot_base",
        "onebot_token",
        "nonebot_force_group_id",
        "napcat_host",
        "napcat_port",
        "napcat_token",
        "api.port",
    )
    for path in PLUGIN_FILES:
        text = path.read_text(encoding="utf-8")
        assert not any(marker.lower() in text.lower() for marker in forbidden), path


def test_cookie_callers_use_typed_core_capability_and_media_callers_use_receipts():
    diary = (ROOT / "plugins/diary_plugin/core/storage.py").read_text(encoding="utf-8")
    bilibili = (ROOT / "plugins/bilibili_video_sender_plugin/plugin.py").read_text(encoding="utf-8")
    music = (ROOT / "plugins/mus_library/plugin.py").read_text(encoding="utf-8")

    assert 'platform_api.get_platform_cookies("user.qzone.qq.com"' in diary
    assert "send_api.local_media_to_stream_receipt" in bilibili
    assert "send_api.custom_to_stream_receipt" in music
    assert "send_api.local_media_to_stream_receipt" in music

    maizone_cookie_manager = ROOT / "plugins/Maizone/cookie_manager.py"
    if maizone_cookie_manager.exists():
        maizone = maizone_cookie_manager.read_text(encoding="utf-8")
        assert 'platform_api.get_platform_cookies("user.qzone.qq.com"' in maizone


def test_ignored_maizone_source_is_present_in_the_scan():
    maizone_directory = ROOT / "plugins/Maizone"
    if not maizone_directory.exists():
        pytest.skip("optional ignored Maizone plugin is not present in this checkout")
    assert all(path.exists() for path in OPTIONAL_IGNORED_MAIZONE_FILES)
