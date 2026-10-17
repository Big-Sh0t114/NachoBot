"""Optional, invocation-local context for tools that need trusted host state.

Tools can implement ``prepare_execution_context(chat_stream)`` and return a
``ToolExecutionPreparation``.  The executor adds its prompt supplement to the
current tool-decision prompt and passes the opaque context only to that same
tool invocation through ``execute_with_context(args, context)``.  Existing
tools continue to use ``execute(args)`` unchanged. Opt-in preparations bypass
executor caching by default; a tool may mark a context irrelevant to caching.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolExecutionPreparation:
    """Prompt guidance and trusted data scoped to one executor invocation."""

    prompt_supplement: str = ""
    context: Any = None
    cache_sensitive: bool = True
