"""
预约提醒调度器

管理所有定时提醒任务，支持持久化缓存（重启不丢失）。
所有操作基于 (chat_id, user_id) 组合，支持群聊场景。

使用方式：
    from src.chat.heart_flow.appointment_scheduler import appointment_scheduler

    # 添加预约
    appt_id = await appointment_scheduler.schedule(
        chat_id="xxx", user_id="123",
        remind_datetime=datetime(...),
        remind_content="喝水",
        remind_text="时间到啦！该喝水了~"
    )

    # 取消预约
    appointment_scheduler.cancel_by_id(appt_id)

    # 获取某用户的待执行预约
    pending = appointment_scheduler.get_pending(chat_id="xxx", user_id="123")
"""

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from src.common.logger import get_logger
from src.plugin_system.apis import send_api

logger = get_logger("appointment_scheduler")

# 缓存文件路径
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data")
_CACHE_FILE = os.path.join(_CACHE_DIR, "appointment_cache.json")

# 中文数字映射
_CN_NUM = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "半": 30,
}


def _cn_to_int(s: str) -> int:
    """将中文数字转为整数（支持一到九十九）"""
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    # 十几、几十、几十几
    total = 0
    if "十" in s:
        parts = s.split("十")
        tens = _CN_NUM.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        total = tens * 10 + ones
    return total if total else 1


def parse_remind_time(raw: str, tz_hours: int = 8) -> Optional[datetime]:
    """
    解析各种格式的提醒时间，返回 aware datetime 或 None。

    支持格式:
        1. 纯相对: +5m, +1h, +30s
        2. 混合: ISO8601+5m
        3. 纯 ISO 8601
        4. 中文自然语言: 五分钟后, 半小时后, 两小时后, 30秒后
    """
    tz_local = timezone(timedelta(hours=tz_hours))
    now = datetime.now(tz_local)
    text = raw.strip()

    # 1. 纯相对: +5m, +1h, +30s
    m = re.match(r"^\+(\d+)([smh])$", text, re.IGNORECASE)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        delta = {"s": timedelta(seconds=amount), "m": timedelta(minutes=amount), "h": timedelta(hours=amount)}
        return now + delta[unit]

    # 2. 混合: ISO8601+相对
    hm = re.search(r"\+(\d+)([smh])$", text, re.IGNORECASE)
    if hm:
        amount = int(hm.group(1))
        unit = hm.group(2).lower()
        delta = {"s": timedelta(seconds=amount), "m": timedelta(minutes=amount), "h": timedelta(hours=amount)}
        iso_part = text[: hm.start()]
        try:
            base = datetime.fromisoformat(iso_part)
            if base.tzinfo is None:
                base = base.replace(tzinfo=tz_local)
            return base + delta[unit]
        except (ValueError, TypeError):
            return now + delta[unit]

    # 3. 纯 ISO 8601
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz_local)
        return dt
    except (ValueError, TypeError):
        pass

    # 4. 中文自然语言: "五分钟后", "半小时后", "30秒后", "两个小时后"
    cn_m = re.search(
        r"(\d+|[零一二两三四五六七八九十半]+)\s*个?\s*(分钟|小时|秒钟?|分|时|hours?|minutes?|mins?|secs?|hrs?)",
        text,
        re.IGNORECASE,
    )
    if cn_m:
        amount_str = cn_m.group(1)
        unit_str = cn_m.group(2)
        if amount_str == "半":
            # "半小时" -> 30 minutes, "半分钟" -> 30 seconds
            if "时" in unit_str or "hour" in unit_str.lower() or "hr" in unit_str.lower():
                return now + timedelta(minutes=30)
            else:
                return now + timedelta(seconds=30)
        amount = _cn_to_int(amount_str) if not amount_str.isdigit() else int(amount_str)
        if amount <= 0:
            amount = 1
        if "时" in unit_str or "hour" in unit_str.lower() or "hr" in unit_str.lower():
            return now + timedelta(hours=amount)
        elif "分" in unit_str or "min" in unit_str.lower():
            return now + timedelta(minutes=amount)
        elif "秒" in unit_str or "sec" in unit_str.lower():
            return now + timedelta(seconds=amount)

    # 5. 钟点时间: "1点", "十点", "3点半", "下午2点", "今晚10点"
    clock_m = re.search(
        r"(?:今天|今晚|明天|下午|上午|晚上|早上)?\s*(\d+|[零一二两三四五六七八九十]+)\s*点\s*(半|(\d+|[一二三四五]十?)分?)?",
        text,
    )
    if clock_m:
        hour = _cn_to_int(clock_m.group(1))
        minute = 0
        if clock_m.group(2) == "半":
            minute = 30
        elif clock_m.group(2):
            minute = _cn_to_int(clock_m.group(3) or clock_m.group(2))

        # 判断 AM/PM
        if "下午" in text or "晚上" in text or "今晚" in text:
            if hour < 12:
                hour += 12
        elif "上午" in text or "早上" in text:
            pass  # keep as-is
        else:
            # 无明确AM/PM: 如果该时间今天已过，假定明天凌晨/上午
            # 但对于 1-6 这种小时数，更可能是指下午/晚上(13-18)
            if 1 <= hour <= 6:
                hour += 12  # 默认视为下午

        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if "明天" in text:
            target += timedelta(days=1)
        elif target <= now:
            target += timedelta(days=1)

        return target

    return None


