from loguru import logger
from .config import logging_level
import sys

logger.remove()
logger.add(
    sys.stderr,
    level=logging_level,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)
