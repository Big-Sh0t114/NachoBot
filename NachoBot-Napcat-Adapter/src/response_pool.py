import asyncio
import time
from typing import Dict
from .config import global_config
from .logger import logger

response_dict: Dict = {}
response_time_dict: Dict = {}


async def get_response(request_id: str, timeout: int = 10) -> dict:
    response = await asyncio.wait_for(_get_response(request_id), timeout)
    _ = response_time_dict.pop(request_id)
    logger.trace(f"响应信息id: {request_id} 已从响应字典中取出")
    return response

async def _get_response(request_id: str) -> dict:
    """
    内部使用的获取响应函数，主要用于在需要时获取响应
    """
    while request_id not in response_dict:
        await asyncio.sleep(0.2)
    return response_dict.pop(request_id)

async def put_response(response: dict):
    echo_id = response.get("echo")
    now_time = time.time()
    response_dict[echo_id] = response
    response_time_dict[echo_id] = now_time
    logger.trace(f"响应信息id: {echo_id} 已存入响应字典")


async def check_timeout_response() -> None:
    while True:
        try:
            cleaned_message_count: int = 0
            now_time = time.time()
            for echo_id, response_time in list(response_time_dict.items()):
                if now_time - response_time > global_config.napcat_server.heartbeat_interval:
                    cleaned_message_count += 1
                    # 使用 pop(..., None) 防止并发读写导致的 KeyError 直接干掉整个循环
                    response_dict.pop(echo_id, None)
                    response_time_dict.pop(echo_id, None)
                    logger.warning(f"响应消息 {echo_id} 超时，已删除")
            logger.info(f"已删除 {cleaned_message_count} 条超时响应消息")
        except Exception as exc:
            logger.exception(f"清理超时响应时出现异常: {exc}")
        await asyncio.sleep(global_config.napcat_server.heartbeat_interval)
