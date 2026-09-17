from __future__ import annotations

import asyncio
import base64

import pytest

from src.log_safety import safe_endpoint, safe_exception, summarize_payload
from src.snowluma_client import SnowLumaClient


def test_secret_safe_payload_summary_redacts_credentials_and_media() -> None:
    sentinel = "synthetic-token-sentinel"
    encoded = "QUJD" * 40
    rendered = summarize_payload(
        {
            "access_token": sentinel,
            "authorization": f"Bearer {sentinel}",
            "cookie": sentinel,
            "message": {"text": "synthetic message"},
            "binary_data_base64": encoded,
        }
    )

    assert sentinel not in rendered
    assert "<redacted>" in rendered
    assert "<base64" in rendered
    assert "synthetic message" in rendered
    raw_rendered = summarize_payload('{"access_token": "synthetic-token-sentinel"}')
    assert "synthetic-token-sentinel" not in raw_rendered


def test_json_string_payload_recursively_sanitizes_nested_media() -> None:
    encoded = base64.b64encode(
        b"unique-nested-json-base64-sentinel-" + b"x" * 160
    ).decode()
    rendered = summarize_payload(
        '{"message":{"image":{"data":"' + encoded + '"}}}'
    )

    assert encoded not in rendered
    assert "<base64" in rendered
    assert "ordinary diagnostic text" in summarize_payload("ordinary diagnostic text")


@pytest.mark.parametrize(
    "credential_key",
    ("api_key", "api-key", "apikey", "app_key", "app-key", "appkey", "auth_key", "auth-key", "authkey", "rkey", "sig", "signature"),
)
def test_common_credential_keys_are_redacted_on_all_text_paths(
    credential_key: str,
) -> None:
    sentinel = f"synthetic-{credential_key}-sentinel"

    payload_rendered = summarize_payload(
        {"nested": {credential_key: sentinel}}
    )
    json_rendered = summarize_payload(
        '{"nested":{"' + credential_key + '":"' + sentinel + '"}}'
    )
    endpoint_rendered = safe_endpoint(
        f"wss://example.test/media?{credential_key}={sentinel}&keep=1"
    )
    exception_rendered = safe_exception(
        RuntimeError(f"https://example.test/media?{credential_key}={sentinel}")
    )

    assert sentinel not in payload_rendered
    assert sentinel not in json_rendered
    assert sentinel not in endpoint_rendered
    assert sentinel not in exception_rendered


def test_short_signature_names_do_not_redact_ordinary_text() -> None:
    ordinary = "assign signal diagnostic text"

    assert ordinary in summarize_payload(ordinary)
    assert "assign=ordinary" in safe_endpoint(
        "wss://example.test/media?assign=ordinary&keep=1"
    )
    assert ordinary in safe_exception(RuntimeError(ordinary))


def test_safe_endpoint_drops_query_credentials() -> None:
    rendered = safe_endpoint(
        "wss://user:password@example.test/onebot?access_token=synthetic-token&keep=1#fragment"
    )

    assert rendered == "wss://example.test/onebot?keep=1"
    assert "synthetic-token" not in rendered
    assert "password" not in rendered


def test_action_error_and_exception_are_bounded_and_scrubbed() -> None:
    sentinel = "synthetic-token-sentinel"
    error = SnowLumaClient.action_error(
        {"status": "failed", "message": f"authorization={sentinel}"}
    )
    exception = safe_exception(RuntimeError(f"https://example.test/?token={sentinel}"))

    assert sentinel not in error
    assert sentinel not in exception
    assert len(error) <= 240


def test_event_callback_task_is_tracked_and_failure_is_consumed() -> None:
    async def scenario() -> None:
        async def callback(_payload: dict[str, object]) -> None:
            raise RuntimeError("synthetic callback failure")

        async def status(_online: bool, _account: str) -> None:
            return None

        client = SnowLumaClient(callback, status)
        await client._handle_text_payload('{"post_type":"message","message_type":"private","user_id":7}')
        assert client._event_tasks
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not client._event_tasks

    asyncio.run(scenario())
