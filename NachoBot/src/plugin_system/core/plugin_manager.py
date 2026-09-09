import os
import importlib
import importlib.metadata
import inspect
import sys
import traceback
import asyncio
import types

from typing import Dict, List, Optional, Tuple, Type, Any
from importlib.util import cache_from_source, spec_from_file_location, module_from_spec
import hashlib
import re
from pathlib import Path
from packaging.requirements import InvalidRequirement, Requirement

# 将兼容层包目录加入 sys.path，保证上游插件运行时能无缝 "import maibot_sdk"
compat_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "compatibility"))
if compat_dir not in sys.path:
    sys.path.insert(0, compat_dir)

from src.common.logger import get_logger
from src.plugin_system.base.plugin_base import PluginBase
from src.plugin_system.base.component_types import ComponentType
from src.plugin_system.utils.manifest_utils import VersionComparator
from .component_registry import component_registry

logger = get_logger("plugin_manager")


class PluginManager:
    """
    插件管理器类

    负责加载，重载和卸载插件，同时管理插件的所有组件
    """

    def __init__(self):
        self.plugin_directories: List[str] = []  # 插件根目录列表
        self.plugin_classes: Dict[str, Type[PluginBase]] = {}  # 全局插件类注册表，插件名 -> 插件类
        self.plugin_paths: Dict[str, str] = {}  # 记录插件名到目录路径的映射，插件名 -> 目录路径

        self.loaded_plugins: Dict[str, PluginBase] = {}  # 已加载的插件类实例注册表，插件名 -> 插件类实例
        self.failed_plugins: Dict[str, str] = {}  # 记录加载失败的插件文件及其错误信息，插件名 -> 错误信息
        self.plugin_requirements: Dict[str, List[str]] = {}
        self._plugin_locks: Dict[str, asyncio.Lock] = {}

        # 确保插件目录存在
        self._ensure_plugin_directories()
        logger.info("插件管理器初始化完成")

    # === 插件目录管理 ===

    def add_plugin_directory(self, directory: str) -> bool:
        """添加插件目录"""
        if os.path.exists(directory):
            if directory not in self.plugin_directories:
                self.plugin_directories.append(directory)
                logger.debug(f"已添加插件目录: {directory}")
                return True
            else:
                logger.warning(f"插件不可重复加载: {directory}")
        else:
            logger.warning(f"插件目录不存在: {directory}")
        return False

    # === 插件加载管理 ===

    def load_all_plugins(self) -> Tuple[int, int]:
        """加载所有插件

        Returns:
            tuple[int, int]: (插件数量, 组件数量)
        """
        logger.debug("开始加载所有插件...")

        # 第一阶段：加载所有插件模块（注册插件类）
        total_loaded_modules = 0
        total_failed_modules = 0

        for directory in self.plugin_directories:
            loaded, failed = self._load_plugin_modules_from_directory(directory)
            total_loaded_modules += loaded
            total_failed_modules += failed

        logger.debug(f"插件模块加载完成 - 成功: {total_loaded_modules}, 失败: {total_failed_modules}")

        total_registered = 0
        total_failed_registration = 0

        for plugin_name in self.plugin_classes.keys():
            load_status, count = self.load_registered_plugin_classes(plugin_name)
            if load_status:
                total_registered += 1
            else:
                total_failed_registration += count

        self._show_stats(total_registered, total_failed_registration)

        return total_registered, total_failed_registration

    def load_registered_plugin_classes(self, plugin_name: str) -> Tuple[bool, int]:
        # sourcery skip: extract-duplicate-method, extract-method
        """
        加载已经注册的插件类
        """
        plugin_class = self.plugin_classes.get(plugin_name)
        if not plugin_class:
            logger.error(f"插件 {plugin_name} 的插件类未注册或不存在")
            return False, 1
        try:
            # 使用记录的插件目录路径
            plugin_dir = self.plugin_paths.get(plugin_name)

            # 如果没有记录，直接返回失败
            if not plugin_dir:
                return False, 1

            plugin_instance = plugin_class(plugin_dir=plugin_dir)  # 实例化插件（可能因为缺少manifest而失败）
            if not plugin_instance:
                logger.error(f"插件 {plugin_name} 实例化失败")
                return False, 1
            # 检查插件是否启用
            if not plugin_instance.enable_plugin:
                logger.info(f"插件 {plugin_name} 已禁用，跳过加载")
                return False, 0

            # 检查版本兼容性
            is_compatible, compatibility_error = self._check_plugin_version_compatibility(
                plugin_name, plugin_instance.manifest_data
            )
            if not is_compatible:
                self.failed_plugins[plugin_name] = compatibility_error
                logger.error(f"❌ 插件加载失败: {plugin_name} - {compatibility_error}")
                return False, 1
            if plugin_instance.register_plugin():
                self.loaded_plugins[plugin_name] = plugin_instance
                self._show_plugin_components(plugin_name)
                return True, 1
            else:
                self.failed_plugins[plugin_name] = "插件注册失败"
                logger.error(f"❌ 插件注册失败: {plugin_name}")
                return False, 1

        except FileNotFoundError as e:
            # manifest文件缺失
            error_msg = f"缺少manifest文件: {str(e)}"
            self.failed_plugins[plugin_name] = error_msg
            logger.error(f"❌ 插件加载失败: {plugin_name} - {error_msg}")
            return False, 1

        except ValueError as e:
            # manifest文件格式错误或验证失败
            traceback.print_exc()
            error_msg = f"manifest验证失败: {str(e)}"
            self.failed_plugins[plugin_name] = error_msg
            logger.error(f"❌ 插件加载失败: {plugin_name} - {error_msg}")
            return False, 1

        except Exception as e:
            # 其他错误
            error_msg = f"未知错误: {str(e)}"
            self.failed_plugins[plugin_name] = error_msg
            logger.error(f"❌ 插件加载失败: {plugin_name} - {error_msg}")
            logger.debug("详细错误信息: ", exc_info=True)
            return False, 1

    async def remove_registered_plugin(self, plugin_name: str) -> bool:
        lock = self._plugin_locks.setdefault(plugin_name, asyncio.Lock())
        async with lock:
            return await self._remove_registered_plugin_locked(plugin_name)

    async def _remove_registered_plugin_locked(self, plugin_name: str) -> bool:
        """
        禁用插件模块
        """
        if not plugin_name:
            raise ValueError("插件名称不能为空")
        if plugin_name not in self.loaded_plugins:
            logger.warning(f"插件 {plugin_name} 未加载")
            return False
        plugin_instance = self.loaded_plugins[plugin_name]
        registry_success = await self._deactivate_plugin_registry(plugin_instance)
        if not registry_success:
            restored = self._restore_plugin_registry(plugin_instance)
            logger.error(f"插件 {plugin_name} 的注册表未完全清理，拒绝执行卸载钩子")
            if not restored:
                logger.critical(f"插件 {plugin_name} 卸载失败后的注册表恢复不完整")
            return False
        unload_success = await self._call_plugin_unload(plugin_instance)
        self.loaded_plugins.pop(plugin_name, None)
        if not unload_success:
            logger.warning(f"插件 {plugin_name} 已从注册表移除，但卸载钩子执行失败")
        return True

    async def reload_registered_plugin(self, plugin_name: str) -> bool:
        lock = self._plugin_locks.setdefault(plugin_name, asyncio.Lock())
        async with lock:
            return await self._reload_registered_plugin_locked(plugin_name)

    async def _reload_registered_plugin_locked(self, plugin_name: str) -> bool:
        """
        重载插件模块
        """
        old_instance = self.loaded_plugins.get(plugin_name)
        old_class = self.plugin_classes.get(plugin_name)
        old_path = self.plugin_paths.get(plugin_name)
        if old_instance is None or old_class is None or not old_path:
            logger.warning(f"插件 {plugin_name} 未加载，无法重载")
            return False

        # 先重新读取模块并实例化候选者；该阶段失败不触碰正在运行的旧实例。
        old_class_present = plugin_name in self.plugin_classes
        old_path_present = plugin_name in self.plugin_paths

        def restore_module_registries() -> None:
            # Restore only this plugin's keys.  A failed scan may legitimately
            # discover or update another plugin concurrently; whole-dict
            # snapshots would clobber that unrelated work.
            self.plugin_classes.pop(plugin_name, None)
            self.plugin_paths.pop(plugin_name, None)
            if old_class_present:
                self.plugin_classes[plugin_name] = old_class
            if old_path_present:
                self.plugin_paths[plugin_name] = old_path

        plugin_file = str(Path(old_path) / "plugin.py")
        package_name = self._package_name_for_plugin_file(plugin_file)
        module_snapshot = self._snapshot_package_modules(package_name)
        registry_snapshot = self._snapshot_plugin_registries()
        self.plugin_classes.pop(plugin_name, None)
        self.plugin_paths.pop(plugin_name, None)
        if not self._load_plugin_module_file(plugin_file):
            self._restore_package_modules(package_name, module_snapshot)
            self._restore_plugin_registry_delta(
                registry_snapshot,
                plugin_name=plugin_name,
                package_name=package_name,
                plugin_dir=str(Path(plugin_file).parent.resolve()),
            )
            restore_module_registries()
            return False

        new_class = self.plugin_classes.get(plugin_name)
        new_path = self.plugin_paths.get(plugin_name, old_path)
        if new_class is None:
            self._restore_package_modules(package_name, module_snapshot)
            self._restore_plugin_registry_delta(
                registry_snapshot,
                plugin_name=plugin_name,
                package_name=package_name,
                plugin_dir=str(Path(plugin_file).parent.resolve()),
            )
            restore_module_registries()
            logger.error(f"重载插件 {plugin_name} 后未找到插件类")
            return False

        try:
            candidate = new_class(plugin_dir=new_path)
            if not candidate.enable_plugin:
                raise ValueError("候选插件已禁用")
            compatible, compatibility_error = self._check_plugin_version_compatibility(
                plugin_name, candidate.manifest_data
            )
            if not compatible:
                raise ValueError(compatibility_error)
        except Exception as exc:
            self._restore_package_modules(package_name, module_snapshot)
            self._restore_plugin_registry_delta(
                registry_snapshot,
                plugin_name=plugin_name,
                package_name=package_name,
                plugin_dir=str(Path(plugin_file).parent.resolve()),
            )
            restore_module_registries()
            self.failed_plugins[plugin_name] = str(exc)
            logger.error(f"重载插件 {plugin_name} 的候选实例失败: {exc}")
            return False

        if not await self._deactivate_plugin_registry(old_instance):
            self._restore_package_modules(package_name, module_snapshot)
            self._restore_plugin_registry_delta(
                registry_snapshot,
                plugin_name=plugin_name,
                package_name=package_name,
                plugin_dir=str(Path(plugin_file).parent.resolve()),
            )
            restore_module_registries()
            if not self._restore_plugin_registry(old_instance):
                logger.critical(f"插件 {plugin_name} 重载失败且旧注册表恢复失败")
            return False

        try:
            candidate_registered = candidate.register_plugin()
        except Exception as exc:
            candidate_registered = False
            self.failed_plugins[plugin_name] = f"插件注册失败: {exc}"
            logger.error(f"重载插件 {plugin_name} 的候选者注册异常: {exc}", exc_info=True)

        if not candidate_registered:
            try:
                await self._deactivate_plugin_registry(candidate)
            except Exception as exc:
                logger.error(f"清理插件 {plugin_name} 的候选注册表失败: {exc}", exc_info=True)
            self._restore_package_modules(package_name, module_snapshot)
            self._restore_plugin_registry_delta(
                registry_snapshot,
                plugin_name=plugin_name,
                package_name=package_name,
                plugin_dir=str(Path(plugin_file).parent.resolve()),
            )
            restore_module_registries()
            self.loaded_plugins[plugin_name] = old_instance
            if not self._restore_plugin_registry(old_instance):
                logger.critical(f"插件 {plugin_name} 候选者注册失败且旧实例恢复失败")
            return False

        self.loaded_plugins[plugin_name] = candidate
        unload_success = await self._call_plugin_unload(old_instance)
        if not unload_success:
            # 新实例已经成功接管注册表，此时不能对外报告“重载失败”，
            # 否则调用方重试会对新实例二次重载。
            logger.warning(f"插件 {plugin_name} 已重载，但旧实例的卸载钩子执行失败")
        self.failed_plugins.pop(plugin_name, None)
        logger.debug(f"插件 {plugin_name} 重载成功")
        return True

    async def _deactivate_plugin_registry(self, plugin_instance: PluginBase) -> bool:
        """停止插件接收新工作，清理组件任务并移除注册表。"""
        plugin_name = plugin_instance.plugin_name
        success = True
        for component in tuple(plugin_instance.plugin_info.components):
            if not await component_registry.remove_component(
                component.name,
                component.component_type,
                plugin_name,
            ):
                success = False
        if not component_registry.remove_plugin_registry(plugin_name):
            success = False
        return success

    def _restore_plugin_registry(self, plugin_instance: PluginBase) -> bool:
        """在卸载/重载中途失败后，只补回缺失的旧组件，不触碰仍存在的注册。"""
        get_components = getattr(plugin_instance, "get_plugin_components", None)
        if not callable(get_components):
            return False
        expected_names = {
            (component.component_type, component.name) for component in plugin_instance.plugin_info.components
        }
        restored_components = []
        success = True
        try:
            definitions = get_components()
        except Exception as exc:
            logger.error(f"读取插件 {plugin_instance.plugin_name} 的组件定义失败: {exc}")
            return False
        for component_info, component_class in definitions:
            key = (component_info.component_type, component_info.name)
            if key not in expected_names:
                continue
            existing_info = component_registry.get_component_info(
                component_info.name,
                component_info.component_type,
            )
            if existing_info is not None:
                if existing_info.plugin_name != plugin_instance.plugin_name:
                    success = False
                    continue
                restored_components.append(existing_info)
                continue
            component_info.plugin_name = plugin_instance.plugin_name
            if component_registry.register_component(component_info, component_class):
                restored_components.append(component_info)
            else:
                success = False

        if len(restored_components) != len(expected_names):
            success = False
        plugin_instance.plugin_info.components = restored_components
        existing_plugin = component_registry.get_plugin_info(plugin_instance.plugin_name)
        if existing_plugin is None:
            if not component_registry.register_plugin(plugin_instance.plugin_info):
                success = False
        elif existing_plugin is not plugin_instance.plugin_info:
            logger.warning(f"恢复插件 {plugin_instance.plugin_name} 时发现其他插件注册对象")
            success = False
        return success

    async def _call_plugin_unload(self, plugin_instance: PluginBase) -> bool:
        """调用 V1/V2 插件的卸载钩子，同时兼容旧的 on_plugin_unload 命名。"""
        v2_plugin = getattr(plugin_instance, "v2_plugin", None)
        if v2_plugin is not None and not getattr(v2_plugin, "_on_load_called", False):
            # SDK V2 的 on_load 是惰性调用，on_unload 只与已执行的 on_load 配对。
            return True
        unload_target = v2_plugin if v2_plugin is not None else plugin_instance
        callback = getattr(unload_target, "on_unload", None)
        if not callable(callback):
            callback = getattr(plugin_instance, "on_plugin_unload", None)
        if not callable(callback):
            return True
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:
            logger.error(f"插件 {plugin_instance.plugin_name} 执行卸载钩子失败: {exc}", exc_info=True)
            return False

    def rescan_plugin_directory(self) -> Tuple[int, int]:
        """
        重新扫描插件根目录
        """
        total_success = 0
        total_fail = 0
        for directory in self.plugin_directories:
            if os.path.exists(directory):
                logger.debug(f"重新扫描插件根目录: {directory}")
                success, fail = self._load_plugin_modules_from_directory(directory)
                total_success += success
                total_fail += fail
            else:
                logger.warning(f"插件根目录不存在: {directory}")
        return total_success, total_fail

    def get_plugin_instance(self, plugin_name: str) -> Optional["PluginBase"]:
        """获取插件实例

        Args:
            plugin_name: 插件名称

        Returns:
            Optional[BasePlugin]: 插件实例或None
        """
        return self.loaded_plugins.get(plugin_name)

    # === 查询方法 ===
    def list_loaded_plugins(self) -> List[str]:
        """
        列出所有当前加载的插件。

        Returns:
            list: 当前加载的插件名称列表。
        """
        return list(self.loaded_plugins.keys())

    def list_registered_plugins(self) -> List[str]:
        """
        列出所有已注册的插件类。

        Returns:
            list: 已注册的插件类名称列表。
        """
        return list(self.plugin_classes.keys())

    def get_plugin_path(self, plugin_name: str) -> Optional[str]:
        """
        获取指定插件的路径。

        Args:
            plugin_name: 插件名称

        Returns:
            Optional[str]: 插件目录的绝对路径，如果插件不存在则返回None。
        """
        return self.plugin_paths.get(plugin_name)

    # === 私有方法 ===
    # == 目录管理 ==
    def _ensure_plugin_directories(self) -> None:
        """确保所有插件根目录存在，如果不存在则创建"""
        default_directories = ["src/plugins/built_in", "plugins"]

        for directory in default_directories:
            if not os.path.exists(directory):
                os.makedirs(directory, exist_ok=True)
                logger.info(f"创建插件根目录: {directory}")
            if directory not in self.plugin_directories:
                self.plugin_directories.append(directory)
                logger.debug(f"已添加插件根目录: {directory}")
            else:
                logger.warning(f"根目录不可重复加载: {directory}")

    # == 插件加载 ==

    def _load_plugin_modules_from_directory(self, directory: str) -> tuple[int, int]:
        """从指定目录加载插件模块"""
        loaded_count = 0
        failed_count = 0

        if not os.path.exists(directory):
            logger.warning(f"插件根目录不存在: {directory}")
            return 0, 1

        logger.debug(f"正在扫描插件根目录: {directory}")

        # 遍历目录中的所有包
        for item in os.listdir(directory):
            item_path = os.path.join(directory, item)

            if os.path.isdir(item_path) and not item.startswith(".") and not item.startswith("__"):
                plugin_file = os.path.join(item_path, "plugin.py")
                if os.path.exists(plugin_file):
                    if self._load_plugin_module_file(plugin_file):
                        loaded_count += 1
                    else:
                        failed_count += 1

        return loaded_count, failed_count

    def _package_name_for_plugin_file(self, plugin_file: str) -> str:
        plugin_path = Path(plugin_file).resolve()
        project_root = Path(__file__).resolve().parents[3]
        try:
            module_parts = plugin_path.parent.relative_to(project_root).parts
        except ValueError:
            # External plugins are imported under a deterministic, readable
            # package name.  The canonical directory path is part of the
            # identity so two directories with the same basename cannot evict
            # one another's modules during a reload.
            canonical_dir = os.path.normcase(str(plugin_path.parent.resolve()))
            digest = hashlib.sha256(canonical_dir.encode("utf-8")).hexdigest()[:12]
            readable_name = re.sub(r"[^0-9A-Za-z_]", "_", plugin_path.parent.name)
            if not readable_name:
                readable_name = "plugin"
            if readable_name[0].isdigit():
                readable_name = f"_{readable_name}"
            module_parts = ("_nachobot_external_plugins", f"{readable_name}_{digest}")
        return ".".join(module_parts)

    def _snapshot_plugin_registries(self) -> tuple[dict[str, Type[PluginBase]], dict[str, str]]:
        return dict(self.plugin_classes), dict(self.plugin_paths)

    def _restore_plugin_registry_delta(
        self,
        snapshot: tuple[dict[str, Type[PluginBase]], dict[str, str]],
        *,
        plugin_name: str | None,
        package_name: str,
        plugin_dir: str,
    ) -> None:
        """Rollback only entries attributable to this plugin transaction."""
        previous_classes, previous_paths = snapshot

        def belongs_to_transaction(key: str, value: object, kind: str) -> bool:
            if plugin_name and key == plugin_name:
                return True
            if kind == "class":
                module_name = getattr(value, "__module__", "")
                return module_name == package_name or module_name.startswith(f"{package_name}.")
            try:
                candidate = str(Path(str(value)).resolve())
                return candidate == plugin_dir or candidate.startswith(plugin_dir + os.sep)
            except (OSError, ValueError, TypeError):
                return False

        for key in set(previous_classes) | set(self.plugin_classes):
            current = self.plugin_classes.get(key)
            previous = previous_classes.get(key)
            if current == previous:
                continue
            if not belongs_to_transaction(key, current if current is not None else previous, "class"):
                continue
            if key in previous_classes:
                self.plugin_classes[key] = previous_classes[key]
            else:
                self.plugin_classes.pop(key, None)

        for key in set(previous_paths) | set(self.plugin_paths):
            current = self.plugin_paths.get(key)
            previous = previous_paths.get(key)
            if current == previous:
                continue
            if not belongs_to_transaction(key, current if current is not None else previous, "path"):
                continue
            if key in previous_paths:
                self.plugin_paths[key] = previous_paths[key]
            else:
                self.plugin_paths.pop(key, None)

    @staticmethod
    def _snapshot_package_modules(package_name: str) -> dict[str, types.ModuleType]:
        parts = package_name.split(".")
        parent_names = {".".join(parts[:index]) for index in range(1, len(parts) + 1)}
        return {
            name: module
            for name, module in sys.modules.items()
            if name in parent_names or name.startswith(f"{package_name}.")
        }

    @staticmethod
    def _restore_package_modules(
        package_name: str,
        snapshot: dict[str, types.ModuleType],
    ) -> None:
        parts = package_name.split(".")
        parent_names = {".".join(parts[:index]) for index in range(1, len(parts) + 1)}
        managed_names = parent_names | {
            name for name in sys.modules if name.startswith(f"{package_name}.")
        }
        for name in managed_names - snapshot.keys():
            sys.modules.pop(name, None)
        for name, module in snapshot.items():
            sys.modules[name] = module

    @staticmethod
    def _evict_package_modules(package_name: str) -> None:
        """Evict only this plugin package before executing fresh source."""
        prefix = f"{package_name}."
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(prefix):
                sys.modules.pop(name, None)
        importlib.invalidate_caches()

    @staticmethod
    def _evict_plugin_source_caches(plugin_dir: Path) -> None:
        """Remove direct-source timestamp caches for one plugin directory.

        Python's timestamp-based bytecode cache can otherwise reuse a stale
        module when a source file is edited without changing its size or
        mtime.  Only cache entries corresponding to direct ``.py`` files in
        this plugin directory are touched; no shared/workspace cache is
        removed.
        """
        plugin_root = plugin_dir.resolve()
        for source_path in plugin_root.rglob("*.py"):
            if not source_path.is_file():
                continue
            try:
                cache_path = Path(cache_from_source(str(source_path))).resolve()
            except (OSError, ValueError):
                continue
            source_cache_dir = (source_path.parent / "__pycache__").resolve()
            try:
                source_path.parent.resolve().relative_to(plugin_root)
            except ValueError:
                continue
            if cache_path.parent != source_cache_dir:
                continue
            if cache_path.exists():
                cache_path.unlink()

    def _load_plugin_module_file(
        self,
        plugin_file: str,
        *,
        transaction_plugin_name: str | None = None,
    ) -> bool:
        # sourcery skip: extract-method
        """加载单个插件模块文件

        Args:
            plugin_file: 插件文件路径
            plugin_name: 插件名称
            plugin_dir: 插件目录路径
        """
        # 生成一个相对项目根目录的包名。plugin_paths 为绝对路径，若直接把
        # Windows drive 或 POSIX root 拼入模块名，热重载时相对导入会失效。
        plugin_path = Path(plugin_file).resolve()
        package_name = self._package_name_for_plugin_file(plugin_file)
        module_name = f"{package_name}.plugin"
        module_snapshot = self._snapshot_package_modules(package_name)
        registry_snapshot = self._snapshot_plugin_registries()

        try:
            self._record_plugin_requirements(plugin_file)
            # Relative imports must observe the candidate's current files,
            # rather than a stale sibling left in sys.modules by an earlier
            # load. Shared ancestors remain installed for other plugins.
            self._evict_package_modules(package_name)
            self._evict_plugin_source_caches(plugin_path.parent)
            # 动态导入插件模块
            spec = spec_from_file_location(module_name, plugin_file)
            if spec is None or spec.loader is None:
                logger.error(f"无法创建模块规范: {plugin_file}")
                self._restore_package_modules(package_name, module_snapshot)
                self._restore_plugin_registry_delta(
                    registry_snapshot,
                    plugin_name=transaction_plugin_name,
                    package_name=package_name,
                    plugin_dir=str(plugin_path.parent),
                )
                return False

            module = module_from_spec(spec)
            module.__package__ = package_name  # 保留插件包名，支持 .sibling 导入
            module.__spec__ = spec

            # Install missing package levels with the path that belongs to
            # each level. This keeps sibling packages importable while also
            # supporting plugins outside the project tree.
            plugin_package_path = plugin_path.parent
            parts = package_name.split(".")
            for index in range(1, len(parts) + 1):
                parent_name = ".".join(parts[:index])
                ancestor_depth = len(parts) - index
                parent_path = (
                    plugin_package_path
                    if ancestor_depth == 0
                    else plugin_package_path.parents[ancestor_depth - 1]
                )
                parent_path_str = str(parent_path)
                parent = sys.modules.get(parent_name)
                if parent is None:
                    parent = types.ModuleType(parent_name)
                    parent.__path__ = [parent_path_str]
                    parent.__package__ = parent_name
                    sys.modules[parent_name] = parent
                elif not hasattr(parent, "__path__"):
                    parent.__path__ = [parent_path_str]
                elif parent_path_str not in parent.__path__:
                    try:
                        parent.__path__.append(parent_path_str)
                    except AttributeError:
                        parent.__path__ = [*parent.__path__, parent_path_str]
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # 检测是否为 V2 插件并生成适配层
            if hasattr(module, "create_plugin"):
                logger.info(f"检测到 V2 SDK 兼容插件: {module_name}，正在生成适配层...")
                from src.plugin_system.core.sdk_adapter import create_adapter_class
                import json
                
                plugin_dir = os.path.dirname(plugin_file)
                manifest_path = os.path.join(plugin_dir, "_manifest.json")
                manifest_data = {}
                if os.path.exists(manifest_path):
                    try:
                        with open(manifest_path, "r", encoding="utf-8") as f:
                            manifest_data = json.load(f)
                    except Exception as e:
                        logger.error(f"无法读取 V2 插件 manifest 配置文件: {e}")
                
                # 创建动态适配类
                adapter_cls = create_adapter_class(plugin_dir, manifest_data, module)
                
                # 模拟 @register_plugin 注册到插件管理器中
                self.plugin_classes[adapter_cls.plugin_name] = adapter_cls
                self.plugin_paths[adapter_cls.plugin_name] = str(Path(plugin_dir).resolve())
                logger.debug(f"V2 插件适配类已成功注册: {adapter_cls.plugin_name}")

            logger.debug(f"插件模块加载成功: {plugin_file}")
            return True

        except Exception as e:
            # Restore the complete package subtree, including relative-import
            # siblings and synthetic parents, rather than only plugin.py.
            self._restore_package_modules(package_name, module_snapshot)
            self._restore_plugin_registry_delta(
                registry_snapshot,
                plugin_name=transaction_plugin_name,
                package_name=package_name,
                plugin_dir=str(plugin_path.parent),
            )
            error_msg = f"加载插件模块 {plugin_file} 失败: {e}"
            logger.error(error_msg)
            self.failed_plugins[module_name] = error_msg
            return False

    def _read_plugin_requirements(self, requirements_file: Path) -> List[str]:
        """读取插件 requirements.txt 中可解析的 PEP 508 依赖。"""
        requirements: List[str] = []
        with requirements_file.open("r", encoding="utf-8") as file:
            for raw_line in file:
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(("-", "--")):
                    continue
                if " #" in line:
                    line = line.split(" #", 1)[0].strip()
                try:
                    Requirement(line)
                except InvalidRequirement:
                    logger.warning(f"插件依赖格式无法解析，跳过检查: {requirements_file} -> {line}")
                    continue
                requirements.append(line)
        return requirements

    def _find_missing_plugin_requirements(self, requirements: List[str]) -> List[str]:
        """检查插件 requirements.txt 中缺失或版本不满足的依赖。"""
        missing: List[str] = []
        package_distributions = importlib.metadata.packages_distributions()

        for requirement in requirements:
            parsed = Requirement(requirement)
            installed_version = None
            distribution_names = [parsed.name]
            top_level_name = parsed.name.replace("-", "_").lower()
            distribution_names.extend(package_distributions.get(top_level_name, []))

            for distribution_name in dict.fromkeys(distribution_names):
                try:
                    installed_version = importlib.metadata.version(distribution_name)
                    break
                except importlib.metadata.PackageNotFoundError:
                    continue

            if installed_version is None:
                missing.append(requirement)
                continue
            if parsed.specifier and not parsed.specifier.contains(installed_version, prereleases=True):
                missing.append(requirement)
        return missing

    def _record_plugin_requirements(self, plugin_file: str) -> None:
        """读取并记录插件依赖，不在插件扫描阶段执行安装。"""
        plugin_dir = Path(plugin_file).parent
        requirements_file = plugin_dir / "requirements.txt"
        if not requirements_file.is_file():
            return

        try:
            requirements = self._read_plugin_requirements(requirements_file)
        except (OSError, UnicodeError) as exc:
            logger.error(f"读取插件 requirements.txt 失败: {requirements_file} - {exc}")
            return

        plugin_key = str(plugin_dir.resolve())
        self.plugin_requirements[plugin_key] = requirements
        if not requirements:
            return

        missing = self._find_missing_plugin_requirements(requirements)
        if missing:
            logger.warning(
                f"插件依赖未满足: {plugin_dir.name} -> {', '.join(missing)}；"
                "请运行项目 uv sync 或手动安装该 requirements.txt"
            )
        else:
            logger.debug(f"插件 requirements.txt 依赖已满足: {requirements_file}")
    # == 兼容性检查 ==

    def _check_plugin_version_compatibility(self, plugin_name: str, manifest_data: Dict[str, Any]) -> Tuple[bool, str]:
        """检查插件版本兼容性

        Args:
            plugin_name: 插件名称
            manifest_data: manifest数据

        Returns:
            Tuple[bool, str]: (是否兼容, 错误信息)
        """
        if "host_application" not in manifest_data:
            return True, ""  # 没有版本要求，默认兼容

        host_app = manifest_data["host_application"]
        if not isinstance(host_app, dict):
            return True, ""

        min_version = host_app.get("min_version", "")
        max_version = host_app.get("max_version", "")

        if not min_version and not max_version:
            return True, ""  # 没有版本要求，默认兼容

        try:
            current_version = VersionComparator.get_current_host_version()
            is_compatible, error_msg = VersionComparator.is_version_in_range(current_version, min_version, max_version)
            if not is_compatible:
                return False, f"版本不兼容: {error_msg}"
            logger.debug(f"插件 {plugin_name} 版本兼容性检查通过")
            return True, ""

        except Exception as e:
            logger.warning(f"插件 {plugin_name} 版本兼容性检查失败: {e}")
            return False, f"插件 {plugin_name} 版本兼容性检查失败: {e}"  # 检查失败时默认不允许加载

    # == 显示统计与插件信息 ==

    def _show_stats(self, total_registered: int, total_failed_registration: int):
        # sourcery skip: low-code-quality
        # 获取组件统计信息
        stats = component_registry.get_registry_stats()
        action_count = stats.get("action_components", 0)
        command_count = stats.get("command_components", 0)
        tool_count = stats.get("tool_components", 0)
        event_handler_count = stats.get("event_handlers", 0)
        total_components = stats.get("total_components", 0)

        # 📋 显示插件加载总览
        if total_registered > 0:
            logger.info("🎉 插件系统加载完成!")
            logger.info(
                f"📊 总览: {total_registered}个插件, {total_components}个组件 (Action: {action_count}, Command: {command_count}, Tool: {tool_count}, EventHandler: {event_handler_count})"
            )

            # 显示详细的插件列表
            logger.info("📋 已加载插件详情:")
            for plugin_name in self.loaded_plugins.keys():
                if plugin_info := component_registry.get_plugin_info(plugin_name):
                    # 插件基本信息
                    version_info = f"v{plugin_info.version}" if plugin_info.version else ""
                    author_info = f"by {plugin_info.author}" if plugin_info.author else "unknown"
                    license_info = f"[{plugin_info.license}]" if plugin_info.license else ""
                    info_parts = [part for part in [version_info, author_info, license_info] if part]
                    extra_info = f" ({', '.join(info_parts)})" if info_parts else ""

                    logger.info(f"  📦 {plugin_info.display_name}{extra_info}")

                    # Manifest信息
                    if plugin_info.manifest_data:
                        """
                        if plugin_info.keywords:
                            logger.info(f"    🏷️ 关键词: {', '.join(plugin_info.keywords)}")
                        if plugin_info.categories:
                            logger.info(f"    📁 分类: {', '.join(plugin_info.categories)}")
                        """
                        if plugin_info.homepage_url:
                            logger.info(f"    🌐 主页: {plugin_info.homepage_url}")

                    # 组件列表
                    if plugin_info.components:
                        action_components = [
                            c for c in plugin_info.components if c.component_type == ComponentType.ACTION
                        ]
                        command_components = [
                            c for c in plugin_info.components if c.component_type == ComponentType.COMMAND
                        ]
                        tool_components = [c for c in plugin_info.components if c.component_type == ComponentType.TOOL]
                        event_handler_components = [
                            c for c in plugin_info.components if c.component_type == ComponentType.EVENT_HANDLER
                        ]

                        if action_components:
                            action_names = [c.name for c in action_components]
                            logger.info(f"    🎯 Action组件: {', '.join(action_names)}")

                        if command_components:
                            command_names = [c.name for c in command_components]
                            logger.info(f"    ⚡ Command组件: {', '.join(command_names)}")
                        if tool_components:
                            tool_names = [c.name for c in tool_components]
                            logger.info(f"    🛠️ Tool组件: {', '.join(tool_names)}")
                        if event_handler_components:
                            event_handler_names = [c.name for c in event_handler_components]
                            logger.info(f"    📢 EventHandler组件: {', '.join(event_handler_names)}")

                    # 依赖信息
                    if plugin_info.dependencies:
                        logger.info(f"    🔗 依赖: {', '.join(plugin_info.dependencies)}")

                    # 配置文件信息
                    if plugin_info.config_file:
                        config_status = "✅" if self.plugin_paths.get(plugin_name) else "❌"
                        logger.info(f"    ⚙️ 配置: {plugin_info.config_file} {config_status}")

            root_path = Path(__file__)

            # 查找项目根目录
            while not (root_path / "pyproject.toml").exists() and root_path.parent != root_path:
                root_path = root_path.parent

            # 显示目录统计
            logger.info("📂 加载目录统计:")
            for directory in self.plugin_directories:
                if os.path.exists(directory):
                    plugins_in_dir = []
                    for plugin_name in self.loaded_plugins.keys():
                        plugin_path = self.plugin_paths.get(plugin_name, "")
                        if (
                            Path(plugin_path)
                            .resolve()
                            .is_relative_to(Path(os.path.join(str(root_path), directory)).resolve())
                        ):
                            plugins_in_dir.append(plugin_name)

                    if plugins_in_dir:
                        logger.info(f" 📁 {directory}: {len(plugins_in_dir)}个插件 ({', '.join(plugins_in_dir)})")
                    else:
                        logger.info(f" 📁 {directory}: 0个插件")

            # 失败信息
            if total_failed_registration > 0:
                logger.info(f"⚠️  失败统计: {total_failed_registration}个插件加载失败")
                for failed_plugin, error in self.failed_plugins.items():
                    logger.info(f"  ❌ {failed_plugin}: {error}")
        else:
            logger.warning("😕 没有成功加载任何插件")

    def _show_plugin_components(self, plugin_name: str) -> None:
        if plugin_info := component_registry.get_plugin_info(plugin_name):
            component_types = {}
            for comp in plugin_info.components:
                comp_type = comp.component_type.name
                component_types[comp_type] = component_types.get(comp_type, 0) + 1

            components_str = ", ".join([f"{count}个{ctype}" for ctype, count in component_types.items()])

            # 显示manifest信息
            manifest_info = ""
            if plugin_info.license:
                manifest_info += f" [{plugin_info.license}]"
            if plugin_info.keywords:
                manifest_info += f" 关键词: {', '.join(plugin_info.keywords[:3])}"  # 只显示前3个关键词
                if len(plugin_info.keywords) > 3:
                    manifest_info += "..."

            logger.info(
                f"✅ 插件加载成功: {plugin_name} v{plugin_info.version} ({components_str}){manifest_info} - {plugin_info.description}"
            )
        else:
            logger.info(f"✅ 插件加载成功: {plugin_name}")


# 全局插件管理器实例
plugin_manager = PluginManager()
