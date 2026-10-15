import asyncio

from ncnk_message import Router, RouteConfig, TargetConfig, get_core_token_from_env
from .config import global_config
from .logger import logger, custom_logger
from .platform_api_handler import handle_platform_api_request
from .send_handler.main_send_handler import send_handler
from .offline_history import get_offline_history_runtime
from .recv_handler.message_handler import message_handler
from .qqcall_runtime import (
    clear_qqcall_runtime,
    configure_qqcall_runtime,
    get_qqcall_runtime,
)

route_config = RouteConfig(
    route_config={
        global_config.nachobot_server.platform_name: TargetConfig(
            url=f"ws://{global_config.nachobot_server.host}:{global_config.nachobot_server.port}/ws",
            token=get_core_token_from_env(),
        )
    }
)
router = Router(route_config, custom_logger)


async def mmc_start_com():
    logger.info("正在连接Nachobot")
    router.register_class_handler(send_handler.handle_message)
    router.register_custom_message_handler("platform_api_request", handle_platform_api_request)
    qqcall_runtime = configure_qqcall_runtime(
        global_config.qqcall,
        router=router,
        message_handler=message_handler,
        platform=global_config.nachobot_server.platform_name,
    )
    router.register_custom_message_handler("qqcall_request", qqcall_runtime.handle_request)
    qqcall_task = asyncio.create_task(qqcall_runtime.run(), name="napcat-qqcall-runtime")
    get_offline_history_runtime().register_core_handlers(router)
    try:
        await router.run()
    finally:
        await qqcall_runtime.stop()
        if not qqcall_task.done():
            await qqcall_task
        clear_qqcall_runtime(qqcall_runtime)


async def mmc_stop_com():
    qqcall_runtime = get_qqcall_runtime()
    if qqcall_runtime is not None:
        await qqcall_runtime.stop()
    await router.stop()
