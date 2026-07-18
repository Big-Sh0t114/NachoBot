from dataclasses import dataclass, field
from typing import Optional, List, TYPE_CHECKING

from . import BaseDataModel

if TYPE_CHECKING:
    from src.chat.focus.reply_context import ReplyContextRef
    from src.common.data_models.message_data_model import ReplySetModel
    from src.llm_models.payload_content.tool_option import ToolCall


@dataclass
class LLMGenerationDataModel(BaseDataModel):
    content: Optional[str] = None
    reasoning: Optional[str] = None
    model: Optional[str] = None
    tool_calls: Optional[List["ToolCall"]] = None
    prompt: Optional[str] = None
    selected_expressions: Optional[List[int]] = None
    reply_set: Optional["ReplySetModel"] = None
    # Internal delivery metadata.  It is deliberately kept off ReplySetModel
    # so adapters never serialize handoff reservations as message content.
    context_refs: List["ReplyContextRef"] = field(default_factory=list)
