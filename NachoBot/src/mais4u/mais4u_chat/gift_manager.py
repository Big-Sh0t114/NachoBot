import asyncio
from typing import Dict, Tuple, Callable, Optional
from dataclasses import dataclass

from src.chat.message_receive.message import MessageRecvS4U
from src.common.logger import get_logger

logger = get_logger("gift_manager")


@dataclass
class PendingGift:
    """等待中的礼物消息"""

    message: MessageRecvS4U
    total_count: int
    timer_task: asyncio.Task
    callback: Callable[[MessageRecvS4U], None]
    total_price: float = 0.0


def _parse_gift_price(message: MessageRecvS4U) -> float:
    """从消息的 raw_message 中解析礼物价格（CNY）"""
    try:
        if message.raw_message:
            import json
            raw = message.raw_message
            if isinstance(raw, str):
                raw = json.loads(raw)
            if isinstance(raw, dict):
                return float(raw.get("price", 0.0))
    except Exception:
        pass
    return 0.0


class GiftManager:
    """礼物管理器，提供防抖功能"""

    def __init__(self):
        """初始化礼物管理器"""
        self.pending_gifts: Dict[Tuple[str, str], PendingGift] = {}
        self.debounce_timeout = 5.0  # 3秒防抖时间

    async def handle_gift(
        self, message: MessageRecvS4U, callback: Optional[Callable[[MessageRecvS4U], None]] = None
    ) -> bool:
        """处理礼物消息，返回是否应该立即处理

        Args:
            message: 礼物消息
            callback: 防抖完成后的回调函数

        Returns:
            bool: False表示消息被暂存等待防抖，True表示应该立即处理
        """
        if not message.is_gift:
            return True

        # 构建礼物的唯一键：(发送人ID, 礼物名称)
        gift_key = (message.message_info.user_info.user_id, message.gift_name)

        # 如果已经有相同的礼物在等待中，则合并
        if gift_key in self.pending_gifts:
            await self._merge_gift(gift_key, message)
            return False

        # 创建新的等待礼物
        await self._create_pending_gift(gift_key, message, callback)
        return False

    async def _merge_gift(self, gift_key: Tuple[str, str], new_message: MessageRecvS4U) -> None:
        """合并礼物消息"""
        pending_gift = self.pending_gifts[gift_key]

        # 取消之前的定时器
        if not pending_gift.timer_task.cancelled():
            pending_gift.timer_task.cancel()

        # 累加礼物数量
        try:
            new_count = int(new_message.gift_count)
            pending_gift.total_count += new_count
            pending_gift.total_price += _parse_gift_price(new_message)

            # 更新消息为最新的（保留最新的消息，但累加数量）
            pending_gift.message = new_message
            pending_gift.message.gift_count = str(pending_gift.total_count)
            pending_gift.message.gift_info = f"{pending_gift.message.gift_name}:{pending_gift.total_count}"

        except ValueError:
            logger.warning(f"无法解析礼物数量: {new_message.gift_count}")
            # 如果无法解析数量，保持原有数量不变

        # 重新创建定时器
        pending_gift.timer_task = asyncio.create_task(self._gift_timeout(gift_key))

        logger.debug(f"合并礼物: {gift_key}, 总数量: {pending_gift.total_count}, 当前累计价值: {pending_gift.total_price} 元")

    async def _create_pending_gift(
        self, gift_key: Tuple[str, str], message: MessageRecvS4U, callback: Optional[Callable[[MessageRecvS4U], None]]
    ) -> None:
        """创建新的等待礼物"""
        try:
            initial_count = int(message.gift_count)
        except ValueError:
            initial_count = 1
            logger.warning(f"无法解析礼物数量: {message.gift_count}，默认设为1")

        # 创建定时器任务
        timer_task = asyncio.create_task(self._gift_timeout(gift_key))

        initial_price = _parse_gift_price(message)

        # 创建等待礼物对象
        pending_gift = PendingGift(
            message=message,
            total_count=initial_count,
            timer_task=timer_task,
            callback=callback,
            total_price=initial_price
        )

        self.pending_gifts[gift_key] = pending_gift

        logger.debug(f"创建等待礼物: {gift_key}, 初始数量: {initial_count}, 初始价值: {initial_price} 元")

    async def _gift_timeout(self, gift_key: Tuple[str, str]) -> None:
        """礼物防抖超时处理"""
        try:
            # 等待防抖时间
            await asyncio.sleep(self.debounce_timeout)

            # 获取等待中的礼物
            if gift_key not in self.pending_gifts:
                return

            pending_gift = self.pending_gifts.pop(gift_key)

            logger.info(f"礼物防抖完成: {gift_key}, 最终数量: {pending_gift.total_count}, 累计价值: {pending_gift.total_price} 元")

            message = pending_gift.message
            message.processed_plain_text = f"用户{message.message_info.user_info.user_nickname}送出了礼物{message.gift_name} x{pending_gift.total_count}"

            # 更新用户礼物打赏记忆点（仅当价格 > 0 时）
            if pending_gift.total_price > 0:
                try:
                    await _update_user_gift_memory(
                        platform="bilibili.live",
                        user_id=message.message_info.user_info.user_id,
                        user_name=message.message_info.user_info.user_nickname,
                        amount_cny=pending_gift.total_price,
                    )
                except Exception as e:
                    logger.error(f"更新用户礼物记忆失败: {e}", exc_info=True)

            # 执行回调
            if pending_gift.callback:
                try:
                    pending_gift.callback(message)
                except Exception as e:
                    logger.error(f"礼物回调执行失败: {e}", exc_info=True)

        except asyncio.CancelledError:
            # 定时器被取消，不需要处理
            pass
        except Exception as e:
            logger.error(f"礼物防抖处理异常: {e}", exc_info=True)

    def get_pending_count(self) -> int:
        """获取当前等待中的礼物数量"""
        return len(self.pending_gifts)

    async def flush_all(self) -> None:
        """立即处理所有等待中的礼物"""
        for gift_key in list(self.pending_gifts.keys()):
            pending_gift = self.pending_gifts.get(gift_key)
            if pending_gift and not pending_gift.timer_task.cancelled():
                pending_gift.timer_task.cancel()
                await self._gift_timeout(gift_key)


async def _update_user_gift_memory(
    platform: str, user_id: str, user_name: str, amount_cny: float
) -> None:
    """更新用户的直播打赏累计记忆。如果用户未注册则先注册。"""
    from src.person_info.person_info import Person

    person = Person(platform=platform, user_id=user_id)
    if not person.is_known:
        # 用户未注册，先注册
        person = Person.register_person(
            platform=platform, user_id=user_id, nickname=user_name
        )
        if not person:
            logger.warning(f"注册用户失败: {platform}:{user_id}:{user_name}")
            return

    person.update_gift_value(amount_cny)


# 创建全局礼物管理器实例
gift_manager = GiftManager()
