from __future__ import annotations

import os


def resolve_listen_address(default_host: str, default_port: int) -> tuple[str, int]:
    """解析 Adapter 监听地址，容器可覆盖本地安全默认值。"""
    host = os.getenv("NACHOBOT_NAPCAT_LISTEN_HOST", default_host).strip()
    port = int(os.getenv("NACHOBOT_NAPCAT_LISTEN_PORT", str(default_port)))
    if not host:
        raise ValueError("NACHOBOT_NAPCAT_LISTEN_HOST cannot be empty")
    if not 1 <= port <= 65535:
        raise ValueError("NACHOBOT_NAPCAT_LISTEN_PORT must be between 1 and 65535")
    return host, port
