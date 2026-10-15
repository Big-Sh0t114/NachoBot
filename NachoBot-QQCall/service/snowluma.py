"""Private SnowLuma authority checks for the Windows QQCall binder.

This module keeps SnowLuma-specific WebUI authentication and configuration
validation out of the NapCat process-tree binding path.  It returns only the
authenticated account row and non-secret runtime/endpoint identity.
"""

from __future__ import annotations

import importlib
import hmac
import json
import os
import re
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_UIN = re.compile(r"^[1-9][0-9]{0,19}$")
_MAX_ADAPTER_CONFIG_BYTES = 256 * 1024
_MAX_ONEBOT_CONFIG_BYTES = 1024 * 1024
_AUTH_FAILURE_STAGES = frozenset({
    "snowluma_auth_runtime_resolution", "snowluma_auth_password_load",
    "snowluma_auth_client_init", "snowluma_auth_login",
    "snowluma_auth_process_list", "snowluma_auth_session_close",
})


def safe_auth_failure_stage(code: Any, value: Any) -> str | None:
    return value if code == "adapter_auth_unavailable" and type(value) is str and value in _AUTH_FAILURE_STAGES else None


class SnowLumaBindingError(RuntimeError):
    """A stable, non-secret failure from the private SnowLuma binding path."""

    def __init__(self, code: str, *, diagnostic_stage: str | None = None):
        allowed = {
            "adapter_account_mismatch",
            "adapter_auth_unavailable",
            "adapter_endpoint_mismatch",
            "adapter_process_mismatch",
            "target_ambiguous",
            "target_process_unavailable",
        }
        self.code = code if code in allowed else "adapter_auth_unavailable"
        self._diagnostic_stage = safe_auth_failure_stage(self.code, diagnostic_stage)
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class SnowLumaProcessAuthority:
    pid: int
    account_id: str
    image_path: str


@dataclass(frozen=True, slots=True)
class SnowLumaAuthority:
    runtime_path: str
    endpoint_host: str
    endpoint_port: int
    endpoint_path: str
    account_id: str
    process: SnowLumaProcessAuthority


def _webui_symbols(root: Path) -> tuple[Any, Any, Any, Any]:
    """Load only the existing trusted WebUI locator, manager, and store."""

    root_text = str(root)
    inserted = root_text not in sys.path
    if inserted:
        sys.path.insert(0, root_text)
    try:
        locator = importlib.import_module("webUI.snowluma_locator")
        manager = importlib.import_module("webUI.snowluma_manager")
        credentials = importlib.import_module("webUI.snowluma_credentials")
        return (
            locator.resolve_snowluma_runtime,
            manager.SnowLumaManager,
            manager.SnowLumaAPIClient,
            credentials.SnowLumaPasswordStore,
        )
    finally:
        if inserted:
            try:
                sys.path.remove(root_text)
            except ValueError:
                pass


def _read_adapter_document(root: Path) -> dict[str, Any]:
    target = root / "NachoBot-SnowLuma-Adapter" / "config.toml"
    try:
        root_real = root.resolve(strict=True)
        target_real = target.resolve(strict=True)
        target_real.relative_to(root_real)
        metadata = target.lstat()
        if (
            target.is_symlink()
            or getattr(metadata, "st_file_attributes", 0) & 0x400
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_ADAPTER_CONFIG_BYTES
        ):
            raise ValueError
        raw = target.read_bytes()
        if len(raw) > _MAX_ADAPTER_CONFIG_BYTES:
            raise ValueError
        document = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError):
        raise SnowLumaBindingError("adapter_endpoint_mismatch") from None
    if type(document) is not dict:
        raise SnowLumaBindingError("adapter_endpoint_mismatch")
    return document


def _host_identity(value: object) -> str | None:
    if value == "127.0.0.1":
        return "127.0.0.1"
    if value in ("::1", "[::1]"):
        return "::1"
    # localhost can resolve to either address family; do not merge it with
    # an IPv4 or IPv6 bind unless a caller can prove the actual resolution.
    return None


def _normalize_path(value: object) -> str | None:
    if type(value) is not str or not value or len(value) > 512 or not value.startswith("/"):
        return None
    if any(ord(char) < 0x20 for char in value):
        return None
    return value.rstrip("/") or "/"


