from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

try:
    from webUI.snowluma_credentials import (
        SnowLumaPasswordStore,
        SnowLumaSecretStoreUnavailable,
    )
    from webUI import setup_deployment
    from webUI import snowluma_manager as snowluma
    from webUI import snowluma_locator as locator
except ModuleNotFoundError:  # pytest's webUI project root on Windows
    from snowluma_credentials import (  # type: ignore
        SnowLumaPasswordStore,
        SnowLumaSecretStoreUnavailable,
    )
    import setup_deployment  # type: ignore
    import snowluma_manager as snowluma  # type: ignore
    import snowluma_locator as locator  # type: ignore


TOKEN = "snowluma-token-123456"
PASSWORD = "GoodPassword!1"


def _write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _prepare_runtime(
    root: Path,
    *,
    version: str = "1.14.17",
    runtime_name: str = "SnowLuma",
) -> None:
    for relative in snowluma.REQUIRED_SNOWLUMA_RUNTIME_FILES:
        _write(root, f"{runtime_name}/{relative}", b"runtime")
    _write(
        root,
        f"{runtime_name}/package.json",
        json.dumps({"name": "snowluma", "version": version}),
    )
    _write(
        root,
        f"{runtime_name}/config/runtime.json",
        json.dumps({"webuiHost": "127.0.0.1", "webuiPort": 5099}),
    )
    _write(
        root,
        "NachoBot-SnowLuma-Adapter/main.py",
        "print('adapter')\n",
    )
    _write(root, "NachoBot-SnowLuma-Adapter/pyproject.toml", "[project]\nname='adapter'\n")
    _write(
        root,
        "NachoBot-SnowLuma-Adapter/config.toml",
        """
[snowluma]
host = "127.0.0.1"
port = 3001
path = "/"
scheme = "ws"
token = "snowluma-token-123456"

[nachobot_server]
host = "127.0.0.1"
port = 8123

[voice]
enabled = false
""",
    )
    _write(
        root,
        f"{runtime_name}/config/onebot_123456.json",
        json.dumps(
            {
                "networks": {
                    "wsServers": [
                        {
                            "enabled": True,
                            "host": "127.0.0.1",
                            "port": 3001,
                            "path": "/",
                            "accessToken": TOKEN,
                        }
                    ]
                }
            }
        ),
    )
    _write(root, "NachoBot/.env", "HOST=127.0.0.1\n# keep this\nqq_adapter=snowluma\nOTHER=value\n")


def _patch_safe_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snowluma, "_port_listening", lambda _port: False)


def _write_onebot_servers(root: Path, account: str, servers: list[dict[str, object]]) -> None:
    _write(
        root,
        f"SnowLuma/config/onebot_{account}.json",
        json.dumps({"networks": {"wsServers": servers}}),
    )


def _credential_statuses(root: Path) -> tuple[dict[str, object], dict[str, object]]:
    return (
        snowluma.SnowLumaManager.credential_consistency(root),
        locator.credential_consistency(root),
    )


def test_password_store_fake_protector_round_trip_and_failed_replace_preserves_old(
    tmp_path: Path,
) -> None:
    store = SnowLumaPasswordStore(
        tmp_path,
        protect=lambda value: b"cipher:" + value[::-1],
        unprotect=lambda value: value.removeprefix(b"cipher:")[::-1],
    )
    store.save(PASSWORD)
    original = store.path.read_bytes()
    assert store.load() == PASSWORD
    assert PASSWORD.encode() not in original

    failing = SnowLumaPasswordStore(
        tmp_path,
        protect=lambda _value: (_ for _ in ()).throw(RuntimeError("injected protector failure")),
        unprotect=lambda value: value,
    )
    with pytest.raises(SnowLumaSecretStoreUnavailable):
        failing.save("ReplacementPassword!1")
    assert store.path.read_bytes() == original


def test_locator_accepts_versioned_release_without_node_executable(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path, runtime_name="SnowLuma-v1.14.17-win-x64")
    runtime = locator.resolve_snowluma_runtime(tmp_path)
    assert runtime.name == "SnowLuma-v1.14.17-win-x64"
    assert runtime.version == "1.14.17"
    assert snowluma.SnowLumaManager.runtime_path(tmp_path) == runtime.path
    assert "node.exe" not in snowluma.REQUIRED_SNOWLUMA_RUNTIME_FILES
    assert snowluma.SnowLumaManager.required_components(tmp_path, "snowluma") == []


