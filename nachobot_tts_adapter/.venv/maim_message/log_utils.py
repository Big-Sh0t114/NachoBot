import logging
import sys
from typing import Optional

# 默认日志格式
DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# 全局日志对象
_logger = None


def setup_logger(
    name: str = "maim_message",
    format_str: str = DEFAULT_FORMAT,
    external_logger: Optional[logging.Logger] = None,
) -> logging.Logger:
    """
    配置并返回日志记录器

    Args:
        name: 日志记录器名称
        format_str: 日志格式
        external_logger: 外部提供的日志记录器

    Returns:
        已配置的日志记录器
    """
    global _logger

    if external_logger:
        _logger = external_logger
        return _logger

    if _logger is not None:
        return _logger

    # 创建新的日志记录器
    logger = logging.getLogger(name)

    # 使用默认INFO级别
    logger.setLevel(logging.INFO)

    # 创建处理器和格式化器
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(format_str)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    _logger = logger
    return logger


def get_logger() -> logging.Logger:
    """
    获取已配置的日志记录器，如果未配置则创建一个默认的

    Returns:
        日志记录器
    """
    global _logger
    if _logger is None:
        return setup_logger()
    return _logger


def set_external_logger(external_logger: logging.Logger):
    """
    设置外部日志记录器作为全局日志记录器。这是外部系统集成的首选方法。

    Args:
        external_logger: 外部提供的日志记录器

    Returns:
        已设置的日志记录器
    """
    return setup_logger(external_logger=external_logger)


def reset_logger():
    """
    重置日志记录器到未初始化状态，以便重新配置
    """
    global _logger
    _logger = None


def configure_uvicorn_logging():
    """
    配置uvicorn日志系统以使用我们的日志设置
    """
    # 获取当前日志级别，默认使用INFO
    level = logging.INFO
    disable_all_logs = False

    try:
        current_logger = get_logger()

        # 检测是否是loguru的Logger
        if (
            hasattr(current_logger, "__class__")
            and str(current_logger.__class__).find("loguru") >= 0
        ):
            # loguru Logger的处理，直接使用默认INFO级别
            pass
        else:
            # 标准logging Logger的处理
            level = getattr(current_logger, "level", logging.INFO)
            if callable(level) or not isinstance(level, int):
                disable_all_logs = True
                level = logging.CRITICAL  # 如果level不是整数，使用CRITICAL级别静默日志
    except Exception:
        # 如果出现错误，禁用所有日志
        disable_all_logs = True
        level = logging.CRITICAL
        pass

    # 获取或创建一个格式化器
    formatter = logging.Formatter(DEFAULT_FORMAT)
    try:
        current_logger = get_logger()
        if (
            hasattr(current_logger, "__class__")
            and str(current_logger.__class__).find("loguru") >= 0
        ):
            # loguru Logger不尝试获取formatter
            pass
        elif current_logger.handlers:
            formatter = current_logger.handlers[0].formatter
    except Exception:
        # 如果出现错误，使用默认格式化器
        pass

    # 配置uvicorn相关的日志记录器
    loggers = [
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "uvicorn.asgi",
    ]

    for logger_name in loggers:
        logger = logging.getLogger(logger_name)
        # 移除所有现有处理器
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)

        # 添加到控制台的处理器
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        # 设置日志级别
        logger.setLevel(level)
        logger.propagate = False  # 避免日志重复

    return True


def get_uvicorn_log_config() -> dict:
    """
    获取适用于uvicorn的日志配置字典

    Returns:
        uvicorn日志配置字典
    """
    # 获取当前日志级别，设置默认值为INFO
    default_level_name = "INFO"
    disable_all_logs = False

    try:
        current_logger = get_logger()

        # 检测是否是loguru的Logger
        if (
            hasattr(current_logger, "__class__")
            and str(current_logger.__class__).find("loguru") >= 0
        ):
            # loguru Logger的处理，直接使用默认INFO级别
            level_name = default_level_name
        else:
            # 标准logging Logger的处理
            level = getattr(current_logger, "level", logging.INFO)

            # 将日志级别转换为uvicorn可接受的字符串格式
            if isinstance(level, int):
                if level <= logging.DEBUG:
                    level_name = "DEBUG"
                elif level <= logging.INFO:
                    level_name = "INFO"
                elif level <= logging.WARNING:
                    level_name = "WARNING"
                elif level <= logging.ERROR:
                    level_name = "ERROR"
                else:
                    level_name = "CRITICAL"
            elif callable(level):
                # 如果level是一个方法，标记为禁用所有日志
                disable_all_logs = True
                level_name = default_level_name
            else:
                # 尝试将字符串转换为大写，如果不是字符串则使用默认值
                try:
                    level_name = str(level).upper()
                    # 检查是否包含bound method等无效内容
                    if "bound method" in level_name or len(level_name) > 30:
                        disable_all_logs = True
                        level_name = default_level_name
                except (AttributeError, TypeError):
                    disable_all_logs = True
                    level_name = default_level_name
    except Exception:
        # 如果出现任何错误，标记为禁用所有日志
        disable_all_logs = True
        level_name = default_level_name

    # 从当前记录器获取格式
    log_format = DEFAULT_FORMAT
    try:
        current_logger = get_logger()
        # 检测是否是loguru的Logger
        if (
            hasattr(current_logger, "__class__")
            and str(current_logger.__class__).find("loguru") >= 0
        ):
            # loguru Logger不尝试获取格式，使用默认格式
            pass
        elif current_logger.handlers:
            formatter = current_logger.handlers[0].formatter
            if hasattr(formatter, "_fmt"):
                log_format = formatter._fmt
    except Exception:
        # 如果出现任何错误，使用默认格式并标记为禁用日志
        disable_all_logs = True
        pass

    # 如果需要禁用所有日志输出，将所有日志级别设置为CRITICAL
    if disable_all_logs:
        level_name = "CRITICAL"
        logger = get_logger()
        logger.info("由于logger配置提取错误，已禁用uvicorn的所有日志输出")

    # 返回uvicorn配置
    return {
        "version": 1,
        "disable_existing_loggers": True if disable_all_logs else False,
        "formatters": {
            "default": {
                "format": log_format,
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.NullHandler"
                if disable_all_logs
                else "logging.StreamHandler",
                "stream": sys.stderr if not disable_all_logs else None,
            },
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["default"],
                "level": level_name,
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["default"],
                "level": level_name,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["default"],
                "level": level_name,
                "propagate": False,
            },
        },
    }
