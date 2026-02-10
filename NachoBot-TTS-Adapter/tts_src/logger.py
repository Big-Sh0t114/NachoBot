from loguru import logger
import sys

LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def set_logging_level(level: str) -> None:
    """Reconfigure loguru level at runtime."""
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=LOG_FORMAT)


# 初始使用 config.py 中的默认值（后续在主程序加载配置后可调用 set_logging_level 覆盖）
set_logging_level("INFO")
