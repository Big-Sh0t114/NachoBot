"""
租户感知消息系统的集成工具和便捷函数
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass

from .tenant_client import TenantMessageClient, ClientConfig
from .tenant_broadcast_api import TenantBroadcastAPI

logger = logging.getLogger(__name__)


@dataclass
class TenantServerConfig:
    """租户服务器配置"""

    host: str = "0.0.0.0"
    port: int = 8091
    ssl_cert: Optional[str] = None
    ssl_key: Optional[str] = None


@dataclass
class TenantSystemConfig:
    """租户系统配置"""

    server: TenantServerConfig
    tenants: Dict[str, Dict[str, Any]]  # tenant_id -> tenant_config


class TenantMessageSystem:
    """租户消息系统集成管理器"""

    def __init__(self, config: TenantSystemConfig):
        self.config = config
        self.server_url = f"https://{config.server.host}:{config.server.port}"

        # 客户端管理：tenant_id -> platform -> client
        self.clients: Dict[str, Dict[str, TenantMessageClient]] = {}

        # 广播API
        self.broadcast_api: Optional[TenantBroadcastAPI] = None

    async def initialize(self):
        """初始化系统"""
        try:
            # 初始化广播API
            self.broadcast_api = TenantBroadcastAPI(self.server_url)

            # 初始化所有租户的客户端
            for tenant_id, tenant_config in self.config.tenants.items():
                await self._initialize_tenant(tenant_id, tenant_config)

            logger.info("租户消息系统初始化完成")
            return True

        except Exception as e:
            logger.error(f"租户消息系统初始化失败: {e}")
            return False

    async def _initialize_tenant(self, tenant_id: str, tenant_config: Dict[str, Any]):
        """初始化单个租户"""
        if tenant_id not in self.clients:
            self.clients[tenant_id] = {}

        platforms = tenant_config.get("platforms", {})

        for platform, platform_config in platforms.items():
            try:
                client_config = ClientConfig(
                    tenant_id=tenant_id,
                    platform=platform,
                    server_url=self.server_url,
                    api_key=platform_config.get("api_key"),
                    connection_pool=platform_config.get("connection_pool"),
                    ssl_verify=platform_config.get("ssl_verify", True),
                    max_retries=platform_config.get("max_retries", 3),
                    heartbeat_interval=platform_config.get("heartbeat_interval", 30),
                )

                client = TenantMessageClient(client_config)
                await client.connect()

                self.clients[tenant_id][platform] = client
                logger.info(f"租户 {tenant_id} 平台 {platform} 客户端连接成功")

            except Exception as e:
                logger.error(f"租户 {tenant_id} 平台 {platform} 客户端连接失败: {e}")

    async def broadcast_message(
        self,
        tenant_id: str,
        platform: str,
        message: Dict[str, Any],
        wait_for_result: bool = False,
    ) -> Dict[str, Any]:
        """向指定租户平台广播消息"""
        if not self.broadcast_api:
            raise RuntimeError("广播API未初始化")

        return await self.broadcast_api.broadcast_with_verification(
            tenant_id=tenant_id,
            platform=platform,
            message=message,
            wait_for_result=wait_for_result,
        )

    async def send_message_from_client(
        self,
        tenant_id: str,
        platform: str,
        message: Dict[str, Any],
        wait_for_result: bool = True,
    ) -> Dict[str, Any]:
        """通过客户端发送消息"""
        client = self._get_client(tenant_id, platform)
        if not client:
            raise ValueError(f"未找到租户 {tenant_id} 平台 {platform} 的客户端")

        return await client.send_message(message, wait_for_result)

    def _get_client(
        self, tenant_id: str, platform: str
    ) -> Optional[TenantMessageClient]:
        """获取指定租户平台的客户端"""
        return self.clients.get(tenant_id, {}).get(platform)

    async def get_system_status(self) -> Dict[str, Any]:
        """获取系统状态"""
        status = {
            "server_url": self.server_url,
            "tenants": {},
            "total_clients": 0,
            "connected_clients": 0,
        }

        for tenant_id, platforms in self.clients.items():
            tenant_status = {
                "platforms": {},
                "total_platforms": len(platforms),
                "connected_platforms": 0,
            }

            for platform, client in platforms.items():
                try:
                    conn_info = await client.get_connection_info()
                    platform_status = {
                        "state": conn_info["state"],
                        "is_connected": conn_info["is_connected"],
                        "connection_uuid": conn_info["connection_uuid"],
                        "pending_messages": conn_info["pending_messages_count"],
                    }

                    if conn_info["is_connected"]:
                        tenant_status["connected_platforms"] += 1
                        status["connected_clients"] += 1

                except Exception as e:
                    platform_status = {"error": str(e), "is_connected": False}

                tenant_status["platforms"][platform] = platform_status

            status["tenants"][tenant_id] = tenant_status
            status["total_clients"] += len(platforms)

        return status

    async def register_callback(
        self,
        tenant_id: str,
        platform: str,
        callback: Callable,
        message_types: List[str],
    ):
        """为指定租户平台注册回调"""
        client = self._get_client(tenant_id, platform)
        if client:
            client.register_callback(callback, message_types)

    async def shutdown(self):
        """关闭系统"""
        # 关闭所有客户端
        for tenant_id, platforms in self.clients.items():
            for platform, client in platforms.items():
                try:
                    await client.disconnect()
                    logger.info(f"租户 {tenant_id} 平台 {platform} 客户端已断开")
                except Exception as e:
                    logger.error(f"断开客户端时出错: {e}")

        # 清空客户端字典
        self.clients.clear()

        logger.info("租户消息系统已关闭")


# 配置加载函数
def load_config_from_file(config_path: str) -> TenantSystemConfig:
    """从文件加载配置"""
    import tomllib

    try:
        import tomllib as tomllib
    except ImportError:
        import tomli as tomllib

    with open(config_path, "r", encoding="utf-8") as f:
        config_data = tomllib.load(f)

    server_config = TenantServerConfig(
        host=config_data.get("server", {}).get("host", "0.0.0.0"),
        port=config_data.get("server", {}).get("port", 8091),
        ssl_cert=config_data.get("server", {}).get("ssl_cert"),
        ssl_key=config_data.get("server", {}).get("ssl_key"),
    )

    return TenantSystemConfig(
        server=server_config, tenants=config_data.get("tenants", {})
    )


# 便捷函数
async def create_tenant_system_from_config(config_path: str) -> TenantMessageSystem:
    """从配置文件创建租户消息系统"""
    config = load_config_from_file(config_path)
    system = TenantMessageSystem(config)

    if await system.initialize():
        return system
    else:
        raise RuntimeError("无法初始化租户消息系统")


async def create_simple_tenant_system(
    server_host: str = "0.0.0.0",
    server_port: int = 8091,
    tenants_config: Dict[str, Dict[str, Any]] = None,
) -> TenantMessageSystem:
    """创建简单的租户消息系统"""
    server_config = TenantServerConfig(host=server_host, port=server_port)

    system_config = TenantSystemConfig(
        server=server_config, tenants=tenants_config or {}
    )

    system = TenantMessageSystem(system_config)

    if await system.initialize():
        return system
    else:
        raise RuntimeError("无法初始化租户消息系统")


# 示例配置生成函数
def generate_example_config() -> str:
    """生成示例配置文件内容"""
    return """