class AppointmentScheduler:
    """预约提醒调度器（单例）"""

    def __init__(self):
        self._appointments: Dict[str, dict] = {}  # id -> appointment data
        self._tasks: Dict[str, asyncio.Task] = {}  # id -> asyncio.Task
        self._load_cache()

    def _load_cache(self):
        """从磁盘加载缓存"""
        try:
            if os.path.exists(_CACHE_FILE):
                with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                now = datetime.now(timezone.utc)
                for appt in data:
                    remind_time = datetime.fromisoformat(appt["remind_time_iso"])
                    if remind_time > now:
                        self._appointments[appt["id"]] = appt
                    else:
                        logger.debug(f"跳过已过期预约: {appt['id']} ({appt['remind_content']})")
                logger.info(f"加载了 {len(self._appointments)} 个待执行预约")
        except Exception as e:
            logger.error(f"加载预约缓存失败: {e}")
            self._appointments = {}

    def _save_cache(self):
        """保存缓存到磁盘"""
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(list(self._appointments.values()), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存预约缓存失败: {e}")

    async def resume_pending(self):
        """恢复所有未过期预约的 asyncio.Task（bot 启动后调用）"""
        now = datetime.now(timezone.utc)
        resumed = 0
        for appt_id, appt in list(self._appointments.items()):
            remind_time = datetime.fromisoformat(appt["remind_time_iso"])
            if remind_time > now:
                self._create_task(appt_id, appt)
                resumed += 1
            else:
                # 启动时发现已过期，清除
                del self._appointments[appt_id]
        if resumed:
            logger.info(f"恢复了 {resumed} 个预约任务")
            self._save_cache()

    async def schedule(
        self,
        chat_id: str,
        user_id: str,
        remind_datetime: datetime,
        remind_content: str,
        remind_text: str,
    ) -> str:
        """
        注册一个预约提醒

        Args:
            chat_id: 聊天流 ID
            user_id: 用户 ID
            remind_datetime: 提醒时间 (aware datetime)
            remind_content: 提醒事项（用户原始描述）
            remind_text: 预生成的提醒消息（由 replyer 生成）

        Returns:
            预约 ID
        """
        appt_id = str(uuid.uuid4())[:8]
        appt = {
            "id": appt_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "remind_time_iso": remind_datetime.isoformat(),
            "remind_content": remind_content,
            "remind_text": remind_text,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        self._appointments[appt_id] = appt
        self._save_cache()
        self._create_task(appt_id, appt)

        logger.info(
            f"预约已注册: id={appt_id}, chat={chat_id}, user={user_id}, "
            f"time={remind_datetime.isoformat()}, content={remind_content}"
        )
        return appt_id

    def _create_task(self, appt_id: str, appt: dict):
        """为预约创建 asyncio.Task"""
        task = asyncio.create_task(self._wait_and_send(appt_id, appt))
        self._tasks[appt_id] = task

    async def _wait_and_send(self, appt_id: str, appt: dict):
        """等待到目标时间后发送提醒"""
        try:
            remind_time = datetime.fromisoformat(appt["remind_time_iso"])
            now = datetime.now(timezone.utc)
            delay = (remind_time - now).total_seconds()

            if delay > 0:
                logger.debug(f"预约 {appt_id} 将在 {delay:.0f} 秒后触发")
                await asyncio.sleep(delay)

            # 发送提醒
            chat_id = appt["chat_id"]
            remind_text = appt["remind_text"]

            logger.info(f"预约 {appt_id} 触发，发送提醒到 {chat_id}")
            success = await send_api.text_to_stream(
                text=remind_text,
                stream_id=chat_id,
                typing=True,
            )

            if success:
                logger.info(f"预约 {appt_id} 提醒发送成功")
            else:
                logger.error(f"预约 {appt_id} 提醒发送失败")

        except asyncio.CancelledError:
            logger.debug(f"预约 {appt_id} 已被取消")
        except Exception as e:
            logger.error(f"预约 {appt_id} 执行失败: {e}", exc_info=True)
        finally:
            # 无论成功/失败/取消，从缓存中清除
            self._appointments.pop(appt_id, None)
            self._tasks.pop(appt_id, None)
            self._save_cache()

    def cancel_by_content(self, chat_id: str, user_id: str, remind_content: str) -> List[dict]:
        """
        根据内容模糊匹配预约，返回匹配列表（不自动取消）

        Args:
            chat_id: 聊天流 ID
            user_id: 用户 ID
            remind_content: 用户描述的要取消的内容

        Returns:
            匹配的预约列表
        """
        matches = []
        content_lower = remind_content.lower()
        for appt in self._appointments.values():
            if appt["chat_id"] == chat_id and appt["user_id"] == user_id:
                if content_lower in appt["remind_content"].lower():
                    matches.append(appt)
        return matches

    def cancel_by_id(self, appointment_id: str) -> bool:
        """
        精确取消单个预约

        Args:
            appointment_id: 预约 ID

        Returns:
            是否成功取消
        """
        if appointment_id not in self._appointments:
            return False

        # 取消 asyncio.Task
        task = self._tasks.pop(appointment_id, None)
        if task and not task.done():
            task.cancel()

        # 从缓存移除
        self._appointments.pop(appointment_id, None)
        self._save_cache()

        logger.info(f"预约 {appointment_id} 已取消")
        return True

    def get_pending(self, chat_id: str, user_id: Optional[str] = None) -> List[dict]:
        """
        获取待执行预约列表

        Args:
            chat_id: 聊天流 ID
            user_id: 用户 ID（可选，不提供则返回该聊天所有预约）

        Returns:
            待执行预约列表
        """
        now = datetime.now(timezone.utc)
        result = []
        for appt in self._appointments.values():
            if appt["chat_id"] != chat_id:
                continue
            if user_id and appt["user_id"] != user_id:
                continue
            remind_time = datetime.fromisoformat(appt["remind_time_iso"])
            if remind_time > now:
                result.append(appt)
        return sorted(result, key=lambda x: x["remind_time_iso"])


# 模块级单例
appointment_scheduler = AppointmentScheduler()
