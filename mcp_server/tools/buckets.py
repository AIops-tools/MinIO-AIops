"""Bucket inventory + per-bucket configuration MCP tools (read-only)."""

from typing import Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from minio_aiops.governance import governed_tool
from minio_aiops.ops import buckets as ops


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def bucket_ls(limit: int = 500, target: Optional[str] = None) -> dict:
    """[READ] Buckets (name + creation time) in a truncation-aware envelope.

    Returns {"buckets": [...], "returned": N, "limit": L, "truncated": bool}.
    When "truncated" is true there are MORE buckets than shown — re-run with a
    higher limit rather than reporting the list as complete.

    Args:
        limit: Maximum buckets to return (default 500).
        target: MinIO target name from config; omit for the default.
    """
    return ops.list_buckets(_get_connection(target), limit=limit)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def bucket_info(bucket_name: str, target: Optional[str] = None) -> dict:
    """[READ] One bucket's full config: policy, versioning, lifecycle, encryption, quota, tags.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        target: MinIO target name from config; omit for the default.
    """
    return ops.bucket_info(_get_connection(target), bucket_name)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def bucket_policy_get(bucket_name: str, target: Optional[str] = None) -> dict:
    """[READ] The bucket's policy JSON (verbatim) + anonymous-access summary.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        target: MinIO target name from config; omit for the default.
    """
    return ops.get_bucket_policy(_get_connection(target), bucket_name)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def bucket_lifecycle_get(bucket_name: str, target: Optional[str] = None) -> dict:
    """[READ] The bucket's lifecycle rules (null when no lifecycle is set).

    Args:
        bucket_name: Bucket name (from bucket_ls).
        target: MinIO target name from config; omit for the default.
    """
    return ops.get_bucket_lifecycle(_get_connection(target), bucket_name)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def bucket_versioning_get(bucket_name: str, target: Optional[str] = None) -> dict:
    """[READ] The bucket's versioning state: Enabled / Suspended / Off.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        target: MinIO target name from config; omit for the default.
    """
    return ops.get_bucket_versioning(_get_connection(target), bucket_name)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def bucket_quota_get(bucket_name: str, target: Optional[str] = None) -> dict:
    """[READ] The bucket's hard quota (0 = unlimited). Needs admin credentials.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        target: MinIO target name from config; omit for the default.
    """
    return ops.get_bucket_quota(_get_connection(target), bucket_name)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def object_ls(
    bucket_name: str, prefix: str = "", limit: int = 100, target: Optional[str] = None
) -> dict:
    """[READ] Objects under `prefix` in a truncation-aware envelope (never a full walk).

    Returns {"objects": [...], "returned": N, "limit": L, "truncated": bool}.
    A bucket can hold millions of keys, so "truncated" being true is the normal
    case — say so and re-run with a higher limit or a narrower prefix instead
    of treating the page as the whole bucket. "lastModified" and "versionId"
    are null when the source had no value.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        prefix: Key prefix filter; empty for the whole bucket.
        limit: Maximum objects to return (default 100, max 1000).
        target: MinIO target name from config; omit for the default.
    """
    return ops.list_objects(_get_connection(target), bucket_name, prefix=prefix, limit=limit)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def incomplete_uploads_ls(
    bucket_name: str, prefix: str = "", limit: int = 200, target: Optional[str] = None
) -> dict:
    """[READ] In-flight/abandoned multipart uploads in a truncation-aware envelope.

    Returns {"uploads": [...], "returned": N, "limit": L, "truncated": bool}.
    Each row: objectName, uploadId, initiated time (null when the source gave
    none). When "truncated" is true, re-run with a higher limit.

    Args:
        bucket_name: Bucket name (from bucket_ls).
        prefix: Key prefix filter; empty for the whole bucket.
        limit: Maximum uploads to return (default 200).
        target: MinIO target name from config; omit for the default.
    """
    return ops.list_incomplete_uploads(
        _get_connection(target), bucket_name, prefix=prefix, limit=limit
    )


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def server_info(target: Optional[str] = None) -> dict:
    """[READ] Admin server info: mode, servers/pools summary. Needs admin credentials.

    Args:
        target: MinIO target name from config; omit for the default.
    """
    return ops.server_info(_get_connection(target))
