from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import types
from unittest.mock import AsyncMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def _stub_module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


_config_api = SimpleNamespace(get_global_config=lambda _key, default=None: default)
_platform_api = SimpleNamespace()
_logger = _stub_module("src.common.logger", get_logger=logging.getLogger)
_apis = _stub_module("src.plugin_system.apis", config_api=_config_api, platform_api=_platform_api)
_plugin_system = _stub_module("src.plugin_system")
_plugin_system.__path__ = [str(PROJECT_ROOT / "src" / "plugin_system")]
_chat = _stub_module("src.chat")
_chat.__path__ = [str(PROJECT_ROOT / "src" / "chat")]
_chat_utils = _stub_module("src.chat.utils")
_chat_utils.__path__ = [str(PROJECT_ROOT / "src" / "chat" / "utils")]
_utils_image = _stub_module("src.chat.utils.utils_image", get_image_manager=lambda: None)
_module_stubs = {
    "src.common.logger": _logger,
    "src.plugin_system": _plugin_system,
    "src.plugin_system.apis": _apis,
    "src.chat": _chat,
    "src.chat.utils": _chat_utils,
    "src.chat.utils.utils_image": _utils_image,
}

# Maizone import transitively loads Core configuration and services. These
# tests exercise the actual Maizone source against tiny API/log boundaries so
# they remain hermetic in a fresh checkout without generated local config.
with patch.dict(sys.modules, _module_stubs):
    from plugins.Maizone import cookie_manager, qzone_api  # noqa: E402
    from plugins.Maizone.qzone_api import (  # noqa: E402
        QzoneAPI,
        QzoneAPIError,
        QzoneAuthError,
        _is_qzone_emoji_url,
        _normalize_qzone_image_url,
    )
    from plugins.Maizone.cookie_manager import (  # noqa: E402
        CookieRefreshError,
        QzoneLogin,
        _build_check_sig_url,
        _extract_qr_login_tokens,
        parse_cookie_string,
        validate_qzone_cookies,
    )


def _run(coro):
    return asyncio.run(coro)


def _api(response) -> QzoneAPI:
    api = object.__new__(QzoneAPI)
    api.cookies = {"p_skey": "secret"}
    api.gtk2 = "123"
    api.uin = 10001
    api.qq_nickname = ""
    api.do = AsyncMock(return_value=response)
    return api


def test_qzone_auth_envelope_is_never_treated_as_an_empty_feed():
    response = SimpleNamespace(
        status_code=200,
        text='_preloadCallback({"code":-3000,"subcode":-4001,"message":"login"});',
    )
    api = _api(response)

    with pytest.raises(QzoneAuthError, match="登录态已失效"):
        _run(api.get_list("10002", 10))


def test_monitor_malformed_envelope_raises_instead_of_returning_error_feed():
    response = SimpleNamespace(status_code=200, text=json.dumps({"code": 0, "data": {}}))
    api = _api(response)

    with pytest.raises(QzoneAPIError, match="动态数据格式无效"):
        _run(api.monitor_get_list())


def test_direct_like_uses_feed_abstime_and_raises_on_qzone_failure():
    response = SimpleNamespace(status_code=200, text=json.dumps({"code": -10000, "message": "limited"}))
    api = _api(response)

    with pytest.raises(QzoneAPIError, match="code=-10000"):
        _run(api.like("tid-1", "10002", abstime=123456))

    sent_data = api.do.await_args.kwargs["data"]
    assert sent_data["abstime"] == 123456


def test_direct_comment_checks_callback_auth_status():
    response = SimpleNamespace(
        status_code=200,
        text='<script>frameElement.callback({"code":-3000,"subcode":-4001});</script>',
    )
    api = _api(response)

    with pytest.raises(QzoneAuthError, match="登录态已失效"):
        _run(api.comment("tid-2", "10002", "hello"))


def test_cookie_validation_rejects_incomplete_local_credentials():
    with pytest.raises(CookieRefreshError, match="p_skey"):
        validate_qzone_cookies({"uin": "10001"})
    with pytest.raises(CookieRefreshError, match="账号标识"):
        validate_qzone_cookies({"p_skey": "secret"})


def test_cookie_parser_preserves_equals_and_ignores_malformed_pairs():
    assert parse_cookie_string("p_skey=one=two; malformed; p_uin=o10001") == {
        "p_skey": "one=two",
        "p_uin": "o10001",
    }


def test_qr_login_token_parser_rejects_untrusted_signature_characters():
    assert _extract_qr_login_tokens(
        "https://ptlogin2.qzone.qq.com/check_sig?ptsigx=abc-123_~&uin=10001&other=value"
    ) == ("abc-123_~", "10001")
    assert _extract_qr_login_tokens(
        "https://ptlogin2.qzone.qq.com/check_sig?ptsigx=opaque%2F%2B%3D&uin=10001"
    ) == ("opaque/+=", "10001")
    with pytest.raises(CookieRefreshError, match="签名格式无效"):
        _extract_qr_login_tokens(
            "https://ptlogin2.qzone.qq.com/check_sig?ptsigx=good%0Aforged&uin=10001"
        )
    check_sig = _build_check_sig_url("opaque&uin=99999", "10001")
    assert "ptsigx=opaque%26uin%3D99999" in check_sig
    assert "&uin=99999" not in check_sig


