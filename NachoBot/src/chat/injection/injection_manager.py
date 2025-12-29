import json
import random
import re
from pathlib import Path
from typing import Dict, List, Tuple

from src.common.logger import get_logger
from src.config.config import global_config
from src.chat.injection.models import InjectionDecision, InjectionTopic

logger = get_logger("injection")


class InjectionManager:
    """基于单条消息的简单注入管理器"""

    def __init__(self):
        self.enabled: bool = False
        self.persistent_rounds: int = 0
        self.topics: List[InjectionTopic] = []
        self._active: Dict[str, Dict[str, InjectionDecision]] = {}
        self._cooldowns: Dict[str, Dict[str, int]] = {}
        self._mus_random_count: int = 3
        self._mus_lib_path: Path = (
            Path(__file__).resolve().parents[4] / "plugins" / "mus_library" / "music_library.json"
        )
        self._load_from_config()

    def _load_from_config(self):
        cfg = getattr(global_config, "injections", None)
        if not cfg or not getattr(cfg, "enable", False):
            self.enabled = False
            self.topics = []
            logger.info("[injection] 注入系统未启用")
            return

        self.enabled = True
        try:
            self.persistent_rounds = max(1, int(getattr(cfg, "persistent_rounds", 10) or 1))
        except Exception:
            self.persistent_rounds = 10

        topics: List[InjectionTopic] = []
        for topic_cfg in getattr(cfg, "topics", []) or []:
            try:
                topics.append(InjectionTopic.from_config(topic_cfg))
            except Exception as e:
                logger.warning(f"[injection] 解析注入主题失败: {e}")
        self.topics = topics
        logger.info(f"[injection] 载入 {len(self.topics)} 个注入主题，持续 {self.persistent_rounds} 轮")

    def _ensure_state(self, chat_id: str):
        self._active.setdefault(chat_id, {})
        self._cooldowns.setdefault(chat_id, {})

    def _prune_expired(self, chat_id: str):
        active = self._active.get(chat_id, {})
        expired = [topic_id for topic_id, decision in active.items() if decision.remaining_rounds <= 0]
        for topic_id in expired:
            active.pop(topic_id, None)

    def _match_topics(self, chat_id: str, message_text: str) -> List[Tuple[InjectionTopic, str]]:
        if not message_text:
            return []

        hits: List[Tuple[InjectionTopic, str]] = []
        cooldowns = self._cooldowns.get(chat_id, {})
        for topic in self.topics:
            if cooldowns.get(topic.id, 0) > 0:
                continue

            reason = ""
            for kw in topic.keywords:
                if kw and kw in message_text:
                    reason = f"keyword:{kw}"
                    break

            if not reason:
                for pattern in topic.regex:
                    if not pattern:
                        continue
                    try:
                        if re.search(pattern, message_text):
                            reason = f"regex:{pattern}"
                            break
                    except re.error as re_err:
                        logger.debug(f"[injection] 无效正则 {pattern}: {re_err}")
                        continue

            if reason:
                hits.append((topic, reason))

        hits.sort(key=lambda item: item[0].priority, reverse=True)
        return hits

    def _render_injections(self, decisions: List[InjectionDecision]) -> str:
        blocks: List[str] = []
        for decision in decisions:
            topic = decision.topic
            lines: List[str] = []
            header = f"[注入:{topic.id}] {topic.title}".strip()
            lines.append(header)
            if topic.payload.system:
                lines.append(topic.payload.system.strip())
            if topic.payload.few_shots:
                lines.append(topic.payload.few_shots.strip())
            note_parts: List[str] = []
            if topic.payload.note:
                note_parts.append(topic.payload.note.strip())
            dynamic_note = self._build_dynamic_note(topic)
            if dynamic_note:
                note_parts.append(dynamic_note)
            if note_parts:
                lines.append("\n".join(note_parts))

            block = "\n".join([line for line in lines if line])
            if block:
                blocks.append(block)

        return "\n\n".join(blocks)

    def _tick(self, chat_id: str):
        active = self._active.get(chat_id, {})
        for decision in active.values():
            if decision.remaining_rounds > 0:
                decision.remaining_rounds -= 1

        cooldowns = self._cooldowns.get(chat_id, {})
        for topic_id in list(cooldowns.keys()):
            if cooldowns[topic_id] > 0:
                cooldowns[topic_id] -= 1
            if cooldowns[topic_id] <= 0:
                cooldowns.pop(topic_id, None)

        self._prune_expired(chat_id)

    def build_injection_text(self, chat_id: str, message_text: str) -> str:
        if not self.enabled or not chat_id:
            return ""

        self._ensure_state(chat_id)
        self._prune_expired(chat_id)

        for topic, reason in self._match_topics(chat_id, message_text or ""):
            decision = self._active[chat_id].get(topic.id)
            if decision:
                decision.remaining_rounds = self.persistent_rounds
                decision.reason = reason
            else:
                decision = InjectionDecision(
                    topic=topic,
                    reason=reason,
                    remaining_rounds=self.persistent_rounds,
                )
                self._active[chat_id][topic.id] = decision

            if topic.cooldown_turns > 0:
                self._cooldowns[chat_id][topic.id] = topic.cooldown_turns

        active_decisions = sorted(self._active[chat_id].values(), key=lambda d: d.topic.priority, reverse=True)
        injection_text = self._render_injections(active_decisions)
        if injection_text:
            logger.debug(
                f"[injection] chat={chat_id} 注入={','.join([d.topic.id for d in active_decisions])} "
                f"剩余={[d.remaining_rounds for d in active_decisions]}"
            )

        self._tick(chat_id)
        return injection_text

    def _build_dynamic_note(self, topic: InjectionTopic) -> str:
        """针对特定topic构建动态注入内容"""
        if topic.id not in {"mus_library", "sing"}:
            return ""

        path = self._mus_lib_path
        if not path.exists():
            return ""

        try:
            data = json.loads(path.read_text("utf-8"))
            if not isinstance(data, list) or not data:
                return ""

            sample_size = min(self._mus_random_count, len(data))
            sampled = random.sample(data, sample_size)
            titles: List[str] = []
            for item in sampled:
                if not isinstance(item, dict):
                    continue
                title = (item.get("title") or "").strip()
                artist = (item.get("artist") or "").strip()
                if title and artist:
                    titles.append(f"{title} - {artist}")
                elif title:
                    titles.append(title)

            if not titles:
                return ""
            return "可播放的内置歌曲（随机挑选）：" + " / ".join(titles)
        except Exception as e:
            logger.debug(f"[injection] 读取 mus_library 失败: {e}")
            return ""


injection_manager = InjectionManager()