def test_locator_fails_closed_for_exact_and_versioned_ambiguity(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path, runtime_name="SnowLuma")
    _prepare_runtime(tmp_path, runtime_name="SnowLuma-v1.14.17-win-x64")
    with pytest.raises(locator.SnowLumaLocatorError, match="候选不唯一") as raised:
        locator.resolve_snowluma_runtime(tmp_path)
    message = str(raised.value)
    assert "SnowLuma" in message
    assert "SnowLuma-v1.14.17-win-x64" in message
    assert str(tmp_path) not in message


def test_locator_fails_closed_for_multiple_versioned_releases(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path, version="1.14.16", runtime_name="SnowLuma-v1.14.16-win-x64")
    _prepare_runtime(tmp_path, version="1.14.17", runtime_name="SnowLuma-v1.14.17-win-x64")
    with pytest.raises(locator.SnowLumaLocatorError) as raised:
        locator.resolve_snowluma_runtime(tmp_path)
    assert raised.value.code == "ambiguous"
    assert "SnowLuma-v1.14.16-win-x64" in raised.value.message
    assert "SnowLuma-v1.14.17-win-x64" in raised.value.message


def test_locator_fails_closed_for_directory_package_version_mismatch(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path, version="1.14.17", runtime_name="SnowLuma-v1.14.16-win-x64")
    with pytest.raises(locator.SnowLumaLocatorError, match="未找到"):
        locator.resolve_snowluma_runtime(tmp_path)


def test_credential_consistency_is_redacted_and_rejects_mismatch_before_launch(
    tmp_path: Path,
) -> None:
    _prepare_runtime(tmp_path)
    status = snowluma.SnowLumaManager.credential_consistency(tmp_path)
    assert status["status"] == "ok"
    assert status["consistent"] is True
    assert TOKEN not in repr(status)

    onebot = snowluma.SnowLumaManager.runtime_path(tmp_path) / "config/onebot_123456.json"
    document = json.loads(onebot.read_text(encoding="utf-8"))
    document["networks"]["wsServers"][0]["accessToken"] = "different-token-123456"
    onebot.write_text(json.dumps(document), encoding="utf-8")
    status = snowluma.SnowLumaManager.credential_consistency(tmp_path)
    assert status["status"] == "mismatch"
    assert status["consistent"] is False
    assert TOKEN not in repr(status)
    with pytest.raises(snowluma.SnowLumaError, match="重新部署"):
        snowluma.SnowLumaManager.validate_launch_boundary(tmp_path)


def test_legacy_multi_account_correct_and_stale_is_ok_for_manager_and_locator(
    tmp_path: Path,
) -> None:
    _prepare_runtime(tmp_path)
    _write_onebot_servers(
        tmp_path,
        "999999",
        [
            {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 3001,
                "path": "/",
                "accessToken": "stale-token-123456",
            }
        ],
    )

    manager_status, locator_status = _credential_statuses(tmp_path)
    assert manager_status["status"] == locator_status["status"] == "ok"
    assert manager_status["consistent"] is locator_status["consistent"] is True
    assert TOKEN not in repr(manager_status)
    assert TOKEN not in repr(locator_status)


def test_authoritative_account_mismatch_wins_over_another_matching_account(
    tmp_path: Path,
) -> None:
    _prepare_runtime(tmp_path)
    adapter_path = tmp_path / "NachoBot-SnowLuma-Adapter/config.toml"
    adapter_path.write_text(
        adapter_path.read_text(encoding="utf-8").replace(
            'scheme = "ws"\n', 'scheme = "ws"\nqq_account = "123456"\n'
        ),
        encoding="utf-8",
    )
    _write_onebot_servers(
        tmp_path,
        "123456",
        [
            {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 3001,
                "path": "/",
                "accessToken": "stale-token-123456",
            },
            {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 3001,
                "path": "/",
                "accessToken": TOKEN,
            },
        ],
    )
    _write_onebot_servers(
        tmp_path,
        "999999",
        [
            {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 3001,
                "path": "/",
                "accessToken": TOKEN,
            }
        ],
    )

    manager_status, locator_status = _credential_statuses(tmp_path)
    assert manager_status["status"] == locator_status["status"] == "mismatch"
    assert manager_status["consistent"] is locator_status["consistent"] is False
    assert "123456" not in repr(manager_status)
    assert "123456" not in repr(locator_status)
    assert TOKEN not in repr(manager_status)
    assert TOKEN not in repr(locator_status)


