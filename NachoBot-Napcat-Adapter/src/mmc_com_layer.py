from ncnk_message import Router, RouteConfig, TargetConfig, get_core_token_from_env
from .config import global_config
from .logger import logger, custom_logger
from .send_handler.main_send_handler import send_handler

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
    await router.run()


async def mmc_stop_com():
    await router.stop()