def _url_endpoint(value: object) -> tuple[str, int, str] | None:
    if type(value) is not str or not value or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in ("ws", "wss")
        or _host_identity(host) is None
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\x00" in value
    ):
        return None
    path = _normalize_path(parsed.path or "/")
    if path is None:
        return None
    return (_host_identity(host) or "", port, path)


def _checked_endpoint(value: object) -> tuple[str, int, str] | None:
    if type(value) is not dict:
        return None
    host = value.get("host")
    port = value.get("port")
    path = value.get("path")
    if (
        _host_identity(host) is None
        or type(port) is not int
        or not 1 <= port <= 65535
        or _normalize_path(path) is None
    ):
        return None
    return (_host_identity(host) or "", port, _normalize_path(path) or "/")


def _adapter_endpoint(section: dict[str, Any]) -> tuple[str, int, str] | None:
    host = _host_identity(section.get("host"))
    port = section.get("port")
    raw_path = section.get("path", "/")
    # SnowLuma's adapter treats an empty configured path as the root path.
    path = _normalize_path("/" if raw_path == "" else raw_path)
    if host is None or type(port) is not int or not 1 <= port <= 65535 or path is None:
        return None
    return host, port, path


def configured_onebot_connection(root: Path) -> tuple[str, str]:
    """Read the adapter's loopback endpoint; authority is still checked by bind."""
    section = _read_adapter_document(root).get("snowluma")
    endpoint = _adapter_endpoint(section) if isinstance(section, dict) else None
    token = section.get("token") if isinstance(section, dict) else None
    if endpoint is None or not isinstance(token, str) or not token or len(token) > 4096:
        raise SnowLumaBindingError("adapter_endpoint_mismatch")
    host, port, path = endpoint
    if ":" in host:
        host = f"[{host}]"
    url = f"ws://{host}:{port}{path}"
    if _url_endpoint(url) != endpoint:
        raise SnowLumaBindingError("adapter_endpoint_mismatch")
    return url, token


def _read_account_onebot_config(runtime_path: Path, account_id: str) -> dict[str, Any]:
    target = runtime_path / "config" / f"onebot_{account_id}.json"
    try:
        runtime_real = runtime_path.resolve(strict=True)
        target_real = target.resolve(strict=True)
        target_real.relative_to(runtime_real)
        metadata = target.lstat()
        if (
            target.is_symlink()
            or getattr(metadata, "st_file_attributes", 0) & 0x400
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_ONEBOT_CONFIG_BYTES
        ):
            raise ValueError
        raw = target.read_bytes()
        if len(raw) > _MAX_ONEBOT_CONFIG_BYTES:
            raise ValueError
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise SnowLumaBindingError("adapter_endpoint_mismatch") from None
    if type(document) is not dict:
        raise SnowLumaBindingError("adapter_endpoint_mismatch")
    return document


