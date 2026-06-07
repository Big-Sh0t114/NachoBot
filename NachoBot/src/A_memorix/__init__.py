"""A_Memorix - NachoBot 长期记忆子系统。

基于上游 MaiBot-dev A_Memorix v2.0.0 移植。
本 __init__.py 负责在模块加载前注册兼容垫片，
将上游的外部依赖重定向到 NachoBot 基础设施。
"""

import sys
from types import ModuleType

__version__ = "2.0.0"
__author__ = "A_Dawn"
__all__ = ["__version__"]


# ---------------------------------------------------------------------------
# 注册 sys.modules 垫片，使 A_memorix 内部的上游 import 语句能正确解析
# ---------------------------------------------------------------------------

def _register_shims() -> None:
    """将上游路径的 import 重定向到本地垫片。"""

    # 1. src.services.llm_service → A_memorix._shims.llm_service
    from src.A_memorix._shims import llm_service as _llm_shim
    from src.A_memorix._shims import message_service as _msg_shim

    # 创建 src.services 包（如果不存在）
    if "src.services" not in sys.modules:
        services_pkg = ModuleType("src.services")
        services_pkg.__path__ = []  # type: ignore
        services_pkg.__package__ = "src.services"
        sys.modules["src.services"] = services_pkg

    sys.modules["src.services.llm_service"] = _llm_shim
    sys.modules["src.services.message_service"] = _msg_shim

    # 2. src.common.data_models.llm_service_data_models.LLMServiceResult
    from src.A_memorix._compat import LLMServiceResult
    if "src.common.data_models.llm_service_data_models" not in sys.modules:
        llm_dm_mod = ModuleType("src.common.data_models.llm_service_data_models")
        llm_dm_mod.LLMServiceResult = LLMServiceResult  # type: ignore
        # 确保父包存在
        if "src.common.data_models" not in sys.modules:
            dm_pkg = ModuleType("src.common.data_models")
            dm_pkg.__path__ = []  # type: ignore
            sys.modules["src.common.data_models"] = dm_pkg
        sys.modules["src.common.data_models.llm_service_data_models"] = llm_dm_mod

    # 3. src.config.official_configs.AMemorixConfig
    from src.A_memorix._compat import AMemorixConfig
    import src.config.official_configs as _official
    if not hasattr(_official, "AMemorixConfig"):
        _official.AMemorixConfig = AMemorixConfig  # type: ignore

    # 4. src.common.utils.utils_config.AMemorixConfigUtils
    from src.A_memorix._compat import AMemorixConfigUtils
    if "src.common.utils" not in sys.modules:
        utils_pkg = ModuleType("src.common.utils")
        utils_pkg.__path__ = []  # type: ignore
        sys.modules["src.common.utils"] = utils_pkg
    if "src.common.utils.utils_config" not in sys.modules:
        utils_config_mod = ModuleType("src.common.utils.utils_config")
        utils_config_mod.AMemorixConfigUtils = AMemorixConfigUtils  # type: ignore
        sys.modules["src.common.utils.utils_config"] = utils_config_mod

    # 5. src.config.config.config_manager / BOT_CONFIG_PATH
    from src.A_memorix._compat import config_manager, BOT_CONFIG_PATH
    import src.config.config as _config_mod
    if not hasattr(_config_mod, "config_manager"):
        _config_mod.config_manager = config_manager  # type: ignore
    if not hasattr(_config_mod, "BOT_CONFIG_PATH"):
        _config_mod.BOT_CONFIG_PATH = BOT_CONFIG_PATH  # type: ignore

    # 6. src.webui.utils.toml_utils._update_toml_doc
    from src.A_memorix._compat import _update_toml_doc
    if "src.webui" not in sys.modules:
        webui_pkg = ModuleType("src.webui")
        webui_pkg.__path__ = []  # type: ignore
        sys.modules["src.webui"] = webui_pkg
    if "src.webui.utils" not in sys.modules:
        webui_utils_pkg = ModuleType("src.webui.utils")
        webui_utils_pkg.__path__ = []  # type: ignore
        sys.modules["src.webui.utils"] = webui_utils_pkg
    if "src.webui.utils.toml_utils" not in sys.modules:
        toml_utils_mod = ModuleType("src.webui.utils.toml_utils")
        toml_utils_mod._update_toml_doc = _update_toml_doc  # type: ignore
        sys.modules["src.webui.utils.toml_utils"] = toml_utils_mod

    # 7. src.config.model_configs (TaskConfig, APIProvider, ModelInfo)
    if "src.config.model_configs" not in sys.modules:
        from src.config.api_ada_configs import APIProvider, ModelInfo, TaskConfig
        model_configs_mod = ModuleType("src.config.model_configs")
        model_configs_mod.TaskConfig = TaskConfig  # type: ignore
        model_configs_mod.APIProvider = APIProvider  # type: ignore
        model_configs_mod.ModelInfo = ModelInfo  # type: ignore
        sys.modules["src.config.model_configs"] = model_configs_mod


_register_shims()
