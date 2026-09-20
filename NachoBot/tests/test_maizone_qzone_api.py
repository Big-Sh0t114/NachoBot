from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.Maizone.qzone_api import (  # noqa: E402
    QzoneAPI,
    QzoneAPIError,
    QzoneAuthError,
)
from plugins.Maizone.cookie_manager import (  # noqa: E402
    CookieRefreshError,
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
