from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from src.config.official_configs import InjectionTopicConfig


@dataclass
class InjectionPayload:
    system: str = ""
    few_shots: str = ""
    note: str = ""


@dataclass
class InjectionTopic:
    id: str
    title: str = ""
    keywords: List[str] = field(default_factory=list)
    regex: List[str] = field(default_factory=list)
    priority: int = 0
    cooldown_turns: int = 0
    payload: InjectionPayload = field(default_factory=InjectionPayload)

    @classmethod
    def from_config(cls, config_topic: "InjectionTopicConfig") -> "InjectionTopic":
        payload = InjectionPayload(
            system=config_topic.payload.system or "",
            few_shots=config_topic.payload.few_shots or "",
            note=config_topic.payload.note or "",
        )
        return cls(
            id=config_topic.id,
            title=config_topic.title or config_topic.id,
            keywords=list(config_topic.keywords or []),
            regex=list(config_topic.regex or []),
            priority=config_topic.priority or 0,
            cooldown_turns=config_topic.cooldown_turns or 0,
            payload=payload,
        )


@dataclass
class InjectionDecision:
    topic: InjectionTopic
    reason: str = ""
    remaining_rounds: int = 0

