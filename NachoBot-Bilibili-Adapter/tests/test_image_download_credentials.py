from __future__ import annotations

import base64
import unittest
from types import SimpleNamespace

from aiohttp import ClientSession, CookieJar, web
from bili_src.api.api import BilibiliApi
from yarl import URL


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class ImageDownloadCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.remote_runner = web.AppRunner(web.Application())
        self.remote_runner.app.router.add_get("/capture", self._capture_image)
        await self.remote_runner.setup()
        self.remote_site = web.TCPSite(self.remote_runner, "127.0.0.1", 0)
        await self.remote_site.start()
        remote_port = self.remote_site._server.sockets[0].getsockname()[1]
        self.remote_url = f"http://localhost:{remote_port}"

        self.api_runner = web.AppRunner(web.Application())
        self.api_runner.app.router.add_get("/redirect", self._redirect_to_remote)
        self.api_runner.app.router.add_get("/api", self._capture_authenticated_api)
        await self.api_runner.setup()
        self.api_site = web.TCPSite(self.api_runner, "127.0.0.1", 0)
        await self.api_site.start()
        api_port = self.api_site._server.sockets[0].getsockname()[1]
        self.api_url = f"http://localhost:{api_port}"

        self.api = BilibiliApi(
            SimpleNamespace(
                user_agent="NachoBot image test",
                sessdata="dummy-sessdata",
                bili_jct="dummy-csrf",
                buvid3="dummy-buvid3",
                buvid4="dummy-buvid4",
                dede_user_id="123456",
            ),
            _Logger(),
        )
        await self.api.start()
        assert self.api.session is not None
        # Force jar state for the local host. The production API session keeps
        # cookies for authenticated requests; image downloads must not share it.
        await self.api.session.close()
        self.api.session = ClientSession(cookie_jar=CookieJar(unsafe=True))
        self.api.session.cookie_jar.update_cookies(
            {"JAR_COOKIE": "jar-secret"}, response_url=URL(self.api_url)
        )
        self.api.session.cookie_jar.update_cookies(
            {"REMOTE_JAR_COOKIE": "remote-jar-secret"},
            response_url=URL(self.remote_url),
        )

    async def asyncTearDown(self):
        await self.api.close()
        await self.api_runner.cleanup()
        await self.remote_runner.cleanup()

    async def _capture_image(self, request: web.Request) -> web.Response:
        self.requests.append((request.path, dict(request.headers)))
        return web.Response(body=b"image-bytes")

    async def _redirect_to_remote(self, request: web.Request) -> web.Response:
        self.requests.append((request.path, dict(request.headers)))
        response = web.Response(status=302)
        response.headers["Location"] = f"{self.remote_url}/capture"
        response.set_cookie("REDIRECT_COOKIE", "redirect-secret")
        return response

    async def _capture_authenticated_api(self, request: web.Request) -> web.Response:
        self.requests.append((request.path, dict(request.headers)))
        return web.json_response({"code": 0, "data": {"ok": True}})

    async def test_external_image_and_redirect_are_anonymous(self):
        assert self.api.session is not None
        jar_headers = self.api.session.cookie_jar.filter_cookies(URL(self.api_url))
        self.assertIn("JAR_COOKIE=jar-secret", jar_headers.output())

        encoded = await self.api.fetch_base64(f"{self.api_url}/redirect")

        self.assertEqual(encoded, base64.b64encode(b"image-bytes").decode("ascii"))
        self.assertEqual(
            [path for path, _headers in self.requests], ["/redirect", "/capture"]
        )
        for path, headers in self.requests:
            with self.subTest(path=path):
                self.assertEqual(headers.get("User-Agent"), "NachoBot image test")
                self.assertEqual(
                    headers.get("Referer"), "https://message.bilibili.com/"
                )
                self.assertNotIn("Cookie", headers)
                self.assertNotIn("Authorization", headers)

    async def test_authenticated_json_requests_keep_configured_cookies(self):
        payload = await self.api.request_json("GET", f"{self.api_url}/api")

        self.assertEqual(payload, {"code": 0, "data": {"ok": True}})
        headers = self.requests[-1][1]
        cookie = headers.get("Cookie", "")
        self.assertIn("SESSDATA=dummy-sessdata", cookie)
        self.assertIn("bili_jct=dummy-csrf", cookie)
        self.assertIn("DedeUserID=123456", cookie)

    async def test_close_releases_both_http_sessions(self):
        await self.api.start()
        authenticated_session = self.api.session
        download_session = getattr(self.api, "download_session", None)

        await self.api.close()

        self.assertIsNotNone(authenticated_session)
        self.assertIsNotNone(download_session)
        self.assertTrue(authenticated_session.closed)
        self.assertTrue(download_session.closed)
        self.assertIsNone(self.api.session)
        self.assertIsNone(self.api.download_session)


if __name__ == "__main__":
    unittest.main()