def test_authoritative_account_correct_ignores_unrelated_stale_account(
    tmp_path: Path,
) -> None:
    _prepare_runtime(tmp_path)
    adapter_path = tmp_path / "NachoBot-SnowLuma-Adapter/config.toml"
    adapter_path.write_text(
        adapter_path.read_text(encoding="utf-8").replace(
            'scheme = "ws"\n', 'scheme = "ws"\nqq_account = "123456"\n'
        ),
        encoding="utf-8",
    )
    _write_onebot_servers(
        tmp_path,
        "999999",
        [
            {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 3001,
                "path": "/",
                "accessToken": "stale-token-123456",
            }
        ],
    )

    manager_status, locator_status = _credential_statuses(tmp_path)
    assert manager_status["status"] == locator_status["status"] == "ok"
    assert manager_status["consistent"] is locator_status["consistent"] is True
    assert TOKEN not in repr(manager_status)
    assert TOKEN not in repr(locator_status)


def test_invalid_authority_metadata_fails_closed_for_manager_and_locator(
    tmp_path: Path,
) -> None:
    _prepare_runtime(tmp_path)
    adapter_path = tmp_path / "NachoBot-SnowLuma-Adapter/config.toml"
    adapter_path.write_text(
        adapter_path.read_text(encoding="utf-8").replace(
            'scheme = "ws"\n', 'scheme = "ws"\nqq_account = "not-a-qq"\n'
        ),
        encoding="utf-8",
    )

    manager_status, locator_status = _credential_statuses(tmp_path)
    assert manager_status["status"] == locator_status["status"] == "missing"
    assert manager_status["consistent"] is locator_status["consistent"] is False
    assert TOKEN not in repr(manager_status)
    assert TOKEN not in repr(locator_status)


def test_manager_and_locator_reject_wildcard_credential_endpoint_equally(
    tmp_path: Path,
) -> None:
    _prepare_runtime(tmp_path)
    adapter_path = tmp_path / "NachoBot-SnowLuma-Adapter/config.toml"
    adapter_path.write_text(
        adapter_path.read_text(encoding="utf-8").replace(
            'host = "127.0.0.1"\n', 'host = "0.0.0.0"\n', 1
        ),
        encoding="utf-8",
    )

    manager_status, locator_status = _credential_statuses(tmp_path)
    assert manager_status["status"] == locator_status["status"] == "missing"
    assert manager_status["consistent"] is locator_status["consistent"] is False
    assert manager_status["endpoint"] is locator_status["endpoint"] is None


def test_implicit_enabled_entry_matches_snowluma_runtime_semantics(
    tmp_path: Path,
) -> None:
    _prepare_runtime(tmp_path)
    onebot_path = tmp_path / "SnowLuma/config/onebot_123456.json"
    document = json.loads(onebot_path.read_text(encoding="utf-8"))
    document["networks"]["wsServers"][0].pop("enabled")
    onebot_path.write_text(json.dumps(document), encoding="utf-8")

    manager_status, locator_status = _credential_statuses(tmp_path)
    assert manager_status["status"] == locator_status["status"] == "ok"
    assert manager_status["consistent"] is locator_status["consistent"] is True


def test_disabled_entries_never_satisfy_or_veto_credential_consistency(
    tmp_path: Path,
) -> None:
    _prepare_runtime(tmp_path)
    disabled_stale = {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 3001,
        "path": "/",
        "accessToken": "stale-token-123456",
    }
    _write_onebot_servers(tmp_path, "123456", [disabled_stale])
    _write_onebot_servers(tmp_path, "999999", [dict(disabled_stale)])

    manager_status, locator_status = _credential_statuses(tmp_path)
    assert manager_status["status"] == locator_status["status"] == "missing"
    assert manager_status["consistent"] is locator_status["consistent"] is False

    _write_onebot_servers(
        tmp_path,
        "999999",
        [
            {
                **disabled_stale,
                "enabled": True,
                "accessToken": TOKEN,
            }
        ],
    )
    manager_status, locator_status = _credential_statuses(tmp_path)
    assert manager_status["status"] == locator_status["status"] == "ok"
    assert manager_status["consistent"] is locator_status["consistent"] is True


