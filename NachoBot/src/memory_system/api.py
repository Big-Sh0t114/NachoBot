"""HTTP API for A_Memorix long-term memory operations."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.memory_system.memory_service import memory_service


router = APIRouter()


class MemorySearchRequest(BaseModel):
    query: str
    chat_id: str = ""
    limit: int = 10


class MemoryMaintainRequest(BaseModel):
    action: str
    target: str = ""
    reason: str = ""


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
