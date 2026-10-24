import asyncio
import base64
import hashlib
import random
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Type

from PIL import Image

from src.common.logger import get_logger
from src.chat.emoji_system.emoji_collage import build_emoji_collage
from src.chat.emoji_system.image_selection import parse_selected_index, request_numbered_image_selection
from src.plugin_system import BasePlugin, ComponentInfo, register_plugin
from src.plugin_system.apis import llm_api, message_api
from src.plugin_system.base.base_action import BaseAction
from src.plugin_system.base.component_types import ActionActivationType
from src.plugin_system.base.config_types import ConfigField

from .description_cache import ArtworkDescriptionCache, hash_file, normalize_description

logger = get_logger("send_artwork")

MAX_VISIBLE_CANDIDATES = 10
MAX_CORRUPT_BACKUPS = 5
MAX_COLLAGE_CANDIDATES = MAX_VISIBLE_CANDIDATES + MAX_CORRUPT_BACKUPS


@dataclass
class _ArtworkCandidate:
    """Image-collage candidate carrying its source identity and optional description."""

    full_path: str
    emoji_hash: str
    description: str | None
    source_path: Path
    filename: str


class SendArtworkAction(BaseAction):
    """根据当前语境选择并发送本地画作"""

    action_name = "send_artwork"
    action_description = "响应用户明确的看图请求，根据当前语境选择并发送一张本地画作"

    activation_type = ActionActivationType.LLM_JUDGE

    llm_judge_prompt = (
        "严格触发规则：仅在用户文本中包含明确的指令性词汇（如'看看画'、'发张图'、'看作品'、'来张图'）时才激活。"
        "绝对禁止基于语境推断（如'用户很开心'、'聊到了画画'）触发。"
        "如果用户只是讨论绘画技巧、说'我也想画'，或者夸奖之前的画，请保持静默，不要选择此动作。"
        "你必须在 action_parameters 中填写 'trigger_evidence' 字段以证明你的判断。"
    )

    parallel_action = True
    action_parameters = {"trigger_evidence": "用户请求看图的原话片段（必须精准摘录自用户消息文本，例如'发张图看看'）"}
    action_require = [
        "用户必须有明确的指令行为（Imperative Request），而非陈述行为",
        "参数 trigger_evidence 必须非空，且必须能从用户的最新消息中找到对应文本",
        "若非用户明确要求，不要连续触发该动作",
        "不符合以上条件时不要触发该动作",
        "若最近十轮对话中已使用过该动作，不再触发该动作",
    ]
    associated_types = ["text"]

    async def execute(self, trigger_evidence: str = "") -> Tuple[bool, str]:
        if str(self.platform).lower() == "discord":
            if self.is_group:
                logger.warning(f"{self.log_prefix} Discord平台群聊禁用发送画作，已拦截")
                return False, "Discord群聊禁用发送画作"

            discord_artwork_whitelist = self.get_config("access_control.discord_artwork_whitelist", [])
            whitelist_strs = [str(uid) for uid in discord_artwork_whitelist]
            current_user_id = str(self.user_id) if self.user_id else ""
            if current_user_id not in whitelist_strs:
                logger.warning(f"{self.log_prefix} 用户 {current_user_id} 不在Discord画作白名单中，已拦截")
                return False, "用户不在Discord画作白名单中"

        artwork_dir = self._resolve_artwork_dir()
        artwork_files = self._collect_artworks(artwork_dir, self._get_allowed_extensions())
        if not artwork_files:
            if not getattr(SendArtworkAction, "_empty_warned", False):
                logger.warning(f"{self.log_prefix} artwork目录为空，画夹内没有可用图片")
                SendArtworkAction._empty_warned = True
            return False, "artwork目录为空"

        target_request, reason, messages_text = self._selection_context(trigger_evidence)
        cache = ArtworkDescriptionCache(self._description_cache_root())
        try:
            candidates = await asyncio.to_thread(self._load_artwork_candidates, artwork_files, cache)
        except Exception as error:
            logger.error(f"{self.log_prefix} 读取画作候选失败: {error}", exc_info=True)
            return False, "读取画作候选失败"
        if not candidates:
            return False, "没有可读取的画作候选"

        pool = self._select_candidate_pool(candidates, target_request or reason)
        collage = None
        collage_inputs: list[_ArtworkCandidate] = []
        with tempfile.TemporaryDirectory(prefix="nachobot-artwork-") as temp_directory:
            try:
                collage_inputs = await asyncio.to_thread(
                    self._snapshot_candidates,
                    pool,
                    cache,
                    Path(temp_directory),
                )
                if not collage_inputs:
                    return False, "没有可读取的画作候选"
                collage = await asyncio.to_thread(build_emoji_collage, collage_inputs, MAX_VISIBLE_CANDIDATES)
            except Exception as error:
                logger.error(f"{self.log_prefix} 构建画作候选拼图失败: {error}", exc_info=True)
                collage = None

            if collage and collage.candidates:
                selection_candidates: list[_ArtworkCandidate] = collage.candidates
                visual_selection = await request_numbered_image_selection(
                    collage.image_base64,
                    prompt=self._visual_selection_prompt(
                        selection_candidates,
                        target_request,
                        reason,
                        messages_text,
                    ),
                    candidate_count=len(selection_candidates),
                    media_format=collage.image_format,
                    image_kind="image",
                    metadata={"temperature": 0, "max_tokens": 700},
                )
                await self._persist_descriptions(selection_candidates, visual_selection.descriptions, cache)
                selected_index = visual_selection.index
            else:
                selection_candidates = await asyncio.to_thread(self._readable_candidates, collage_inputs)
                visual_selection = None
                selected_index = None
                if not selection_candidates:
                    return False, "没有可读取的画作候选"
                logger.warning(f"{self.log_prefix} 画作拼图不可用，将尝试文字选图")

        if selected_index is None:
            if visual_selection and visual_selection.error:
                logger.warning(f"{self.log_prefix} 视觉选图不可用，尝试文字选图: {visual_selection.error}")
            elif visual_selection and visual_selection.degraded:
                logger.warning(f"{self.log_prefix} 视觉选图不可用，尝试文字选图")
            elif visual_selection:
                logger.warning(f"{self.log_prefix} 视觉选图返回无效编号，尝试文字选图")
            selected_index = await self._select_with_text_model(
                selection_candidates,
                target_request,
                reason,
                messages_text,
            )

        if selected_index is None or not 0 <= selected_index < len(selection_candidates):
            if not selection_candidates:
                return False, "没有可读取的画作候选"
            selected_candidate = random.choice(selection_candidates)
            logger.warning(
                f"{self.log_prefix} 视觉和文字选图不可用，在有界候选中随机选择: {selected_candidate.filename}"
            )
        else:
            selected_candidate = selection_candidates[selected_index]

        try:
            image_bytes = await asyncio.to_thread(selected_candidate.source_path.read_bytes)
            if hashlib.sha256(image_bytes).hexdigest() != selected_candidate.emoji_hash:
                logger.warning(f"{self.log_prefix} 画作在选图期间发生变化，跳过发送: {selected_candidate.filename}")
                return False, "画作内容已变化，未发送"
            image_base64 = base64.b64encode(image_bytes).decode("ascii")
        except OSError as error:
            logger.error(f"{self.log_prefix} 读取画作失败 {selected_candidate.filename}: {error}")
            return False, f"读取画作失败: {selected_candidate.filename}"

        try:
            sent = await self.send_image(image_base64, reply_message=self.action_message)
        except Exception as error:
            logger.error(f"{self.log_prefix} 发送画作失败 {selected_candidate.filename}: {error}", exc_info=True)
            return False, f"发送画作失败: {selected_candidate.filename}"
        if not sent:
            return False, f"发送画作失败: {selected_candidate.filename}"

        description = selected_candidate.description
        prompt_display = f"发送了画作: {selected_candidate.filename}"
        if description:
            prompt_display += f"；图片内容描述: {description}"
        else:
            prompt_display += "；该图片暂无可用语义描述"
        await self.store_action_info(
            action_build_into_prompt=True,
            action_prompt_display=prompt_display,
            action_done=True,
        )
        return True, f"已发送画作: {selected_candidate.filename}"

    def _description_cache_root(self) -> Path:
        return Path(__file__).resolve().parents[2] / "data" / "artwork_descriptions"

    def _selection_context(self, trigger_evidence: str) -> tuple[str, str, str]:
        target_request = ""
        if self.action_message:
            target_request = (
                getattr(self.action_message, "processed_plain_text", None)
                or getattr(self.action_message, "display_message", None)
                or ""
            )
        target_request = str(target_request).strip() or str(trigger_evidence or "").strip()
        reason = str(self.action_data.get("reason") or self.reasoning or "响应用户的看图请求").strip()
        messages_text = ""
        try:
            recent_messages = message_api.get_recent_messages(chat_id=self.chat_id, limit=5)
            if recent_messages:
                messages_text = message_api.build_readable_messages(
                    messages=recent_messages,
                    timestamp_mode="normal_no_YMD",
                    truncate=False,
                    show_actions=False,
                )
        except Exception as error:
            logger.debug(f"{self.log_prefix} 获取画作选择历史失败: {error}")
        return target_request, reason, messages_text

    def _load_artwork_candidates(
        self,
        artwork_files: list[Path],
        cache: ArtworkDescriptionCache,
    ) -> list[_ArtworkCandidate]:
        candidates = []
        for path in artwork_files:
            try:
                content_hash = hash_file(path)
            except OSError as error:
                logger.warning(f"{self.log_prefix} 跳过无法读取的画作 {path.name}: {error}")
                continue
            candidates.append(
                _ArtworkCandidate(
                    full_path=str(path),
                    emoji_hash=content_hash,
                    description=cache.get(content_hash),
                    source_path=path,
                    filename=path.name,
                )
            )
        return candidates

    def _select_candidate_pool(
        self,
        candidates: list[_ArtworkCandidate],
        query: str,
    ) -> list[_ArtworkCandidate]:
        limit = min(MAX_COLLAGE_CANDIDATES, len(candidates))
        scored = [(self._description_match_score(query, candidate), candidate) for candidate in candidates]
        matches = [(score, candidate) for score, candidate in scored if score > 0]
        if not matches:
            return random.sample(candidates, limit)

        matches.sort(key=lambda item: item[0], reverse=True)
        selected = [candidate for _, candidate in matches[:limit]]
        if len(selected) < limit:
            selected_ids = {id(candidate) for candidate in selected}
            remaining = [candidate for candidate in candidates if id(candidate) not in selected_ids]
            selected.extend(random.sample(remaining, min(limit - len(selected), len(remaining))))
        return selected

    @staticmethod
    def _description_match_score(query: str, candidate: _ArtworkCandidate) -> int:
        query_terms = SendArtworkAction._selection_terms(query)
        if not query_terms:
            return 0
        description_terms = SendArtworkAction._selection_terms(candidate.description or "")
        filename_terms = SendArtworkAction._selection_terms(candidate.filename)
        return len(query_terms & description_terms) * 2 + len(query_terms & filename_terms)

    @staticmethod
    def _selection_terms(value: str) -> set[str]:
        terms: set[str] = set()
        ascii_run = ""
        cjk_run = ""

        def flush_ascii() -> None:
            nonlocal ascii_run
            if ascii_run:
                terms.add(ascii_run.casefold())
                ascii_run = ""

        def flush_cjk() -> None:
            nonlocal cjk_run
            if len(cjk_run) == 1:
                terms.add(cjk_run)
            else:
                terms.update(cjk_run)
                terms.update(cjk_run[index : index + 2] for index in range(len(cjk_run) - 1))
            cjk_run = ""

        for char in value:
            codepoint = ord(char)
            if "a" <= char.lower() <= "z" or "0" <= char <= "9":
                flush_cjk()
                ascii_run += char
            elif 0x3400 <= codepoint <= 0x9FFF:
                flush_ascii()
                cjk_run += char
            else:
                flush_ascii()
                flush_cjk()
        flush_ascii()
        flush_cjk()
        return terms

    def _snapshot_candidates(
        self,
        candidates: list[_ArtworkCandidate],
        cache: ArtworkDescriptionCache,
        temp_directory: Path,
    ) -> list[_ArtworkCandidate]:
        snapshots = []
        for index, candidate in enumerate(candidates):
            try:
                data = candidate.source_path.read_bytes()
            except OSError as error:
                logger.warning(f"{self.log_prefix} 跳过无法读取的画作 {candidate.filename}: {error}")
                continue
            content_hash = hashlib.sha256(data).hexdigest()
            description = candidate.description if content_hash == candidate.emoji_hash else cache.get(content_hash)
            temp_path = temp_directory / f"{index:02d}{candidate.source_path.suffix.lower()}"
            try:
                temp_path.write_bytes(data)
            except OSError as error:
                logger.warning(f"{self.log_prefix} 创建画作拼图候选失败 {candidate.filename}: {error}")
                continue
            snapshots.append(
                _ArtworkCandidate(
                    full_path=str(temp_path),
                    emoji_hash=content_hash,
                    description=description,
                    source_path=candidate.source_path,
                    filename=candidate.filename,
                )
            )
        return snapshots

    @staticmethod
    def _readable_candidates(candidates: list[_ArtworkCandidate]) -> list[_ArtworkCandidate]:
        readable = []
        for candidate in candidates:
            try:
                with Image.open(candidate.full_path) as source:
                    frame_count = getattr(source, "n_frames", 1)
                    if frame_count > 1:
                        source.seek(frame_count // 2)
                    image = source.convert("RGBA")
                    image.load()
                    if image.width > 0 and image.height > 0:
                        readable.append(candidate)
            except Exception as error:
                logger.warning(f"跳过无法读取的画作候选 {candidate.filename}: {type(error).__name__}")
        return readable

    @staticmethod
    def _visual_selection_prompt(
        candidates: list[_ArtworkCandidate],
        target_request: str,
        reason: str,
        messages_text: str,
    ) -> str:
        candidate_lines = []
        for index, candidate in enumerate(candidates, start=1):
            hint = candidate.description or f"仅文件名提示（没有语义描述）: {candidate.filename}"
            candidate_lines.append(f"{index}. 文件名: {candidate.filename}；已有描述: {hint}")
        return f"""
你正在为聊天选择一张本地画作。图片是一张带连续编号的候选拼图。
当前用户请求（最高优先级）: {target_request or reason}
动作理由: {reason}

最近的聊天记录:
{messages_text or "（无）"}

编号与文件名、已有描述的对应关系:
{chr(10).join(candidate_lines)}

请优先按拼图中的实际画面选择，当前用户请求优先于历史消息。已有描述和文件名只是辅助信息；标明“仅文件名提示”时，不能据此臆测画面内容。
请为每个你能辨认的编号补充一句简短、客观的画面描述；无法辨认的编号可省略。然后选择最符合语境的一张。
只输出 JSON: {{"index": 3, "descriptions": {{"1": "简短画面描述", "3": "简短画面描述"}}}}。index 必须是 1 到 {len(candidates)} 的整数，descriptions 是可省略的编号到描述映射，不要输出解释或 Markdown。
""".strip()

    async def _persist_descriptions(
        self,
        candidates: list[_ArtworkCandidate],
        descriptions: dict[int, str],
        cache: ArtworkDescriptionCache,
    ) -> None:
        valid_descriptions: dict[int, str] = {}
        for index, description in descriptions.items():
            if not 0 <= index < len(candidates):
                continue
            candidate = candidates[index]
            normalized = normalize_description(description, filename=candidate.filename)
            if normalized:
                valid_descriptions[index] = normalized
        if not valid_descriptions:
            return

        indexes = list(valid_descriptions)
        current_hashes = await asyncio.gather(
            *(asyncio.to_thread(hash_file, candidates[index].source_path) for index in indexes),
            return_exceptions=True,
        )
        for index, current_hash in zip(indexes, current_hashes, strict=True):
            candidate = candidates[index]
            if isinstance(current_hash, Exception) or current_hash != candidate.emoji_hash:
                logger.debug(f"{self.log_prefix} 画作内容已变化，跳过描述缓存: {candidate.filename}")
                continue
            description = valid_descriptions[index]
            candidate.description = description
            saved = await asyncio.to_thread(
                cache.save,
                candidate.emoji_hash,
                description,
                filename=candidate.filename,
            )
            if not saved:
                logger.warning(f"{self.log_prefix} 保存画作描述失败，继续本次选图: {candidate.filename}")

    @staticmethod
    def _text_selection_prompt(
        candidates: list[_ArtworkCandidate],
        target_request: str,
        reason: str,
        messages_text: str,
    ) -> str:
        candidate_lines = []
        for index, candidate in enumerate(candidates, start=1):
            if candidate.description:
                hint = candidate.description
            else:
                hint = f"仅文件名提示，暂无语义描述: {candidate.filename}"
            candidate_lines.append(f"{index}. {candidate.filename}：{hint}")
        return f"""
请根据当前用户请求和最近聊天记录，从编号画作描述中选择一张最合适的作品。
当前用户请求（最高优先级）: {target_request or reason}
动作理由: {reason}
最近的聊天记录:
{messages_text or "（无）"}

候选画作:
{chr(10).join(candidate_lines)}

没有语义描述的候选只提供文件名，不能根据文件名臆测画面内容。只返回严格 JSON，例如 {{"index": 2}}；index 必须是 1 到 {len(candidates)} 的整数，不要解释或添加其他字段。
""".strip()

    async def _select_with_text_model(
        self,
        candidates: list[_ArtworkCandidate],
        target_request: str,
        reason: str,
        messages_text: str,
    ) -> int | None:
        try:
            model_config = llm_api.get_available_models().get("utils_small")
            if not model_config:
                logger.warning(f"{self.log_prefix} 未找到'utils_small'模型配置，文字选图不可用")
                return None
            prompt = self._text_selection_prompt(candidates, target_request, reason, messages_text)
            success, response, _, _ = await llm_api.generate_with_model(
                prompt,
                model_config=model_config,
                request_type="artwork.text_select",
                temperature=0,
                max_tokens=50,
            )
            selected_index = parse_selected_index(response, len(candidates)) if success else None
            if selected_index is None:
                logger.warning(f"{self.log_prefix} 文字选图不可用或返回无效编号")
            return selected_index
        except Exception as error:
            logger.warning(f"{self.log_prefix} 文字选图失败: {error}")
            return None

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
        except Exception as error:  # pragma: no cover - filesystem failure
            logger.error(f"{self.log_prefix} 创建artwork目录失败: {error}")
            return []
        try:
            return [
                path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in allowed_extensions
            ]
        except OSError as error:
            logger.error(f"{self.log_prefix} 读取artwork目录失败: {error}")
            return []

    def _is_view_request(self) -> bool:
        """Return whether the target message explicitly asks to see or receive art."""
        if not self.action_message:
            return False
        text = (
            getattr(self.action_message, "display_message", None)
            or getattr(self.action_message, "processed_plain_text", None)
            or ""
        )
        text = str(text).lower()
        view_verbs = ["看", "看看", "想看", "给", "发", "来", "给我", "来张", "求", "想要", "发张"]
        art_nouns = ["画", "画作", "作品", "插画", "图片", "图", "图图", "画廊"]
        if "画画" in text and "看" not in text and "发" not in text:
            return False
        return any(verb in text for verb in view_verbs) and any(noun in text for noun in art_nouns)


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
