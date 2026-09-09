from src.common.server import (
    get_global_server,
    is_loopback_host,
    supports_message_server_token_auth,
)
import os
import inspect
from pathlib import Path
from ncnk_message import MessageServer
from src.common.logger import get_logger
from src.config.config import global_config

global_api = None


def _validate_custom_message_server_config(config, message_server_parameters) -> None:
    """Validate custom transport/TLS before constructing MessageServer.

    ``NcnkMessageConfig`` is populated from user-controlled TOML, so its
    ``Literal``/``bool`` annotations are not sufficient runtime validation.
    Fail closed for unknown modes, TCP+WSS combinations, incomplete paths, or
    an imported MessageServer that cannot actually receive TLS parameters.
    """
    mode = getattr(config, "mode", None)
    if mode not in {"ws", "tcp"}:
        raise RuntimeError("自定义 ncnk_message mode 仅支持 'ws' 或 'tcp'")
    use_wss = getattr(config, "use_wss", False)
    if not isinstance(use_wss, bool):
        raise RuntimeError("自定义 ncnk_message use_wss 必须是布尔值")
    if not use_wss:
        return
    if mode != "ws":
        raise RuntimeError("WSS 仅可用于 ws 模式，拒绝在 TCP 模式启用")
    cert_file = str(getattr(config, "cert_file", "") or "").strip()
    key_file = str(getattr(config, "key_file", "") or "").strip()
    if not cert_file or not key_file:
        raise RuntimeError("启用 WSS 必须同时配置 cert_file 和 key_file")
    for label, raw_path in (("cert_file", cert_file), ("key_file", key_file)):
        try:
            path = Path(raw_path).expanduser().resolve()
            readable_regular_file = path.is_file() and os.access(path, os.R_OK)
        except (OSError, ValueError):
            readable_regular_file = False
        if not readable_regular_file:
            raise RuntimeError(f"WSS {label} 必须是可读的常规文件")
    if not {"ssl_certfile", "ssl_keyfile"}.issubset(message_server_parameters):
        raise RuntimeError("当前导入的 ncnk_message 不支持 WSS TLS 参数，拒绝降级启动")


def get_global_api() -> MessageServer:  # sourcery skip: extract-method
    """获取全局MessageServer实例"""
    global global_api
    if global_api is None:
        # 读取配置项
        ncnk_message_config = global_config.ncnk_message
        server = get_global_server()
        server.configure_auth(ncnk_message_config.auth_token)
        server.validate_security()
        auth_tokens = server.auth_tokens
        if (
            ncnk_message_config.use_custom
            and not is_loopback_host(ncnk_message_config.host)
            and not auth_tokens
        ):
            raise RuntimeError(
                "自定义 ncnk_message 服务拒绝在无认证时监听非回环地址。"
            )

        # 设置基本参数
        kwargs = {
            "host": os.environ["HOST"],
            "port": int(os.environ["PORT"]),
            "app": server.get_app(),
        }

        try:
            # Inspect the imported implementation itself.  A distribution-level
            # version check is not sufficient because deployments may resolve a
            # local/legacy MessageServer class with a different constructor.
            message_server_parameters = inspect.signature(MessageServer).parameters
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "无法检查当前导入的 ncnk_message MessageServer 能力，拒绝启动"
            ) from exc
        if "custom_logger" in message_server_parameters:
            ncnk_message_logger = get_logger("ncnk_message")
            kwargs["custom_logger"] = ncnk_message_logger

        if auth_tokens:
            if not supports_message_server_token_auth(MessageServer):
                raise RuntimeError(
                    "当前导入的 ncnk_message MessageServer 不支持令牌认证，"
                    "拒绝以仅保护 HTTP 的降级模式启动"
                )
            kwargs["enable_token"] = True

        if ncnk_message_config.use_custom:
            required_parameters = {"mode", "enable_custom_uvicorn_logger"}
            if not required_parameters.issubset(message_server_parameters):
                raise RuntimeError("当前导入的 ncnk_message 不支持自定义服务模式")
            _validate_custom_message_server_config(
                ncnk_message_config,
                message_server_parameters,
            )
            del kwargs["app"]
            kwargs["host"] = ncnk_message_config.host
            kwargs["port"] = ncnk_message_config.port
            kwargs["mode"] = ncnk_message_config.mode
            if ncnk_message_config.use_wss:
                if ncnk_message_config.cert_file:
                    kwargs["ssl_certfile"] = ncnk_message_config.cert_file
                if ncnk_message_config.key_file:
                    kwargs["ssl_keyfile"] = ncnk_message_config.key_file
            if "enable_custom_uvicorn_logger" in message_server_parameters:
                kwargs["enable_custom_uvicorn_logger"] = False

        global_api = MessageServer(**kwargs)
        if auth_tokens:
            for token in auth_tokens:
                global_api.add_valid_token(token)
    return global_api
