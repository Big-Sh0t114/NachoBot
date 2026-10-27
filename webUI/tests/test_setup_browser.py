"""Real DOM startup checks with APIs intercepted; no services or accounts are changed."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from urllib.parse import urlsplit

WEBUI_DIR = Path(__file__).resolve().parents[1]
HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


@unittest.skipUnless(HAS_PLAYWRIGHT, "run with an existing Playwright test interpreter")
class SetupBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        renderer = WEBUI_DIR / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        result = subprocess.run(
            [str(renderer), "-c", "import asyncio,sys,server; sys.stdout.buffer.write(asyncio.run(server.index()).body)"],
            cwd=WEBUI_DIR, capture_output=True, check=True,
        )
        cls.html = result.stdout.decode("utf-8")

    def check_startup(self, *, stalled_metadata):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                if stalled_metadata:
                    page.add_init_script("""(() => {
                        const nativeFetch = window.fetch.bind(window);
                        window.fetch = (input, options = {}) => {
                            if (String(input).endsWith('/api/webui/info')) {
                                return new Promise((resolve, reject) => {
                                    options.signal.addEventListener('abort', () =>
                                        reject(new DOMException('metadata stalled', 'AbortError')));
                                });
                            }
                            return nativeFetch(input, options);
                        };
                    })();""")
                errors, checks, scripts = [], [], []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def respond(route):
                    url = urlsplit(route.request.url)
                    if url.path == "/":
                        route.fulfill(status=200, content_type="text/html", body=self.html)
                    elif url.path.startswith("/api/"):
                        if url.path == "/api/webui/info":
                            data = {"version": "test"}
                        elif url.path == "/api/setup/check":
                            checks.append(url.path)
                            data = {"python": {"status": "ok", "message": "Test Python"},
                                    "git": {"status": "ok", "message": "Test Git"},
                                    "docker": {"status": "warning", "message": "Optional"},
                                    "ports": [], "configs": []}
                        else:
                            data = {}
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))
                    elif url.path.startswith("/static/") and url.path.endswith((".js", ".css")):
                        if url.path.startswith("/static/js/"):
                            scripts.append(url.query)
                        asset = WEBUI_DIR / "static" / url.path.removeprefix("/static/")
                        if asset.is_file():
                            route.fulfill(path=asset)
                        else:
                            route.fulfill(status=404)
                    else:
                        route.fulfill(status=204)

                page.route("**/*", respond)
                page.goto("http://webui.test/", wait_until="domcontentloaded")
                if stalled_metadata:
                    self.assertIn("正在准备环境检测", page.locator("#setup-check-list").inner_text())
                page.wait_for_function(
                    "document.getElementById('setup-check-list').innerText.includes('Test Python')", timeout=9000,
                )
                self.assertEqual(len(checks), 1)
                self.assertFalse(errors, errors)
                self.assertTrue(scripts)
                self.assertTrue(all(query.startswith("v=") for query in scripts))
            finally:
                browser.close()

    def test_real_initial_page_starts_environment_check_without_navigation(self):
        self.check_startup(stalled_metadata=False)

    def test_stalled_version_request_does_not_leave_the_wizard_uninitialized(self):
        self.check_startup(stalled_metadata=True)


if __name__ == "__main__":
    unittest.main()
