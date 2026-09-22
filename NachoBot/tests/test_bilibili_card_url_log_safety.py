from __future__ import annotations

import unittest
from pathlib import Path

from plugins.bilibili_video_sender_plugin.log_safety import video_link_log_fields


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "plugins" / "bilibili_video_sender_plugin" / "plugin.py"


class BilibiliCardUrlLogSafetyTests(unittest.TestCase):
    def test_qq_card_log_fields_exclude_full_url(self) -> None:
        url = "https://www.bilibili.com/video/BV1SAFE123?token=synthetic-secret"

        fields = video_link_log_fields(url, "qq_card", video_id="BV1SAFE123")

        self.assertEqual(fields, {"source": "qq_card", "video_id": "BV1SAFE123"})
        self.assertNotIn(url, repr(fields))
        self.assertNotIn("synthetic-secret", repr(fields))

    def test_text_link_log_fields_preserve_existing_url_context(self) -> None:
        url = "https://www.bilibili.com/video/BV1TEXT123"

        fields = video_link_log_fields(url, "text", video_id="BV1TEXT123")

        self.assertEqual(fields, {"source": "text", "url": url})

    def test_plugin_disables_url_logging_for_card_extraction_and_uses_safe_fields(self) -> None:
        source = PLUGIN_PATH.read_text(encoding="utf-8")

        self.assertIn("find_first_bilibili_url(candidate, log_urls=False)", source)
        self.assertGreaterEqual(source.count("**video_link_log_fields("), 2)


if __name__ == "__main__":
    unittest.main()
