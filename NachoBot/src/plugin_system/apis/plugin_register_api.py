from pathlib import Path

from src.common.logger import get_logger

logger = get_logger("plugin_manager")  # 复用plugin_manager名称


def register_plugin(cls):
    from src.plugin_system.core.plugin_manager import plugin_manager
    from src.plugin_system.base.base_plugin import BasePlugin

    """插件注册装饰器

    用法:
        @register_plugin
        class MyPlugin(BasePlugin):
            plugin_name = "my_plugin"
            plugin_description = "我的插件"
            ...
    """
    if not issubclass(cls, BasePlugin):
        logger.error(f"类 {cls.__name__} 不是 BasePlugin 的子类")
        return cls

    # 只是注册插件类，不立即实例化
    # 插件管理器会负责实例化和注册
    plugin_name: str = cls.plugin_name  # type: ignore
    if "." in plugin_name:
        logger.error(f"插件名称 '{plugin_name}' 包含非法字符 '.'，请使用下划线替代")
        raise ValueError(f"插件名称 '{plugin_name}' 包含非法字符 '.'，请使用下划线替代")
    splitted_name = cls.__module__.split(".")
    root_path = Path(__file__)

    # 查找项目根目录
    while not (root_path / "pyproject.toml").exists() and root_path.parent != root_path:
        root_path = root_path.parent

    if not (root_path / "pyproject.toml").exists():
        logger.error(f"注册 {plugin_name} 无法找到项目根目录")
        return cls

    plugin_manager.plugin_classes[plugin_name] = cls
    plugin_manager.plugin_paths[plugin_name] = str(Path(root_path, *splitted_name).resolve())
    logger.debug(f"插件类已注册: {plugin_name}, 路径: {plugin_manager.plugin_paths[plugin_name]}")

    return cls
