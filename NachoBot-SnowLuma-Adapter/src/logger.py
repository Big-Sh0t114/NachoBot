import sys
from loguru import logger as _logger
from .config import global_config

_logger.remove()
_logger.add(
    sys.stderr,
    level=global_config.debug.level,
    format=(
        "<blue>{time:YYYY-MM-DD HH:mm:ss}</blue> | <level>{level: <8}</level> | "
        "<cyan>{extra[name]}</cyan> | <cyan>{module}:{function}:{line}</cyan> - <level>{message}</level>"
    ),
    filter=lambda record: record["extra"].get("name") != "ncnk_message",
)
_logger.add(
    sys.stderr,
    level="INFO",
    format=(
        "<red>{time:YYYY-MM-DD HH:mm:ss}</red> | <level>{level: <8}</level> | "
        "<cyan>{extra[name]}</cyan> | <cyan>{module}:{function}:{line}</cyan> - <level>{message}</level>"
    ),
    filter=lambda record: record["extra"].get("name") == "ncnk_message",
)
custom_logger = _logger.bind(name="ncnk_message")
logger = _logger.bind(name="NachoBot-SnowLuma-Adapter")
