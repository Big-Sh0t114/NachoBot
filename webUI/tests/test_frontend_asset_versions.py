import asyncio
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import server  # noqa: E402


class FrontendAssetVersionTests(unittest.TestCase):
    def test_frontend_urls_change_with_content_even_if_timestamps_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            static = Path(temp)
            script = static / "js" / "setup.js"
            script.parent.mkdir()
            script.write_text("old-code", encoding="utf-8")
            (static / "index.html").write_text('<script src="/static/js/setup.js"></script>', encoding="utf-8")
            before_stat = script.stat()
            with patch.object(server, "STATIC_DIR", static):
                first = asyncio.run(server.index())
                script.write_text("new-code", encoding="utf-8")
                os.utime(script, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
                second = asyncio.run(server.index())
            self.assertEqual(first.headers["cache-control"], "no-store")
            self.assertNotEqual(first.body, second.body)
            self.assertRegex(second.body.decode(), r'/static/js/setup.js\?v=[a-f0-9]{16}')

    def test_current_page_versions_every_first_party_script(self):
        response = asyncio.run(server.index())
        html = response.body.decode()
        scripts = re.findall(r'<script\s+src="(/static/js/[^"?]+\.js)([^"]*)"', html)
        self.assertGreater(len(scripts), 10)
        self.assertTrue(all(re.fullmatch(r"\?v=[a-f0-9]{16}", query) for _, query in scripts))


if __name__ == "__main__":
    unittest.main()
