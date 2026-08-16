"""HTTP API for A_Memorix long-term memory operations."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.memory_system.memory_service import memory_service


router = APIRouter()


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8192)
    chat_id: str = Field(default="", max_length=512)
    limit: int = Field(default=10, ge=1, le=100)


class MemoryMaintainRequest(BaseModel):
    action: str = Field(min_length=1, max_length=128)
    target: str = Field(default="", max_length=2048)
    reason: str = Field(default="", max_length=4096)


@router.get("/status")
async def memory_status() -> dict:
    return {"enabled": memory_service.is_enabled()}


@router.get("/stats")
async def memory_stats() -> dict:
    return await memory_service.memory_stats()


@router.post("/search")
async def memory_search(body: MemorySearchRequest) -> dict:
    result = await memory_service.search(
        query=body.query,
        chat_id=body.chat_id,
        limit=body.limit,
    )
    if isinstance(result, dict) and "results" not in result and isinstance(result.get("hits"), list):
        return {**result, "results": result.get("hits", [])}
    return result


@router.post("/maintain")
async def memory_maintain(body: MemoryMaintainRequest) -> dict:
    return await memory_service.maintain_memory(
        action=body.action,
        target=body.target,
        reason=body.reason,
    )
