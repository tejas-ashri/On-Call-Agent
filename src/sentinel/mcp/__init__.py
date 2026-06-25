"""The MCP Router & JIT credential broker — Sentinel's security spine (doc 05 §2).

This package owns credential brokering (ADR-005): an :class:`MCPRouter` that mints
short-lived, tenant-scoped Just-In-Time credentials per tool call, and a
:class:`ToolBroker` closed over a *frozen* ``tenant_id`` so a tenant's incident
graph physically cannot mint another tenant's credential (the confused-deputy fix).
"""

from __future__ import annotations

from sentinel.mcp.broker import (
    AuditSink,
    BrokerRequest,
    Credential,
    MCPRouter,
    MCPServer,
    ToolBroker,
    ToolResult,
    VaultClient,
)

__all__ = [
    "AuditSink",
    "BrokerRequest",
    "Credential",
    "MCPRouter",
    "MCPServer",
    "ToolBroker",
    "ToolResult",
    "VaultClient",
]