def test_transport_status_distinguishes_closed_onebot_from_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_runtime(tmp_path)
    monkeypatch.setattr(snowluma, "_port_listening", lambda _port: False)
    status = snowluma.SnowLumaManager.transport_status(tmp_path)
    assert status["transport"] == "webui_not_listening"
    assert status["webui"] == "not_listening"
    assert status["onebot"] == "not_listening"
    assert status["credential_consistency"]["status"] == "ok"


def test_installation_status_has_official_url_and_missing_components(tmp_path: Path) -> None:
    status = snowluma.SnowLumaManager.installation_status(tmp_path, "snowluma")
    assert status["installed"] is False
    assert status["download_url"] == snowluma.SNOWLUMA_RELEASE_URL
    assert any("SnowLuma Runtime" in item for item in status["missing"])


def test_synchronize_writes_real_three_file_schema_and_preserves_generated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_runtime(tmp_path)
    _patch_safe_sync(monkeypatch)
    manager = SimpleNamespace(states={})

    with mock.patch.object(snowluma.secrets, "token_bytes", return_value=b"0123456789abcdef"):
        result = snowluma.SnowLumaManager.synchronize(
            tmp_path,
            "123456",
            TOKEN,
            PASSWORD,
            process_manager=manager,
        )

    assert result.status == "ok"
    assert TOKEN not in result.as_dict().__repr__()
    assert all("snowluma_webui_password.dpapi" not in item for item in result.files)
    assert all("snowluma_webui_password.dpapi" not in item for item in result.backups)
    secret_path = tmp_path / ".runtime/secrets/snowluma_webui_password.dpapi"
    assert secret_path.is_file()
    assert PASSWORD.encode() not in secret_path.read_bytes()
    adapter = (tmp_path / "NachoBot-SnowLuma-Adapter/config.toml").read_text(encoding="utf-8")
    assert 'host = "127.0.0.1"' in adapter
    assert 'path = "/"' in adapter
    assert 'scheme = "ws"' in adapter
    assert 'qq_account = "123456"' in adapter
    assert f'token = "{TOKEN}"' in adapter
    runtime = snowluma.SnowLumaManager.runtime_path(tmp_path)
    onebot = json.loads((runtime / "config/onebot_123456.json").read_text(encoding="utf-8"))
    entry = onebot["networks"]["wsServers"][0]
    assert entry["enabled"] is True
    assert "enable" not in entry
    assert entry["host"] == "127.0.0.1"
    assert entry["port"] == 3001
    assert entry["path"] == "/"
    assert entry["role"] == "Universal"
    assert entry["accessToken"] == TOKEN

    webui_path = runtime / "config/webui.json"
    first = json.loads(webui_path.read_text(encoding="utf-8"))
    assert len(bytes.fromhex(first["passwordSalt"])) == 16
    assert len(bytes.fromhex(first["passwordHash"])) == 64
    assert first["mustChangePassword"] is False
    expected = hashlib.scrypt(
        PASSWORD.encode(),
        salt=bytes.fromhex(first["passwordSalt"]),
        n=16384,
        r=8,
        p=1,
        dklen=64,
    ).hex()
    assert first["passwordHash"] == expected
    generated_at = first["generatedAt"]

    snowluma.SnowLumaManager.synchronize(
        tmp_path,
        "123456",
        TOKEN,
        PASSWORD,
        process_manager=manager,
    )
    second = json.loads(webui_path.read_text(encoding="utf-8"))
    assert second["generatedAt"] == generated_at


@pytest.mark.parametrize(
    ("version", "token", "password", "message"),
    [
        ("1.14.17", "short", PASSWORD, "至少需要 16"),
        ("1.14.17", TOKEN, "ALLUPPERCASE!", "小写"),
    ],
)
def test_synchronize_rejects_version_and_credential_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    token: str,
    password: str,
    message: str,
) -> None:
    _prepare_runtime(tmp_path, version=version)
    _patch_safe_sync(monkeypatch)
    with pytest.raises(snowluma.SnowLumaSynchronizationError, match=message):
        snowluma.SnowLumaManager.synchronize(tmp_path, "123456", token, password)


