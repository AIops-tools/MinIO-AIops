"""Bucket exposure audit + ILM gap analysis MCP tools (read-only) —
flagship analyses #2 and #3."""

from typing import Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from minio_aiops.governance import governed_tool
from minio_aiops.ops import exposure as exp_ops
from minio_aiops.ops import ilm as ilm_ops


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def bucket_exposure_audit(limit: int = 100, target: Optional[str] = None) -> dict:
    """[READ] Ranked bucket-exposure findings (riskiest first), cause + action.

    Scores every bucket for anonymous/public policy statements (read and
    write), missing default encryption, versioning off, and no lifecycle —
    the fastest answer to "is anything in my object storage exposed?".

    Args:
        limit: Maximum buckets to audit (default 100).
        target: MinIO target name from config; omit for the default.
    """
    return exp_ops.bucket_exposure_audit(_get_connection(target), limit=limit)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def lifecycle_gap_analysis(limit: int = 100, target: Optional[str] = None) -> dict:
    """[READ] ILM gaps per bucket + reclaimable estimate, cause + action.

    Finds versioned buckets with no noncurrent expiry (old bytes accrue
    forever), incomplete multipart uploads with no abort rule (invisible
    space), and large buckets with no lifecycle at all.

    Args:
        limit: Maximum buckets to analyze (default 100).
        target: MinIO target name from config; omit for the default.
    """
    return ilm_ops.lifecycle_gap_analysis(_get_connection(target), limit=limit)
