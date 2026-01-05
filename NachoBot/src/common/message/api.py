from src.common.server import get_global_server
import os
import importlib.metadata
from ncnk_message import MessageServer
from src.common.logger import get_logger
from src.config.config import global_config

global_api = None


def get_global_api() -> MessageServer:  # sourcery skip: extract-method
    """获取全局MessageServer实例"""
    global global_api
    if global_api is None:
        # 检查ncnk_message版本
        try:
            ncnk_message_version = importlib.metadata.version("ncnk_message")
            version_compatible = [int(x) for x in ncnk_message_version.split(".")] >= [0, 3, 3]
        except (importlib.metadata.PackageNotFoundError, ValueError):
            version_compatible = False

        # 读取配置项
        ncnk_message_config = global_config.ncnk_message

        # 设置基本参数
        kwargs = {
            "host": os.environ["HOST"],
            "port": int(os.environ["PORT"]),
            "app": get_global_server().get_app(),
        }

        # 只有在版本 >= 0.3.0 时才使用高级特性
        if version_compatible:
            # 添加自定义logger
            ncnk_message_logger = get_logger("ncnk_message")
            kwargs["custom_logger"] = ncnk_message_logger

            # 添加token认证
            if ncnk_message_config.auth_token and len(ncnk_message_config.auth_token) > 0:
                kwargs["enable_token"] = True

            if ncnk_message_config.use_custom:
                # 添加WSS模式支持
                del kwargs["app"]
                kwargs["host"] = ncnk_message_config.host
                kwargs["port"] = ncnk_message_config.port
                kwargs["mode"] = ncnk_message_config.mode
                if ncnk_message_config.use_wss:
                    if ncnk_message_config.cert_file:
                        kwargs["ssl_certfile"] = ncnk_message_config.cert_file
                    if ncnk_message_config.key_file:
                        kwargs["ssl_keyfile"] = ncnk_message_config.key_file
                kwargs["enable_custom_uvicorn_logger"] = False

        global_api = MessageServer(**kwargs)
        if version_compatible and ncnk_message_config.auth_token:
            for token in ncnk_message_config.auth_token:
                global_api.add_valid_token(token)
    return global_api
