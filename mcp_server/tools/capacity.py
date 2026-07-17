"""Capacity & usage RCA MCP tools (read-only) — flagship analysis #1."""

from typing import Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from minio_aiops.governance import governed_tool
from minio_aiops.ops import capacity as ops


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def capacity_rca(target: Optional[str] = None) -> dict:
    """[READ] Capacity & usage RCA: cause + suggested action per finding.

    Call this first on any "storage is filling up / writes are failing"
    question — it folds capacity vs used, offline drives/nodes, per-drive
    hotspots, and imbalance into ranked findings with what to do next.

    Args:
        target: MinIO target name from config; omit for the default.
    """
    return ops.capacity_rca(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("list")
def usage_by_bucket(limit: int = 25, target: Optional[str] = None) -> list:
    """[READ] Per-bucket usage (bytes + objects), biggest first.

    Args:
        limit: Maximum rows to return (default 25).
        target: MinIO target name from config; omit for the default.
    """
    return ops.usage_by_bucket(_get_connection(target), limit=limit)