def test_qr_login_retry_count_and_polling_delay_are_bounded():
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **_kwargs):
            calls.append(url)
            if url == cookie_manager.qrcode_url:
                return SimpleNamespace(headers={"Set-Cookie": "qrsig=token; Path=/"}, content=b"qr")
            return SimpleNamespace(text="二维码已失效", headers={})

    async def no_wait(_seconds):
        return None

    original_qrcode_path = cookie_manager.qrcode_path
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_manager.qrcode_path = str(Path(temp_dir) / "qr.png")
            with patch.object(cookie_manager.httpx, "AsyncClient", side_effect=lambda **_kwargs: FakeClient()), patch.object(
                cookie_manager.asyncio, "sleep", new=AsyncMock(side_effect=no_wait)
            ) as sleep:
                with pytest.raises(CookieRefreshError, match="3次尝试失败"):
                    _run(QzoneLogin().login_via_qrcode(max_timeout_times=100_000))
            assert calls.count(cookie_manager.qrcode_url) == cookie_manager.MAX_QR_LOGIN_ATTEMPTS
            assert sleep.await_count == cookie_manager.MAX_QR_LOGIN_ATTEMPTS
            assert cookie_manager.QR_LOGIN_POLL_INTERVAL_SECONDS == 2
    finally:
        cookie_manager.qrcode_path = original_qrcode_path


@pytest.mark.parametrize(
    "url",
    [
        "https://qzonestyle.gtimg.cn.evil.example/image.png",
        "https://evil.example/?next=qzonestyle.gtimg.cn",
        "https://qzonestyle.gtimg.cn@evil.example/image.png",
        "http://img.qpic.cn:8080/image.png",
    ],
)
def test_qzone_image_url_rejects_host_lookalikes_and_unexpected_ports(url):
    with pytest.raises(ValueError, match="unsupported Qzone image URL"):
        _normalize_qzone_image_url(url)


def test_qzone_image_url_accepts_official_hosts_and_filters_only_exact_emoji_host():
    assert _normalize_qzone_image_url("//a1.qpic.cn/photo.jpg").startswith("https://a1.qpic.cn/")
    assert _normalize_qzone_image_url("https://q1.qlogo.cn/avatar.png").startswith("https://q1.qlogo.cn/")
    assert _normalize_qzone_image_url("https://static.gtimg.com/photo.png").startswith("https://static.gtimg.com/")
    assert _is_qzone_emoji_url("http://qzonestyle.gtimg.cn/a.png")
    assert not _is_qzone_emoji_url("https://qzonestyle.gtimg.cn.evil.example/a.png")


def test_qzone_image_download_validates_each_redirect_before_following():
    api = _api(SimpleNamespace())
    requested = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, _method, url, **_kwargs):
            requested.append(url)
            return _FakeImageResponse(
                status_code=302,
                headers={"Location": "http://127.0.0.1/admin"},
            )

    async def scenario():
        with patch.object(qzone_api.httpx, "AsyncClient", side_effect=lambda **_kwargs: FakeClient()):
            with pytest.raises(ValueError, match="unsupported Qzone image URL"):
                await api.get_image_base64_by_url("https://a1.qpic.cn/photo.jpg")

    _run(scenario())
    assert requested == ["https://a1.qpic.cn/photo.jpg"]


class _FakeImageResponse:
    def __init__(self, *, status_code, headers, chunks=()):
        self.status_code = status_code
        self.headers = headers
        self.chunks = chunks
        self.consumed_chunks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_bytes(self):
        for chunk in self.chunks:
            self.consumed_chunks += 1
            yield chunk


def test_qzone_image_streaming_enforces_byte_limit_before_buffering_more():
    api = _api(SimpleNamespace())
    response = _FakeImageResponse(status_code=200, headers={}, chunks=(b"1234", b"5678", b"not-read"))

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, _method, _url, **_kwargs):
            return response

    async def scenario():
        with patch.object(qzone_api.httpx, "AsyncClient", side_effect=lambda **_kwargs: FakeClient()), patch.object(
            qzone_api, "_MAX_QZONE_IMAGE_BYTES", 4
        ):
            with pytest.raises(QzoneAPIError, match="超过大小限制"):
                await api.get_image_base64_by_url("https://a1.qpic.cn/photo.jpg")

    _run(scenario())
    assert response.consumed_chunks == 2


def test_qzone_image_streaming_checks_content_length_before_reading_body():
    api = _api(SimpleNamespace())
    response = _FakeImageResponse(status_code=200, headers={"Content-Length": "5"}, chunks=(b"1234",))

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, _method, _url, **_kwargs):
            return response

    async def scenario():
        with patch.object(qzone_api.httpx, "AsyncClient", side_effect=lambda **_kwargs: FakeClient()), patch.object(
            qzone_api, "_MAX_QZONE_IMAGE_BYTES", 4
        ):
            with pytest.raises(QzoneAPIError, match="超过大小限制"):
                await api.get_image_base64_by_url("https://a1.qpic.cn/photo.jpg")

    _run(scenario())
    assert response.consumed_chunks == 0


def test_qzone_image_streaming_returns_base64_for_bounded_body():
    api = _api(SimpleNamespace())
    response = _FakeImageResponse(status_code=200, headers={"Content-Length": "4"}, chunks=(b"im", b"ag"))

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, _method, _url, **_kwargs):
            return response

    async def scenario():
        with patch.object(qzone_api.httpx, "AsyncClient", side_effect=lambda **_kwargs: FakeClient()):
            return await api.get_image_base64_by_url("https://a1.qpic.cn/photo.jpg")

    assert _run(scenario()) == base64.b64encode(b"imag").decode("ascii")
