"""垫片模块: 模拟 src.services.llm_service 接口。

A_Memorix 内部多处 `from src.services import llm_service as llm_api`，
本模块将这些调用桥接到 NachoBot 的 LLMRequest 体系。
"""

from src.A_memorix._compat import (
    LLMServiceResult,
    LLMServiceRequest,
    LLMServiceClient,
    LLMGenerationOptions,
    generate,
    get_available_models,
)

__all__ = [
    "LLMServiceResult",
    "LLMServiceRequest",
    "LLMServiceClient",
    "LLMGenerationOptions",
    "generate",
    "get_available_models",
]
