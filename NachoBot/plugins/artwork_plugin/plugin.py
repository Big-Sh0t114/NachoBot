import random
from pathlib import Path
from typing import List, Tuple, Type

from src.common.logger import get_logger
from src.chat.utils.utils_image import image_path_to_base64
from src.plugin_system import BasePlugin, ComponentInfo, register_plugin
from src.plugin_system.base.base_action import BaseAction
from src.plugin_system.base.component_types import ActionActivationType
from src.plugin_system.base.config_types import ConfigField

logger = get_logger("send_artwork")


class SendArtworkAction(BaseAction):
    """随机从本地artwork目录发送一张画作"""

    action_name = "send_artwork"
    action_description = "响应用户明确的看图请求，随机发送一张本地画作"  # 修改描述，强调“响应请求”而非“当用户想看时”

    activation_type = ActionActivationType.LLM_JUDGE

    # --- 修改重点 1: 强化 LLM 判断提示词 ---
    llm_judge_prompt = (
        "严格触发规则：仅在用户文本中包含明确的指令性词汇（如'看看画'、'发张图'、'看作品'、'来张图'）时才激活。"
        "绝对禁止基于语境推断（如'用户很开心'、'聊到了画画'）触发。"
        "如果用户只是讨论绘画技巧、说'我也想画'，或者夸奖之前的画，请保持静默，不要选择此动作。"
        "你必须在 action_parameters 中填写 'trigger_evidence' 字段以证明你的判断。"
    )

    parallel_action = True

    # --- 修改重点 2: 增加证据参数 (核心) ---
    # 这迫使模型必须从用户的话里“摘抄”出证据，如果摘抄不出来，它就无法通过校验
    action_parameters = {"trigger_evidence": "用户请求看图的原话片段（必须精准摘录自用户消息文本，例如'发张图看看'）"}

    # --- 修改重点 3: 完善触发条件 ---
    action_require = [
        "用户必须有明确的指令行为（Imperative Request），而非陈述行为",
        "参数 trigger_evidence 必须非空，且必须能从用户的最新消息中找到对应文本",  # 双重保险
        "若非用户明确要求，不要连续触发该动作",
        "不符合以上条件时不要触发该动作",
        "若最近十轮对话中已使用过该动作，不再触发该动作",
    ]

    associated_types = ["text"]

    async def execute(self, trigger_evidence: str = "") -> Tuple[bool, str]:
        artwork_dir = self._resolve_artwork_dir()
        allowed_exts = self._get_allowed_extensions()
        artwork_files = self._collect_artworks(artwork_dir, allowed_exts)

        # Discord 平台画作发送控制逻辑
        if str(self.platform).lower() == "discord":
            # ... (原有逻辑保持不变) ...
            if self.is_group:
                logger.warning(f"{self.log_prefix} Discord平台群聊禁用发送画作，已拦截")
                return False, "Discord群聊禁用发送画作"

            discord_artwork_whitelist = self.get_config("access_control.discord_artwork_whitelist", [])
            whitelist_strs = [str(uid) for uid in discord_artwork_whitelist]
            current_user_id = str(self.user_id) if self.user_id else ""

            if current_user_id not in whitelist_strs:
                logger.warning(f"{self.log_prefix} 用户 {current_user_id} 不在Discord画作白名单中，已拦截")
                return False, "用户不在Discord画作白名单中"

        if not artwork_files:
            if not getattr(SendArtworkAction, "_empty_warned", False):
                logger.warning(f"{self.log_prefix} artwork目录为空，画夹内没有可用图片")
                SendArtworkAction._empty_warned = True
            return False, "artwork目录为空"

        chosen_path = random.choice(artwork_files)
        try:
            image_base64 = image_path_to_base64(str(chosen_path))
        except Exception as e:  # pragma: no cover
            logger.error(f"{self.log_prefix} 读取画作失败 {chosen_path}: {e}")
            await self.send_text("有点小问题，暂时没法把画发出去~")
            return False, f"读取画作失败: {chosen_path}"

        # 只发送图片，不附带文字
        sent = await self.send_image(image_base64)
        if not sent:
            return False, "发送画作失败"

        await self.store_action_info(
            action_build_into_prompt=False,
            action_prompt_display=f"发送了画作: {chosen_path.name}",
            action_done=True,
        )
        return True, f"已发送画作: {chosen_path.name}"

    def _resolve_artwork_dir(self) -> Path:
        configured = self.get_config("artwork.directory", "artwork")
        path = Path(configured)
        if not path.is_absolute():
            repo_root = Path(__file__).resolve().parents[2]
            path = repo_root / path
        return path

    def _get_allowed_extensions(self) -> List[str]:
        configured_exts = self.get_config(
            "artwork.allowed_extensions",
            [".png", ".jpg", ".jpeg", ".gif", ".webp"],
        )
        return self._normalize_extensions(configured_exts)

    def _normalize_extensions(self, exts: List[str]) -> List[str]:
        normalized: List[str] = []
        for ext in exts:
            if not isinstance(ext, str):
                continue
            cleaned = ext.strip().lower()
            if not cleaned:
                continue
            if not cleaned.startswith("."):
                cleaned = f".{cleaned}"
            normalized.append(cleaned)
        return normalized or [".png", ".jpg", ".jpeg", ".gif", ".webp"]

    def _collect_artworks(self, directory: Path, allowed_extensions: List[str]) -> List[Path]:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # pragma: no cover
            logger.error(f"{self.log_prefix} 创建artwork目录失败: {e}")
            return []

        return [path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in allowed_extensions]

    def _is_view_request(self) -> bool:
        """
        仅当用户文本明确想“看/发”画作时返回True，避免“想画画”误触发。
        """
        if not self.action_message:
            return False

        text = (
            getattr(self.action_message, "display_message", None)
            or getattr(self.action_message, "processed_plain_text", None)
            or ""
        )
        text = str(text).lower()

        # 需要同时包含动词和画作相关名词
        view_verbs = ["看", "看看", "想看", "给", "发", "来", "给我", "来张", "求", "想要", "发张"]
        art_nouns = ["画", "画作", "作品", "插画", "图片", "图", "图图", "画廊"]

        has_verb = any(v in text for v in view_verbs)
        has_noun = any(n in text for n in art_nouns)

        # 明确排除“画画”这类表达（表示想自己画）
        if "画画" in text and "看" not in text and "发" not in text:
            return False

        return has_verb and has_noun