def test_synchronize_rejects_running_and_totp_or_malformed_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_runtime(tmp_path)
    _patch_safe_sync(monkeypatch)
    running = SimpleNamespace(
        states={"snowluma_runtime": SimpleNamespace(status="running")}
    )
    with pytest.raises(snowluma.SnowLumaSynchronizationError, match="正在运行"):
        snowluma.SnowLumaManager.synchronize(tmp_path, "123456", TOKEN, PASSWORD, process_manager=running)

    webui = snowluma.SnowLumaManager.runtime_path(tmp_path) / "config/webui.json"
    _write(
        tmp_path,
        str(webui.relative_to(tmp_path)),
        json.dumps(
            {
                "passwordHash": "0" * 128,
                "passwordSalt": "0" * 32,
                "mustChangePassword": False,
                "generatedAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
                "totp": {"enabled": True},
            }
        ),
    )
    with pytest.raises(snowluma.SnowLumaSynchronizationError, match="TOTP"):
        snowluma.SnowLumaManager.synchronize(tmp_path, "123456", TOKEN, PASSWORD)
    assert webui.exists()


def test_synchronize_rolls_back_target_that_throws_after_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_runtime(tmp_path)
    _patch_safe_sync(monkeypatch)
    target = snowluma.SnowLumaManager.runtime_path(tmp_path) / "config/onebot_123456.json"
    original_adapter = (tmp_path / "NachoBot-SnowLuma-Adapter/config.toml").read_bytes()
    original_target = b'{"old": true}\n'
    target.write_bytes(original_target)
    original_webui = None
    real_atomic = snowluma._atomic_write
    failed = False

    def fail_once(path: Path, payload: bytes) -> None:
        nonlocal failed
        real_atomic(path, payload)
        if path == target and not failed:
            failed = True
            raise OSError("injected commit failure")

    monkeypatch.setattr(snowluma, "_atomic_write", fail_once)
    with pytest.raises(snowluma.SnowLumaSynchronizationError, match="已回滚"):
        snowluma.SnowLumaManager.synchronize(tmp_path, "123456", TOKEN, PASSWORD)
    assert (tmp_path / "NachoBot-SnowLuma-Adapter/config.toml").read_bytes() == original_adapter
    assert target.read_bytes() == original_target
    assert original_webui is None or (tmp_path / "SnowLuma/config/webui.json").read_bytes() == original_webui


def test_api_client_uses_list_fields_without_probing_and_sanitizes_fields(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path)
    client = snowluma.SnowLumaAPIClient(tmp_path)
    client._token = "session-token"
    with mock.patch.object(
        client,
        "_request",
        return_value={
            "list": [
                {
                    "pid": 123,
                    "name": "QQ.exe",
                    "path": "C:/QQ.exe",
                    "injected": True,
                    "uin": "10001",
                    "loggedIn": True,
                    "secret": "must-not-leak",
                }
            ]
        },
    ) as request:
        with mock.patch.object(client, "probe_login", side_effect=AssertionError("list must not probe")):
            processes = client.list_processes()
    assert processes == [
        {
            "pid": 123,
            "name": "QQ.exe",
            "path": "C:/QQ.exe",
            "injected": True,
            "connected": False,
            "loggedIn": True,
            "uin": "10001",
            "status": "",
            "error": "",
            "method": "",
        }
    ]
    assert "secret" not in json.dumps(processes)
    request.assert_called_once_with("GET", "/api/processes")


@pytest.mark.parametrize(
    "payload",
    (
        {
            "success": True,
            "process": {
                "status": "connecting",
                "injected": False,
                "error": "unload verification failed: pipe still up",
            },
        },
        [],
        {"success": True},
        {"success": True, "process": []},
        {"success": True, "process": {"status": "available"}},
        {"success": True, "process": {"status": "available", "injected": None}},
        {"success": True, "process": {"status": "available", "injected": 0}},
        {"process": {"status": "available", "injected": False}},
        {"success": True, "process": {"status": " Available", "injected": False}},
        {"success": True, "process": {"status": "available ", "injected": False}},
        {"success": True, "process": {"status": "AVAILABLE", "injected": False}},
    ),
    ids=(
        "connecting",
        "non_mapping_payload",
        "missing_process",
        "non_mapping_process",
        "missing_injected",
        "null_injected",
        "numeric_injected",
        "missing_success",
        "leading_space_status",
        "trailing_space_status",
        "case_variant_status",
    ),
)
def test_api_client_unload_requires_verified_terminal_state(
    tmp_path: Path, payload: object
) -> None:
    _prepare_runtime(tmp_path)
    client = snowluma.SnowLumaAPIClient(tmp_path)
    client._token = "session-token"
    with mock.patch.object(client, "_request", return_value=payload) as request:
        with pytest.raises(snowluma.SnowLumaApiError, match="未能解除注入") as raised:
            client.process_action(123, "unload")
    assert "pipe still up" not in str(raised.value)
    assert raised.value.code == snowluma.SNOWLUMA_UNLOAD_VERIFICATION_FAILED
    assert raised.value.http_status == 502
    request.assert_called_once_with("POST", "/api/processes/123/unload", {})