def load_snowluma_authority(
    root: str | os.PathLike[str],
    account_id: str,
    onebot_url: str,
    *,
    symbols: tuple[Any, Any, Any, Any] | None = None,
) -> SnowLumaAuthority:
    """Authenticate to the existing WebUI and resolve one exact QQ account.

    Passwords and tokens stay inside this call.  Returned values contain only
    the private target PID, account string, image path, and non-secret runtime
    and endpoint identity.
    """

    if type(account_id) is not str or _UIN.fullmatch(account_id) is None:
        raise SnowLumaBindingError("adapter_account_mismatch")
    requested_endpoint = _url_endpoint(onebot_url)
    if requested_endpoint is None:
        raise SnowLumaBindingError("adapter_endpoint_mismatch")

    base = Path(root)
    try:
        base = base.resolve(strict=True)
        resolver, manager_class, api_client_class, password_store_class = (
            symbols if symbols is not None else _webui_symbols(base)
        )
        runtime = resolver(base)
        runtime_path = Path(runtime.path).resolve(strict=True)
        runtime_path.relative_to(base)
    except SnowLumaBindingError:
        raise
    except Exception:
        raise SnowLumaBindingError("adapter_auth_unavailable", diagnostic_stage="snowluma_auth_runtime_resolution") from None

    document = _read_adapter_document(base)
    section = document.get("snowluma")
    configured_account = section.get("qq_account") if type(section) is dict else None
    # The current SnowLuma adapter config has no account selector.  In that
    # deployment the requested canonical UIN is anchored by the authenticated
    # WebUI process row below.  If an adapter version does expose the selector,
    # it remains an additional consistency check.
    if type(section) is not dict or (
        configured_account is not None and configured_account != account_id
    ):
        raise SnowLumaBindingError("adapter_account_mismatch")
    if _adapter_endpoint(section) != requested_endpoint:
        raise SnowLumaBindingError("adapter_endpoint_mismatch")

    try:
        consistency = manager_class.credential_consistency(base, resolution=runtime)
    except Exception:
        raise SnowLumaBindingError("adapter_endpoint_mismatch") from None
    if type(consistency) is not dict or consistency.get("consistent") is not True:
        raise SnowLumaBindingError("adapter_endpoint_mismatch")
    endpoint = _checked_endpoint(consistency.get("endpoint"))
    if (
        endpoint is None
        or endpoint[1:] != requested_endpoint[1:]
        or type(consistency.get("matched_servers")) is not int
        or consistency["matched_servers"] < 1
    ):
        raise SnowLumaBindingError("adapter_endpoint_mismatch")

    account_config = _read_account_onebot_config(Path(runtime.path), account_id)
    networks = account_config.get("networks")
    servers = networks.get("wsServers") if type(networks) is dict else None
    if type(servers) is not list:
        raise SnowLumaBindingError("adapter_endpoint_mismatch")
    matching_servers = [
        server
        for server in servers
        if type(server) is dict
        and server.get("enabled") is not False
        and _adapter_endpoint(server) == requested_endpoint
    ]
    if len(matching_servers) != 1:
        raise SnowLumaBindingError("adapter_endpoint_mismatch")

    # Global consistency counts saved servers across accounts. The account
    # selected below must independently have the exact endpoint and credential;
    # another account's matching token cannot authorize this target.
    adapter_token = section.get("token")
    account_token = matching_servers[0].get("accessToken")
    if (type(adapter_token) is not str or not adapter_token or
            type(account_token) is not str or not account_token or
            not hmac.compare_digest(adapter_token.encode("utf-8"), account_token.encode("utf-8"))):
        raise SnowLumaBindingError("adapter_endpoint_mismatch")

    auth_stage = "snowluma_auth_password_load"
    try:
        password_store = password_store_class(base)
        password = password_store.load()
        if type(password) is not str or not password:
            raise ValueError
        auth_stage = "snowluma_auth_client_init"
        client = api_client_class(base)
    except Exception:
        raise SnowLumaBindingError("adapter_auth_unavailable", diagnostic_stage=auth_stage) from None

    rows: list[dict[str, Any]] | None = None
    api_failed = False
    try:
        auth_stage = "snowluma_auth_login"
        client.login(password)
        auth_stage = "snowluma_auth_process_list"
        candidate_rows = client.list_processes()
        if type(candidate_rows) is not list:
            api_failed = True
        else:
            rows = candidate_rows
    except Exception:
        api_failed = True
    finally:
        # Clearing the client session is part of this authenticated read even
        # when login or the process-list request fails.
        try:
            close = getattr(client, "close", None) or getattr(client, "logout", None)
            if callable(close):
                close()
        except Exception:
            if rows is not None:
                api_failed = True
                auth_stage = "snowluma_auth_session_close"

    if api_failed or rows is None:
        raise SnowLumaBindingError("adapter_auth_unavailable", diagnostic_stage=auth_stage)

    matches = [row for row in rows if type(row) is dict and row.get("uin") == account_id]
    if len(matches) > 1:
        raise SnowLumaBindingError("target_ambiguous")
    if len(matches) != 1:
        raise SnowLumaBindingError("adapter_account_mismatch")
    row = matches[0]
    pid = row.get("pid")
    image_path = row.get("path")
    if (
        type(pid) is not int
        or pid <= 0
        or type(image_path) is not str
        or len(image_path) > 260
        or "\x00" in image_path
        or any(type(row.get(field)) is not bool for field in ("injected", "connected", "loggedIn"))
        or any(row.get(field) is not True for field in ("injected", "connected", "loggedIn"))
    ):
        raise SnowLumaBindingError("adapter_process_mismatch")

    return SnowLumaAuthority(
        str(runtime_path), endpoint[0], endpoint[1], endpoint[2], account_id,
        SnowLumaProcessAuthority(pid, account_id, image_path),
    )


__all__ = [
    "SnowLumaAuthority",
    "SnowLumaBindingError",
    "SnowLumaProcessAuthority",
    "load_snowluma_authority",
]
