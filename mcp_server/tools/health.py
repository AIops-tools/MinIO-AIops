"""Service health + cluster status MCP tools (read-only)."""

from typing import Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from minio_aiops.governance import governed_tool
from minio_aiops.ops import health as ops


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def health_live(target: Optional[str] = None) -> dict:
    """[READ] Node liveness probe (unauthenticated /minio/health/live).

    Args:
        target: MinIO target name from config; omit for the default.
    """
    return ops.service_health(_get_connection(target))["live"]


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def health_ready(target: Optional[str] = None) -> dict:
    """[READ] Node readiness probe (unauthenticated /minio/health/ready).

    Args:
        target: MinIO target name from config; omit for the default.
    """
    return ops.service_health(_get_connection(target))["ready"]


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def health_cluster(target: Optional[str] = None) -> dict:
    """[READ] Cluster write-quorum health (/minio/health/cluster; 503 = degraded).

    Combined with liveness/readiness in one structured answer plus an overall
    'healthy' verdict.

    Args:
        target: MinIO target name from config; omit for the default.
    """
    return ops.service_health(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def cluster_status(target: Optional[str] = None) -> dict:
    """[READ] Dashboard-header summary: nodes, drives, capacity, buckets, objects.

    Args:
        target: MinIO target name from config; omit for the default.
    """
    return ops.cluster_status(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def fleet_overview(target: Optional[str] = None) -> dict:
    """[READ] One-shot deployment overview: health + capacity + exposure headline.

    Call this first on any broad "how is my object storage doing" question.

    Args:
        target: MinIO target name from config; omit for the default.
    """
    from minio_aiops.ops import overview

    return overview.fleet_overview(_get_connection(target))
