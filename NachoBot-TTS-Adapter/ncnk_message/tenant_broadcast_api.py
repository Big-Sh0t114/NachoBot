"""
租户广播API客户端 - 用于服务端主动向租户的特定平台发送消息
"""

import asyncio
import logging
import aiohttp
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


class TenantBroadcastAPI:
    """租户广播API客户端"""

    def __init__(self, base_url: str, api_key: Optional[str] = None):
        """
        初始化广播API客户端

        Args:
            base_url: 租户消息服务器的基础URL，例如 "https://message-server.example.com"
            api_key: API密钥（可选）
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        """异步上下文管理器入口"""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), headers=self._get_headers()
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        if self.session:
            await self.session.close()

    def _get_headers(self) -> Dict[str, str]:
        """获取请求头"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def broadcast_to_tenant_platform(
        self, tenant_id: str, platform: str, message: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        向租户的指定平台广播消息

        Args:
            tenant_id: 租户ID
            platform: 平台标识（如 "qq", "discord" 等）
            message: 要广播的消息内容

        Returns:
            广播结果，包含成功和失败的连接数量

        Raises:
            Exception: 当广播失败时
        """
        url = (
            f"{self.base_url}/api/v1/tenants/{tenant_id}/platforms/{platform}/broadcast"
        )

        async with self.session.post(url, json=message) as response:
            if response.status == 200:
                result = await response.json()
                self._log_broadcast_result(tenant_id, platform, result)
                return result
            else:
                error_text = await response.text()
                raise Exception(f"广播失败: {response.status} - {error_text}")

    async def get_tenant_connections(self, tenant_id: str) -> Dict[str, Any]:
        """
        获取租户的所有连接信息

        Args:
            tenant_id: 租户ID

        Returns:
            连接信息列表

        Raises:
            Exception: 当获取连接失败时
        """
        url = f"{self.base_url}/api/v1/tenants/{tenant_id}/connections"

        async with self.session.get(url) as response:
            if response.status == 200:
                return await response.json()
            else:
                error_text = await response.text()
                raise Exception(f"获取连接失败: {response.status} - {error_text}")

    async def get_tenant_platform_connections(
        self, tenant_id: str, platform: str
    ) -> List[Dict[str, Any]]:
        """
        获取租户指定平台的连接信息

        Args:
            tenant_id: 租户ID
            platform: 平台标识

        Returns:
            指定平台的连接信息列表
        """
        all_connections = await self.get_tenant_connections(tenant_id)

        # 过滤指定平台的连接
        platform_connections = [
            conn
            for conn in all_connections.get("connections", [])
            if conn.get("platform") == platform
        ]

        return platform_connections

    async def get_message_result(self, message_id: str) -> Dict[str, Any]:
        """
        获取消息发送结果

        Args:
            message_id: 消息ID

        Returns:
            消息发送结果

        Raises:
            Exception: 当获取结果失败时
        """
        url = f"{self.base_url}/api/v1/message/{message_id}/result"

        async with self.session.get(url) as response:
            if response.status == 200:
                return await response.json()
            elif response.status == 404:
                raise Exception(f"消息结果未找到: {message_id}")
            else:
                error_text = await response.text()
                raise Exception(f"获取消息结果失败: {response.status} - {error_text}")

    async def flush_retry_pool(self, tenant_id: str) -> Dict[str, Any]:
        """
        清空租户的重试池

        Args:
            tenant_id: 租户ID

        Returns:
            操作结果

        Raises:
            Exception: 当操作失败时
        """
        url = f"{self.base_url}/api/v1/tenants/{tenant_id}/retry-pool/flush"

        async with self.session.post(url) as response:
            if response.status == 200:
                return await response.json()
            else:
                error_text = await response.text()
                raise Exception(f"清空重试池失败: {response.status} - {error_text}")

    async def check_tenant_platform_status(
        self, tenant_id: str, platform: str
    ) -> Dict[str, Any]:
        """
        检查租户指定平台的状态

        Args:
            tenant_id: 租户ID
            platform: 平台标识

        Returns:
            平台状态信息，包括连接数量等
        """
        try:
            connections = await self.get_tenant_platform_connections(
                tenant_id, platform
            )

            active_connections = len(
                [conn for conn in connections if conn.get("is_connected", False)]
            )

            return {
                "tenant_id": tenant_id,
                "platform": platform,
                "total_connections": len(connections),
                "active_connections": active_connections,
                "status": "active" if active_connections > 0 else "inactive",
                "connections": connections,
            }
        except Exception as e:
            return {
                "tenant_id": tenant_id,
                "platform": platform,
                "total_connections": 0,
                "active_connections": 0,
                "status": "error",
                "error": str(e),
            }

    async def broadcast_with_verification(
        self,
        tenant_id: str,
        platform: str,
        message: Dict[str, Any],
        wait_for_result: bool = True,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """
        带验证的消息广播

        Args:
            tenant_id: 租户ID
            platform: 平台标识
            message: 要广播的消息
            wait_for_result: 是否等待消息结果
            timeout: 等待超时时间

        Returns:
            广播结果和可选的消息结果
        """
        # 首先广播消息
        broadcast_result = await self.broadcast_to_tenant_platform(
            tenant_id, platform, message
        )

        result = {"broadcast_result": broadcast_result}

        # 如果需要等待结果且有消息ID
        if wait_for_result and "message_id" in broadcast_result:
            message_id = broadcast_result["message_id"]

            try:
                # 等待消息结果
                start_time = asyncio.get_event_loop().time()
                while asyncio.get_event_loop().time() - start_time < timeout:
                    try:
                        message_result = await self.get_message_result(message_id)
                        result["message_result"] = message_result
                        break
                    except Exception:
                        # 消息结果可能还没有准备好，稍等一下
                        await asyncio.sleep(0.5)
                else:
                    # 超时
                    result["message_result_timeout"] = True
                    result["message_result"] = {
                        "message_id": message_id,
                        "success": False,
                        "error": "timeout",
                    }

            except Exception as e:
                result["message_result_error"] = str(e)
                result["message_result"] = {
                    "message_id": message_id,
                    "success": False,
                    "error": str(e),
                }

        return result

    def _log_broadcast_result(
        self, tenant_id: str, platform: str, result: Dict[str, Any]
    ):
        """记录广播结果"""
        success_count = result.get("success_count", 0)
        failed_count = result.get("failed_count", 0)
        total = result.get("total_connections", 0)

        if success_count > 0:
            logger.info(
                f"广播成功: 租户 {tenant_id} 平台 {platform} - "
                f"成功 {success_count}/{total} 个连接"
            )

        if failed_count > 0:
            logger.warning(
                f"广播部分失败: 租户 {tenant_id} 平台 {platform} - "
                f"失败 {failed_count}/{total} 个连接"
            )

        if success_count == 0 and total > 0:
            logger.error(
                f"广播完全失败: 租户 {tenant_id} 平台 {platform} - "
                f"所有 {total} 个连接都失败"
            )


# 便捷函数
async def create_broadcast_api(
    base_url: str, api_key: Optional[str] = None
) -> TenantBroadcastAPI:
    """
    创建广播API客户端的便捷函数

    Args:
        base_url: 基础URL
        api_key: API密钥

    Returns:
        广播API客户端实例
    """
    return TenantBroadcastAPI(base_url, api_key)


# 使用示例
async def broadcast_example():
    """广播API使用示例"""
    async with TenantBroadcastAPI(
        base_url="https://message-server.example.com:8091", api_key="your_api_key"
    ) as api:
        tenant_id = "tenant1"
        platform = "qq"

        # 检查平台状态
        status = await api.check_tenant_platform_status(tenant_id, platform)
        print(f"平台状态: {status}")

        if status["active_connections"] > 0:
            # 向平台广播消息
            message = {
                "type": "system_announcement",
                "content": "系统维护通知",
                "timestamp": 1234567890,
                "metadata": {"priority": "high", "source": "admin_console"},
            }

            result = await api.broadcast_with_verification(
                tenant_id=tenant_id,
                platform=platform,
                message=message,
                wait_for_result=True,
                timeout=15.0,
            )

            print(f"广播结果: {result}")

            # 获取租户连接信息
            connections = await api.get_tenant_connections(tenant_id)
            print(f"租户连接: {connections}")
        else:
            print(f"平台 {platform} 没有活跃连接，跳过广播")


# 管理工具示例
async def management_example():
    """管理工具使用示例"""
    async with TenantBroadcastAPI(
        base_url="https://message-server.example.com:8091"
    ) as api:
        tenant_id = "tenant1"

        try:
            # 列出租户的所有平台连接
            connections = await api.get_tenant_connections(tenant_id)

            # 按平台分组统计
            platform_stats = {}
            for conn in connections.get("connections", []):
                platform = conn.get("platform", "unknown")
                if platform not in platform_stats:
                    platform_stats[platform] = {"total": 0, "active": 0}

                platform_stats[platform]["total"] += 1
                if conn.get("is_connected", False):
                    platform_stats[platform]["active"] += 1

            print(f"租户 {tenant_id} 连接统计:")
            for platform, stats in platform_stats.items():
                print(f"  {platform}: {stats['active']}/{stats['total']} 活跃连接")

            # 如果需要，清空某个租户的重试池
            # await api.flush_retry_pool(tenant_id)

        except Exception as e:
            print(f"管理操作失败: {e}")


if __name__ == "__main__":
    print("运行广播示例...")
    asyncio.run(broadcast_example())

    print("\n运行管理示例...")
    asyncio.run(management_example())