# 租户消息系统配置文件

[server]
host = "0.0.0.0"
port = 8091
# ssl_cert = "/path/to/cert.pem"
# ssl_key = "/path/to/key.pem"

[tenants.tenant1]
name = "租户1"
description = "第一个租户"

[tenants.tenant1.platforms.qq]
api_key = "tenant1_qq_api_key"
connection_pool = "pool1"
max_retries = 3
heartbeat_interval = 30

[tenants.tenant1.platforms.discord]
api_key = "tenant1_discord_api_key"
connection_pool = "pool2"
max_retries = 3

[tenants.tenant2]
name = "租户2"
description = "第二个租户"

[tenants.tenant2.platforms.qq]
api_key = "tenant2_qq_api_key"
connection_pool = "pool3"
max_retries = 5
heartbeat_interval = 20
"""


# 使用示例
async def integration_example():
    """集成使用示例"""
    # 生成配置
    example_config = generate_example_config()
    print("示例配置:")
    print(example_config)

    # 创建简单系统
    tenants_config = {
        "tenant1": {
            "platforms": {"qq": {"api_key": "test_api_key_1", "max_retries": 2}}
        },
        "tenant2": {"platforms": {"qq": {"api_key": "test_api_key_2"}}},
    }

    try:
        # 创建系统
        system = await create_simple_tenant_system(
            server_host="localhost", server_port=8091, tenants_config=tenants_config
        )

        # 注册回调
        def handle_message(message):
            print(f"收到消息: {message.get('type')} - {message.get('content')}")

        await system.register_callback(
            "tenant1", "qq", handle_message, ["chat_message"]
        )

        # 获取系统状态
        status = await system.get_system_status()
        print(f"系统状态: {json.dumps(status, indent=2, ensure_ascii=False)}")

        # 广播消息
        try:
            result = await system.broadcast_message(
                tenant_id="tenant1",
                platform="qq",
                message={
                    "type": "system_announcement",
                    "content": "测试广播消息",
                    "timestamp": 1234567890,
                },
            )
            print(f"广播结果: {result}")
        except Exception as e:
            print(f"广播失败（预期，因为没有服务器运行）: {e}")

        # 发送消息
        try:
            result = await system.send_message_from_client(
                tenant_id="tenant1",
                platform="qq",
                message={"type": "test_message", "content": "测试客户端消息"},
            )
            print(f"发送结果: {result}")
        except Exception as e:
            print(f"发送失败（预期，因为没有服务器运行）: {e}")

    except Exception as e:
        print(f"系统创建或使用失败（预期，因为没有服务器运行）: {e}")

    finally:
        # 清理
        if "system" in locals():
            await system.shutdown()


if __name__ == "__main__":
    print("运行租户消息系统集成示例...")
    asyncio.run(integration_example())