def test_api_client_unload_accepts_verified_terminal_state(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path)
    client = snowluma.SnowLumaAPIClient(tmp_path)
    client._token = "session-token"
    payload = {
        "success": True,
        "process": {"status": "available", "injected": False},
    }
    with mock.patch.object(client, "_request", return_value=payload) as request:
        assert client.process_action(123, "unload") == {
            "status": "ok",
            "pid": 123,
            "action": "unload",
        }
    request.assert_called_once_with("POST", "/api/processes/123/unload", {})


@pytest.mark.parametrize("action", ("load", "refresh"))
def test_api_client_non_unload_actions_keep_success_contract(
    tmp_path: Path, action: str
) -> None:
    _prepare_runtime(tmp_path)
    client = snowluma.SnowLumaAPIClient(tmp_path)
    client._token = "session-token"
    with mock.patch.object(client, "_request", return_value={"success": True}) as request:
        assert client.process_action(123, action) == {
            "status": "ok",
            "pid": 123,
            "action": action,
        }
    request.assert_called_once_with("POST", f"/api/processes/123/{action}", {})


def test_api_client_probe_login_uses_get_transport_contract(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path)
    client = snowluma.SnowLumaAPIClient(tmp_path)
    client._token = "session-token"
    with mock.patch.object(
        client,
        "_request",
        return_value={"info": {"uin": "10001", "loggedIn": True}},
    ) as transport:
        assert client.probe_login(123) == {"uin": "10001", "loggedIn": True}
    transport.assert_called_once_with("GET", "/api/processes/123/probe-login")


def test_api_client_logout_uses_empty_body_and_clears_session(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path)
    client = snowluma.SnowLumaAPIClient(tmp_path)
    client._token = "session-token"
    client._cookie = "session-cookie"
    with mock.patch.object(client, "_request", return_value={}) as transport:
        assert client.logout() == {}
    transport.assert_called_once_with("POST", "/api/logout")
    assert client._token == ""
    assert client._cookie == ""


def test_api_client_close_clears_session_when_logout_fails(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path)
    client = snowluma.SnowLumaAPIClient(tmp_path)
    client._token = "session-token"
    client._cookie = "session-cookie"
    with mock.patch.object(
        client,
        "_request",
        side_effect=snowluma.SnowLumaApiError("logout failed"),
    ):
        with pytest.raises(snowluma.SnowLumaApiError, match="logout failed"):
            client.close()
    assert client._token == ""
    assert client._cookie == ""


def test_snowluma_runtime_manifest_tracks_unconditional_index_imports(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path)
    index_path = tmp_path / "SnowLuma/index.mjs"
    index_path.write_text(
        '\n'.join(
            f'import "./{name}";'
            for name in (
                "utils-tSVKpzEf.js",
                "logger-BAozzyTt.js",
                "config-GJCFWjtq.js",
                "server-CLw7fwOG.js",
            )
        ),
        encoding="utf-8",
    )
    runtime_root = tmp_path / "SnowLuma"
    source = index_path.read_text(encoding="utf-8")
    for chunk in (
        "utils-tSVKpzEf.js",
        "logger-BAozzyTt.js",
        "config-GJCFWjtq.js",
        "server-CLw7fwOG.js",
    ):
        assert f'./{chunk}' in source
        assert (runtime_root / chunk).is_file()
        assert chunk in snowluma.REQUIRED_SNOWLUMA_RUNTIME_FILES


