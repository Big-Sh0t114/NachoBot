"""Core-owned Model Context Protocol runtime."""

from src.mcp.service import MCPService, mcp_service
from src.mcp.types import MCPAccessContext, MCPInvocationResult

__all__ = ["MCPAccessContext", "MCPInvocationResult", "MCPService", "mcp_service"]
