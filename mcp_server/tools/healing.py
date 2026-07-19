"""Healing & erasure-set health MCP tools (read-only) — flagship analysis #4."""

from typing import Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from minio_aiops.governance import governed_tool
from minio_aiops.ops import healing as ops


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def healing_health(target: Optional[str] = None) -> dict:
    """[READ] Healing backlog + erasure-set write-quorum risk, cause + action.

    Answers "how many more drive failures can I take?": per-erasure-set online
    drives vs write quorum (remaining failure tolerance), drives currently
    healing, heal backlog/errors — each risk as a plain-language finding.

    Args:
        target: MinIO target name from config; omit for the default.
    """
    return ops.healing_health(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def drive_status(target: Optional[str] = None) -> dict:
    """[READ] Per-drive rows (server, drive, used ratio), fullest first.

    Returns {"drives": [...], "returned": N, "error": str | None}. A non-null
    "error" means the metrics scrape failed — that is NOT the same as a server
    with no drives, so do not report an empty list as "healthy, nothing to see".

    Args:
        target: MinIO target name from config; omit for the default.
    """
    return ops.drive_status(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def node_status(target: Optional[str] = None) -> dict:
    """[READ] Node-level view: online/offline nodes + per-node drive counts.

    Args:
        target: MinIO target name from config; omit for the default.
    """
    return ops.node_status(_get_connection(target))
