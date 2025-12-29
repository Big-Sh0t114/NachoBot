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
    action_description = "当用户想看画时，随机发送一张本地画作图片"

    activation_type = ActionActivationType.LLM_JUDGE  # 由planner判断是否触发
    llm_judge_prompt = (
        "仅在用户文本明确表示想看画/作品/插画/图片且已确认时才激活；"
        "如果只是说“想画画”或讨论绘画、未要求查看，或由机器人自身消息触发，请不要选择该动作。"
    )
    parallel_action = True

    action_parameters = {}
    action_require = [
        "用户已明确说想看/确认要看画之后使用（未确认时不要触发）",
        "当用户提到想看画、作品图或发一张图时使用",
        "若非用户明确要求，不要连续触发该动作",
        "不符合以上条件时不要触发该动作",
    ]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        # 文本未明确要“看/发”画作则直接跳过
        if not self._is_view_request():
            return False, "未检测到明确的看画请求"

        artwork_dir = self._resolve_artwork_dir()
        allowed_exts = self._get_allowed_extensions()
        artwork_files = self._collect_artworks(artwork_dir, allowed_exts)

        if not artwork_files:
            empty_reply = self.get_config(
                "artwork.empty_message",
                "画夹里暂时没有图片，等我补几张再给你看~",
            )
            await self.send_text(empty_reply)
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
            repo_root = Path(__file__).resolve().parents[4]
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

        return [
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in allowed_extensions
        ]

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
        view_verbs = ["看", "看看", "想看", "给", "发", "来", "给我", "来张", "求", "想要"]
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
            "empty_message": ConfigField(type=str, default="画夹里暂时没有图片，等我补几张再给你看~", description="画夹为空时的回复"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        components = []
        if self.get_config("components.enable_send_artwork", True):
            components.append((SendArtworkAction.get_action_info(), SendArtworkAction))
        return components