@register_plugin
class ArtworkPlugin(BasePlugin):
    """内置画作发送插件"""

    plugin_name: str = "artwork_plugin"
    enable_plugin: bool = True
    dependencies: list[str] = []
    python_dependencies: list[str] = []
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "components": "组件启用配置",
        "artwork": "画作目录与行为配置",
    }

    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="artwork_plugin", description="插件名称", required=True),
            "version": ConfigField(type=str, default="0.1.0", description="插件版本"),
            "config_version": ConfigField(type=str, default="0.1.0", description="配置版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
        },
        "components": {
            "enable_send_artwork": ConfigField(type=bool, default=True, description="启用发送画作动作"),
        },
        "artwork": {
            "directory": ConfigField(type=str, default="artwork", description="画作目录，可用相对或绝对路径"),
            "allowed_extensions": ConfigField(
                type=list,
                default=[".png", ".jpg", ".jpeg", ".gif", ".webp"],
                description="允许读取的图片后缀",
            ),
            "caption": ConfigField(type=str, default="送你一张最近的画~", description="发送图片时附带的文案"),
            "empty_message": ConfigField(
                type=str, default="画夹里暂时没有图片，等我补几张再给你看~", description="画夹为空时的回复"
            ),
        },
        "access_control": {
            "discord_artwork_whitelist": ConfigField(
                type=list, default=[], description="Discord画作功能白名单用户ID列表"
            ),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        components = []
        if self.get_config("components.enable_send_artwork", True):
            components.append((SendArtworkAction.get_action_info(), SendArtworkAction))
        return components
