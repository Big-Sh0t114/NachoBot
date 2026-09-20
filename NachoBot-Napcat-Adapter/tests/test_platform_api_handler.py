from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CORE_ROOT = PROJECT_ROOT.parent / "NachoBot"
if str(CORE_ROOT) not in sys.path:
    sys.path.append(str(CORE_ROOT))

from src import platform_api_handler as handler  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _request(**overrides):
    content = {
        "version": 1,
        "request_id": "A" * 32,
        "operation": "get_platform_cookies",
        "platform": "qq",
        "params": {"domain": "example.com"},
    }
    content.update(overrides)
    return {"platform": "qq", "content": content}


def test_request_validation_is_allowlisted_and_schema_strict(monkeypatch):
    monkeypatch.setattr(handler, "_local_platform", lambda: "qq")
    assert handler._validate_request(_request()) == ("A" * 32, "get_platform_cookies", {"domain": "example.com"})
    assert handler._validate_request(_request(operation="arbitrary_action")) is None
    assert handler._validate_request(_request(params={"domain": "example.com", "extra": "x"})) is None
    assert handler._validate_request({"platform": "other", "content": _request()["content"]}) is None


def test_handler_maps_validated_operation_without_logging_payload(monkeypatch):
    monkeypatch.setattr(handler, "_local_platform", lambda: "qq")
    calls = []
    responses = []

    async def fake_upstream(action, params):
        calls.append((action, params))
        return {"status": "ok", "data": {"cookies": "sid=secret=value"}}

    async def fake_response(request_id, operation, status, *, cookies=None, error_code=None):
        responses.append((request_id, operation, status, cookies, error_code))

    monkeypatch.setattr(handler.nc_message_sender, "send_message_to_napcat", fake_upstream)
    monkeypatch.setattr(handler, "_send_response", fake_response)

    async def scenario():
        await handler.handle_platform_api_request(_request())

    _run(scenario())
    assert calls == [("get_cookies", {"domain": "example.com"})]
    assert responses == [("A" * 32, "get_platform_cookies", "ok", "sid=secret=value", None)]


@pytest.mark.parametrize(
    "upstream",
    [None, {"status": "error"}, {"status": "ok", "data": {}}, {"status": "ok", "data": {"cookies": []}}],
)
def test_malformed_upstream_is_reduced_to_generic_error(monkeypatch, upstream):
    monkeypatch.setattr(handler, "_local_platform", lambda: "qq")
    responses = []

    async def fake_upstream(*_args, **_kwargs):
        return upstream

    async def fake_response(request_id, operation, status, *, cookies=None, error_code=None):
        responses.append((request_id, operation, status, cookies, error_code))

    monkeypatch.setattr(handler.nc_message_sender, "send_message_to_napcat", fake_upstream)
    monkeypatch.setattr(handler, "_send_response", fake_response)
    _run(handler.handle_platform_api_request(_request()))
    assert responses == [("A" * 32, "get_platform_cookies", "error", None, "upstream_error")]


def test_qzone_write_operations_are_explicitly_unsupported(monkeypatch):
    monkeypatch.setattr(handler, "_local_platform", lambda: "qq")
    responses = []

    async def fake_response(request_id, operation, status, *, cookies=None, error_code=None):
        responses.append((request_id, operation, status, cookies, error_code))

    monkeypatch.setattr(handler, "_send_response", fake_response)
    request = _request(
        operation="like_qzone",
        params={"tid": "tid-1", "target_uin": 12345, "abstime": 0},
    )
    _run(handler.handle_platform_api_request(request))
    assert responses == [("A" * 32, "like_qzone", "error", None, "unsupported_operation")]


def test_invalid_request_never_reaches_upstream(monkeypatch):
    monkeypatch.setattr(handler, "_local_platform", lambda: "qq")
    called = False

    async def fake_upstream(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "ok", "data": {"cookies": "ignored"}}

    monkeypatch.setattr(handler.nc_message_sender, "send_message_to_napcat", fake_upstream)
    responses = []

    async def fake_response(request_id, operation, status, *, cookies=None, error_code=None):
        responses.append((request_id, operation, status, error_code))

    monkeypatch.setattr(handler, "_send_response", fake_response)
    _run(handler.handle_platform_api_request(_request(request_id="short")))
    assert not called
    assert responses == []


def test_invalid_correlated_request_returns_error_instead_of_timing_out(monkeypatch):
    monkeypatch.setattr(handler, "_local_platform", lambda: "qq")
    responses = []

    async def fake_response(request_id, operation, status, *, cookies=None, error_code=None):
        responses.append((request_id, operation, status, error_code))

    monkeypatch.setattr(handler, "_send_response", fake_response)
    request = _request(params={"domain": "example.com", "extra": "x"})
    _run(handler.handle_platform_api_request(request))
    assert responses == [("A" * 32, "get_platform_cookies", "error", "invalid_request")]


@pytest.mark.parametrize("version", [None, 2])
def test_omitted_or_wrong_request_version_never_reaches_upstream(monkeypatch, version):
    monkeypatch.setattr(handler, "_local_platform", lambda: "qq")
    called = False

    async def fake_upstream(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "ok", "data": {"cookies": "ignored"}}

    monkeypatch.setattr(handler.nc_message_sender, "send_message_to_napcat", fake_upstream)
    request = _request()
    if version is None:
        request["content"].pop("version")
    else:
        request["content"]["version"] = version

    _run(handler.handle_platform_api_request(request))
    assert not called
