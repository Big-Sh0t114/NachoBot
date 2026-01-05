"""WebSocket 客户端配置工厂函数 - 仅用于构造配置"""

from .ws_config import create_client_config, create_ssl_client_config


def create_client_config(url: str, api_key: str, **kwargs):
    """创建单连接客户端配置的便捷函数

    Args:
        url: WebSocket服务器URL
        api_key: API密钥
        platform: 平台标识 (默认: "default")
        **kwargs: 其他ClientConfig参数

    Returns:
        ClientConfig: 客户端配置实例

    Example:
        config = create_client_config(
            "ws://localhost:18040/ws",
            "your_api_key",
            platform="wechat"
        )
        client = WebSocketClient(config)
    """
    from .ws_config import ClientConfig
    return ClientConfig(url=url, api_key=api_key, **kwargs)


def create_ssl_client_config(url: str, api_key: str, **kwargs):
    """创建SSL客户端配置的便捷函数

    Args:
        url: WebSocket服务器URL (使用wss://协议)
        api_key: API密钥
        **kwargs: 其他SSL配置参数

    Returns:
        ClientConfig: SSL客户端配置实例

    Example:
        config = create_ssl_client_config(
            "wss://localhost:18044/ws",
            "your_api_key",
            ssl_ca_certs="/path/to/ca.crt"
        )
        client = WebSocketClient(config)
    """
    from .ws_config import ClientConfig
    return ClientConfig(url=url, api_key=api_key, ssl_enabled=True, **kwargs)