def test_snowluma_launch_boundary_requires_loopback_and_both_free_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_runtime(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(snowluma, "_port_listening", lambda port: calls.append(port) or False)
    result = snowluma.SnowLumaManager.validate_launch_boundary(tmp_path)
    assert result["webui"] == 5099
    assert result["onebot"] == 3001
    assert calls == [5099, 3001]

    _write(
        tmp_path,
        "SnowLuma/config/runtime.json",
        json.dumps({"webuiHost": "0.0.0.0", "webuiPort": 5099}),
    )
    with pytest.raises(snowluma.SnowLumaError, match="回环"):
        snowluma.SnowLumaManager.validate_launch_boundary(tmp_path)


@pytest.mark.parametrize("host", ("localhost", "::1", "[::1]", "0.0.0.0", "192.168.1.10"))
def test_snowluma_launch_boundary_rejects_alias_ipv6_and_wildcard_hosts(
    tmp_path: Path, host: str
) -> None:
    _prepare_runtime(tmp_path)
    _write(
        tmp_path,
        "SnowLuma/config/runtime.json",
        json.dumps({"webuiHost": host, "webuiPort": 5099}),
    )
    with pytest.raises(snowluma.SnowLumaError, match="回环"):
        snowluma.SnowLumaManager.validate_launch_boundary(tmp_path)


@pytest.mark.parametrize("host", ("localhost", "::1", "[::1]", "0.0.0.0", "192.168.1.10"))
def test_api_client_rejects_non_ipv4_loopback_hosts(tmp_path: Path, host: str) -> None:
    _prepare_runtime(tmp_path)
    _write(
        tmp_path,
        "SnowLuma/config/runtime.json",
        json.dumps({"webuiHost": host, "webuiPort": 5099}),
    )
    with pytest.raises(snowluma.SnowLumaApiError, match="回环"):
        snowluma.SnowLumaAPIClient(tmp_path)


def test_api_client_login_rejects_success_false(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path)
    client = snowluma.SnowLumaAPIClient(tmp_path)
    with mock.patch.object(client, "_request", return_value={"success": False}):
        with pytest.raises(snowluma.SnowLumaApiError):
            client.login("request-only-password")


def test_api_client_typed_login_states_and_bounded_agreements(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path)
    client = snowluma.SnowLumaAPIClient(tmp_path)
    with mock.patch.object(client, "_request", return_value={"success": False, "needsTotp": True}):
        with pytest.raises(snowluma.SnowLumaApiError) as raised:
            client.login(PASSWORD)
    assert raised.value.code == snowluma.SNOWLUMA_TOTP_REQUIRED

    document = {
        "id": "eula",
        "title": "EULA",
        "declaredVersion": "1.0",
        "effectiveDate": "2026-01-01",
        "text": "<not-html>\nfull agreement text",
    }
    with mock.patch.object(
        client,
        "_request",
        side_effect=[
            {"version": "v1", "consentRequired": True, "documents": [document]},
            {"success": True, "version": "v1"},
        ],
    ) as request:
        agreements = client.get_agreements()
        consent = client.record_consent("v1")
    assert agreements["documents"][0]["text"] == document["text"]
    assert consent == {"status": "ok", "version": "v1"}
    assert request.call_args_list[1].args == ("POST", "/api/agreements/record-consent", {"version": "v1"})


def test_api_client_rejects_oversized_agreement_payload(tmp_path: Path) -> None:
    _prepare_runtime(tmp_path)
    client = snowluma.SnowLumaAPIClient(tmp_path)
    document = {
        "id": "oversized",
        "title": "Oversized",
        "declaredVersion": "1",
        "effectiveDate": "2026-01-01",
        "text": "x" * (snowluma.MAX_AGREEMENT_TOTAL_TEXT_BYTES + 1),
    }
    with mock.patch.object(
        client,
        "_request",
        return_value={"version": "v1", "consentRequired": True, "documents": [document]},
    ):
        with pytest.raises(snowluma.SnowLumaApiError):
            client.get_agreements()


def test_select_qq_adapter_preserves_comments_and_rebuilds_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write(
        tmp_path,
        "NachoBot/.env",
        "# header\nqq_adapter = napcat  \nUNRELATED = keep # comment\n",
    )
    monkeypatch.setattr(setup_deployment, "ROOT_DIR", tmp_path)
    process_manager_module = (
        f"{setup_deployment.__package__}.process_manager"
        if setup_deployment.__package__
        else "process_manager"
    )
    with mock.patch(f"{process_manager_module}._register_services") as register:
        result = setup_deployment.select_qq_adapter(
            "snowluma",
            root=tmp_path,
            process_manager=SimpleNamespace(states={}),
        )
    text = env.read_text(encoding="utf-8")
    assert text == "# header\nqq_adapter = snowluma  \nUNRELATED = keep # comment\n"
    assert result["selected"] == "snowluma"
    assert result["backup"]
    register.assert_called_once()
