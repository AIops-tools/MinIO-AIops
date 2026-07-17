"""Service health + cluster status (read-only).

``service_health`` folds the three unauthenticated health endpoints
(live / ready / cluster write-quorum) into one answer; ``cluster_status``
summarises the cluster metrics (nodes, drives, capacity, objects) the way an
operator reads a dashboard header. Resilient by design: a failing probe
degrades to an ``error`` field, never a raised traceback (a health probe must
survive the thing it probes).
"""

from __future__ import annotations

from typing import Any

from minio_aiops.ops._util import pct, s
from minio_aiops.prom import first_value, sum_values

# Cluster metrics the status summary reads (documented MinIO v2 metric names).
M_CAP_RAW_TOTAL = "minio_cluster_capacity_raw_total_bytes"
M_CAP_RAW_FREE = "minio_cluster_capacity_raw_free_bytes"
M_CAP_USABLE_TOTAL = "minio_cluster_capacity_usable_total_bytes"
M_CAP_USABLE_FREE = "minio_cluster_capacity_usable_free_bytes"
M_NODES_ONLINE = "minio_cluster_nodes_online_total"
M_NODES_OFFLINE = "minio_cluster_nodes_offline_total"
M_DRIVES_ONLINE = "minio_cluster_drive_online_total"
M_DRIVES_OFFLINE = "minio_cluster_drive_offline_total"
M_DRIVES_TOTAL = "minio_cluster_drive_total"
M_BUCKET_TOTAL = "minio_cluster_bucket_total"
M_OBJECT_TOTAL = "minio_cluster_usage_object_total"
M_USAGE_TOTAL = "minio_cluster_usage_total_bytes"


def service_health(conn: Any) -> dict:
    """[READ] Liveness + readiness + cluster write-quorum health in one shot."""
    out: dict[str, Any] = {}
    errors: list[str] = []
    for key, probe in (
        ("live", "health_live"),
        ("ready", "health_ready"),
        ("cluster", "health_cluster"),
    ):
        try:
            out[key] = getattr(conn, probe)()
        except Exception as exc:  # noqa: BLE001 — report as partial
            out[key] = {"reachable": False, "healthy": False}
            errors.append(f"{key}: {s(exc, 160)}")
    out["healthy"] = all(
        isinstance(v, dict) and v.get("healthy") for k, v in out.items() if k != "healthy"
    )
    if errors:
        out["errors"] = errors
    return out


def cluster_status(conn: Any) -> dict:
    """[READ] Dashboard-header summary: nodes, drives, capacity, buckets, objects."""
    try:
        metrics = conn.metrics()
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}

    usable_total = first_value(metrics, M_CAP_USABLE_TOTAL)
    usable_free = first_value(metrics, M_CAP_USABLE_FREE)
    used = (
        usable_total - usable_free
        if isinstance(usable_total, (int, float)) and isinstance(usable_free, (int, float))
        else None
    )
    drives_offline = sum_values(metrics, M_DRIVES_OFFLINE)
    nodes_offline = sum_values(metrics, M_NODES_OFFLINE)
    return {
        "nodesOnline": sum_values(metrics, M_NODES_ONLINE),
        "nodesOffline": nodes_offline,
        "drivesOnline": sum_values(metrics, M_DRIVES_ONLINE),
        "drivesOffline": drives_offline,
        "drivesTotal": sum_values(metrics, M_DRIVES_TOTAL),
        "rawTotalBytes": first_value(metrics, M_CAP_RAW_TOTAL),
        "usableTotalBytes": usable_total,
        "usableFreeBytes": usable_free,
        "usableUsedBytes": used,
        "usableUsedRatio": pct(used, usable_total),
        "buckets": first_value(metrics, M_BUCKET_TOTAL),
        "objects": first_value(metrics, M_OBJECT_TOTAL),
        "usageTotalBytes": first_value(metrics, M_USAGE_TOTAL),
        "degraded": bool(drives_offline or nodes_offline),
    }
