"""Authentication and browser-origin protection for the WebUI control plane."""

from __future__ import annotations

import base64
import ipaddress
import os
import secrets
from collections.abc import Mapping
from urllib.parse import urlsplit

import tomlkit
from fastapi import Request, WebSocket
from fastapi.responses import JSONResponse


TOKEN_ENV = "NACHOBOT_WEBUI_TOKEN"
TOKEN_HEADER = "x-nachobot-token"
WS_AUTH_PREFIX = "nachobot.auth."
WS_PROTOCOL = "nachobot"


def is_loopback_bind(host: str) -> bool:
    """Return whether *host* is an unambiguous loopback bind target."""
    normalized = str(host or "").strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_server_security(host: str, token: str) -> None:
    """Refuse a remotely reachable control plane without authentication."""
    if not is_loopback_bind(host) and not str(token or "").strip():
        raise RuntimeError(
            "WebUI 拒绝在非回环地址上无认证启动；请设置 "
            f"{TOKEN_ENV}"
        )


def validate_webui_config_raw(
    raw: str,
    *,
    runtime_bind_host: str | None = None,
) -> None:
    """Validate both the persisted target and the socket that is already bound."""
    doc = tomlkit.parse(raw)
    server_section = doc.get("server", {})
    host = str(
        server_section.get("host", "127.0.0.1")
        if hasattr(server_section, "get")
        else "127.0.0.1"
    )
    security_section = doc.get("security", {})
    configured_token = str(
        security_section.get("auth_token", "")
        if hasattr(security_section, "get")
        else ""
    )
    if configured_token.strip():
        raise RuntimeError(
            f"WebUI 访问令牌不得写入已跟踪配置文件；请清空 auth_token 并设置 {TOKEN_ENV}"
        )
    effective_token = str(os.environ.get(TOKEN_ENV) or "")
    validate_server_security(host, effective_token)
    if runtime_bind_host is not None:
        validate_server_security(runtime_bind_host, effective_token)


def _normalize_origin(origin: str) -> str | None:
    try:
        parsed = urlsplit(str(origin or "").strip())
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    default_port = 443 if parsed.scheme == "https" else 80
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = "" if port in (None, default_port) else f":{port}"
    return f"{parsed.scheme}://{host}{suffix}"


def _decode_ws_token(protocols: str) -> str:
    for protocol in (item.strip() for item in protocols.split(",")):
        if not protocol.startswith(WS_AUTH_PREFIX):
            continue
        encoded = protocol.removeprefix(WS_AUTH_PREFIX)
        try:
            padding = "=" * (-len(encoded) % 4)
            return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return ""


def _websocket_protocols(protocols: str) -> set[str]:
    return {item.strip() for item in protocols.split(",") if item.strip()}


class WebUISecurity:
    """Evaluate HTTP and WebSocket requests against current WebUI settings."""

    def __init__(self, config):
        self.config = config
        # Uvicorn does not rebind its listening socket when the TOML is hot-
        # reloaded. Keep the actual startup address for security decisions.
        self.runtime_bind_host = str(config.host)
        self.runtime_bind_port = int(config.port)

    @property
    def token(self) -> str:
        return str(os.environ.get(TOKEN_ENV) or "").strip()

    def ensure_safe_bind(self) -> None:
        validate_server_security(self.runtime_bind_host, self.token)

    def allowed_origins(self) -> set[str]:
        port = self.runtime_bind_port
        origins = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
            f"https://127.0.0.1:{port}",
            f"https://localhost:{port}",
            f"https://[::1]:{port}",
        }
        for origin in self.config.allowed_origins:
            normalized = _normalize_origin(origin)
            if normalized:
                origins.add(normalized)
        return origins

    def origin_allowed(self, origin: str | None) -> bool:
        # Non-browser clients do not send Origin. Browser cross-site requests and
        # all browser WebSockets do, so an absent Origin is not treated as a CSRF
        # bypass for a hostile web page.
        if origin is None:
            return True
        normalized = _normalize_origin(origin)
        return normalized is not None and normalized in self.allowed_origins()

    def token_allowed(self, headers: Mapping[str, str], ws_protocols: str = "") -> bool:
        expected = self.token
        if not expected:
            return True
        supplied = str(headers.get(TOKEN_HEADER, "") or "")
        if not supplied:
            authorization = str(headers.get("authorization", "") or "")
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() == "bearer":
                supplied = value.strip()
        if not supplied and ws_protocols:
            supplied = _decode_ws_token(ws_protocols)
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    def authorize_http(self, request: Request) -> JSONResponse | None:
        if not request.url.path.startswith("/api"):
            return None
        if not self.origin_allowed(request.headers.get("origin")):
            return JSONResponse({"detail": "请求来源不受信任"}, status_code=403)
        if not self.token_allowed(request.headers):
            return JSONResponse(
                {"detail": f"需要 WebUI 访问令牌（{TOKEN_HEADER}）"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None

    async def authorize_websocket(self, websocket: WebSocket) -> bool:
        if not self.origin_allowed(websocket.headers.get("origin")):
            await websocket.close(code=1008, reason="untrusted origin")
            return False
        protocols = websocket.headers.get("sec-websocket-protocol", "")
        if not self.token_allowed(websocket.headers, protocols):
            await websocket.close(code=1008, reason="authentication required")
            return False
        offered = _websocket_protocols(protocols)
        await websocket.accept(subprotocol=WS_PROTOCOL if WS_PROTOCOL in offered else None)
        return True
